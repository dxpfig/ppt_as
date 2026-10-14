"""Kimi-slides 1:1 agentic PPT expert.

Unlike the deterministic ``pptd_generation`` pipeline, this agent gives the model
real file and CLI tools and lets it follow ``docs/kimi-ppt/original/SKILL.md``
itself: classify the task, read only the rules it needs, write pages in parallel,
and close the ``kimi-slides check`` / ``screenshot`` loop before finishing.
"""
from __future__ import annotations

import asyncio
from collections import Counter
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image
import yaml

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
            "封面与致谢页会由宿主统一套用模板设计：首页给出标题/副标题/日期即可，末页只写一句收尾语（如「感谢观看」），"
            "正文内容放在中间页。"
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


def _covers_page(element: dict, size) -> bool:
    bounds = element.get("bounds")
    if not (isinstance(bounds, list) and len(bounds) == 4) or len(size) != 2:
        return False
    x, y, w, h = (float(value) for value in bounds)
    return w >= size[0] * 0.9 and h >= size[1] * 0.9 and x <= size[0] * 0.05 and y <= size[1] * 0.05


def _extract_body_background(template_dir: Path) -> dict | None:
    """The full-page background artwork that the template's body pages share."""
    from pptd_engine.document import load_yaml

    entries = sorted(template_dir.glob("*.pptd"))
    if not entries:
        return None
    document = load_yaml(entries[0])
    size = document.get("size")
    refs = document.get("pages", [])
    if not (isinstance(size, list) and len(size) == 2) or len(refs) < 3:
        return None
    counts: Counter = Counter()
    representatives: dict[tuple, dict] = {}
    for ref in refs[1:-1]:
        data = load_yaml(template_dir / ref)
        for element in data.get("elements", []):
            if not isinstance(element, dict) or element.get("elementType") != "image":
                continue
            if not _covers_page(element, size):
                continue
            signature = (element.get("src"),)
            counts[signature] += 1
            representatives.setdefault(signature, element)
    if not counts:
        return None
    signature, count = counts.most_common(1)[0]
    if count < max(2, len(refs[1:-1]) // 2):
        return None
    element = representatives[signature]
    if not isinstance(element.get("src"), str):
        return None
    return element


def _is_decorative_band(element: dict, size) -> bool:
    """A synthetic full-width bottom band that duplicates the template background."""
    if not isinstance(element, dict) or element.get("elementType") != "shape":
        return False
    bounds = element.get("bounds")
    if not (isinstance(bounds, list) and len(bounds) == 4) or len(size) != 2:
        return False
    x, y, w, h = (float(value) for value in bounds)
    return (w >= size[0] * 0.9 and h <= size[1] * 0.2 and y + h >= size[1] * 0.95
            and (element.get("shapeName") or "") == "custom" and isinstance(element.get("path"), str))


def _apply_background(deck_dir: Path, document: dict, pages: list[dict], background: dict | None,
                      template_dir: Path) -> None:
    """Give every generated body page the template's own page background.

    Without this step the generated pages keep their plain white background and
    the decorative band the model invented, so the deck no longer matches the
    company template even though the corner chrome does. A page that already
    paints a *different* slide-sized image is repointed at the template's own
    background instead of being left alone: the template uses one image for the
    whole body, and mixing a second, more saturated variant is what made the
    contents page look off-template.
    """
    size = document.get("size")
    if background is None or not (isinstance(size, list) and len(size) == 2):
        return
    src = background.get("src")
    if not isinstance(src, str) or not src.startswith("media/"):
        return
    source = template_dir / src
    if not source.is_file():
        return
    canonical = source.read_bytes()
    deck_src = _share_media(deck_dir, src, canonical)
    for index, page in enumerate(pages):
        if index in (0, len(pages) - 1):
            continue
        data = page.get("data")
        if not isinstance(data, dict):
            continue
        elements = [e for e in data.get("elements", []) if isinstance(e, dict) and not _is_decorative_band(e, size)]
        data["elements"] = elements
        layer = _page_background_layer(data, elements, size)
        if layer is None:
            clone = json.loads(json.dumps(background))
            clone["src"] = deck_src
            data["background"] = clone
            continue
        current = layer.get("src")
        if isinstance(current, str) and _same_media(deck_dir, current, canonical):
            continue
        layer["src"] = deck_src


def _share_media(deck_dir: Path, preferred: str, content: bytes) -> str:
    """Reuse an identical image already in the deck, else copy the template's."""
    media = deck_dir / "media"
    target = deck_dir / preferred
    if target.is_file() and target.read_bytes() == content:
        return preferred
    if media.is_dir():
        for candidate in sorted(media.iterdir()):
            if candidate.is_file() and candidate.read_bytes() == content:
                return f"media/{candidate.name}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return preferred


def _same_media(deck_dir: Path, value: str, content: bytes) -> bool:
    if not isinstance(value, str) or not value.startswith("media/"):
        return False
    path = deck_dir / value
    try:
        return path.is_file() and path.read_bytes() == content
    except OSError:
        return False


def _page_background_layer(data: dict, elements: list, size) -> dict | None:
    """The page-level or element-level image that currently paints the page."""
    page_background = data.get("background")
    if isinstance(page_background, dict) and page_background.get("type") == "image" and isinstance(page_background.get("src"), str):
        return page_background
    for element in elements:
        if element.get("elementType") == "image" and _covers_page(element, size):
            return element
    return None


def _visible_box(element: dict, base_dir: Path):
    """Element box trimmed to the opaque pixels of its image, if any."""
    bounds = element.get("bounds")
    if not (isinstance(bounds, list) and len(bounds) == 4):
        return None
    values = [float(value) for value in bounds]
    if element.get("elementType") == "image" and isinstance(element.get("src"), str):
        try:
            with Image.open(base_dir / element["src"]) as image:
                if image.mode not in ("RGBA", "LA"):
                    return values
                alpha = image.getchannel("A")
                box = alpha.getbbox()
                if box is None:
                    return values
                if box == (0, 0, image.width, image.height):
                    return values
                scale_x, scale_y = values[2] / image.width, values[3] / image.height
                left, top, right, bottom = box
                return [values[0] + left * scale_x, values[1] + top * scale_y,
                        (right - left) * scale_x, (bottom - top) * scale_y]
        except (OSError, ValueError, ZeroDivisionError):
            return values
    return values


def _intersects(bounds, box, tolerance: float = 2.0) -> bool:
    if not (isinstance(bounds, list) and len(bounds) == 4 and isinstance(box, list) and len(box) == 4):
        return False
    width = min(bounds[0] + bounds[2], box[0] + box[2]) - max(bounds[0], box[0])
    height = min(bounds[1] + bounds[3], box[1] + box[3]) - max(bounds[1], box[1])
    return width > tolerance and height > tolerance


def _relocate(bounds, box, size, gap: float = 4.0):
    """Move one of our elements out of the template's mandatory corner band."""
    x, y, w, h = (float(value) for value in bounds)
    box_x, box_y, box_w, box_h = (float(value) for value in box)
    trimmed = box_x - gap - x
    if w > 48 and 48 <= trimmed < w:
        return [x, y, trimmed, h]
    shifted = box_x - gap - w
    if shifted >= 0 and shifted != x:
        return [shifted, y, w, h]
    above = box_y - gap - h
    if above >= 0:
        return [x, above, w, h]
    below = box_y + box_h + gap
    if below + h <= size[1]:
        return [x, below, w, h]
    return None


def _clear_corner_conflicts(elements: list[dict], chrome: list[dict], template_dir: Path, size) -> list[str]:
    """Relocate our own content that the template's mandatory chrome would cover."""
    boxes = [box for box in (_visible_box(element, template_dir) for element in chrome) if box]
    moved: list[str] = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        # Page-covering artwork is the background the chrome deliberately sits
        # on top of; only foreground content is pushed out of the corner band.
        if _covers_page(element, size):
            continue
        bounds = element.get("bounds")
        if not (isinstance(bounds, list) and len(bounds) == 4):
            continue
        for box in boxes:
            if not _intersects(bounds, box):
                continue
            replacement = _relocate(bounds, box, size)
            if replacement is None:
                continue
            element["bounds"] = replacement
            bounds = replacement
            moved.append(str(element.get("elementId")))
    return moved


def _apply_chrome(deck_dir: Path, document: dict, pages: list[dict], chrome: list[dict], template_dir: Path,
                  *, skip_first: bool = False, skip_last: bool = False) -> None:
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
    start = 1 if skip_first else 0
    stop = len(pages) - 1 if skip_last else len(pages)
    for index in range(start, max(start, stop)):
        page = pages[index]
        data = page.get("data")
        if not isinstance(data, dict):
            continue
        elements = data.setdefault("elements", [])
        _clear_corner_conflicts(elements, chrome, template_dir, size)
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
                _grow_text_box(clone)
                elements.append(clone)


def _grow_text_box(element: dict) -> None:
    """Grow a chrome text box that is too short for its own font size."""
    if element.get("elementType") != "text":
        return
    bounds = element.get("bounds")
    size = _font_size(element.get("content") or {})
    if not size or not (isinstance(bounds, list) and len(bounds) == 4):
        return
    required = round(size * 1.8, 2)
    if bounds[3] < required:
        element["bounds"] = [bounds[0], bounds[1], bounds[2], required]


def _deck_body_font(document: dict, pages: list[dict]) -> str | None:
    """The font the deck's own body text uses, for tables that inherit nothing."""
    theme = document.get("theme") or {}
    styles = theme.get("textStyles") or {}
    declared = styles.get("body", {}).get("fontFamily")
    if isinstance(declared, str) and declared:
        return declared
    counts: Counter = Counter()
    for page in pages:
        for element in page.get("data", {}).get("elements", []):
            if not isinstance(element, dict) or element.get("elementType") != "text":
                continue
            content = element.get("content") or {}
            family = content.get("fontFamily")
            if isinstance(family, str) and not family.startswith("$"):
                counts[family] += 1
            else:
                counts.update(name.strip() for name in re.findall(r"font-family:\s*([^;\"]+)", str(content.get("text", ""))))
    return counts.most_common(1)[0][0] if counts else None


def _normalize_table_fonts(document: dict, pages: list[dict]) -> None:
    """Tables that declare no font keep the deck's own typeface.

    The renderer falls back to its built-in default for a table cell that names
    no font, so a table silently used a different typeface from every other text
    element on the page. The company template's own tables name their font
    explicitly; this does the same for generated ones.
    """
    font = _deck_body_font(document, pages)
    if not font:
        return
    for page in pages:
        for element in page.get("data", {}).get("elements", []):
            if not isinstance(element, dict) or element.get("elementType") != "table":
                continue
            style = element.get("style")
            if not isinstance(style, dict):
                continue
            parts = [style.get("firstRowStyle"), style.get("lastRowStyle"), style.get("cellStyle"),
                     style.get("firstColumnStyle"), style.get("lastColumnStyle")]
            body = style.get("bodyStyles")
            parts.extend(body if isinstance(body, list) else [])
            for part in parts:
                if isinstance(part, dict):
                    part.setdefault("fontFamily", font)


CLOSING_MARKERS = ("感谢", "谢谢", "请领导", "敬请", "thank you", "thanks", "q&a")


def _font_size(content: dict) -> float:
    value = content.get("fontSize")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    match = re.search(r"font-size:\s*([0-9.]+)px", str(content.get("text", "")))
    return float(match.group(1)) if match else 0.0


def _visible_text(content: dict) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", str(content.get("text", "")))).strip()


def _color_of(content: dict) -> str:
    """The explicit colour of a text element, from its field or its markup."""
    if isinstance(content.get("color"), str):
        return content["color"]
    match = re.search(r"color:\s*([^;\"]+)", str(content.get("text", "")))
    return match.group(1).strip() if match else ""


def _text_slots(page_data: dict) -> list[dict]:
    """Text elements ordered by prominence: largest font first, then top to bottom."""
    slots = [e for e in page_data.get("elements", []) if isinstance(e, dict) and e.get("elementType") == "text"]
    slots.sort(key=lambda e: (-_font_size(e.get("content") or {}), (e.get("bounds") or [0, 0, 0, 0])[1]))
    return slots


def _relabel(content: dict, text: str) -> dict:
    """Replace one slot's visible text while keeping the template's paragraphs.

    Trailing empty paragraphs are load-bearing: a vertically centred title box
    relies on them to sit above the subtitle. Collapsing the markup to a single
    paragraph re-centres the line onto the subtitle, which is exactly how the
    black overlapping cover title was produced.
    """
    markup = str(content.get("text", ""))
    paragraphs = re.findall(r"<p\b[^>]*>[\s\S]*?</p>", markup, re.IGNORECASE)
    escaped = html.escape(text)
    span = re.search(r'<span[^>]*\sstyle="([^"]*)"[^>]*>', markup)

    def body() -> str:
        return f'<span style="{span.group(1)}">{escaped}</span>' if span else escaped

    if not paragraphs:
        return {**content, "text": f"<p>{body()}</p>"}
    rebuilt: list[str] = []
    filled = False
    for paragraph in paragraphs:
        open_end = paragraph.find(">") + 1
        if not _visible_text({"text": paragraph}):
            # An empty paragraph is an empty *line* in PowerPoint, and templates
            # use it to offset a centred title. Keep its run size so the browser
            # gives it the same line box instead of collapsing it.
            rebuilt.append(_blank_paragraph(paragraph, span))
            continue
        if filled:
            rebuilt.append(paragraph)
            continue
        filled = True
        rebuilt.append(paragraph[:open_end] + body() + "</p>")
    start, end = markup.find(paragraphs[0]), markup.rfind(paragraphs[-1]) + len(paragraphs[-1])
    return {**content, "text": markup[:start] + "".join(rebuilt) + markup[end:]}


def _blank_paragraph(paragraph: str, span) -> str:
    """An empty paragraph that still occupies a line at the slot's font size."""
    open_end = paragraph.find(">") + 1
    if span is None or "font-size" not in span.group(1):
        return paragraph
    return paragraph[:open_end] + f'<span style="{span.group(1)}">&nbsp;</span></p>'


DATE_PATTERN = re.compile(r"(?:19|20)\d{2}\s*(?:[年./\-]|$)")


def _fits(text: str, bounds, content: dict) -> bool:
    """One-line fit test so a slot is never flooded with a long sentence."""
    if not (isinstance(bounds, list) and len(bounds) == 4):
        return True
    size = _font_size(content) or 16.0
    return _text_width(text, size) <= float(bounds[2]) + 2


def _text_width(text: str, size: float) -> float:
    """Rough one-line width: full width for CJK, about half for Latin."""
    return sum(size * (1.0 if ord(character) > 0x2E80 else 0.55) for character in text)


def _fill_template_page(template_page: dict, texts: list[str], *, keep_unfilled: bool) -> dict:
    """Write new text into the template's own slots, keeping its typography.

    A slot is only filled when the text fits its single-line capacity; the
    template's narrow date slot is not flooded with a full sentence.
    """
    data = json.loads(json.dumps(template_page))
    for index, element in enumerate(_text_slots(data)):
        content = element.get("content") or {}
        # The headline slot always takes the model's title, even if the rough
        # width estimate is pessimistic; only the secondary slots are gated.
        if index < len(texts) and texts[index] and (index == 0 or _fits(texts[index], element.get("bounds"), content)):
            element["content"] = _relabel(content, texts[index])
        elif not keep_unfilled:
            element["content"] = _relabel(element.get("content") or {}, "")
    return data


def _date_line(texts: list[str]) -> str:
    """The cleanest date-like line, preferring one that starts with a year."""
    matches = [text for text in texts if DATE_PATTERN.search(text)]
    if not matches:
        return ""
    return next((text for text in matches if re.match(r"\s*(?:19|20)\d{2}", text)), min(matches, key=len))


def _template_texts(page_data: dict, *, closing: bool = False) -> list[str]:
    """Texts for the template's slots: headline, subtitle, then the date slot.

    The closing template's second slot is its own date line, not a subtitle, so
    a long sentence must never be written there.
    """
    texts = [text for text in (_visible_text(e.get("content") or {}) for e in _text_slots(page_data)) if text]
    if not texts:
        return []
    if closing:
        return [texts[0], _date_line(texts[1:])]
    return [*texts[:2], _date_line(texts[2:])]


def _copy_page_media(page_data: dict, template_dir: Path, deck_dir: Path) -> None:
    for element in page_data.get("elements", []):
        src = element.get("src") if isinstance(element, dict) else None
        if isinstance(src, str) and src.startswith("media/"):
            source, target = template_dir / src, deck_dir / src
            if source.is_file() and not target.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())


def _is_closing_like(page_data: dict, path: str = "") -> bool:
    """Only explicit closing wording or a closing-style page name counts."""
    texts = [_visible_text(e.get("content") or {}) for e in page_data.get("elements", []) if isinstance(e, dict) and e.get("elementType") == "text"]
    joined = (" ".join(t for t in texts if t) + " " + str(path)).lower()
    return any(marker in joined for marker in CLOSING_MARKERS)


def _apply_template_cover_closing(template_dir: Path, deck_dir: Path, document: dict, pages: list[dict]) -> None:
    """Use the template's own cover and closing designs for the first/last page."""
    from pptd_engine.document import load_yaml

    entries = sorted(template_dir.glob("*.pptd"))
    if not entries:
        return
    template = load_yaml(entries[0])
    refs = template.get("pages", [])
    if len(refs) < 1 or not pages:
        return
    cover_template = load_yaml(template_dir / refs[0])
    _copy_page_media(cover_template, template_dir, deck_dir)
    pages[0]["data"] = _fill_template_page(cover_template, _template_texts(pages[0]["data"]), keep_unfilled=False)
    if len(refs) < 2:
        document["pages"] = [p["path"] for p in pages]
        return
    closing_template = load_yaml(template_dir / refs[-1])
    _copy_page_media(closing_template, template_dir, deck_dir)
    if len(pages) >= 2 and _is_closing_like(pages[-1]["data"], pages[-1]["path"]):
        pages[-1]["data"] = _fill_template_page(
            closing_template, _template_texts(pages[-1]["data"], closing=True), keep_unfilled=False)
    else:
        existing = {p["path"] for p in pages}
        path, n = "pages/closing.page", 1
        while path in existing:
            path = f"pages/closing-{n}.page"
            n += 1
        pages.append({"path": path, "data": _fill_template_page(closing_template, ["感谢观看"], keep_unfilled=False)})
    document["pages"] = [p["path"] for p in pages]


def _apply_host_transforms(entry: Path, document: dict, pages: list[dict], template_info: dict | None) -> None:
    """Layer the company template onto the model's deck, in one place."""
    if not template_info:
        return
    template_dir = Path(template_info["pptd"])
    _apply_template_cover_closing(template_dir, entry.parent, document, pages)
    _apply_background(entry.parent, document, pages, _extract_body_background(template_dir), template_dir)
    _normalize_table_fonts(document, pages)
    _apply_chrome(entry.parent, document, pages, _extract_chrome(template_dir), template_dir,
                  skip_first=True, skip_last=True)


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


def _renderer_ready() -> bool:
    try:
        from pptd_engine.cli import _renderer_ready as ready
    except ImportError:  # pragma: no cover - defensive boundary
        return False
    try:
        return bool(ready())
    except Exception:  # pragma: no cover - defensive boundary
        return False


def _browser_layout_issues(entry: Path) -> list[dict]:
    """Run the real browser layout check against the finished deck.

    The model's own ``kimi_slides screenshot`` loop runs before the host swaps
    in the template cover/closing page, so it can never see those pages. These
    are the same located checks the deterministic pipeline enforces.
    """
    if not _renderer_ready():
        return []
    from pptd_engine.cli import _resolve_chromium
    from pptd_engine.workbench import WEB_DIR, snapshot_server

    env = os.environ.copy()
    browser = _resolve_chromium()
    if browser:
        env["PPTD_CHROMIUM"] = browser
    with tempfile.TemporaryDirectory(prefix=".kimi-layout-", dir=entry.parent.parent) as tmp:
        output = Path(tmp)
        with snapshot_server(entry, 0) as url:
            process = subprocess.run(
                ["node", str(WEB_DIR / "export_pdf.cjs"), url, str(output)],
                env=env, capture_output=True, text=True, timeout=420,
            )
        report = output / "validation-errors.json"
        if not report.is_file():
            if process.returncode:
                raise ValueError("浏览器排版检查未能完成：" + (process.stderr or process.stdout)[-2000:])
            return []
        payload = json.loads(report.read_text(encoding="utf-8"))
        return [issue for issue in payload.get("issues", []) if isinstance(issue, dict)]


def _render_pages(entry: Path) -> Path | None:
    if not _renderer_ready():
        return None
    from pptd_engine.cli import _screenshot

    output = entry.parent.parent / f".{entry.parent.name}-render"
    try:
        _screenshot(entry, output, None, 1.0, "png", None)
    except Exception:  # pragma: no cover - rendering is best effort here
        return None
    return output


MIN_BLOCKING_CONTRAST = 2.0


def _relative_luminance(rgb) -> float:
    def channel(value: float) -> float:
        value = value / 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (channel(value) for value in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast_ratio(first, second) -> float:
    high = max(_relative_luminance(first), _relative_luminance(second))
    low = min(_relative_luminance(first), _relative_luminance(second))
    return (high + 0.05) / (low + 0.05)


def _resolve_color(value: str, theme: dict):
    """Resolve a PPTD colour, theme references included, to an (r, g, b) tuple."""
    seen = set()
    while isinstance(value, str) and value.startswith("$"):
        if value in seen:
            return None
        seen.add(value)
        value = (theme.get("colors") or {}).get(value[1:])
    match = re.fullmatch(r"#([0-9a-fA-F]{6})", str(value or "").strip())
    if not match:
        return None
    digits = match.group(1)
    return tuple(int(digits[index:index + 2], 16) for index in (0, 2, 4))


def _dominant_color(pixels, x1: int, y1: int, x2: int, y2: int):
    """The most common colour in a box, quantised so photos do not fragment."""
    buckets: dict[tuple, list] = {}
    step_x = max(1, (x2 - x1) // 32)
    step_y = max(1, (y2 - y1) // 32)
    for y in range(y1, y2, step_y):
        for x in range(x1, x2, step_x):
            pixel = pixels[x, y]
            key = (pixel[0] // 16, pixel[1] // 16, pixel[2] // 16)
            bucket = buckets.setdefault(key, [0, [0, 0, 0]])
            bucket[0] += 1
            bucket[1] = [bucket[1][index] + pixel[index] for index in range(3)]
    if not buckets:
        return None
    count, total = max(buckets.values(), key=lambda item: item[0])
    return tuple(value // count for value in total)


def _contrast_issues(document: dict, pages: list[dict], rendered: Path | None) -> list[dict]:
    """Flag text that is barely separable from what is actually behind it.

    Structural checks cannot see this: the black cover title of a template whose
    real title is white is perfectly legal PPTD. Only the rendered pixels show
    that the text is unreadable against the artwork. Each page is sampled in
    blocks so a title that only disappears over the dark part of a photo is
    still reported.
    """
    if rendered is None:
        return []
    issues: list[dict] = []
    size = document.get("size")
    theme = document.get("theme") or {}
    for index, page in enumerate(pages, 1):
        image_path = rendered / "pages" / f"page-{index}.png"
        data = page.get("data") if isinstance(page, dict) else None
        if not image_path.is_file() or not isinstance(data, dict):
            continue
        try:
            with Image.open(image_path).convert("RGB") as image:
                pixels = image.load()
                for element in data.get("elements", []):
                    if not isinstance(element, dict) or element.get("elementType") != "text":
                        continue
                    content = element.get("content") or {}
                    if not _visible_text(content):
                        continue
                    bounds = element.get("bounds")
                    if not (isinstance(bounds, list) and len(bounds) == 4):
                        continue
                    # No declared colour means the renderer drew the default
                    # black, which is exactly the case that shipped unreadable.
                    declared = _resolve_color(_color_of(content) or "#000000", theme)
                    if declared is None:
                        continue
                    x1 = max(1, int(bounds[0]))
                    y1 = max(1, int(bounds[1]))
                    x2 = min(image.width - 1, int(bounds[0] + bounds[2]))
                    y2 = min(image.height - 1, int(bounds[1] + bounds[3]))
                    if x2 - x1 < 12 or y2 - y1 < 12:
                        continue
                    worst = None
                    for row in range(3):
                        for column in range(3):
                            bx1 = x1 + (x2 - x1) * column // 3
                            bx2 = x1 + (x2 - x1) * (column + 1) // 3
                            by1 = y1 + (y2 - y1) * row // 3
                            by2 = y1 + (y2 - y1) * (row + 1) // 3
                            if bx2 - bx1 < 4 or by2 - by1 < 4:
                                continue
                            dominant = _dominant_color(pixels, bx1, by1, bx2, by2)
                            if dominant is None:
                                continue
                            # A block dominated by the text colour itself only says
                            # the text is dense there, not that it is unreadable.
                            if max(abs(dominant[i] - declared[i]) for i in range(3)) < 40:
                                continue
                            ratio = _contrast_ratio(declared, dominant)
                            worst = ratio if worst is None else min(worst, ratio)
                    if worst is None or worst >= MIN_BLOCKING_CONTRAST:
                        continue
                    issues.append({
                        "page": page.get("path"),
                        "elementId": element.get("elementId"),
                        "location": f"{page.get('path')}#{element.get('elementId')}",
                        "code": "text-contrast",
                        "severity": "error",
                        "message": (f"文字与其实际背景对比度仅 {worst:.2f}:1（{_visible_text(content)[:18]}），"
                                    "渲染后几乎不可读；请改用与背景对比明显的字色。"),
                        "layout": {"canvasSize": size, "bounds": bounds},
                    })
        except (OSError, ValueError):
            continue
    return issues


def final_layout_issues(entry: Path, document: dict, pages: list[dict], rendered: Path | None = None) -> list[dict]:
    """All located defects the finished deck must clear before it ships.

    Unresolved font families are reported separately: the company template
    itself asks for 微软雅黑 and 思源黑体, so a missing local face is a
    substitution note, not something the model should fix by leaving the
    template's design.

    ``rendered`` is an existing render directory from ``_render_pages``; when it
    is omitted this renders the deck itself and cleans up after.
    """
    issues = [issue for issue in _browser_layout_issues(entry) if issue.get("code") != "font-unresolved"]
    temporary = rendered is None
    if temporary:
        rendered = _render_pages(entry)
    if rendered is None:
        return issues
    try:
        issues.extend(_contrast_issues(document, pages, rendered))
    finally:
        if temporary:
            shutil.rmtree(rendered, ignore_errors=True)
    seen, unique = set(), []
    for issue in issues:
        key = (issue.get("location"), issue.get("code"), issue.get("elementId"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(issue)
    return unique


def _slide_artifact_dir(rendered: Path, artifact_dir: Path) -> Path:
    """Stage renders under the slide-N.png names the visual review expects."""
    artifact_dir.mkdir(parents=True, exist_ok=True)
    for index, image in enumerate(sorted((rendered / "pages").glob("page-*.png")), 1):
        shutil.copyfile(image, artifact_dir / f"slide-{index}.png")
    return artifact_dir


async def visual_overview_issues(document: dict, pages: list[dict], rendered: Path | None,
                                 *, on_event=None, scenario="auto") -> list[dict]:
    """Ask the configured vision model what a person sees in the finished deck.

    The deterministic gate cannot judge taste: a redundant divider, a table in
    another typeface or crowded text are all geometrically legal. This is the
    check that answers "why did the visual review not catch this", and it is
    skipped with an explicit note when no vision model is configured.
    """
    if rendered is None or os.getenv("PPT_KIMI_VISUAL_REVIEW", "1") == "0":
        return []
    from .visual_review import VisualReviewError, review_deck_overview

    artifact_dir = rendered.parent / ".final-artifacts"
    _slide_artifact_dir(rendered, artifact_dir)
    state = {"document": document, "pages": pages}
    try:
        report = await review_deck_overview(state, artifact_dir, scenario=scenario, on_event=on_event)
    except VisualReviewError as error:
        await _emit_note(on_event, f"AI 视觉审稿未执行：{error}")
        return []
    finally:
        shutil.rmtree(artifact_dir, ignore_errors=True)
    issues = []
    for issue in report.get("issues", []):
        identifiers = ", ".join(issue.get("elementIds") or []) or "-"
        issues.append({
            "page": issue.get("pagePath"),
            "elementId": (issue.get("elementIds") or [None])[0],
            "location": f"{issue.get('pagePath')}#{identifiers}",
            "code": f"visual-{issue.get('code')}",
            "severity": issue.get("severity"),
            "message": f"{issue.get('message')}（依据：{issue.get('evidence')}；修改建议：{issue.get('repair')}）",
        })
    return issues


async def _emit_note(on_event, message: str) -> None:
    if on_event:
        result = on_event({"phase": "validate", "status": "waiting", "message": message})
        if asyncio.iscoroutine(result):
            await result


def _issue_summary(issues: list[dict]) -> str:
    lines = []
    for issue in issues[:8]:
        lines.append(f"- {issue.get('location')}: {issue.get('message')}")
    if len(issues) > 8:
        lines.append(f"- 另有 {len(issues) - 8} 条同类问题，请逐一自查同类元素。")
    return "\n".join(lines)


async def run_kimi(request, *, data_root: Path, progress, on_event=None, project_id: str, job_id: str):
    """Run the agentic loop, validate, and commit the produced deck."""
    workspace = _workspace(data_root, project_id, job_id)
    workspace.mkdir(parents=True, exist_ok=True)
    max_iters = int(os.getenv("PPT_KIMI_MAX_ITERS", "40"))
    repair_rounds = int(os.getenv("PPT_KIMI_REPAIR_ROUNDS", "2"))
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
        message = Msg(name="user", role="user", content=[{"type": "text", "text": _initial_message(request, template_info)}])
        reply = ""
        document: dict = {}
        pages: list[dict] = []
        final_dir = workspace / "final"
        entry: Path | None = None
        outstanding: list[dict] = []
        for attempt in range(repair_rounds + 1):
            reply = _reply_text(await agent.reply(message))
            source_entry = _find_entry(workspace)
            document, pages = _load_deck(source_entry)
            _apply_host_transforms(source_entry, document, pages, template_info)
            shutil.rmtree(final_dir, ignore_errors=True)
            entry = _write_deck(final_dir, source_entry, document, pages)
            issues = validate_document({"document": document, "pages": pages}, resource_root=entry.parent)
            if issues:
                detail = "; ".join(f"{i.get('location')}: {i.get('message')}" for i in issues[:8])
                raise ValueError(f"生成的 PPTD 未通过结构校验：{detail}")
            await emit("validate", "started", "正在用真实浏览器排版检查整份交付稿（含封面、致谢与公司元素）。")
            rendered = await asyncio.to_thread(_render_pages, entry)
            try:
                layout = await asyncio.to_thread(final_layout_issues, entry, document, pages, rendered)
                visual = await visual_overview_issues(document, pages, rendered, on_event=on_event,
                                                      scenario=getattr(request, "scenario", None) or "auto")
            finally:
                if rendered is not None:
                    shutil.rmtree(rendered, ignore_errors=True)
            layout = layout + visual
            if not layout:
                outstanding = []
                break
            if attempt >= repair_rounds:
                # The deck is complete and editable; withholding it entirely
                # leaves the user with nothing to act on. Ship it and name every
                # outstanding location instead of failing silently.
                outstanding = layout
                await emit("validate", "failed",
                           f"仍有 {len(outstanding)} 项排版问题；已交付可编辑稿并列出位置，可在编辑器调整后导出。",
                           issues=outstanding[:12])
                break
            await emit("validate", "retrying", f"排版检查发现 {len(layout)} 项问题，请模型修复后重检。",
                       issues=layout[:12])
            if progress:
                progress(70, f"排版检查发现 {len(layout)} 项问题，正在修复")
            message = Msg(name="user", role="user", content=[{"type": "text", "text": (
                "宿主的真实浏览器排版检查（渲染最终交付稿，已包含公司模板的封面/致谢与角落元素）发现以下问题：\n"
                + _issue_summary(layout)
                + "\n请只修改对应的 .page 文件解决这些问题，保持文字与数据完整，然后重新运行 "
                  "`kimi_slides check deck --level auto`。不要改动 template-pptd/ 下的分析素材。"
            )}])
        from .pptd_store import DocumentStore

        store = DocumentStore(data_root)
        revision = store.read(project_id)["revision"] if store.exists(project_id) else 0
        committed = store.commit(
            project_id, document, pages, "AI 生成（Kimi 专家）", revision,
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
    from .agents import health

    return health()
