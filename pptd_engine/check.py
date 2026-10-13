"""kimi-slides `check` parity: page selection, structural validation, issue mapping.

The authoritative schema check lives in ``agent_service.pptd_validation`` and the
real layout metrics live in the Chromium renderer. This module exposes a
command-line compatible surface: 14 base issue names plus chart/animation codes,
``--level auto`` deterministic repairs, and a shared page-range parser.

Text-related checks are intentionally heuristic (mock character metrics), exactly
as documented in the original ``reference/cli.md``; the model is expected to use
the actual design as the final source of truth.
"""
from __future__ import annotations

import math
import re
from pathlib import Path

import yaml

from pptd_engine.document import load_yaml, ProbeDumper, Yaml12SafeLoader


# Internal validation code -> (CLI issue name, severity). Severity follows the
# original output format: deterministic structure is Error, heuristic layout is
# Warning. Unknown codes are surfaced verbatim with a stable severity.
_CODE_META = {
    "YamlParseError": ("YamlParseError", "error"),
    "FileReadError": ("FileReadError", "error"),
    "MissingField": ("MissingField", "error"),
    "InvalidType": ("InvalidType", "error"),
    "OutOfRange": ("OutOfRange", "error"),
    "InvalidTheme": ("InvalidTheme", "error"),
    "PageNotFound": ("PageNotFound", "error"),
    "UnknownField": ("UnknownField", "error"),
    "BoundsOutside": ("BoundsOutside", "warning"),
    "SrcNotFound": ("SrcNotFound", "error"),
    "TextOverflow": ("TextOverflow", "warning"),
    "TextUnderFill": ("TextUnderFill", "warning"),
    "TextOcclusion": ("TextOcclusion", "warning"),
    "TextDrift": ("TextDrift", "warning"),
    # Structural codes produced by the local validator beyond the base 14.
    "InvalidSize": ("InvalidSize", "error"),
    "InvalidVersion": ("InvalidVersion", "error"),
    "InvalidColor": ("InvalidColor", "error"),
    "UnsafePath": ("UnsafePath", "error"),
    "DuplicatePage": ("DuplicatePage", "error"),
    "UnreferencedPage": ("UnreferencedPage", "error"),
    "DuplicateId": ("DuplicateId", "error"),
    "InvalidBounds": ("InvalidBounds", "error"),
    "InvalidShape": ("InvalidShape", "error"),
    "InvalidLine": ("InvalidLine", "error"),
    "InvalidIcon": ("InvalidIcon", "error"),
    "InvalidChart": ("InvalidChart", "error"),
    "InvalidAnimation": ("InvalidAnimation", "error"),
    "UnrequestedNotes": ("UnrequestedNotes", "error"),
    "UnrequestedAnimations": ("UnrequestedAnimations", "error"),
}

# Fields whose values are numeric in PPTD. Used by ``--level auto`` coercion.
NUMERIC_FIELDS = {
    "fontSize", "lineHeight", "lineHeightPx", "letterSpacing", "marginTop",
    "width", "height", "opacity", "rotation", "angle", "position", "durationMs",
    "delayMs", "repeat", "colSpan", "rowSpan", "amount", "scale",
}
BOOL_FIELDS = {"bold", "italic", "wrap"}
STRING_FIELDS = {"color", "backgroundColor", "lineColor", "areaColor", "src",
                 "shapeName", "iconName", "fontFamily", "text", "path", "points",
                 "elementId", "elementType", "style", "textStyle", "curve"}

# Whole-element structural findings that justify deleting the element in
# ``--level auto``. Heuristic layout findings never delete content.
ELEMENT_FATAL_CODES = {
    "MissingField", "InvalidType", "InvalidBounds", "InvalidShape",
    "InvalidLine", "InvalidIcon", "InvalidChart", "DuplicateId",
}


def parse_page_spec(spec: str | None, count: int) -> list[int]:
    """Return 1-based selected page numbers in ascending order.

    Accepts ``None`` (all), ``3``, ``1,2``, ``2-10`` and mixtures such as
    ``1,3-5``. Out-of-range values raise ``ValueError``.
    """
    if count <= 0:
        raise ValueError("page count must be positive")
    if spec is None or spec.strip() == "" or spec.strip().lower() == "all":
        return list(range(1, count + 1))
    selected: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"invalid page spec: {spec!r}")
        if "-" in part:
            lo_s, hi_s = part.split("-", 1)
            lo, hi = int(lo_s), int(hi_s)
            if lo > hi:
                raise ValueError(f"invalid page range: {part!r}")
            selected.update(range(lo, hi + 1))
        else:
            selected.add(int(part))
    if any(p < 1 or p > count for p in selected):
        raise ValueError(f"page out of range 1..{count}: {spec!r}")
    return sorted(selected)


def _find_entry(directory: Path) -> Path:
    entries = sorted(directory.glob("*.pptd"))
    if not entries:
        raise FileNotFoundError(f"no .pptd entry in {directory}")
    if len(entries) > 1:
        raise ValueError(f"multiple .pptd entries in {directory}: {[e.name for e in entries]}")
    return entries[0]


def load_deck(directory: Path):
    """Load a PPTD project directory into the validator's bundle shape.

    Returns ``(entry_path, bundle)`` where ``bundle`` is
    ``{"document": ..., "pages": [{"path":..., "data":...}]}``. YAML and file
    access failures are captured as synthetic issues rather than raised, so the
    checker can report ``YamlParseError`` / ``FileReadError`` like the CLI.
    """
    directory = Path(directory).resolve()
    issues: list[dict] = []
    entry = None
    try:
        entry = _find_entry(directory)
    except (FileNotFoundError, ValueError) as exc:
        issues.append({"code": "FileReadError", "location": str(directory), "message": str(exc), "severity": "error"})
        return None, {"document": None, "pages": []}, issues
    try:
        document = load_yaml(entry)
    except (yaml.YAMLError, ValueError, OSError) as exc:
        issues.append({"code": "YamlParseError", "location": str(entry), "message": str(exc), "severity": "error"})
        return entry, {"document": None, "pages": []}, issues
    pages: list[dict] = []
    refs = document.get("pages") if isinstance(document, dict) else None
    if isinstance(refs, list):
        for ref in refs:
            if not isinstance(ref, str):
                pages.append({"path": ref, "data": None})
                continue
            page_path = (directory / ref).resolve()
            if not page_path.is_file():
                issues.append({"code": "PageNotFound", "location": ref, "message": f"referenced page does not exist: {ref}", "severity": "error"})
                pages.append({"path": ref, "data": None})
                continue
            try:
                data = load_yaml(page_path)
            except (yaml.YAMLError, ValueError, OSError) as exc:
                issues.append({"code": "YamlParseError", "location": ref, "message": str(exc), "severity": "error"})
                data = None
            pages.append({"path": ref, "data": data})
    return entry, {"document": document, "pages": pages}, issues


def _coerce(value):
    """Return a scalar-coerced copy of ``value`` when unambiguous, else the input."""
    if isinstance(value, str):
        stripped = value.strip()
        if re.fullmatch(r"-?\d+", stripped):
            return int(stripped)
        if re.fullmatch(r"-?(?:\d+\.\d*|\.\d+)(?:[eE][-+]?\d+)?", stripped):
            return float(stripped)
        if stripped in ("true", "True", "TRUE"):
            return True
        if stripped in ("false", "False", "FALSE"):
            return False
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return value


def _coerce_scalars(value):
    if isinstance(value, list):
        return [_coerce_scalars(item) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if key in NUMERIC_FIELDS:
                coerced = _coerce(child)
                if isinstance(coerced, bool) or not isinstance(coerced, (int, float)):
                    coerced = child
                result[key] = coerced
            elif key in BOOL_FIELDS and isinstance(child, str) and child.strip() in ("true", "false", "True", "False", "TRUE", "FALSE"):
                result[key] = child.strip().lower() in ("true",)
            elif key in STRING_FIELDS and isinstance(child, (int, float, bool)):
                result[key] = str(child).lower() if isinstance(child, bool) else str(child)
            else:
                result[key] = _coerce_scalars(child)
        return result
    return value


def _serialize_yaml(value) -> str:
    json_roundtrip = __import__("json")
    json_roundtrip.dumps(value, allow_nan=False)
    return yaml.dump(value, Dumper=ProbeDumper, allow_unicode=True, sort_keys=False, width=1000)


def _group_element_indices(issues: list[dict]) -> dict[str, set[int]]:
    grouped: dict[str, set[int]] = {}
    for issue in issues:
        location = issue.get("location", "")
        match = re.fullmatch(r"(.+?)\.elements\[(\d+)\]", location)
        if match:
            grouped.setdefault(match.group(1), set()).add(int(match.group(2)))
    return grouped


def _auto_repair(entry: Path, bundle: dict, issues: list[dict]) -> set[tuple[str, int]]:
    """Apply deterministic repairs and return the ``(location,index)`` repaired.

    Only two safe transforms are performed: scalar type coercion and deletion of
    elements that fail their own structural contract. Every other issue is left
    untouched and reported as ``fixed: false``.
    """
    repaired: set[tuple[str, int]] = set()
    document = bundle["document"]
    if not isinstance(document, dict):
        return repaired
    pages = {page["path"]: page["data"] for page in bundle["pages"] if isinstance(page, dict) and isinstance(page.get("path"), str)}
    coerced_document = _coerce_scalars(document)
    coerced_pages = {path: _coerce_scalars(data) for path, data in pages.items() if isinstance(data, dict)}
    # Only whole-element structural findings (exact ``elements[i]`` location and
    # a fatal code) justify deleting an element. Scalar issues are coerced and
    # heuristic layout findings never mutate content.
    fatal_indices = _group_element_indices([issue for issue in issues if issue.get("code") in ELEMENT_FATAL_CODES])
    for path, indices in fatal_indices.items():
        data = coerced_pages.get(path)
        if not isinstance(data, dict) or not isinstance(data.get("elements"), list):
            continue
        removed = sorted(indices, reverse=True)
        kept = list(data["elements"])
        for index in removed:
            if index < len(kept):
                del kept[index]
                repaired.add((path, index))
        if len(kept) != len(data["elements"]):
            data["elements"] = kept
    directory = entry.parent
    entry.write_text(_serialize_yaml(coerced_document), encoding="utf-8")
    for path, data in coerced_pages.items():
        if isinstance(data, dict):
            (directory / path).write_text(_serialize_yaml(data), encoding="utf-8")
    return repaired


def _heuristic_layout_issues(bundle: dict) -> list[dict]:
    """Heuristic layout findings mirroring the four text checks of the CLI."""
    document = bundle.get("document")
    if not isinstance(document, dict):
        return []
    size = document.get("size")
    if not (isinstance(size, list) and len(size) == 2 and all(isinstance(v, (int, float)) for v in size)):
        return []
    page_w, page_h = size
    issues: list[dict] = []
    for page in bundle.get("pages", []):
        path = page.get("path") if isinstance(page, dict) else None
        data = page.get("data") if isinstance(page, dict) else None
        if not isinstance(path, str) or not isinstance(data, dict):
            continue
        elements = data.get("elements")
        if not isinstance(elements, list):
            continue
        for index, element in enumerate(elements):
            if not isinstance(element, dict):
                continue
            eid = element.get("elementId", "?")
            bounds = element.get("bounds")
            if not (isinstance(bounds, list) and len(bounds) == 4 and all(isinstance(v, (int, float)) for v in bounds)):
                continue
            x, y, w, h = bounds
            if x < -0.5 or y < -0.5 or x + w > page_w + 0.5 or y + h > page_h + 0.5:
                issues.append({"code": "BoundsOutside", "location": f"{path}.elements[{index}]", "elementId": eid, "message": f"Element bounds exceed the {page_w}x{page_h} page", "severity": "warning"})
            if element.get("elementType") == "text":
                content = element.get("content") or {}
                text = content.get("text", "")
                font_size = content.get("fontSize") or element.get("fontSize") or 16
                line_height = content.get("lineHeight") or 1.25
                if isinstance(line_height, str):
                    line_height = 1.25
                chars_per_line = max(1, math.floor(w / (float(font_size) * 0.9)))
                lines = 0
                for paragraph in str(text).split("\n"):
                    lines += max(1, math.ceil(len(paragraph) / chars_per_line))
                rendered_height = lines * float(font_size) * float(line_height)
                if rendered_height > h + 1:
                    issues.append({"code": "TextOverflow", "location": f"{path}.elements[{index}]", "elementId": eid, "message": f"Text may overflow its {h:g}pt box (~{rendered_height:g}pt)", "severity": "warning"})
                elif rendered_height < h * 0.5:
                    issues.append({"code": "TextUnderFill", "location": f"{path}.elements[{index}]", "elementId": eid, "message": "Text may occupy less than 50% of the box height", "severity": "warning"})
        for index, element in enumerate(elements):
            if not isinstance(element, dict) or element.get("elementType") != "text":
                continue
            bounds = element.get("bounds")
            if not (isinstance(bounds, list) and len(bounds) == 4):
                continue
            ax1, ay1, ax2, ay2 = bounds[0], bounds[1], bounds[0] + bounds[2], bounds[1] + bounds[3]
            area = max(0.0, (ax2 - ax1) * (ay2 - ay1))
            for other in elements[index + 1:]:
                if not isinstance(other, dict) or other.get("elementType") == "text":
                    continue
                ob = other.get("bounds")
                if not (isinstance(ob, list) and len(ob) == 4):
                    continue
                ox1, oy1, ox2, oy2 = ob[0], ob[1], ob[0] + ob[2], ob[1] + ob[3]
                inter_w = max(0.0, min(ax2, ox2) - max(ax1, ox1))
                inter_h = max(0.0, min(ay2, oy2) - max(ay1, oy1))
                if inter_w * inter_h > area * 0.3:
                    issues.append({"code": "TextOcclusion", "location": f"{path}.elements[{index}]", "elementId": element.get("elementId", "?"), "message": "Text may be occluded by a later element", "severity": "warning"})
                    break
    return issues


def _issue_meta(code: str):
    return _CODE_META.get(code, (code, "error"))


def check_deck(directory: Path, page_spec: str | None = None, severity: str = "all", level: str = "keep"):
    """Run ``check`` against a PPTD project directory and return a printable report.

    ``severity`` selects ``all``, ``error``, ``warning``, or a comma list of
    issue names. ``level`` is ``keep`` (read-only) or ``auto`` (deterministic
    repair). Each returned issue carries ``fixed`` and the CLI ``name``.
    """
    from agent_service.pptd_validation import validate_document

    directory = Path(directory).resolve()
    if not directory.is_dir():
        raise FileNotFoundError(f"input is not a directory: {directory}")

    def collect(bundle, load_issues):
        issues = list(load_issues)
        if bundle.get("document") is not None:
            issues.extend(validate_document(bundle, resource_root=directory))
            issues.extend(_heuristic_layout_issues(bundle))
        seen: set[tuple[str, str]] = set()
        unique: list[dict] = []
        for issue in issues:
            key = (str(issue.get("location", "")), str(issue.get("code", "")), str(issue.get("message", "")))
            if key in seen:
                continue
            seen.add(key)
            unique.append(issue)
        return unique

    entry, bundle, load_issues = load_deck(directory)
    first = collect(bundle, load_issues)
    first_keys = {(issue.get("location", ""), issue.get("code", "")) for issue in first}

    if level == "auto" and entry is not None and bundle.get("document") is not None:
        _auto_repair(entry, bundle, first)
        _, bundle, _ = load_deck(directory)
        final = collect(bundle, [])
    else:
        final = first

    final_keys = {(issue.get("location", ""), issue.get("code", "")) for issue in final}
    fixed = [issue for issue in first if (issue.get("location", ""), issue.get("code", "")) not in final_keys]

    report = []
    for issue in [*fixed, *final]:
        name, sev = _issue_meta(issue.get("code", ""))
        item = dict(issue)
        item.update(name=name, severity=item.get("severity", sev))
        item["fixed"] = (issue.get("location", ""), issue.get("code", "")) in first_keys and (issue.get("location", ""), issue.get("code", "")) not in final_keys
        report.append(item)
    if severity and severity not in ("all", ""):
        wanted = {token.strip() for token in severity.split(",") if token.strip()}
        report = [issue for issue in report if issue["severity"] in wanted or issue["name"] in wanted]
    return report


def format_issue(issue: dict) -> str:
    """Render one issue as the documented CLI text line."""
    sev_label = issue["severity"].capitalize()
    element = f' id="{issue["elementId"]}"' if issue.get("elementId") else ""
    return f'[fixed: {"true" if issue["fixed"] else "false"}][{sev_label}:{issue["name"]}] {issue["location"]}{element} {issue["message"]}'
