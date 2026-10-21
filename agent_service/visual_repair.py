"""Bounded AI layout repairs with a code-enforced semantic preservation contract.

The model never replaces pages. It proposes typed styling operations on existing
elements; all text (including rich text/formulas), numbers, chart encodings,
media, notes, animations, unknown fields and page identities are immutable.
Browser/native rendering and image review remain required after these patches.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import inspect
import json
import re
from fractions import Fraction
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .agents import make_model
from .kimi_rules import KimiRules, SCENARIOS
from .pptd_generation import _ask, _emit, _pointer
from .pptd_validation import number, validate_document

VISUAL_POINTER_PATTERN = r"^/(?:[A-Za-z][A-Za-z0-9]*|[0-9]+)(?:/(?:[A-Za-z][A-Za-z0-9]*|[0-9]+))*$"


class VisualPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pagePath: str = Field(description="Exact existing .page path from document.pages, e.g. pages/two.page. This is independent of the JSON pointer path.")
    elementId: str | None = Field(default=None, description="Exact existing elementId identifying the target element; omit only for a page background patch.")
    path: str = Field(pattern=VISUAL_POINTER_PATTERN, description="Canonical JSON pointer relative to the element identified by elementId; MUST start with / and use / separators. Examples: /content/color, /content/fontSize, /bounds, /bounds/1. Never use content.color, content/color, /elements/0/content/color, the elementId, or pagePath inside this pointer. Without elementId only /background or /background/color is permitted.")
    op: Literal["set"]
    value: Any


class VisualPatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operations: list[VisualPatch] = Field(min_length=1, max_length=500)


class VisualRepairError(ValueError):
    def __init__(self, message, issues, original_error=None, rejections=None):
        super().__init__(message)
        self.issues = copy.deepcopy(issues)
        self.original_error = original_error
        self.failure_kind = "visual-repair"
        self.generation_context = {"phase": "visual"}
        self.rejections = copy.deepcopy(rejections or [])


TEXT_STYLE = {"fontSize", "fontFamily", "color", "backgroundColor", "bold", "italic", "lineHeight", "lineHeightPx", "letterSpacing", "marginTop", "align", "wrap"}
CHART_TEXT_STYLE = {"fontSize", "fontFamily", "color"}
CHART_AXIS_STYLE = CHART_TEXT_STYLE | {"rotation", "interval"}
CHART_LABEL_STYLE = CHART_TEXT_STYLE | {"position"}
LABEL_POSITIONS = {"top", "bottom", "left", "right", "inside", "insideEnd", "outsideEnd"}
BORDER_STYLE = {"color", "width", "style"}


def located_pages(state, issues):
    """Resolve only real page locations; global findings must first be localized."""
    paths = state["document"]["pages"]
    pages = {page["path"]: page["data"] for page in state["pages"]}
    targets = {}
    for issue in issues:
        path, index = issue.get("pagePath"), issue.get("pageIndex")
        if index is not None:
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(paths):
                raise ValueError("Visual finding contains an invalid pageIndex")
            if path and path != paths[index]:
                raise ValueError("Visual finding pagePath and pageIndex disagree")
            path = paths[index]
        if path not in pages:
            raise ValueError("Visual findings must identify a real page before repair")
        identities = {e["elementId"] for e in pages[path]["elements"]}
        ids = issue.get("elementIds", [])
        if not isinstance(ids, list) or any(not isinstance(i, str) or i not in identities for i in ids):
            raise ValueError("Visual finding references an unknown element ID")
        targets.setdefault(path, []).append({**issue, "pagePath": path, "pageIndex": paths.index(path)})
    if not targets:
        raise ValueError("Visual repair requires located findings")
    return targets


def _style_value(key, value):
    if key in ("fontSize", "lineHeightPx"):
        return number(value) and value >= 12
    if key == "lineHeight": return number(value) and .8 <= value <= 3
    if key == "marginTop": return number(value) and value >= 0
    if key == "letterSpacing": return number(value) and -2 <= value <= 20
    if key == "width": return number(value) and 0 <= value <= 40
    if key == "rotation": return number(value) and -360 <= value <= 360
    if key in ("color", "backgroundColor"):
        return isinstance(value, str) and bool(re.fullmatch(r"#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?|\$[\w-]+", value))
    if key == "fontFamily":
        return isinstance(value, str) and bool(value.strip()) or isinstance(value, dict) and set(value) == {"latin", "ea"} and all(isinstance(v, str) and v.strip() for v in value.values())
    if key in ("bold", "italic", "wrap"): return isinstance(value, bool)
    if key == "align":
        return isinstance(value, list) and len(value) == 2 and value[0] in ("left", "center", "right", "justify", "distributed") and value[1] in ("top", "middle", "bottom")
    if key == "style": return value in ("solid", "dash", "dot")
    if key == "position": return value in ("top", "bottom", "left", "right")
    return False


def _fill_patch(target, parts, value):
    root = parts[0]
    current = target.get(root)
    if current is not None and (not isinstance(current, dict) or current.get("type") != "solid"):
        return False  # Image/gradient source semantics must never be replaced.
    if len(parts) == 1:
        return isinstance(value, dict) and set(value) == {"type", "color"} and value["type"] == "solid" and _style_value("color", value["color"]) and (current is None or set(current) <= {"type", "color"})
    return parts == [root, "color"] and current is not None and _style_value("color", value)


def _allowed_patch(element, parts, value):
    kind = element["elementType"]
    if parts == ["bounds"]:
        return isinstance(value, list) and len(value) == 4 and all(number(n) for n in value) and all(n > 0 for n in value[2:])
    if len(parts) == 2 and parts[0] == "bounds" and parts[1] in ("0", "1", "2", "3"):
        return number(value) and (int(parts[1]) < 2 or value > 0)
    if kind == 'line' and parts == ['viewBox']:
        return isinstance(value, list) and len(value) == 2 and all(number(n) and n > 0 for n in value)
    if kind == 'line' and parts == ['points']:
        return isinstance(value, str) and bool(value.strip())
    if parts == ["rotation"] and kind in ("text", "shape", "line", "image", "icon"):
        return _style_value("rotation", value)
    if kind == "text" and len(parts) == 2 and parts[0] == "content" and parts[1] in TEXT_STYLE:
        return _style_value(parts[1], value)
    if kind in ("shape", "icon", "table", "chart") and parts[0] == "fill":
        return _fill_patch(element, parts, value)
    if kind in ("shape", "line", "icon", "table", "chart", "image") and parts[0] == "border":
        if element.get("border") is not None and not isinstance(element["border"], dict):
            return False
        if parts == ["border"]:
            return isinstance(value, dict) and bool(value) and set(value) <= BORDER_STYLE and all(_style_value(key, child) for key, child in value.items())
        if len(parts) == 2 and parts[1] in BORDER_STYLE:
            return _style_value(parts[1], value)
    if kind == "table":
        if parts in (["columnWidths"], ["rowHeights"]):
            old = element[parts[0]]
            return isinstance(value, list) and len(value) == len(old) and all(number(n) and 0 < n <= 1 for n in value) and abs(sum(value) - 1) < 1e-6
        if len(parts) == 4 and parts[0] == "rows" and parts[1].isdigit() and parts[2].isdigit() and parts[3] in TEXT_STYLE - {"wrap"}:
            try: cell = element["rows"][int(parts[1])][int(parts[2])]
            except (IndexError, TypeError): return False
            return isinstance(cell, dict) and _style_value(parts[3], value)
    if kind == "chart":
        if parts == ["fontFamily"]: return _style_value("fontFamily", value)
        if len(parts) == 2 and parts[0] == 'dataLabels' and parts[1] == 'position':
            return (element.get('dataLabels') is None or isinstance(element['dataLabels'], dict)) and isinstance(value, str) and value in LABEL_POSITIONS
        if len(parts) == 2 and parts[0] in ("title", "legend", "dataLabels") and parts[1] in CHART_TEXT_STYLE | ({"position"} if parts[0] == "legend" else set()):
            return (element.get(parts[0]) is None or isinstance(element[parts[0]], dict)) and _style_value(parts[1], value)
        if len(parts) in (3, 4) and parts[0] in ("xAxis", "yAxis", "spokeAxis") and parts[-2] == "label" and parts[-1] in CHART_AXIS_STYLE:
            if len(parts) == 4:
                axes = element.get(parts[0])
                if not isinstance(axes, list) or not parts[1].isdigit() or int(parts[1]) >= len(axes): return False
            elif element.get(parts[0]) is not None and not isinstance(element[parts[0]], dict): return False
            if parts[-1] == 'rotation': return parts[0] != 'spokeAxis' and number(value) and -90 <= value <= 90
            if parts[-1] == 'interval': return parts[0] != 'spokeAxis' and type(value) is int and value == 0
            return _style_value(parts[-1], value)
    return False


def semantic_payload(state, *, legacy=False, contract_version=None):
    """Keep a deep copy of every field except the precisely permitted visuals."""
    version = contract_version if contract_version is not None else (1 if legacy else 3)
    value = copy.deepcopy({"document": state["document"], "pages": state["pages"]})
    def strip_styles(obj, keys):
        if isinstance(obj, dict):
            for key in keys: obj.pop(key, None)
    def strip_fill(obj, key):
        fill = obj.get(key)
        if isinstance(fill, dict) and fill.get("type") == "solid" and set(fill) <= {"type", "color"}: obj.pop(key)
        elif isinstance(fill, dict) and fill.get("type") == "solid": fill.pop("color", None)
    for item in value["pages"]:
        page = item["data"]; strip_fill(page, "background")
        for element in page["elements"]:
            element.pop("bounds", None)
            kind = element["elementType"]
            if kind in ("text", "shape", "line", "image", "icon"): element.pop("rotation", None)
            if kind == "text": strip_styles(element["content"], TEXT_STYLE - ({'letterSpacing'} if version < 3 else set()))
            if kind in ("shape", "icon", "table", "chart"): strip_fill(element, "fill")
            if kind in ("shape", "line", "icon", "table", "chart", "image"):
                strip_styles(element.get("border"), BORDER_STYLE)
                if element.get("border") == {}: element.pop("border")
            if kind == "table":
                element.pop("columnWidths", None); element.pop("rowHeights", None)
                for row in element["rows"]:
                    for cell in row: strip_styles(cell, TEXT_STYLE - {"wrap"} - ({'letterSpacing'} if version < 3 else set()))
            if kind == "chart":
                element.pop("fontFamily", None)
                for field in ("title", "legend", "dataLabels"):
                    strip_styles(element.get(field), CHART_TEXT_STYLE | ({"position"} if field == 'legend' or field == 'dataLabels' and version >= 2 else set()))
                    if element.get(field) == {}: element.pop(field)
                for field in ("xAxis", "yAxis", "spokeAxis"):
                    axes = element.get(field)
                    for axis in axes if isinstance(axes, list) else [axes]:
                        if isinstance(axis, dict):
                            strip_styles(axis.get("label"), CHART_AXIS_STYLE if field != 'spokeAxis' and version >= 2 else CHART_TEXT_STYLE)
                            if axis.get("label") == {}: axis.pop("label")
                    if axes == {}: element.pop(field)
    return value


def apply_visual_patches(state, operations, issues, *, layout_measurements=None, _defer_geometry=False):
    targets = located_pages(state, issues)
    parsed = VisualPatchResult.model_validate({"operations": operations}, strict=True)
    result = copy.deepcopy({"document": state["document"], "pages": state["pages"]})
    pages = {p["path"]: p["data"] for p in result["pages"]}
    changed_pages = set()
    for patch in parsed.operations:
        if patch.pagePath not in targets:
            raise ValueError("Visual patch attempted to modify a page outside located findings")
        page = pages[patch.pagePath]
        # Canonical literal pointers only. Encoded slash/tilde keys are outside
        # the visual schema and may not be used to reach arbitrary fields.
        if not re.fullmatch(VISUAL_POINTER_PATTERN, patch.path):
            raise ValueError("Visual patch uses an invalid or noncanonical JSON pointer")
        parts = patch.path[1:].split("/")
        if patch.elementId is None:
            target = page
            allowed = parts[0] == "background" and _fill_patch(page, parts, patch.value)
        else:
            matches = [e for e in page["elements"] if e["elementId"] == patch.elementId]
            if len(matches) != 1: raise ValueError("Visual patch references an unknown or ambiguous element ID")
            target = matches[0]
            allowed = _allowed_patch(target, parts, patch.value)
        if not allowed:
            raise ValueError(f"Visual repair may not modify protected field or invalid style: {patch.path}")
        before = copy.deepcopy(page)
        if parts == ["border"] and patch.elementId is not None:
            # Compound border syntax is equivalent to validated leaf patches.
            # Never replace the object: future/unknown border fields are locked.
            for key, value in patch.value.items():
                _pointer(target, "/border/" + key, op="set", value=value)
        else:
            _pointer(target, patch.path, op="set", value=patch.value)
        if page != before: changed_pages.add(patch.pagePath)
    if not changed_pages:
        raise ValueError("Visual repair did not change any page with findings")
    # A model may rescale a line's local coordinate system alongside bounds.
    # Accept only mathematically identical paths, and retain the original bytes
    # so semantic checkpoint hashes and every curve/control point stay stable.
    def line_shape(element):
        w, h = (Fraction(str(v)) for v in element['viewBox'])
        return [(Fraction(x) / w, Fraction(y) / h)
                for x, y in (pair.split(',') for pair in element['points'].split())]
    for before_page, after_page in zip(state['pages'], result['pages']):
        for before, after in zip(before_page['data']['elements'], after_page['data']['elements']):
            if before['elementType'] == 'line' and any(before.get(k) != after.get(k) for k in ('points', 'viewBox')):
                if line_shape(before) != line_shape(after):
                    raise ValueError('Visual repair may rescale line units but not change its normalized path')
                for key in ('points', 'viewBox'):
                    after[key] = copy.deepcopy(before[key])
    if semantic_payload(state) != semantic_payload(result):
        raise ValueError("Visual repair changed protected presentation meaning")
    invalid = validate_document(result, existing=state)
    if invalid: raise ValueError("Visual patch failed PPTD validation: " + json.dumps(invalid, ensure_ascii=False))
    if _defer_geometry:
        return result  # Private preparation only; no candidate may be accepted here.
    width, height = result['document']['size']
    invalid_geometry = []
    originals = {p['path']: {e['elementId']: e for e in p['data']['elements']} for p in state['pages']}
    for path in targets:
        measurements = {m['elementId']: m for m in (layout_measurements or {}).get(path, {}).get('elements', [])}
        for element in pages[path]['elements']:
            if element['elementType'] not in ('text', 'table') or element.get('rotation', 0) % 360:
                continue  # Rotated text is checked by the browser renderer.
            x, y, w, h = element['bounds']
            if x < -1 or y < -1 or x + w > width + 1 or y + h > height + 1:
                invalid_geometry.append(f"{path}#{element['elementId']}: bounds {element['bounds']} exceed canvas {width}×{height}")
            measurement = measurements.get(element['elementId'])
            original = originals[path][element['elementId']]
            if (measurement and element['elementType'] == 'text' and w == original['bounds'][2]
                    and element['content'] == original['content']):
                minimum = measurement['measuredTextSize'][1] + measurement.get('paddingSize', [0, 0])[1]
                if h + 2 < minimum:
                    invalid_geometry.append(f"{path}#{element['elementId']}: unchanged text/width requires height >= {minimum}px, proposed {h}px")
    if invalid_geometry:
        raise ValueError('Invalid measured layout: ' + '; '.join(invalid_geometry))
    return result


def _geometry_operations(before, after, paths):
    originals = {p['path']: {e['elementId']: e['bounds'] for e in p['data']['elements']} for p in before['pages']}
    return [{'pagePath': p['path'], 'elementId': e['elementId'], 'path': '/bounds', 'op': 'set', 'value': e['bounds']}
            for p in after['pages'] if p['path'] in paths for e in p['data']['elements']
            if e['bounds'] != originals[p['path']][e['elementId']]]


async def repair_deck(request, state, issues, *, on_event=None, rendered_pages=None, layout_measurements=None, validate_candidate=None, on_audit=None):
    """Return an uncommitted candidate. The caller must render and review it."""
    targets = located_pages(state, issues)
    scenario = getattr(request, "scenario", "auto")
    if scenario not in SCENARIOS: scenario = (state.get("generationMeta") or {}).get("scenario", "management-report")
    if scenario not in SCENARIOS: scenario = "management-report"
    rules = KimiRules()
    model = make_model(output_mode=getattr(request, "outputMode", "auto"), observable_retries=True)
    async def repair_event(event):
        event = {**event, "phase": "visual", "scope": "repair"}
        # Public visual attempt belongs to the outer review round. Inner model
        # retries must not reset coverage or pretend another review has started.
        if "attempt" in event:
            event["modelAttempt"] = event.pop("attempt")
        await _emit(on_event, event)
    target_pages = [p for p in state["pages"] if p["path"] in targets]
    def screenshots(images):
        return [{'pagePath': path, 'name': path, 'imageData': 'data:image/png;base64,' + base64.b64encode(raw).decode('ascii')}
                for path, raw in (images or {}).items() if path in targets]
    example_page = target_pages[0]
    example_element = next((e for e in example_page["data"]["elements"] if e["elementType"] == "text"), None)
    example_operation = ({"pagePath": example_page["path"], "elementId": example_element["elementId"],
                          "path": "/content/color", "op": "set", "value": "#25334A"}
                         if example_element else {"pagePath": example_page["path"], "path": "/background",
                                                  "op": "set", "value": {"type": "solid", "color": "#FFFFFF"}})
    context = {
        "request": getattr(request, "prompt", ""), "designSystem": getattr(request, "designSystem", ""),
        "usage": getattr(request, "usage", "reading"), "document": state["document"],
        "pages": target_pages,
        "renderedPages": screenshots(rendered_pages),
        "layoutMeasurements": {p: m for p, m in (layout_measurements or {}).items() if p in targets},
        "issues": [i for group in targets.values() for i in group],
        "layoutContract": {
            "canvasSize": state['document']['size'],
            "bounds": "All bounds are [x, y, width, height] in slide coordinates, NOT left/top/right/bottom. Keep every text/table box inside the canvas. Reflow the whole affected layout, including neighboring labels, rules and footers, when necessary.",
            "measurement": "Renderer issue.layout contains actual browser measurements. At unchanged width and typography, height must accommodate measuredTextSize plus padding. Changing width changes wrapping; reserve enough room and avoid neighboring text. Keep full text and a readable font size. Do not push another element off-slide to clear an overlap. Repairs will be rendered again before acceptance.",
            "completion": "A constraint solver will complete your geometry using measured minimum text heights and preserve your intended reading order. Choose a feasible overall composition first: use multiple columns or a grid when a vertical list cannot fit. Do not only move the last item upward into earlier items. Keep headings with their bodies, and footers below the entire content region. Prefer keeping typography and widths to reuse reliable measurements. The solver cannot fit more text than the available area.",
        },
        "immutable": "All document fields, element IDs/types/order, text including formulas, numbers, chart data/encodings, images, unknown fields, notes and animations are locked.",
        "patchContract": {
            "path": "Each path MUST start with / and is relative to the single element selected by elementId. Use /content/color to change a text element's color, /bounds for all four geometry numbers, or /bounds/1 for its y coordinate. pagePath and elementId are separate fields; never insert either, elements, or an element array index into path. Dot notation and missing leading / are invalid.",
            "pagePatch": "Omit elementId only to set /background or /background/color on the page.",
            "exampleIsSyntaxOnly": "This example demonstrates the exact wire format. Apply only the changes justified by the actual findings; use the supplied real pagePath and elementId.",
            "example": {"operations": [example_operation]},
        },
        "allowedPaths": ["/bounds", "/bounds/0", "/bounds/1", "/bounds/2", "/bounds/3", "/rotation",
                         *["/content/" + field for field in sorted(TEXT_STYLE)], "/fill", "/fill/color",
                         "/border", "/border/color", "/border/width", "/border/style", "/viewBox", "/points", "/columnWidths", "/rowHeights",
                         "/rows/{rowIndex}/{cellIndex}/fontSize", "/rows/{rowIndex}/{cellIndex}/color",
                         "/rows/{rowIndex}/{cellIndex}/align", "/fontFamily", "/title/fontSize", "/title/color",
                         "/legend/fontSize", "/legend/color", "/legend/position", "/dataLabels/fontSize",
                         "/dataLabels/color", "/dataLabels/position", "/xAxis/label/rotation", "/xAxis/label/interval",
                         "/yAxis/label/rotation", "/yAxis/label/interval", "/xAxis/label/fontSize", "/yAxis/label/fontSize", "/spokeAxis/label/fontSize",
                         "/background", "/background/color"],
        "allowedConstraints": "Set only. Paths must apply to the specified element type. Every bounds width and height must be positive, including lines (use height 1 for a thin line). /viewBox and /points are allowed only on lines and only when all point coordinates normalized by viewBox remain exactly unchanged; changing bounds alone is sufficient to scale a line. Replace {rowIndex}/{cellIndex} with existing zero-based numeric indices. Table rowHeights/columnWidths keep their original array lengths. Table cell text styling may use the same supported text style field names, never text. Chart title/legend/dataLabels/axis label text style may use fontSize/fontFamily/color; axis arrays add their numeric index after /xAxis or /yAxis. /border accepts a nonempty object containing only valid style, width and/or color fields on supported non-text elements; it merges those leaves and preserves every existing unknown border field. Border null, empty objects, unknown fields and deletion are forbidden. Decorative borders may use width 0. /fill may only be a new or existing solid fill {type,color}; /fill/color requires an existing solid fill; null is forbidden. Page backgrounds may only change solid colors. No add/remove elements or semantic fields. No fontSize below 12. Never replace text to change wrapping; adjust geometry instead.",
    }
    context['chartLayoutContract'] = 'Axis label rotation is -90..90 degrees; interval may only be 0 (show every category). Never hide labels. dataLabels.position supports top/bottom/left/right/inside/insideEnd/outsideEnd; use outsideEnd for bars with a dark readable color on a light background, or inside with a contrasting light color. All settings reach both browser and editable PPTX export. Never invent fields such as offset/distance or rewrite labels. Existing small fonts may be retained by omitting fontSize; newly set sizes must be >=12. Missing content is handled separately; only fix existing elements here.'
    last = None
    rejections = []
    async def audit(status, attempt, operations, error=None):
        record = {'kind': 'style', 'status': status, 'attempt': attempt, 'operations': copy.deepcopy(operations)}
        if error is not None:
            from .job_execution import redact
            record['reason'] = redact(str(error))[:4000]
            rejections.append(record)
        await _emit(on_audit, record)
    try:
        for attempt in range(1, 3):
            await repair_event({"phase": "visual", "status": "started" if attempt == 1 else "retrying", "attempt": attempt,
                                   "message": f"AI 正在修复 {len(targets)} 页的视觉问题（第 {attempt} 次安全校验尝试），保留全部内容与数据。"})
            answer = await _ask(model, "visual-repair", rules, context, scenario=scenario,
                                session_id=request.project.id, on_event=repair_event, response_schema=VisualPatchResult)
            submitted_ops = answer['operations']
            try:
                element_types = {p['path']: {e['elementId']: e['elementType'] for e in p['data']['elements']} for p in state['pages']}
                geometry_targets = {op['pagePath'] for op in answer['operations']
                                    if element_types.get(op['pagePath'], {}).get(op.get('elementId')) in ('text', 'table')
                                    and (op['path'].startswith('/bounds') or op['path'].startswith('/content/'))
                                    and op['path'] not in ('/content/color', '/content/backgroundColor')}
                use_geometry = bool(layout_measurements and validate_candidate and geometry_targets)
                candidate = apply_visual_patches(state, answer["operations"], issues,
                    layout_measurements=layout_measurements, _defer_geometry=use_geometry)
                if use_geometry:
                    from .visual_geometry import complete_geometry
                    proposed = candidate
                    candidate = complete_geometry(proposed, state, layout_measurements, geometry_targets)
                    # Re-enter the exact same semantic/style/geometry guard.
                    submitted_ops = answer['operations'] + _geometry_operations(proposed, candidate, targets)
                    candidate = apply_visual_patches(state, submitted_ops, issues, layout_measurements=layout_measurements)
            except (ValueError, KeyError, TypeError) as exc:
                last = exc
                await audit('rejected', attempt, submitted_ops, exc)
                context["rejectedPatchError"] = str(exc)
                context['rejectedCandidate'] = {
                    'operations': submitted_ops, 'validationError': str(exc),
                    'instruction': 'This patch was rejected and NOT applied. Correct the listed invalid operations, retain its other valid layout adjustments, and return the COMPLETE replacement operations against the original pages. Do not start an unrelated layout or return only the incremental correction. Protected text/data changes must be omitted.',
                }
                continue
            if validate_candidate:
                from .pptd_exports import ValidationError
                try:
                    await validate_candidate(candidate)
                except ValidationError as exc:
                    if use_geometry and exc.layouts:
                        from .pptd_exports import layout_only_issues
                        if layout_only_issues(exc.issues):
                            try:
                                measured_candidate = candidate
                                candidate = complete_geometry(candidate, measured_candidate, exc.layouts, geometry_targets)
                                next_ops = submitted_ops + _geometry_operations(measured_candidate, candidate, targets)
                                candidate = apply_visual_patches(state, next_ops, issues)
                                await validate_candidate(candidate)
                            except ValidationError as measured_error:
                                submitted_ops = next_ops
                                exc = measured_error
                            except (ValueError, KeyError, TypeError):
                                pass  # The original failed evidence remains available to the AI.
                            else:
                                await audit('accepted', attempt, next_ops)
                                await repair_event({'status': 'completed', 'message': '修复稿已按浏览器实测尺寸完成排版校正，并通过重新导出检查，正在进入 AI 视觉复审。'})
                                return candidate
                    last = exc
                    await audit('rejected', attempt, submitted_ops, exc)
                    # A rejected layout never becomes the new baseline. The AI
                    # sees the original, the rejected patch and actual pixels /
                    # measurements, then proposes a complete replacement patch.
                    context['rejectedCandidate'] = {
                        'operations': submitted_ops, 'issues': exc.issues,
                        'layoutMeasurements': exc.layouts,
                        'renderedPages': screenshots(exc.previews),
                        'instruction': 'This patch FAILED actual rendering and was NOT applied. Return a replacement patch against the original pages. Reflow the affected content region if local moves cannot fit all text. Fix every measured error; never repeat a rejected arrangement.',
                    }
                    await repair_event({'status': 'retrying', 'message': f'修复候选稿实测仍有 {len(exc.issues)} 项问题，已拒绝应用，AI 正在依据截图与尺寸重新调整。'})
                    continue
            await repair_event({"phase": "visual", "status": "completed", "attempt": attempt,
                                   "message": "视觉修复已通过内容与数据保护检查，正在重新渲染并由 AI 复审。"})
            await audit('accepted', attempt, submitted_ops)
            return candidate
        raise VisualRepairError("AI 视觉修复连续未通过安全校验，原稿与审稿记录已保留。", issues, last, rejections) from last
    except asyncio.CancelledError:
        raise
    finally:
        client = getattr(model, "client", None)
        close = getattr(client, "close", None)
        if close:
            outcome = close()
            if inspect.isawaitable(outcome): await outcome
