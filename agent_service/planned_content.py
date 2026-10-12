"""Conservative plan completeness checks and lossless missing-body recovery.

This detects empty heading shells, not arbitrary semantic or stylistic gaps.
Recovery copies only the already-authored plan content; it cannot invent facts
or modify the existing page. Normal visual repair remains style/layout only.
"""
from __future__ import annotations

import copy
import math
import re

from pydantic import BaseModel, ConfigDict, Field


class ContentLayoutBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    segmentIds: list[int] = Field(min_length=1)
    bounds: list[float] = Field(min_length=4, max_length=4)
    fontSize: float = Field(gt=0)
    color: str = Field(min_length=1)
    bold: bool
    align: list[str] = Field(default_factory=lambda: ["left", "top"], min_length=2, max_length=2)


class ContentLayout(BaseModel):
    model_config = ConfigDict(extra="forbid")
    blocks: list[ContentLayoutBlock] = Field(min_length=1, max_length=60)


def _compact(value):
    return re.sub(r"\s+", "", value) if isinstance(value, str) else ""


def planned_content_issues(bundle, plan):
    """Locate substantial planned pages whose only content is in the heading.

    Covers, section separators and short closing intentions are intentionally
    sparse. A real body text/image/chart/table already defeats this narrow test;
    completeness beyond an empty shell belongs to a semantic review.
    """
    if not isinstance(bundle, dict) or not isinstance(plan, dict):
        return []
    document = bundle.get("document", {})
    planned_paths = plan.get("document", {}).get("pages", [])
    planned_pages = plan.get("pages", [])
    size = document.get("size", [960, 540])
    if not isinstance(size, list) or len(size) != 2 or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0 for v in size):
        return []  # Structural validation owns malformed geometry.
    _, height = size
    issues = []
    for page in bundle.get("pages", []):
        path = page.get("path")
        if path not in planned_paths:
            continue
        index = planned_paths.index(path)
        if index >= len(planned_pages) or not isinstance(planned_pages[index], dict):
            continue
        content = planned_pages[index].get("content", "")
        data = page.get("data", {})
        if not isinstance(data, dict) or data.get("pageType") in {"cover", "section", "divider", "section-divider"} or len(_compact(content)) < 80:
            continue
        elements = data.get("elements")
        if not isinstance(elements, list):
            continue
        has_body = False
        for element in elements:
            if not isinstance(element, dict):
                continue
            bounds = element.get("bounds", [])
            if not isinstance(bounds, list) or len(bounds) != 4 or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in bounds):
                continue
            _, y, width, box_height = bounds
            if width <= 0 or box_height <= 0:
                continue
            kind = element.get("elementType")
            text = _compact(element.get("content", {}).get("text", "")) if isinstance(element.get("content"), dict) else ""
            # Header labels and footer/page-number chrome are not a body. A long
            # text block is content regardless of its position on the canvas.
            in_body = y + box_height > height * .38 and y < height * .88
            if kind == "text" and (len(text) >= 80 or in_body and len(text) >= 8):
                has_body = True
            elif in_body and kind in {"chart", "table", "image"}:
                has_body = True
            if has_body:
                break
        if not has_body:
            issues.append({"code": "PlannedContentMissing", "pagePath": path, "pageIndex": index,
                           "location": path + ".elements", "severity": "error",
                           "message": "页面只保留了标题区，原始逐页规划中的正文尚未落到页面。必须仅从该页 plan.content 完整恢复正文，保留全部现有元素与页面字段，不得补写新事实或数字。"})
    return issues


_ELEMENT_KEYS = {
    "text": {"elementId", "elementType", "bounds", "content", "rotation", "opacity"},
    "shape": {"elementId", "elementType", "bounds", "shapeName", "fill", "border", "rotation", "opacity", "path", "viewBox"},
    "line": {"elementId", "elementType", "bounds", "points", "viewBox", "border", "rotation", "opacity", "startArrowType", "endArrowType"},
}
_TEXT_KEYS = {"text", "style", "fontFamily", "fontSize", "color", "bold", "italic", "underline", "lineHeight", "letterSpacing", "align", "padding", "verticalAlign"}


def apply_content_additions(original, page_plan, additions):
    """Append a lossless layout of plan.content without accepting replacements."""
    elements = original.get("elements")
    if not isinstance(elements, list) or not isinstance(additions, list) or not additions:
        raise ValueError("Content recovery requires an existing element array and nonempty appendElements")
    ids = {e.get("elementId") for e in elements if isinstance(e, dict)}
    texts = []
    for element in additions:
        if not isinstance(element, dict):
            raise ValueError("Every recovered element must be an object")
        kind = element.get("elementType")
        if kind not in _ELEMENT_KEYS or set(element) - _ELEMENT_KEYS[kind]:
            raise ValueError("Content recovery accepts only plain text and decorative shape/line elements; unknown semantic fields are forbidden")
        identity = element.get("elementId")
        if not isinstance(identity, str) or not identity or identity in ids:
            raise ValueError("Recovered elements require new unique element IDs; existing elements cannot be replaced")
        ids.add(identity)
        if kind == "text":
            content = element.get("content")
            if not isinstance(content, dict) or set(content) - _TEXT_KEYS or not isinstance(content.get("text"), str):
                raise ValueError("Recovered text requires plain content.text and supported presentation style fields")
            texts.append(content["text"])
    expected = _compact(page_plan.get("content"))
    if not expected or _compact("".join(texts)) != expected:
        raise ValueError("Recovered text in appendElements order must equal the complete original pagePlan.content exactly, allowing whitespace only; no omissions, rewrites, extra labels, facts or numbers")
    result = copy.deepcopy(original)
    result["elements"].extend(copy.deepcopy(additions))
    return result


def content_segments(content):
    """Split at source sentence boundaries without dropping any source bytes."""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Content recovery requires nonempty original pagePlan.content")
    parts = re.findall(r"[^。；;\n]*[。；;\n]+|[^。；;\n]+$", content)
    if "".join(parts) != content:
        raise ValueError("Original content could not be segmented without loss")
    return [{"id": index, "text": text} for index, text in enumerate(parts)]


def apply_content_layout(original, page_plan, answer, document):
    """Bind AI geometry to immutable source segments, then check full provenance."""
    layout = ContentLayout.model_validate(answer, strict=True)
    segments = content_segments(page_plan.get("content"))
    order = [identity for block in layout.blocks for identity in block.segmentIds]
    if order != list(range(len(segments))):
        raise ValueError("Every source segment ID must appear exactly once and in original order; do not omit, duplicate, reorder or invent IDs")
    existing_ids = {element.get("elementId") for element in original.get("elements", []) if isinstance(element, dict)}
    text_styles = document.get("theme", {}).get("textStyles", {})
    additions = []
    next_identity = 1
    for block in layout.blocks:
        if block.bounds[2] <= 0 or block.bounds[3] <= 0:
            raise ValueError("Every content block must have positive width and height")
        if block.align[0] not in {"left", "center", "right", "justify"} or block.align[1] not in {"top", "middle", "bottom"}:
            raise ValueError("align must be [horizontal, vertical] using supported PPTD alignment values")
        while (identity := "recovered-body-" + str(next_identity)) in existing_ids:
            next_identity += 1
        existing_ids.add(identity)
        next_identity += 1
        text = "".join(segments[index]["text"] for index in block.segmentIds)
        content = {"text": text, "fontSize": block.fontSize, "color": block.color, "bold": block.bold, "align": list(block.align)}
        if isinstance(text_styles, dict) and isinstance(text_styles.get("body"), dict):
            content["style"] = "$body"
        else:
            content["lineHeight"] = 1.5
        additions.append({"elementId": identity, "elementType": "text", "bounds": list(block.bounds), "content": content})
    return apply_content_additions(original, page_plan, additions)


async def recover_planned_content(model, rules, page_context, original, *, scenario="auto", session_id=None, on_event=None):
    """Ask AI to lay out known missing text; enforce its origin before returning."""
    from .pptd_generation import _ask, _emit
    from .pptd_validation import validate_document

    path = page_context["pagePath"]
    source_document = page_context["plan"]["document"]
    document = {key: copy.deepcopy(source_document[key]) for key in ("version", "title", "size", "theme", "customFonts") if key in source_document}
    document["pages"] = [path]
    contract = {
        "instruction": "Design only the layout of the immutable source segments below. Return {blocks:[{segmentIds:[0,...],bounds:[x,y,width,height],fontSize:number,color:string,bold:boolean,align:[horizontal,vertical]}]}. Each block selects one or more consecutive segments to render as a native text box. Across blocks, every segment ID MUST appear exactly once in source order. Return no text, element IDs, decorations, notes, new content or replacement page. The program inserts the original source text verbatim and preserves all existing page elements and fields. You choose the layout, grouping, typography, spacing and alignment based on the page's reader task and visual plan; there is no fixed template. Use generous boxes, account for wrapping at the selected font size and the theme body's lineHeight (1.5 if absent), keep all boxes within slide bounds, and avoid the existing title and other elements. Font family inherits the theme's body style. Do not request changing any source text or existing page element.",
        "segments": content_segments(page_context["pagePlan"]["content"]),
    }
    feedback = None
    for attempt in range(2):
        payload = {"document": copy.deepcopy(document), "pageIndex": page_context["pageIndex"], "pagePath": path,
                   "pagePlan": copy.deepcopy(page_context["pagePlan"]), "draft": copy.deepcopy(original), "contentRecovery": contract}
        if feedback:
            payload["contentRecoveryValidation"] = feedback
        answer = await _ask(model, "repair", rules, payload, scenario=scenario, session_id=session_id, on_event=on_event, response_schema=ContentLayout)
        try:
            # Also validate the envelope here so mocked/custom callers cannot
            # accidentally feed a full replacement page into the append path.
            candidate = apply_content_layout(original, page_context["pagePlan"], answer, document)
            issues = validate_document({"document": document, "pages": [{"path": path, "data": candidate}]},
                                       speaker_notes=page_context.get("speakerNotes"), animation_requested=page_context.get("animationRequested"),
                                       existing={"pages": [{"path": path, "data": original}]})
            issues += planned_content_issues({"document": document, "pages": [{"path": path, "data": candidate}]}, page_context["plan"])
            if issues:
                raise ValueError("Recovered content did not pass page validation: " + str(issues))
            return candidate
        except ValueError as exc:
            feedback = str(exc)
            if attempt == 1:
                raise ValueError("原规划正文恢复未通过完整性校验，未修改现有页面。" + feedback) from exc
            await _emit(on_event, {"phase": "validate", "status": "retrying", "pagePath": page_context["pagePath"],
                                   "pageIndex": page_context["pageIndex"], "message": "正文恢复未满足原文保护校验，AI 正在重新排版原规划内容。", "attempt": attempt + 2})
    raise AssertionError("Unreachable content recovery state")
