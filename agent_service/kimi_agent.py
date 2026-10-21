"""Kimi-slides 1:1 agentic PPT expert.

Unlike the deterministic ``pptd_generation`` pipeline, this agent gives the model
real file and CLI tools and lets it follow ``docs/kimi-ppt/original/SKILL.md``
itself: classify the task, read only the rules it needs, write pages in parallel,
and close the ``kimi-slides check`` / ``screenshot`` loop before finishing.
"""
from __future__ import annotations

import asyncio
from collections import Counter
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from agentscope.agent import Agent, ContextConfig, ReActConfig
from agentscope.message import Msg
from agentscope.middleware import TracingMiddleware
from agentscope.permission import PermissionBehavior, PermissionDecision, PermissionMode
from agentscope.state import AgentState
from agentscope.tool import Bash, Edit, FunctionTool, Glob, Grep, Read, Toolkit, Write

from .model_gateway import make_model, timeout_setting
from .docx_markdown import docx_to_markdown
from .execution_policy import expert_policy
from .pptd_compat import normalize_pages
from .pptd_validation import normalize_table_tracks, validate_document
from .kimi_validation import (
    _browser_layout_issues,
    _browser_layout_report,
    _contrast_issues,
    _emit_note,
    _render_pages,
    _renderer_ready,
    _slide_artifact_dir,
    final_layout_issues,
    visual_overview_issues,
)
from .kimi_template import (
    _apply_background,
    _apply_chrome,
    _apply_configured_background,
    _apply_host_transforms,
    _apply_template_cover_closing,
    _clear_corner_conflicts,
    _extract_body_background,
    _extract_chrome,
    _fill_template_page,
    _fit_cover_slots,
    _normalize_table_fonts,
    _relabel,
    _visible_text,
)


ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = ROOT / "docs" / "kimi-ppt" / "original"


def _cli_env() -> dict:
    """Keep the repo importable when the CLI runs with the workspace as cwd."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _workspace(data_root: Path, project_id: str, job_id: str) -> Path:
    override = os.getenv("PPT_KIMI_WORKSPACE", "").strip()
    base = Path(override).expanduser().resolve() if override else data_root
    return base / "kimi-workspace" / project_id / job_id


def _prepare_template(data_root: Path, template_id, workspace: Path) -> Path | None:
    """Copy the uploaded company template into the agent workspace and return it."""
    if not template_id or not isinstance(template_id, str):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", template_id):
        raise ValueError("无效的模板标识")
    source = data_root / "templates" / template_id / "template.pptx"
    if not source.is_file():
        raise ValueError(f"未找到公司模板文件：{source}")
    destination = workspace / "template" / "template.pptx"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())
    return destination


def _skill_text() -> str:
    return (ORIGINAL / "SKILL.md").read_text(encoding="utf-8")


def _prepare_reference_material(data_root: Path, project_id: str, request, workspace: Path) -> dict:
    """Copy originals into the agent workspace and build a structured reading copy."""
    folder = data_root / "reference-files" / project_id
    sources = workspace / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    files, sections = [], []
    if folder.is_dir():
        metadata_paths = sorted(folder.glob("*/metadata.json"))
        for index, metadata_path in enumerate(metadata_paths, 1):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            extension = str(metadata.get("extension") or "").lower()
            original = metadata_path.parent / ("original" + extension)
            if not original.is_file():
                continue
            name = str(metadata.get("name") or f"source-{index}{extension}")
            safe_stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(name).stem).strip("-") or f"source-{index}"
            original_copy = sources / f"{safe_stem}{extension}"
            if original_copy.exists():
                original_copy = sources / f"{safe_stem}-{index}{extension}"
            original_copy.write_bytes(original.read_bytes())
            if extension == ".docx":
                text = docx_to_markdown(original_copy)
                reading = sources / f"{original_copy.stem}.md"
                reading.write_text(text, encoding="utf-8")
            else:
                reading = sources / f"{original_copy.stem}{extension}"
                text = original.read_text(encoding="utf-8", errors="replace")
                reading.write_text(text, encoding="utf-8")
            files.append({
                "name": name,
                "original": str(original_copy.relative_to(workspace)),
                "readingCopy": str(reading.relative_to(workspace)),
                "characters": len(text),
            })
            sections.append(f"# {name}\n\n{text}".strip())
    references = getattr(request, "references", "") or ""
    if not files and references:
        reading = sources / "reference.md"
        reading.write_text(references, encoding="utf-8")
        files.append({
            "name": "参考资料",
            "original": None,
            "readingCopy": str(reading.relative_to(workspace)),
            "characters": len(references),
        })
        sections.append(references)
    if not sections:
        return {"files": [], "outline": "", "characters": 0}
    combined = "\n\n".join(sections)
    reference_md = sources / "reference.md"
    reference_md.write_text(combined, encoding="utf-8")
    outline_lines = [
        line.strip() for line in combined.splitlines()
        if re.match(r"^#{1,4}\s+", line.strip())
    ]
    outline = "\n".join(outline_lines[:500])
    (sources / "reference-outline.md").write_text(outline + "\n", encoding="utf-8")
    return {
        "files": files,
        "outline": outline[:20000],
        "characters": len(combined),
        "referenceFile": str(reference_md.relative_to(workspace)),
        "outlineFile": "sources/reference-outline.md",
    }


def _plain_heading(value: str) -> str:
    """Normalize one Markdown heading for deterministic plan coverage checks."""
    text = re.sub(r"^#{1,6}\s+", "", str(value or "")).strip()
    return re.sub(r"[\s#>*_`~:：,，。；;、\-—–·\"'“”‘’()（）\[\]【】]+", "", text).casefold()


def _source_sections(outline: str) -> list[str]:
    """Return principal source chapters from the generated Markdown outline."""
    headings = []
    for line in str(outline or "").splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line.strip())
        if not match:
            continue
        heading = match.group(1).strip()
        # The first generated H1 normally repeats the uploaded filename.
        if re.search(r"\.[A-Za-z0-9]{2,5}$", heading):
            continue
        if heading and heading not in headings:
            headings.append(heading)
    return headings


def plan_requirements(request, reference_info: dict | None) -> dict:
    """Derive a minimum page contract before any page is written.

    A write batch is capped at five pages. The complete plan is deliberately
    independent of that cap and is derived from source volume and chapter count.
    """
    reference_info = reference_info or {}
    characters = int(reference_info.get("characters") or len(getattr(request, "references", "") or ""))
    sections = _source_sections(reference_info.get("outline", ""))
    requested = getattr(request, "pageCount", None)
    if type(requested) is int and requested > 0:
        minimum_pages = requested
        exact_pages = True
    elif characters >= 12000:
        by_volume = (characters + 3499) // 3500
        by_chapters = len(sections) * 2
        substantive = min(28, max(8, by_volume, by_chapters))
        # Cover, agenda and closing support the substantive body.
        minimum_pages = substantive + 3
        exact_pages = False
    else:
        minimum_pages = 4
        exact_pages = False
    return {
        "characters": characters,
        "principalSections": sections,
        "minimumPlanPages": minimum_pages,
        "maximumPlanPages": 32,
        "exactPlanPages": exact_pages,
        "fieldLimits": {
            "title": 60,
            "readerTask": 100,
            "content": 240,
            "visualPlan": 180,
        },
    }


def _plan_source_sections(page: dict) -> list[str]:
    values = page.get("sourceSections") if isinstance(page, dict) else None
    if not isinstance(values, list):
        single = page.get("sourceSection") if isinstance(page, dict) else None
        values = [single] if isinstance(single, str) else []
    return [str(value) for value in values if isinstance(value, str) and value.strip()]


def audit_plan(plan: dict, requirements: dict) -> list[str]:
    """Reject an incomplete or under-sized plan before page generation."""
    issues = []
    pages = plan.get("pages") if isinstance(plan, dict) else None
    if not isinstance(pages, list) or not pages:
        return ["plan.json 缺少非空 pages 数组"]
    minimum = int(requirements.get("minimumPlanPages") or 0)
    if requirements.get("exactPlanPages"):
        if len(pages) != minimum:
            issues.append(f"用户指定 {minimum} 页，但 plan.json 规划了 {len(pages)} 页")
    elif len(pages) < minimum:
        issues.append(
            f"长资料计划页数不足：至少需要 {minimum} 页，当前仅 {len(pages)} 页"
        )
    maximum = int(requirements.get("maximumPlanPages") or 0)
    if maximum and len(pages) > maximum:
        issues.append(f"计划页数超过上限：最多 {maximum} 页，当前为 {len(pages)} 页")
    paths = []
    for index, page in enumerate(pages, 1):
        if not isinstance(page, dict):
            issues.append(f"第 {index} 个计划项不是对象")
            continue
        path = page.get("path")
        if not isinstance(path, str) or not re.fullmatch(r"pages/[A-Za-z0-9_.-]+\.page", path):
            issues.append(f"第 {index} 个计划项缺少合法的 pages/*.page 路径")
        elif path in paths:
            issues.append(f"计划路径重复：{path}")
        else:
            paths.append(path)
        if not isinstance(page.get("title"), str) or not page["title"].strip():
            issues.append(f"第 {index} 个计划项缺少 title")
        if not isinstance(page.get("readerTask"), str) or not page["readerTask"].strip():
            issues.append(f"第 {index} 个计划项缺少 readerTask")
        if not isinstance(page.get("content"), str) or not page["content"].strip():
            issues.append(f"第 {index} 个计划项缺少 content")
        if not isinstance(page.get("visualPlan"), str) or not page["visualPlan"].strip():
            issues.append(f"第 {index} 个计划项缺少 visualPlan")
        for field, limit in (requirements.get("fieldLimits") or {}).items():
            value = page.get(field)
            if isinstance(value, str) and len(value) > int(limit):
                issues.append(
                    f"第 {index} 个计划项的 {field} 过长（{len(value)} 字，上限 {limit}）"
                )
    required = requirements.get("principalSections") or []
    if required:
        known = {_plain_heading(value) for value in required}
        covered = {
            _plain_heading(section)
            for page in pages if isinstance(page, dict)
            for section in _plan_source_sections(page)
        }
        missing = [
            section for section in required
            if _plain_heading(section) not in covered
        ]
        if missing:
            issues.append(
                "计划没有覆盖全部一级章节，缺少：" + "；".join(missing)
            )
        if (
            not requirements.get("exactPlanPages")
            and int(requirements.get("characters") or 0) >= 12000
            and len(required) >= 2
        ):
            coverage_count = Counter()
            for page in pages:
                if not isinstance(page, dict):
                    continue
                for section in set(_plan_source_sections(page)):
                    coverage_count[_plain_heading(section)] += 1
            thin = [
                section for section in required
                if coverage_count[_plain_heading(section)] < 2
            ]
            if thin:
                issues.append(
                    "长报告的一级章节不能压成单页，以下章节至少需要 2 页："
                    + "；".join(thin)
                )
    return issues


def _load_plan(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_plan_deck(workspace: Path, plan: dict, template_info: dict | None) -> Path:
    """Materialize the approved plan as the stable deck entry for write batches."""
    deck = workspace / "deck"
    (deck / "pages").mkdir(parents=True, exist_ok=True)
    (deck / "media").mkdir(parents=True, exist_ok=True)
    normalized_pages = []
    for page in plan["pages"]:
        item = dict(page)
        item["sourceSections"] = _plan_source_sections(page)
        normalized_pages.append(item)
    normalized = {**plan, "pages": normalized_pages}
    (deck / "plan.json").write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    size = [960, 540]
    if template_info:
        from pptd_engine.document import load_yaml

        try:
            template = load_yaml(Path(template_info["entry"]))
            if isinstance(template.get("size"), list) and len(template["size"]) == 2:
                size = template["size"]
        except (OSError, ValueError):
            pass
    document = {
        "version": "v2",
        "title": str(plan.get("deckTitle") or "演示文稿"),
        "size": size,
        "pages": [page["path"] for page in normalized_pages],
    }
    entry = deck / "deck.pptd"
    entry.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return entry


def _clear_page_drafts(workspace: Path) -> None:
    """Enforce the planning boundary if the model ignored the no-page rule."""
    pages = workspace / "deck" / "pages"
    if not pages.is_dir():
        return
    for path in pages.glob("*.page"):
        if path.is_file() and not path.is_symlink():
            path.unlink()


def kimi_system_prompt(workspace: Path) -> str:
    """Layer the archived skill verbatim with a local host adapter."""
    adapter = f"""

HOST ADAPTER: local kimi-slides PPT expert
You are an agentic PPT expert in a local application. The archived Kimi
``kimi-slides`` skill above is your authoritative workflow; follow its steps
1-5 and all its rules. Local equivalents for the Kimi App private pieces:

- ``kimi_slides`` tool with subcommands convert / check / screenshot replaces
  the proprietary ``kimi-slides`` CLI. Never claim to run that private binary.
- The reference files (pptd.md, cli.md, slides_categories.md and the seven
  scenario guides, fonts.md, shapes.md) live under
  {ORIGINAL}. Read exactly the files you need with the Read tool; do not
  preload the whole 200KB.
- Your working directory is {workspace}. Produce the deck there with this
  layout: ``deck/deck.pptd`` (entry), ``deck/pages/*.page``, and
  ``deck/media/*`` for local images. Do not write to /tmp or /work.
- The deliverable is ONLY a brand-new deck under ``{workspace}/deck/``. Any
  converted template (``template-pptd/``) and its screenshots
  (``template-screenshots/``) are analysis material: never deliver them, and
  never copy the template's own text, data, page order or page count into the
  answer. Generate content for the user's request and reuse only the template's
  design language (colors, fonts, layout, reusable components).
- ``<KIMI_REF .../>`` is not emitted as text. Finishing with a valid deck under
  the working directory is how you deliver; the host commits it and shows the
  editable card. The host owns export and versioning, so do not run any PPTX
  export; PPTX is only exported from the editor.
- Research and media tools are not attached by default. Use only information
  the user supplied. Never invent URLs, data, cases, sources or local image
  paths. If a real image is needed and none was supplied, use a shape/icon/text
  composition and note the missing asset in your final reply.
- Validation loop: after writing pages, run ``kimi_slides check deck`` (with
  ``--level auto`` to apply deterministic fixes), then ``kimi_slides screenshot
  deck -o deck-screenshots`` and inspect the page PNGs with your image input
  when available. Fix the relevant ``.page`` files and re-check.
- The host separates planning from writing. A planning call writes only the
  complete ``deck/plan.json`` and no page. Page-writing calls receive a fixed
  batch from that approved plan. A five-page write cap is never a five-page
  plan cap and must not shorten a long report.
- The host renders the finished deck in a real browser and blocks delivery on
  located overflow, overlap, clipping and text/background contrast failures.
  Text drawn with a colour that nearly matches what is behind it is a defect,
  not a style choice.
- Company template discipline: the template already owns its page background
  and its top-right and bottom-right corner marks, and the host re-applies them.
  Do not invent your own page background, gradient or bottom wave band, and
  never place your own footer, navigation, page number or section list in the
  right-hand corner band (roughly the right 25% of the width, top 12% or bottom
  15%): the company logo and name live there. If you want a running section
  label, keep it left of that band.
- Keep parallel siblings consistent: numbered steps, category chips and cards
  that sit in one row use the same fill colour. Do not fade one brand colour
  across a row; use one fill, and distinguish states with a single accent
  instead.
- Do not separate parallel items with decorative rule lines. If two blocks need
  to be told apart, use spacing, a card, or one accent; a full-width hairline
  between every list item reads as clutter, and reviewers reject it.
- Use one size for body-level text across a page, tables included: a list body
  at 12px next to a table body at 11px is an inconsistency, not a hierarchy.
  Headings may be larger; body copy should match.
- For a complete report or a long source document, preserve its major argument,
  not merely its topic. A professional deck normally needs a real cover, an agenda
  that matches the later sections, a progression through evidence and decisions,
  and a formal closing. Do not compress several major chapters into one generic
  summary page.
- Choose page count from the source hierarchy and reader workload. For structured
  long reports, a practical default is roughly 14–20 substantive pages; use more
  only when the source genuinely requires it. Do not compress principal sections
  merely to reduce pages, and do not inflate the deck with a standalone section
  divider for every chapter. The explicit user page count always wins.
- Before writing pages, turn the source outline into an argument map: what the
  reader must understand first, which facts/numbers/tables support each claim,
  and what decision or next action follows. Preserve concrete entities, dates,
  metrics, comparisons and source tables when they materially support a claim.
- Vary page structure according to the content: comparison matrix for alternatives,
  timeline for phases, process/diagram for architecture, table for structured data,
  numbered cards for parallel requirements, and lede plus evidence for arguments.
  Do not repeat one card/list layout for more than two consecutive pages.
- When the source contains a comparison, responsibility matrix, phased plan or
  structured project/accountability table, preserve its relationships as an
  editable table or matrix when that communicates the argument better than prose.
  Give the most decision-relevant metrics a clear visual anchor; never fabricate
  a number or turn a qualitative statement into measured data.
- Every page carries one controlling judgement. If a page needs more than one
  competing headline, split it or move secondary material to another page.
- Reference files may be long. Read `sources/reference-outline.md` first, then
  inspect relevant sections of `sources/reference.md` and the original file as
  needed. Never ignore a principal section because it is later in the document.
  The original file is authoritative if Markdown conversion appears incomplete.
- Output exactly the deck files; the final assistant message should be a brief
  natural-language summary. No shell/code output is required in the reply.
"""
    return _skill_text() + adapter


def _cli_tool(workspace: Path) -> FunctionTool:
    def run(command: str, args: list[str]) -> str:
        if command not in ("convert", "check", "screenshot"):
            return "error: unknown kimi_slides command; use convert/check/screenshot"
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            return "error: args must be a list of strings"
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pptd_engine.cli", command, *args],
                cwd=str(workspace), env=_cli_env(), capture_output=True, text=True, timeout=360,
            )
        except subprocess.TimeoutExpired:
            return "error: kimi_slides timed out"
        except Exception as exc:  # pragma: no cover - defensive boundary
            return f"error: {exc}"
        output = (proc.stdout or "").strip()
        if proc.returncode:
            output = "\n".join(part for part in (output, proc.stderr or "") if part)
            return f"exit {proc.returncode}:\n{output}" if output else f"exit {proc.returncode}"
        return output or "(no output)"

    return FunctionTool(
        func=run,
        name="kimi_slides",
        description="Run the local kimi-slides CLI: convert a PPTX to PPTD, check a PPTD directory, or render screenshots. command is one of convert/check/screenshot; args are the remaining CLI arguments.",
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "enum": ["convert", "check", "screenshot"]},
                "args": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["command", "args"],
        },
        is_read_only=False,
        permission=PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="kimi_slides is always allowed inside the bounded agent workspace.",
        ),
    )


def _make_agent(model, workspace: Path, session_id: str | None, max_iters: int):
    toolkit = Toolkit(
        tools=[
            Read(),
            Write(),
            Edit(),
            Grep(),
            Glob(),
            Bash(cwd=str(workspace)),
            _cli_tool(workspace),
        ]
    )
    state = AgentState(session_id=session_id) if session_id else AgentState()
    state.permission_context.mode = PermissionMode.BYPASS
    return Agent(
        name="KimiSlidesExpert",
        system_prompt=kimi_system_prompt(workspace),
        model=model,
        toolkit=toolkit,
        middlewares=[TracingMiddleware()],
        state=state,
        react_config=ReActConfig(
            max_iters=max_iters,
            structured_output_grace_iters=2,
            interruption_raise_cancelled_error=True,
        ),
        context_config=ContextConfig(
            compression_tool_enabled=False,
            trigger_ratio=0.9,
            compression_fallback_to_truncation=False,
        ),
    )


def _run_cli(command: str, args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        [sys.executable, "-m", "pptd_engine.cli", command, *args],
        cwd=str(cwd), env=_cli_env(), capture_output=True, text=True, timeout=900,
    )
    if proc.returncode:
        raise ValueError(f"模板预处理 {command} 失败：{(proc.stderr or proc.stdout)[-2000:]}")
    return proc.stdout or ""


def _prepare_template_analysis(workspace: Path, template_pptx: Path) -> dict:
    """Convert and screenshot the company template so the agent can analyze it."""
    pptd_dir = workspace / "template-pptd"
    shots_dir = workspace / "template-screenshots"
    entry = pptd_dir / f"{template_pptx.stem}.pptd"
    if not entry.is_file():
        _run_cli("convert", [str(template_pptx), "-o", str(pptd_dir)], workspace)
    if not any((shots_dir / "pages").glob("*.png")):
        _run_cli("screenshot", [str(pptd_dir), "-o", str(shots_dir)], workspace)
    return {
        "pptx": str(template_pptx),
        "pptd": str(pptd_dir),
        "entry": str(pptd_dir / f"{template_pptx.stem}.pptd"),
        "screenshots": str(shots_dir),
    }


def _base_message_payload(request, template_info: dict | None, reference_info: dict | None) -> dict:
    attachments = []
    for item in getattr(request, "attachments", []) or []:
        if isinstance(item, dict):
            attachments.append({"name": item.get("name"), "visualAnalysis": item.get("visualAnalysis")})
    references = getattr(request, "references", "") or ""
    reference_info = reference_info or {}
    use_external_reference = bool(reference_info.get("files")) and len(references) > 24000
    return {
        "prompt": getattr(request, "prompt", "") or "",
        "audience": getattr(request, "audience", "") or "通用汇报",
        "pageCount": getattr(request, "pageCount", None),
        "references": "" if use_external_reference else references,
        "referenceCharacters": len(references),
        "referenceFiles": reference_info.get("files", []),
        "referenceOutline": reference_info.get("outline", ""),
        "referenceFile": reference_info.get("referenceFile"),
        "attachments": attachments,
        "template": template_info,
        "_usesExternalReference": use_external_reference,
    }


def _planning_message(request, template_info: dict | None, reference_info: dict | None,
                      requirements: dict) -> str:
    payload = _base_message_payload(request, template_info, reference_info)
    use_external_reference = payload.pop("_usesExternalReference")
    payload["planRequirements"] = requirements
    payload["note"] = (
        "你会被后续宿主校验。如果你违背以下约束，结果会被拒绝并重新规划。"
        "当前阶段只做完整内容规划：写出 deck/plan.json 后立即停止。"
        "本阶段禁止创建或修改任何 deck/pages/*.page、deck/deck.pptd 或 deck/media/*；"
        "页面写入会在计划通过后由宿主分批派发。首批最多 5 页是后续写入批次上限，"
        "不是整份 PPT 的计划页数，绝不能因此把完整报告压缩成 5 页。"
        "plan.json 的 pages 必须覆盖整份报告，并与最终 deck.pptd 页码顺序一致；"
        "每个计划项必须包含 path（pages/*.page）、title、readerTask、content、visualPlan 和 sourceSections。"
        "sourceSections 必须逐字复制 sources/reference-outline.md 中的相关一级章节标题；封面、目录、致谢填 []。"
        "长报告中每个一级章节至少分配到 2 个计划页，不能把主要章节压成一张泛化摘要页。"
        "plan.json 是紧凑的页面契约，不是正文草稿：title ≤ 60 字、readerTask ≤ 100 字、"
        "content ≤ 240 字、visualPlan ≤ 180 字。content 只写本页要覆盖的论点和事实线索，"
        "严禁复制原文长段落、表格内容或证据全文；页面作者会按计划再读取对应源章节。"
        "先用一次 Write 写完整合法 JSON，再由 GLOB/READ 核对，不要逐页重写整个计划。"
        "规划阶段只需阅读 reference-outline.md 和少量相关章节，不需要逐张查看模板截图；"
        "模板封面、背景、页脚和公司元素由宿主统一套用。"
        "references 是内容、数据和结构的首要事实来源。参考资料的章节、关键事实、指标、"
        "结论和专有名词必须进入计划，不得只规划前几章。"
        "附件仅提供名称与已有视觉分析；未提供的二进制内容不要假设。按 SKILL.md 判定任务与设计方式。"
    )
    if template_info:
        payload["note"] += (
            f" 已提供公司模板：pptx={template_info['pptx']}，已预转换的 PPTD={template_info['pptd']}"
            f"（入口 {template_info['entry']}），总览截图={template_info['screenshots']}。"
            "按 SKILL.md「Using a template」：分析其配色/字体/版式/可复用组件，然后为本次需求从头生成新内容。"
            "template-pptd/ 与 template-screenshots/ 只是分析素材，严禁把它们当作交付物，也严禁照抄模板的文字、数据或页数。"
            "封面与致谢页会由宿主统一套用模板设计：首页给出标题/副标题/日期即可，末页只写一句收尾语（如「感谢观看」），"
            "正文内容放在中间页。"
        )
    if use_external_reference:
        payload["note"] += (
            " Long reference material is stored inside the workspace. Read the complete "
            "sources/reference-outline.md before planning, then inspect every principal "
            "chapter in sources/reference.md. The written plan must cover the entire "
            "outline, not only its opening chapters."
        )
    return json.dumps(payload, ensure_ascii=False)


def _execution_message(request, template_info: dict | None, reference_info: dict | None,
                       plan: dict, continuation: dict) -> str:
    payload = _base_message_payload(request, template_info, reference_info)
    use_external_reference = payload.pop("_usesExternalReference")
    pages = {
        page["path"]: page
        for page in plan.get("pages", [])
        if isinstance(page, dict) and isinstance(page.get("path"), str)
    }
    payload["plan"] = plan
    payload["continuation"] = continuation
    payload["batchPagePlans"] = [
        pages[path] for path in continuation.get("batchPages", []) if path in pages
    ]
    batch_paths = set(continuation.get("batchPages", []))
    payload["pageValidationIssues"] = [
        issue for issue in continuation.get("invalidPages", [])
        if issue.get("path") in batch_paths
    ]
    payload["note"] = (
        "完整计划已经通过宿主校验，当前只执行分配给你的写页批次。"
        "写入 continuation.batchPages 中列出的 .page 文件；不得修改 deck/plan.json、"
        "不得改变页面数量或顺序、不得删除已有页面、不得把多个计划页合并成一页。"
        "deck/deck.pptd 已由宿主按完整计划创建；不要重写它。"
        "尽量并行写本批全部页面，完成后停止，宿主会启动下一批。"
        "pageValidationIssues 中列出的页面虽然已经存在，但 YAML 无法解析，必须完整重写；"
        "所有数组元素和嵌套字段必须使用合法 YAML 缩进。"
        "页面内容必须对应 batchPagePlans 中的 title、readerTask、content、visualPlan 和 sourceSections。"
        "使用 deck/plan.json、源提纲和最多两个已有代表页维持整套视觉系统。"
    )
    if template_info:
        payload["note"] += " 样式与模板复用规则见系统提示；宿主会在最终阶段统一套用封面、致谢和公司元素。"
    if use_external_reference:
        payload["note"] += (
            " 只读取当前页码对应的源章节；如计划信息不足，再读取 sources/reference.md 的相关位置。"
        )
    return json.dumps(payload, ensure_ascii=False)


def _yaml_parse_issue(page_path: str, exc: BaseException) -> dict:
    """Expose a compact, located page syntax error without leaking page content."""
    problem = getattr(exc, "problem", None) or str(exc)
    mark = getattr(exc, "problem_mark", None)
    location = page_path
    if mark is not None:
        location = f"{page_path}:{mark.line + 1}:{mark.column + 1}"
    return {
        "path": page_path,
        "location": location,
        "code": "yaml-invalid",
        "message": f"{location} 的 YAML 无法解析：{problem}",
    }


def _deck_status(entry: Path):
    """Read the declared deck and distinguish valid, absent and malformed pages."""
    from pptd_engine.document import load_yaml

    document = load_yaml(entry)
    paths = [path for path in document.get("pages", []) if isinstance(path, str)]
    existing = []
    missing = []
    invalid = []
    for path in paths:
        page_path = entry.parent / path
        if not page_path.is_file():
            missing.append(path)
            continue
        try:
            load_yaml(page_path)
        except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
            invalid.append(_yaml_parse_issue(path, exc))
        else:
            existing.append(path)
    return document, paths, existing, missing, invalid


def _deck_progress(entry: Path):
    """Read the declared deck without treating malformed pages as complete."""
    document, paths, existing, _, _ = _deck_status(entry)
    missing = [path for path in paths if path not in existing]
    return document, paths, existing, missing


def _batch_page_target(missing_count: int, remaining_batches: int, minimum: int) -> int:
    """Distribute all missing pages across the batches that are still available.

    The configured batch size is a floor. A first batch is allowed to stop
    before its five-page suggestion, so a 23-page deck can otherwise hit the
    hard batch limit with only 16 pages written (2 + 7 * 2).
    """
    if missing_count <= 0:
        return 0
    if remaining_batches <= 0:
        return missing_count
    return max(minimum, (missing_count + remaining_batches - 1) // remaining_batches)


def recovery_checkpoint(data_root: Path, project_id: str, job_id: str) -> dict | None:
    """Return a public checkpoint summary for an agent-written partial deck."""
    workspace = _workspace(data_root, project_id, job_id)
    deck_dir = workspace / "deck"
    entries = sorted(deck_dir.glob("*.pptd")) if deck_dir.is_dir() else []
    if not entries:
        return None
    try:
        _, paths, existing, missing, invalid = _deck_status(entries[0])
    except (OSError, ValueError, yaml.YAMLError):
        return None
    if not paths:
        return None
    checkpoint = {
        "stage": "pages",
        "completedPages": len(existing),
        "totalPages": len(paths),
        "missingPages": len(missing) + len(invalid),
        "summary": f"已保留 {len(existing)}/{len(paths)} 页",
    }
    if invalid:
        checkpoint["invalidPages"] = len(invalid)
    return checkpoint


def restore_workspace(data_root: Path, project_id: str, job_id: str, parent_job_id: str | None):
    """Resume a failed agent run from its last written deck files."""
    if not parent_job_id:
        return
    source = _workspace(data_root, project_id, parent_job_id)
    destination = _workspace(data_root, project_id, job_id)
    if source.is_dir() and not destination.exists():
        shutil.copytree(source, destination, dirs_exist_ok=True)


def _reply_text(reply_msg) -> str:
    """Extract the assistant text and strip leaked reasoning blocks."""
    if reply_msg is None:
        return ""
    text = getattr(reply_msg, "text", None)
    if not text:
        parts = []
        for block in getattr(reply_msg, "content", []) or []:
            value = getattr(block, "text", None)
            if isinstance(value, str):
                parts.append(value)
        text = "\n".join(parts)
    if not isinstance(text, str):
        return ""
    text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
    return text


def _find_entry(workspace: Path) -> Path:
    """Only the agent's brand-new deliverable counts; template analysis never does."""
    deck_dir = workspace / "deck"
    entries = sorted(deck_dir.glob("*.pptd")) if deck_dir.is_dir() else []
    if not entries:
        raise ValueError("PPT 专家没有在 deck/ 目录产出新的 .pptd 交付物（模板分析产物不会被当作答案）。")
    return entries[0]


def _load_deck(entry: Path):
    from pptd_engine.document import load_yaml

    document = load_yaml(entry)
    pages = []
    for ref in document.get("pages", []):
        try:
            data = load_yaml(entry.parent / ref)
        except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
            issue = _yaml_parse_issue(ref, exc)
            raise ValueError(issue["message"]) from exc
        pages.append({"path": ref, "data": data})
    return document, pages


def _collect_assets(entry: Path) -> dict[str, Path]:
    media = entry.parent / "media"
    assets: dict[str, Path] = {}
    if media.is_dir():
        for path in media.rglob("*"):
            if path.is_file() and not path.is_symlink():
                assets[path.relative_to(entry.parent).as_posix()] = path
    return assets


def _write_deck(directory: Path, entry: Path, document: dict, pages: list[dict]) -> Path:
    """Materialise the final deck so the browser can render exactly what ships."""
    directory.mkdir(parents=True, exist_ok=True)
    media = entry.parent / "media"
    if media.is_dir():
        shutil.copytree(media, directory / "media", dirs_exist_ok=True)
    for page in pages:
        target = directory / str(page["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.safe_dump(page["data"], allow_unicode=True, sort_keys=False), encoding="utf-8")
    target = directory / entry.name
    target.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return target



def _issue_summary(issues: list[dict], limit: int = 8) -> str:
    lines = []
    for issue in issues[:limit]:
        lines.append(f"- {issue.get('location')}: {issue.get('message')}")
    if len(issues) > limit:
        lines.append(f"- 另有 {len(issues) - limit} 条同类问题，请逐一自查同类元素。")
    return "\n".join(lines)


def _issue_page(issue: dict) -> str:
    return str(issue.get("page") or issue.get("location", "").split("#", 1)[0])


def _page_hashes(entry: Path, paths: list[str]) -> dict[str, str]:
    result = {}
    for path in paths:
        target = entry.parent / path
        result[path] = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else ""
    return result


async def run_kimi(request, *, data_root: Path, progress, on_event=None, project_id: str, job_id: str):
    """Run the agentic loop, validate, and commit the produced deck."""
    workspace = _workspace(data_root, project_id, job_id)
    workspace.mkdir(parents=True, exist_ok=True)
    policy = expert_policy()
    max_iters = policy.max_iterations
    batch_iters = min(max_iters, max(8, int(os.getenv("PPT_KIMI_BATCH_ITERS", "18"))))
    plan_iters = min(max_iters, max(12, policy.planning_iterations))
    plan_attempts = policy.planning_attempts
    batch_limit = max(1, int(os.getenv("PPT_KIMI_BATCHES", "8")))
    batch_page_size = policy.page_batch_size
    repair_rounds = policy.repair_rounds
    layout_page_attempts = policy.layout_page_attempts
    layout_page_batch = policy.layout_page_batch
    template_path = _prepare_template(data_root, getattr(request, "templateId", None), workspace)
    template_info = None
    if template_path:
        template_info = await asyncio.to_thread(_prepare_template_analysis, workspace, template_path)
    model = make_model(output_mode="adaptive")
    # The agentic loop performs many independent model calls. Give transient
    # overloads a bounded retry window without changing prompt/context behavior.
    model.max_retries = max(int(getattr(model, 'max_retries', 0) or 0), 3)
    # The agentic ReAct loop makes many short model calls. Per-call remote token
    # counting is both slow and rate-limit-prone, and AgentScope's own context
    # compression already bounds the loop. Disable the deterministic-pipeline
    # budget for this route so one busy counting endpoint cannot abort the job.
    model.budget_enabled = False

    async def emit(phase, status, message, **fields):
        if on_event:
            result = on_event({"phase": phase, "status": status, "message": message, **fields})
            if asyncio.iscoroutine(result):
                await result

    try:
        reference_info = await asyncio.to_thread(
            _prepare_reference_material, data_root, project_id, request, workspace
        )
        reference_characters = reference_info.get("characters") or len(getattr(request, "references", "") or "")
        reference_summary = (
            f"，已加入 {reference_characters:,} 字参考资料。"
            if reference_characters
            else "，未提供文本参考资料。"
        )
        await emit("context", "started", "PPT 专家正在读取上下文并规划" + reference_summary)
        if progress:
            progress(5, "PPT 专家正在按原生工作流生成")
        reply = ""
        document: dict = {}
        pages: list[dict] = []
        final_dir = workspace / "final"
        entry: Path | None = None
        outstanding: list[dict] = []
        requirements = plan_requirements(request, reference_info)
        plan_path = workspace / "deck" / "plan.json"
        plan = _load_plan(plan_path)
        if plan:
            plan_issues = audit_plan(plan, requirements)
        elif plan_path.is_file():
            plan_issues = [
                "deck/plan.json 已存在，但不是完整合法的 JSON；"
                "通常表示计划内容过长，在单次写入时被截断"
            ]
        else:
            plan_issues = ["尚未生成 deck/plan.json"]
        if plan is not None and not plan_issues:
            _write_plan_deck(workspace, plan, template_info)
        else:
            last_issues = plan_issues
            for plan_attempt in range(plan_attempts):
                await emit(
                    "plan",
                    "started" if plan_attempt == 0 else "retrying",
                    "正在规划完整叙事与章节覆盖"
                    + (f"（计划校验未通过，第 {plan_attempt + 1} 次）" if plan_attempt else ""),
                )
                planning_text = _planning_message(request, template_info, reference_info, requirements)
                if plan_attempt:
                    planning_text = (
                        "宿主拒绝了上一版 deck/plan.json：\n- "
                        + "\n- ".join(last_issues[:30])
                        + "\n请重写完整 deck/plan.json，仍然禁止写任何页面。\n\n"
                        + planning_text
                    )
                planning_agent = _make_agent(model, workspace, project_id, plan_iters)
                await planning_agent.reply(Msg(
                    name="user",
                    role="user",
                    content=[{"type": "text", "text": planning_text}],
                ))
                plan = _load_plan(plan_path)
                if plan:
                    last_issues = audit_plan(plan, requirements)
                elif plan_path.is_file():
                    last_issues = [
                        "deck/plan.json 已存在，但不是完整合法的 JSON；"
                        "通常表示计划内容过长，在单次写入时被截断"
                    ]
                else:
                    last_issues = ["未生成 deck/plan.json"]
                if not last_issues:
                    _clear_page_drafts(workspace)
                    _write_plan_deck(workspace, plan, template_info)
                    break
            if plan is None or last_issues:
                raise ValueError(
                    "完整内容计划未通过宿主校验："
                    + "；".join(last_issues[:12])
                )
        source_entry = _find_entry(workspace)
        _, planned_paths, _, _ = _deck_progress(source_entry)
        batch_limit = max(batch_limit, (len(planned_paths) + 4) // 5 + 2)
        await emit(
            "plan",
            "completed",
            f"完整计划已通过校验：{len(planned_paths)} 页，"
            f"覆盖 {len(requirements['principalSections'])} 个一级章节",
            covered=0,
            total=len(planned_paths),
        )
        for batch_index in range(batch_limit):
            continuation = None
            if source_entry is not None:
                _, paths, existing, missing, invalid = _deck_status(source_entry)
                pending = [path for path in paths if path not in existing]
                if not pending:
                    break
                target_pages = _batch_page_target(
                    len(pending), batch_limit - batch_index, batch_page_size
                )
                target_pages = min(5, target_pages)
                continuation = {
                    "batch": batch_index + 1,
                    "totalPages": len(paths),
                    "existingPages": existing,
                    "missingPages": pending,
                    "absentPages": missing,
                    "invalidPages": invalid,
                    "batchPages": pending[:target_pages],
                    "planFile": "deck/plan.json" if (source_entry.parent / "plan.json").is_file() else None,
                }
                await emit(
                    "pages",
                    "started",
                    f"继续原生 Kimi 编排：已完成 {len(existing)}/{len(paths)} 页，"
                    f"正在补写 {len(pending)} 页（本段目标 {target_pages} 页）。",
                    covered=len(existing),
                    total=len(paths),
                )
            agent = _make_agent(model, workspace, project_id, batch_iters)
            message = Msg(
                name="user",
                role="user",
                content=[{
                    "type": "text",
                    "text": _execution_message(
                        request,
                        template_info,
                        reference_info,
                        plan,
                        continuation,
                    ),
                }],
            )
            reply = _reply_text(await agent.reply(message))
            source_entry = _find_entry(workspace)
            _, paths, existing, _, _ = _deck_status(source_entry)
            pending = [path for path in paths if path not in existing]
            await emit(
                "pages",
                "started",
                f"Kimi 本段完成：{len(existing)}/{len(paths)} 页已写入。",
                covered=len(existing),
                total=len(paths),
            )
            if not pending:
                break
        if source_entry is None:
            raise ValueError("PPT 专家没有在 deck/ 目录产出新的 .pptd 交付物。")
        _, planned_paths, existing_paths, _, invalid_paths = _deck_status(source_entry)
        pending_paths = [path for path in planned_paths if path not in existing_paths]
        if pending_paths:
            missing_detail = ", ".join(pending_paths[:6])
            if len(pending_paths) > 6:
                missing_detail += f" 等 {len(pending_paths)} 页"
            invalid_detail = ""
            if invalid_paths:
                invalid_detail = "；YAML 问题：" + "；".join(
                    issue["message"] for issue in invalid_paths[:6]
                )
            raise ValueError(
                f"PPT 专家连续 {batch_limit} 段仍未写完全部页面："
                f"{len(existing_paths)}/{len(planned_paths)} 页可解析，"
                f"待完成 {missing_detail}{invalid_detail}"
            )
        document: dict = {}
        pages: list[dict] = []
        entry: Path | None = None
        for structure_attempt in range(repair_rounds + 1):
            document, pages = _load_deck(source_entry)
            _apply_host_transforms(source_entry, document, pages, template_info)
            shutil.rmtree(final_dir, ignore_errors=True)
            entry = _write_deck(final_dir, source_entry, document, pages)
            issues = validate_document({"document": document, "pages": pages}, resource_root=entry.parent)
            if not issues:
                break
            detail = "; ".join(f"{i.get('location')}: {i.get('message')}" for i in issues[:8])
            if structure_attempt >= repair_rounds:
                raise ValueError(f"生成的 PPTD 未通过结构校验：{detail}")
            await emit(
                "validate",
                "retrying",
                f"结构校验发现 {len(issues)} 项问题，请模型修复后重检。",
                issues=issues[:12],
            )
            if progress:
                progress(65, f"结构校验发现 {len(issues)} 项问题，正在修复")
            message = Msg(name="user", role="user", content=[{"type": "text", "text": (
                "宿主的 PPTD 结构校验发现以下问题：\n"
                + _issue_summary(issues)
                + "\n请只修改对应的 .page 文件修正结构和表格网格，保留全部文字与数据，"
                  "然后重新运行 `kimi_slides check deck --level auto`。"
            )}])
            repair_agent = _make_agent(
                model, workspace, project_id, min(batch_iters, 14)
            )
            reply = _reply_text(await repair_agent.reply(message))

        if entry is None:
            raise ValueError("PPT 专家没有生成可检查的 PPTD 交付稿。")
        page_attempts: dict[str, int] = {}
        page_noops: dict[str, int] = {}
        while True:
            document, pages = _load_deck(source_entry)
            _apply_host_transforms(source_entry, document, pages, template_info)
            shutil.rmtree(final_dir, ignore_errors=True)
            entry = _write_deck(final_dir, source_entry, document, pages)
            issues = validate_document({"document": document, "pages": pages}, resource_root=entry.parent)
            if issues:
                detail = "; ".join(f"{i.get('location')}: {i.get('message')}" for i in issues[:8])
                raise ValueError(f"排版修复引入了结构问题：{detail}")
            await emit("validate", "started", "正在用真实浏览器排版检查整份交付稿（含封面、致谢与公司元素）。")
            rendered = await asyncio.to_thread(_render_pages, entry)
            try:
                layout = await asyncio.to_thread(final_layout_issues, entry, document, pages, rendered)
                visual = []
                if not layout:
                    visual = await visual_overview_issues(
                        document, pages, rendered, on_event=on_event,
                        scenario=getattr(request, "scenario", None) or "auto",
                        cache_dir=data_root / "review-cache",
                    )
            finally:
                if rendered is not None:
                    shutil.rmtree(rendered, ignore_errors=True)
            layout = layout + visual
            if not layout:
                outstanding = []
                break

            source_paths = list(document.get("pages") or [])
            issue_pages = []
            for issue in layout:
                path = _issue_page(issue)
                if path in source_paths and path not in issue_pages:
                    issue_pages.append(path)
            pending = [
                path for path in issue_pages
                if page_attempts.get(path, 0) < layout_page_attempts
            ]
            if not pending:
                outstanding = layout
                await emit(
                    "validate", "waiting",
                    f"仍有 {len(layout)} 项排版问题，已保留可编辑稿；可继续从检查点恢复。",
                    issues=layout[:12],
                )
                break

            batch = pending[:layout_page_batch]
            batch_issues = [issue for issue in layout if _issue_page(issue) in batch]
            page_numbers = [source_paths.index(path) + 1 for path in batch]
            before = _page_hashes(source_entry, batch)
            for path in batch:
                page_attempts[path] = page_attempts.get(path, 0) + 1

            await emit(
                "validate", "retrying",
                f"排版检查发现 {len(layout)} 项问题；正在修复第 "
                f"{', '.join(str(number) for number in page_numbers)} 页。",
                issues=batch_issues[:12],
            )
            if progress:
                progress(70, f"正在修复第 {', '.join(str(number) for number in page_numbers)} 页的排版问题")
            screenshot_dir = workspace / f"layout-repair-{page_attempts[batch[0]]}-{page_numbers[0]}"
            noop_note = "".join(
                f"\n注意：上一轮没有实际修改 {path}，本轮必须修改该页面。"
                for path in batch if page_noops.get(path)
            )
            message = Msg(name="user", role="user", content=[{"type": "text", "text": (
                "宿主的真实浏览器排版检查（渲染最终交付稿，已包含公司模板的封面/致谢与角落元素）发现以下问题：\n"
                + _issue_summary(batch_issues, limit=len(batch_issues))
                + f"\n本轮只处理这些页面：{', '.join(batch)}（页码 {', '.join(str(number) for number in page_numbers)}）。"
                + f"\n请先运行 `kimi_slides screenshot deck -p {','.join(str(number) for number in page_numbers)} "
                  f"-o {screenshot_dir}` 查看实际渲染效果，再编辑对应的 .page 文件。"
                + "\n修改后必须重新运行 `kimi_slides check deck --level auto` 和同一页的 screenshot 验证。"
                  "保持文字与数据完整，不要改动 template-pptd/ 下的分析素材。"
                + noop_note
            )}])
            repair_agent = _make_agent(
                model, workspace, project_id, min(batch_iters, 14)
            )
            reply = _reply_text(await repair_agent.reply(message))
            after = _page_hashes(source_entry, batch)
            for path in batch:
                if after[path] == before[path]:
                    page_noops[path] = page_noops.get(path, 0) + 1
                else:
                    page_noops[path] = 0
        from .pptd_store import DocumentStore

        store = DocumentStore(data_root)
        revision = store.read(project_id)["revision"] if store.exists(project_id) else 0
        committed = store.commit(
            project_id, document, pages, "AI 生成（PPT 专家）", revision,
            assets=_collect_assets(entry),
        )
        await emit("validate", "completed", "结构检查与真实排版检查通过，已提交可编辑文档。")
        if outstanding:
            reply = (reply + "\n\n## 排版检查未通过项\n\n"
                     "以下问题已定位到页面与元素；文档仍可编辑预览，请确认或调整后再导出：\n\n"
                     + _issue_summary(outstanding)).strip()
        return {
            "document": committed["document"],
            "pages": committed["pages"],
            "revision": committed["revision"],
            "reply": reply,
            "warnings": [{"location": issue.get("location"), "code": issue.get("code"),
                          "message": issue.get("message")} for issue in outstanding],
            "generationMeta": {
                "workflow": "kimi-slides",
                "model": getattr(model, "model", None) or getattr(model, "_model", None),
                "workspace": str(workspace),
            },
        }
    finally:
        await model.client.close()


def kimi_health() -> dict:
    from .model_gateway import health

    return health()
