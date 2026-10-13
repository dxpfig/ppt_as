"""Kimi-slides 1:1 agentic PPT expert.

Unlike the deterministic ``pptd_generation`` pipeline, this agent gives the model
real file and CLI tools and lets it follow ``docs/kimi-ppt/original/SKILL.md``
itself: classify the task, read only the rules it needs, write pages in parallel,
and close the ``kimi-slides check`` / ``screenshot`` loop before finishing.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from agentscope.agent import Agent, ContextConfig, ReActConfig
from agentscope.message import Msg
from agentscope.middleware import TracingMiddleware
from agentscope.permission import PermissionBehavior, PermissionDecision, PermissionMode
from agentscope.state import AgentState
from agentscope.tool import Bash, Edit, FunctionTool, Glob, Grep, Read, Toolkit, Write

from .agents import make_model, timeout_setting
from .pptd_validation import validate_document


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
    _run_cli("convert", [str(template_pptx), "-o", str(pptd_dir)], workspace)
    _run_cli("screenshot", [str(pptd_dir), "-o", str(shots_dir)], workspace)
    return {
        "pptx": str(template_pptx),
        "pptd": str(pptd_dir),
        "entry": str(pptd_dir / f"{template_pptx.stem}.pptd"),
        "screenshots": str(shots_dir),
    }


def _initial_message(request, template_info: dict | None = None) -> str:
    attachments = []
    for item in getattr(request, "attachments", []) or []:
        if isinstance(item, dict):
            attachments.append({"name": item.get("name"), "visualAnalysis": item.get("visualAnalysis")})
    payload = {
        "prompt": getattr(request, "prompt", "") or "",
        "audience": getattr(request, "audience", "") or "通用汇报",
        "pageCount": getattr(request, "pageCount", None),
        "references": getattr(request, "references", "") or "",
        "attachments": attachments,
        "template": template_info,
        "note": (
            "交付物必须在 deck/ 目录写出全新的 .pptd（建议 deck/deck.pptd + deck/pages/*.page + deck/media/*）；"
            "不在 deck/ 下的产物不会被视为交付物。页数以用户指定为准。"
            "附件仅提供名称与已有视觉分析；未提供的二进制内容不要假设。按 SKILL.md 判定任务与设计方式。"
        ),
    }
    if template_info:
        payload["note"] += (
            f" 已提供公司模板：pptx={template_info['pptx']}，已预转换的 PPTD={template_info['pptd']}"
            f"（入口 {template_info['entry']}），总览截图={template_info['screenshots']}。"
            "按 SKILL.md「Using a template」：分析其配色/字体/版式/可复用组件，然后为本次需求从头生成新内容。"
            "template-pptd/ 与 template-screenshots/ 只是分析素材，严禁把它们当作交付物，也严禁照抄模板的文字、数据或页数。"
        )
    return json.dumps(payload, ensure_ascii=False)


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
        raise ValueError("Kimi 专家没有在 deck/ 目录产出新的 .pptd 交付物（模板分析产物不会被当作答案）。")
    return entries[0]


def _load_deck(entry: Path):
    from pptd_engine.document import load_yaml

    document = load_yaml(entry)
    pages = []
    for ref in document.get("pages", []):
        pages.append({"path": ref, "data": load_yaml(entry.parent / ref)})
    return document, pages


def _collect_assets(entry: Path) -> dict[str, Path]:
    media = entry.parent / "media"
    assets: dict[str, Path] = {}
    if media.is_dir():
        for path in media.rglob("*"):
            if path.is_file() and not path.is_symlink():
                assets[path.relative_to(entry.parent).as_posix()] = path
    return assets


def _corner(bounds, size):
    """Classify an element into the top-right / bottom-right company chrome band."""
    if not (isinstance(bounds, list) and len(bounds) == 4 and all(isinstance(v, (int, float)) for v in bounds)):
        return False, False
    x, y, w, h = bounds
    width, height = size
    # Deliberately tight: chrome is a small element glued to the right edge, not a
    # full-page background, a wide title, or a large content shape.
    right = x >= width * 0.72 and w <= width * 0.4
    top = right and h <= height * 0.4 and y <= height * 0.03
    bottom = right and h <= height * 0.12 and y + h >= height * 0.97
    return bool(top), bool(bottom)


def _chrome_signature(element):
    if element.get("elementType") == "image":
        return ("image", element.get("src"))
    if element.get("elementType") == "text":
        text = re.sub(r"<[^>]+>", "", str((element.get("content") or {}).get("text", ""))).strip()
        return ("text", text[:40])
    return (element.get("elementType"), element.get("shapeName") or element.get("elementId"))


def _extract_chrome(template_dir: Path) -> list[dict]:
    """Recurring top-right / bottom-right elements shared by the template's pages."""
    from pptd_engine.document import load_yaml

    entries = sorted(template_dir.glob("*.pptd"))
    if not entries:
        return []
    document = load_yaml(entries[0])
    size = document.get("size")
    if not (isinstance(size, list) and len(size) == 2):
        return []
    top: dict[tuple, list] = {}
    bottom: dict[tuple, list] = {}
    for ref in document.get("pages", []):
        data = load_yaml(template_dir / ref)
        for element in data.get("elements", []):
            if not isinstance(element, dict):
                continue
            in_top, in_bottom = _corner(element.get("bounds"), size)
            if not (in_top or in_bottom):
                continue
            bucket = top if in_top else bottom
            signature = _chrome_signature(element)
            count, representative = bucket.get(signature, (0, element))
            bucket[signature] = (count + 1, representative)
    chrome: list[dict] = []
    for bucket in (top, bottom):
        if not bucket:
            continue
        highest = max(count for count, _ in bucket.values())
        threshold = 2 if highest >= 2 else 1
        chrome.extend(representative for count, representative in bucket.values() if count >= threshold)
    return chrome


def _apply_chrome(deck_dir: Path, document: dict, pages: list[dict], chrome: list[dict], template_dir: Path) -> None:
    """Re-impose the template's company chrome onto every generated page."""
    if not chrome:
        return
    size = document.get("size")
    for element in chrome:
        src = element.get("src")
        if isinstance(src, str) and src.startswith("media/"):
            source = template_dir / src
            target = deck_dir / src
            if source.is_file() and not target.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
    top = [e for e in chrome if _corner(e.get("bounds"), size)[0]]
    bottom = [e for e in chrome if _corner(e.get("bounds"), size)[1]]
    for page in pages:
        data = page.get("data")
        if not isinstance(data, dict):
            continue
        elements = data.setdefault("elements", [])
        has_top = any(_corner(e.get("bounds"), size)[0] for e in elements if isinstance(e, dict))
        has_bottom = any(_corner(e.get("bounds"), size)[1] for e in elements if isinstance(e, dict))
        for group, present in ((top, has_top), (bottom, has_bottom)):
            if present:
                continue
            for element in group:
                clone = json.loads(json.dumps(element))
                ids = {e.get("elementId") for e in elements if isinstance(e, dict)}
                base = str(clone.get("elementId") or "chrome")
                new_id = f"{base}-chrome"
                while new_id in ids:
                    new_id += "x"
                clone["elementId"] = new_id
                elements.append(clone)


async def run_kimi(request, *, data_root: Path, progress, on_event=None, project_id: str, job_id: str):
    """Run the agentic loop, validate, and commit the produced deck."""
    workspace = _workspace(data_root, project_id, job_id)
    workspace.mkdir(parents=True, exist_ok=True)
    max_iters = int(os.getenv("PPT_KIMI_MAX_ITERS", "40"))
    template_path = _prepare_template(data_root, getattr(request, "templateId", None), workspace)
    template_info = None
    if template_path:
        template_info = await asyncio.to_thread(_prepare_template_analysis, workspace, template_path)
    model = make_model()
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
        await emit("context", "started", "Kimi PPT 专家正在读取上下文并规划。")
        if progress:
            progress(5, "Kimi PPT 专家正在按 kimi-slides 流程工作")
        agent = _make_agent(model, workspace, project_id, max_iters)
        await emit("pages", "started", "模型正在自由编排：判定任务、写页并自查。")
        reply_msg = await agent.reply(
            Msg(name="user", role="user", content=[{"type": "text", "text": _initial_message(request, template_info)}])
        )
        reply = _reply_text(reply_msg)
        entry = _find_entry(workspace)
        document, pages = _load_deck(entry)
        if template_info:
            _apply_chrome(entry.parent, document, pages, _extract_chrome(Path(template_info["pptd"])), Path(template_info["pptd"]))
        issues = validate_document({"document": document, "pages": pages}, resource_root=entry.parent)
        if issues:
            detail = "; ".join(f"{i.get('location')}: {i.get('message')}" for i in issues[:8])
            raise ValueError(f"生成的 PPTD 未通过结构校验：{detail}")
        from .pptd_store import DocumentStore

        store = DocumentStore(data_root)
        revision = store.read(project_id)["revision"] if store.exists(project_id) else 0
        committed = store.commit(
            project_id, document, pages, "AI 生成（Kimi 专家）", revision,
            assets=_collect_assets(entry),
        )
        await emit("validate", "completed", "PPTD 结构检查通过，已提交可编辑文档。")
        return {
            "document": committed["document"],
            "pages": committed["pages"],
            "revision": committed["revision"],
            "reply": reply,
            "generationMeta": {
                "workflow": "kimi-slides",
                "model": getattr(model, "model", None) or getattr(model, "_model", None),
                "workspace": str(workspace),
            },
        }
    finally:
        await model.client.close()


def kimi_health() -> dict:
    from .agents import health

    return health()
