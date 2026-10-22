"""Expert-mode browser validation and visual review gates."""
from __future__ import annotations

import asyncio
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from PIL import Image

from .execution_policy import expert_policy
from .review_service import review_deck


def _renderer_ready() -> bool:
    try:
        from pptd_engine.cli import _renderer_ready as ready
    except ImportError:  # pragma: no cover - defensive boundary
        return False
    try:
        return bool(ready())
    except Exception:  # pragma: no cover - defensive boundary
        return False


def _browser_layout_report(entry: Path) -> tuple[list[dict], dict]:
    """Run the real browser layout check and return measurements too.

    The model's own ``kimi_slides screenshot`` loop runs before the host swaps
    in the template cover/closing page, so it can never see those pages. These
    are the same located checks the deterministic pipeline enforces.
    """
    if not _renderer_ready():
        return [], {}
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
            return [], {}
        payload = json.loads(report.read_text(encoding="utf-8"))
        return (
            [issue for issue in payload.get("issues", []) if isinstance(issue, dict)],
            payload.get("layouts") if isinstance(payload.get("layouts"), dict) else {},
        )


def _browser_layout_issues(entry: Path) -> list[dict]:
    return _browser_layout_report(entry)[0]


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


def _visible_text(content: dict) -> str:
    text = re.sub(r"<[^>]+>", "", str(content.get("text", "")))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _color_of(content: dict) -> str:
    if isinstance(content.get("color"), str):
        return content["color"]
    match = re.search(r"color:\s*([^;\"]+)", str(content.get("text", "")))
    return match.group(1).strip() if match else ""


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


def _background_color(pixels, x1: int, y1: int, x2: int, y2: int, text_color):
    """Estimate the visible background without mistaking dense text for it."""
    buckets: dict[tuple, list] = {}
    step_x = max(1, (x2 - x1) // 32)
    step_y = max(1, (y2 - y1) // 32)
    total = 0
    for y in range(y1, y2, step_y):
        for x in range(x1, x2, step_x):
            pixel = pixels[x, y]
            key = (pixel[0] // 16, pixel[1] // 16, pixel[2] // 16)
            bucket = buckets.setdefault(key, [0, [0, 0, 0]])
            bucket[0] += 1
            bucket[1] = [bucket[1][index] + pixel[index] for index in range(3)]
            total += 1
    if not buckets:
        return None
    ranked = sorted(buckets.values(), key=lambda item: item[0], reverse=True)
    minimum = max(2, total // 50)
    for count, values in ranked:
        candidate = tuple(value // count for value in values)
        if count >= minimum and max(abs(candidate[i] - text_color[i]) for i in range(3)) >= 40:
            return candidate
    # If every visible colour matches the text, either the glyphs fill the
    # complete box or the text is invisible against the same-colour background.
    # Both cases must be evaluated for contrast instead of being skipped.
    count, values = ranked[0]
    return tuple(value // count for value in values)


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
                            background = _background_color(
                                pixels, bx1, by1, bx2, by2, declared
                            )
                            if background is None:
                                continue
                            ratio = _contrast_ratio(declared, background)
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


def final_layout_report(
    entry: Path,
    document: dict,
    pages: list[dict],
    rendered: Path | None = None,
) -> tuple[list[dict], dict]:
    """Return final located defects together with browser measurements.

    Unresolved font families are reported separately: the company template
    itself asks for 微软雅黑 and 思源黑体, so a missing local face is a
    substitution note, not something the model should fix by leaving the
    template's design.
    """
    browser_issues, layouts = _browser_layout_report(entry)
    issues = [
        issue for issue in browser_issues
        if issue.get("code") != "font-unresolved"
    ]
    temporary = rendered is None
    if temporary:
        rendered = _render_pages(entry)
    if rendered is None:
        return issues, layouts
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
    return unique, layouts


def final_layout_issues(
    entry: Path,
    document: dict,
    pages: list[dict],
    rendered: Path | None = None,
) -> list[dict]:
    """Return only the defects from :func:`final_layout_report`."""
    issues, _ = final_layout_report(entry, document, pages, rendered)
    return issues


def _slide_artifact_dir(rendered: Path, artifact_dir: Path) -> Path:
    """Stage renders under the slide-N.png names the visual review expects."""
    artifact_dir.mkdir(parents=True, exist_ok=True)
    for index, image in enumerate(sorted((rendered / "pages").glob("page-*.png")), 1):
        shutil.copyfile(image, artifact_dir / f"slide-{index}.png")
    return artifact_dir


async def visual_overview_issues(document: dict, pages: list[dict], rendered: Path | None,
                                 *, on_event=None, scenario="auto", cache_dir: Path | None = None) -> list[dict]:
    """Run the high-resolution deck and per-page visual review.

    Browser geometry catches measurable defects first. Once those are cleared,
    this pass inspects every rendered page at full resolution so low-contrast
    text and inconsistent components cannot hide in a contact sheet.
    """
    if rendered is None or os.getenv("PPT_KIMI_VISUAL_REVIEW", "1") == "0":
        return []
    from .visual_review import VisualReviewError
    from .review_service import review_deck

    artifact_dir = rendered.parent / ".final-artifacts"
    _slide_artifact_dir(rendered, artifact_dir)
    state = {"document": document, "pages": pages}
    try:
        report = await review_deck(
            state,
            artifact_dir,
            scenario=scenario,
            on_event=on_event,
            cache_dir=cache_dir,
            concurrency=expert_policy().visual_review_concurrency,
        )
    except VisualReviewError as error:
        await _emit_note(on_event, f"AI 视觉审稿未执行：{error}")
        raise
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
