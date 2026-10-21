"""Measured typography fitting for PPTD text and table elements."""
from __future__ import annotations

import math
import re

FONT_SIZE = re.compile(r"font-size:\s*([0-9.]+)px")
LINE_HEIGHT = re.compile(r"line-height:\s*([0-9.]+)")
MARGIN_TOP = re.compile(r"margin-top:\s*([0-9.]+)px")


def _number(value: float, minimum: float = 0.0) -> str:
    return str(max(minimum, round(value, 1))).rstrip("0").rstrip(".")


def _scale_markup(markup: str, factor: float, min_size: float) -> str:
    if not isinstance(markup, str) or not markup:
        return markup
    text = FONT_SIZE.sub(
        lambda match: "font-size:" + _number(max(min_size, float(match.group(1)) * factor), min_size) + "px",
        markup,
    )
    text = LINE_HEIGHT.sub(
        lambda match: "line-height:" + _number(min(float(match.group(1)), 1.2), 1.0),
        text,
    )
    text = MARGIN_TOP.sub(
        lambda match: "margin-top:" + _number(max(1.0, float(match.group(1)) * 0.5), 1.0) + "px",
        text,
    )
    return text


def _scale_style(style: dict, factor: float, min_size: float) -> None:
    if not isinstance(style, dict):
        return
    size = style.get("fontSize")
    if isinstance(size, (int, float)):
        style["fontSize"] = max(min_size, round(size * factor, 1))
    if isinstance(style.get("lineHeight"), (int, float)):
        style["lineHeight"] = min(float(style["lineHeight"]), 1.2)


def scale_element(element: dict, factor: float, min_size: float = 8.5) -> bool:
    """Scale every font declaration on one element while preserving its content."""
    if not isinstance(element, dict) or factor >= 0.999:
        return False
    before = repr(element)
    kind = element.get("elementType")
    content = element.get("content")
    if kind == "text" and isinstance(content, dict):
        if isinstance(content.get("fontSize"), (int, float)):
            content["fontSize"] = max(min_size, round(float(content["fontSize"]) * factor, 1))
        if isinstance(content.get("lineHeight"), (int, float)):
            content["lineHeight"] = min(float(content["lineHeight"]), 1.2)
        content["text"] = _scale_markup(content.get("text"), factor, min_size)
    elif kind == "table":
        style = element.get("style")
        if isinstance(style, dict):
            _scale_style(style.get("cellStyle"), factor, min_size)
            _scale_style(style.get("firstRowStyle"), factor, min_size)
            _scale_style(style.get("lastRowStyle"), factor, min_size)
            for item in style.get("bodyStyles") or []:
                _scale_style(item, factor, min_size)
        for row in element.get("rows") or []:
            for cell in row or []:
                if isinstance(cell, dict):
                    cell["text"] = _scale_markup(cell.get("text"), factor, min_size)
    return repr(element) != before


def _factor(measurement: dict | None) -> float:
    if not measurement:
        return 0.9
    available = measurement.get("availableSize") or [0, 0]
    measured = measurement.get("measuredTextSize") or [0, 0]
    ratios = []
    for actual, room in zip(measured, available):
        if isinstance(actual, (int, float)) and isinstance(room, (int, float)) and room > 0:
            ratios.append(float(actual) / float(room))
    ratio = max(ratios or [1.0])
    if ratio <= 1.02:
        return 1.0
    return min(0.98, max(0.70, 0.98 / math.sqrt(ratio)))


def autofit_state(state: dict, layouts: dict, issues: list[dict], min_size: float = 8.5) -> dict[str, int]:
    """Apply one measured fitting pass and return changed element counts."""
    measurements = {
        path: {item.get("elementId"): item for item in page.get("elements") or []}
        for path, page in layouts.items()
    }
    issues_by_page: dict[str, set[str]] = {}
    issue_measurements: dict[tuple[str, str], dict] = {}
    for issue in issues:
        if issue.get("code") not in {"text-overflow", "text-overlap"}:
            continue
        page = issue.get("page") or issue.get("location", "").split("#", 1)[0]
        element_id = issue.get("elementId")
        if page and element_id:
            issues_by_page.setdefault(page, set()).add(element_id)
            if isinstance(issue.get("layout"), dict):
                issue_measurements[(page, element_id)] = issue["layout"]
    changed = {"text": 0, "table": 0}
    for page in state.get("pages") or []:
        path = page.get("path")
        ids = issues_by_page.get(path)
        if not ids:
            continue
        for element in page.get("data", {}).get("elements", []):
            element_id = element.get("elementId")
            if element_id not in ids:
                continue
            factor = _factor(
                measurements.get(path, {}).get(element_id)
                or issue_measurements.get((path, element_id))
            )
            if factor >= 0.999:
                continue
            if scale_element(element, factor, min_size):
                kind = element.get("elementType")
                if kind in changed:
                    changed[kind] += 1
    return changed
