"""Deterministic planning contract for the PPT expert before page writing."""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re

import yaml

from .number_fidelity import numeric_tokens


def plain_heading(value: str) -> str:
    """Normalize one Markdown heading for deterministic plan coverage checks."""
    text = re.sub(r"^#{1,6}\s+", "", str(value or "")).strip()
    return re.sub(
        r"[\s#>*_`~:：,，。；;、\-—–·\"'“”‘’()（）\[\]【】]+",
        "",
        text,
    ).casefold()


def source_sections(outline: str) -> list[str]:
    """Return principal source chapters from the generated Markdown outline."""
    headings = []
    for line in str(outline or "").splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line.strip())
        if not match:
            continue
        heading = match.group(1).strip()
        if re.search(r"\.[A-Za-z0-9]{2,5}$", heading):
            continue
        if heading and heading not in headings:
            headings.append(heading)
    return headings


def source_subsections(outline: str) -> list[str]:
    """Return every second-level source section in document order."""
    headings = []
    for line in str(outline or "").splitlines():
        match = re.match(r"^##\s+(.+?)\s*$", line.strip())
        if not match:
            continue
        heading = match.group(1).strip()
        if heading and heading not in headings:
            headings.append(heading)
    return headings


def source_subsection_owners(outline: str) -> dict[str, str]:
    """Map second-level headings to their current first-level chapter."""
    owners = {}
    current = ""
    for line in str(outline or "").splitlines():
        heading = re.match(r"^(#{1,2})\s+(.+?)\s*$", line.strip())
        if not heading:
            continue
        level, title = len(heading.group(1)), heading.group(2).strip()
        if level == 1:
            if title and not re.search(r"\.[A-Za-z0-9]{2,5}$", title):
                current = title
        elif level == 2 and current and title:
            owners.setdefault(title, current)
    return owners


def source_details(outline: str) -> list[str]:
    """Return every third-level source section in document order."""
    headings = []
    for line in str(outline or "").splitlines():
        match = re.match(r"^###\s+(.+?)\s*$", line.strip())
        if not match:
            continue
        heading = match.group(1).strip()
        if heading and heading not in headings:
            headings.append(heading)
    return headings


def source_detail_owners(outline: str) -> dict[str, str]:
    """Map third-level headings to their current second-level section."""
    owners = {}
    current = ""
    for line in str(outline or "").splitlines():
        heading = re.match(r"^(#{2,3})\s+(.+?)\s*$", line.strip())
        if not heading:
            continue
        level, title = len(heading.group(1)), heading.group(2).strip()
        if level == 2:
            current = title
        elif level == 3 and current and title:
            owners.setdefault(title, current)
    return owners


def plan_requirements(request, reference_info: dict | None) -> dict:
    """Derive the minimum page contract before any page is written."""
    reference_info = reference_info or {}
    characters = int(
        reference_info.get("characters")
        or len(getattr(request, "references", "") or "")
    )
    sections = source_sections(reference_info.get("outline", ""))
    subsections = source_subsections(reference_info.get("outline", ""))
    subsection_owners = source_subsection_owners(
        reference_info.get("outline", "")
    )
    details = source_details(reference_info.get("outline", ""))
    detail_owners = source_detail_owners(reference_info.get("outline", ""))
    source_text = (
        reference_info.get("planningBrief")
        or getattr(request, "references", "")
        or ""
    )
    source_numeric_tokens = reference_info.get("numericTokens")
    if source_numeric_tokens is None:
        source_numeric_tokens = numeric_tokens(str(source_text))
    requested = getattr(request, "pageCount", None)
    explicit_pages = (
        requested
        if type(requested) is int and 1 <= requested <= 60
        else None
    )
    if explicit_pages is not None:
        minimum_pages = explicit_pages
        maximum_pages = explicit_pages
    elif characters >= 12000:
        by_volume = (characters + 3499) // 3500
        by_chapters = len(sections) * 2
        substantive = min(28, max(8, by_volume, by_chapters))
        minimum_pages = substantive + 3
        maximum_pages = 32
    else:
        minimum_pages = 4
        maximum_pages = 32
    recommended_pages = minimum_pages
    requires_agenda_page = len(sections) >= 2 and minimum_pages >= 3
    return {
        "characters": characters,
        "principalSections": sections,
        "requiredSubsections": subsections,
        "subsectionOwners": subsection_owners,
        "requiredDetails": details,
        "detailOwners": detail_owners,
        "sourceNumericTokens": source_numeric_tokens,
        "preferredPlanPages": explicit_pages,
        "recommendedPlanPages": min(32, recommended_pages),
        "minimumPlanPages": minimum_pages,
        "maximumPlanPages": maximum_pages,
        "exactPlanPages": explicit_pages,
        "requiresAgendaPage": requires_agenda_page,
        "agendaPagePosition": 2 if requires_agenda_page else None,
        "agendaItems": sections[:8] if requires_agenda_page else [],
        "fieldLimits": {
            "title": 60,
            "readerTask": 120,
            "content": 320,
            "visualPlan": 200,
        },
    }


def is_agenda_plan_page(page: dict) -> bool:
    """Recognize a planner-authored agenda without treating body mentions as one."""
    if not isinstance(page, dict):
        return False
    title = str(page.get("title") or "").strip()
    visual = str(page.get("visualPlan") or "").strip()
    if title.startswith(("目录", "提纲", "议程")):
        return True
    combined = f"{title} {visual}".casefold()
    return "agenda" in combined or any(
        marker in combined
        for marker in ("目录页", "目录版式", "汇报主线")
    )


def _agenda_page_index(plan: dict) -> int | None:
    pages = plan.get("pages") if isinstance(plan, dict) else None
    if not isinstance(pages, list):
        return None
    for index, page in enumerate(pages):
        if is_agenda_plan_page(page):
            return index
    return None


def ensure_required_agenda_page(
    plan: dict,
    requirements: dict,
) -> bool:
    """Guarantee the agenda position without violating an exact page count.

    Non-exact plans are normalized by the host. Exact-count plans are left for
    ``audit_plan`` to reject so the planner repairs them without exceeding the
    user's requested page count.
    """
    if not requirements.get("requiresAgendaPage") or not isinstance(plan, dict):
        return False
    pages = plan.get("pages")
    if not isinstance(pages, list) or not pages:
        return False
    existing = _agenda_page_index(plan)
    if existing is not None:
        if existing == 1:
            return False
        page = pages.pop(existing)
        pages.insert(1, page)
        return True
    if requirements.get("exactPlanPages"):
        return False
    items = [
        str(value).strip()
        for value in requirements.get("agendaItems") or []
        if str(value).strip()
    ]
    if len(items) < 2:
        return False
    occupied = {
        str(page.get("path") or "")
        for page in pages
        if isinstance(page, dict)
    }
    agenda_path = "pages/02-agenda.page"
    suffix = 2
    while agenda_path in occupied:
        agenda_path = f"pages/02-agenda-{suffix}.page"
        suffix += 1
    pages.insert(
        1,
        {
            "path": agenda_path,
            "title": "目录",
            "kind": "agenda",
            "readerTask": "预览汇报的章节结构与论证顺序。",
            "content": "；".join(items),
            "visualPlan": "套用模板目录版式，按章节顺序展示汇报主线。",
            "sourceSections": [],
            "sourceSubsections": [],
        },
    )
    return True


def _truncate_plan_text(value: str, limit: int) -> str:
    """Trim a compact plan field at a natural boundary without mid-token cuts."""
    if len(value) <= limit:
        return value
    if limit <= 1:
        return value[:limit]
    search_end = limit - 1
    minimum = max(1, limit * 3 // 5)
    for delimiter in ("\n", "。", "；", ";", "，", ","):
        boundary = value.rfind(delimiter, minimum, search_end)
        if boundary >= minimum:
            return value[:boundary + 1].rstrip()
    candidate = value[:search_end].rstrip()
    while candidate and (
        (candidate[-1].isdigit() and value[len(candidate):len(candidate) + 1].isdigit())
        or (
            candidate[-1] in ".%"
            and value[len(candidate):len(candidate) + 1].isdigit()
        )
    ):
        candidate = candidate[:-1].rstrip()
    return candidate + "…"


def normalize_plan_field_limits(plan: dict, requirements: dict) -> bool:
    """Make the compact plan contract deterministic before rejecting a plan."""
    if not isinstance(plan, dict):
        return False
    pages = plan.get("pages")
    if not isinstance(pages, list):
        return False
    changed = False
    limits = requirements.get("fieldLimits") or {}
    for page in pages:
        if not isinstance(page, dict):
            continue
        for field, raw_limit in limits.items():
            value = page.get(field)
            if not isinstance(value, str):
                continue
            try:
                limit = int(raw_limit)
            except (TypeError, ValueError):
                continue
            normalized = _truncate_plan_text(value, limit)
            if normalized != value:
                page[field] = normalized
                changed = True
    return changed


def plan_source_sections(page: dict) -> list[str]:
    values = page.get("sourceSections") if isinstance(page, dict) else None
    if not isinstance(values, list):
        single = page.get("sourceSection") if isinstance(page, dict) else None
        values = [single] if isinstance(single, str) else []
    return [
        str(value)
        for value in values
        if isinstance(value, str) and value.strip()
    ]


def plan_source_subsections(page: dict) -> list[str]:
    values = page.get("sourceSubsections") if isinstance(page, dict) else None
    if not isinstance(values, list):
        return []
    return [
        str(value)
        for value in values
        if isinstance(value, str) and value.strip()
    ]


def plan_source_details(page: dict) -> list[str]:
    values = page.get("sourceDetails") if isinstance(page, dict) else None
    if not isinstance(values, list):
        return []
    return [
        str(value)
        for value in values
        if isinstance(value, str) and value.strip()
    ]


def attach_source_details(plan: dict, requirements: dict) -> dict:
    """Attach exact third-level headings to pages selected by the planner."""
    pages = plan.get("pages") if isinstance(plan, dict) else None
    if not isinstance(pages, list):
        return plan
    details = list(requirements.get("requiredDetails") or [])
    owners = {
        str(key): str(value)
        for key, value in (requirements.get("detailOwners") or {}).items()
    }
    by_parent: dict[str, list[str]] = {}
    for title in details:
        owner = owners.get(title)
        if owner:
            by_parent.setdefault(plain_heading(owner), []).append(title)
    for page in pages:
        if not isinstance(page, dict):
            continue
        values = []
        for subsection in plan_source_subsections(page):
            for detail in by_parent.get(plain_heading(subsection), []):
                if detail not in values:
                    values.append(detail)
        page["sourceDetails"] = values
    return plan


def audit_plan(plan: dict, requirements: dict) -> list[str]:
    """Reject an incomplete or under-sized plan before page generation."""
    issues = []
    pages = plan.get("pages") if isinstance(plan, dict) else None
    if not isinstance(pages, list) or not pages:
        return ["plan.json 缺少非空 pages 数组"]
    minimum = int(requirements.get("minimumPlanPages") or 0)
    if len(pages) < minimum:
        issues.append(
            f"长资料计划页数不足：至少需要 {minimum} 页，当前仅 {len(pages)} 页"
        )
    maximum = int(requirements.get("maximumPlanPages") or 0)
    if maximum and len(pages) > maximum:
        issues.append(
            f"计划页数超过上限：最多 {maximum} 页，当前为 {len(pages)} 页"
        )
    paths = []
    for index, page in enumerate(pages, 1):
        if not isinstance(page, dict):
            issues.append(f"第 {index} 个计划项不是对象")
            continue
        path = page.get("path")
        if not isinstance(path, str) or not re.fullmatch(
            r"pages/[A-Za-z0-9_.-]+\.page",
            path,
        ):
            issues.append(f"第 {index} 个计划项缺少合法的 pages/*.page 路径")
        elif path in paths:
            issues.append(f"计划路径重复：{path}")
        else:
            paths.append(path)
        for field in ("title", "readerTask", "content", "visualPlan"):
            if not isinstance(page.get(field), str) or not page[field].strip():
                issues.append(f"第 {index} 个计划项缺少 {field}")
        for field, limit in (requirements.get("fieldLimits") or {}).items():
            value = page.get(field)
            if isinstance(value, str) and len(value) > int(limit):
                issues.append(
                    f"第 {index} 个计划项的 {field} 过长（{len(value)} 字，上限 {limit}）"
                )
    required = requirements.get("principalSections") or []
    required_subsections = requirements.get("requiredSubsections") or []
    required_details = requirements.get("requiredDetails") or []
    subsection_owners = {
        str(key): str(value)
        for key, value in (requirements.get("subsectionOwners") or {}).items()
    }
    if required:
        known = {plain_heading(value) for value in required}
        covered = {
            plain_heading(section)
            for page in pages
            if isinstance(page, dict)
            for section in plan_source_sections(page)
        }
        missing = [section for section in required if plain_heading(section) not in covered]
        if missing:
            issues.append("计划没有覆盖全部一级章节，缺少：" + "；".join(missing))
        if (
            int(requirements.get("characters") or 0) >= 12000
            and len(required) >= 2
        ):
            coverage_count = Counter()
            for page in pages:
                if not isinstance(page, dict):
                    continue
                for section in set(plan_source_sections(page)):
                    coverage_count[plain_heading(section)] += 1
            thin = [
                section
                for section in required
                if coverage_count[plain_heading(section)] < 2
            ]
            if thin:
                issues.append(
                    "长报告的一级章节不能压成单页，以下章节至少需要 2 页："
                    + "；".join(thin)
                )
    if required_subsections:
        known = {
            plain_heading(section): section
            for section in required_subsections
        }
        sections_with_children = {
            plain_heading(owner)
            for owner in subsection_owners.values()
        }
        covered = set()
        for index, page in enumerate(pages, 1):
            if not isinstance(page, dict):
                continue
            values = page.get("sourceSubsections")
            if not isinstance(values, list) or not values:
                page_sections = {
                    plain_heading(value)
                    for value in plan_source_sections(page)
                }
                if page_sections & sections_with_children:
                    issues.append(
                        f"第 {index} 个正文计划项缺少 sourceSubsections"
                    )
                continue
            page_sections = {
                plain_heading(value)
                for value in plan_source_sections(page)
            }
            for value in values:
                normalized = plain_heading(value)
                if normalized not in known:
                    issues.append(
                        f"第 {index} 个计划项引用了未知二级章节：{value}"
                    )
                    continue
                covered.add(normalized)
                owner = subsection_owners.get(str(value))
                if owner and plain_heading(owner) not in page_sections:
                    issues.append(
                        f"第 {index} 个计划项把二级章节“{value}”"
                        f"错误归属于 {page_sections or '未指定的一级章节'}"
                    )
        missing = [
            section
            for section in required_subsections
            if plain_heading(section) not in covered
        ]
        if missing:
            issues.append(
                "计划没有覆盖全部二级章节，缺少：" + "；".join(missing)
            )
    if required_details:
        known_details = {
            plain_heading(section): section
            for section in required_details
        }
        detail_owners = {
            str(key): str(value)
            for key, value in (requirements.get("detailOwners") or {}).items()
        }
        covered_details = set()
        for index, page in enumerate(pages, 1):
            if not isinstance(page, dict):
                continue
            page_subsections = {
                plain_heading(value)
                for value in plan_source_subsections(page)
            }
            for value in plan_source_details(page):
                normalized = plain_heading(value)
                if normalized not in known_details:
                    issues.append(
                        f"第 {index} 个计划项引用了未知三级章节：{value}"
                    )
                    continue
                covered_details.add(normalized)
                owner = detail_owners.get(str(value))
                if owner and plain_heading(owner) not in page_subsections:
                    issues.append(
                        f"第 {index} 个计划项把三级章节“{value}”"
                        f"错误归属于二级章节 {page_subsections or '未指定'}"
                    )
        missing_details = [
            section
            for section in required_details
            if plain_heading(section) not in covered_details
        ]
        if missing_details:
            issues.append(
                "计划没有覆盖全部三级章节，缺少："
                + "；".join(missing_details)
            )
    if requirements.get("requiresAgendaPage"):
        agenda_indexes = [
            index
            for index, page in enumerate(pages)
            if is_agenda_plan_page(page)
        ]
        if not agenda_indexes:
            issues.append(
                "计划必须包含目录页，并放在封面之后、正文之前（第 2 页）"
            )
        elif len(agenda_indexes) > 1:
            issues.append("计划只能包含一个目录页")
        elif agenda_indexes[0] != 1:
            issues.append(
                "目录页必须放在第 2 页，紧跟封面并位于正文之前"
            )
    source_numbers = set(requirements.get("sourceNumericTokens") or [])
    if source_numbers:
        invented_numbers = set()
        for page in pages:
            if not isinstance(page, dict):
                continue
            invented_numbers.update(
                set(numeric_tokens(str(page.get("content") or "")))
                - source_numbers
            )
        if invented_numbers:
            issues.append(
                "计划包含原文中不存在的数字："
                + "；".join(sorted(invented_numbers))
            )
    return issues


def load_plan(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def write_plan_deck(
    workspace: Path,
    plan: dict,
    template_info: dict | None,
) -> Path:
    """Materialize the approved plan as the stable deck entry for write batches."""
    deck = workspace / "deck"
    (deck / "pages").mkdir(parents=True, exist_ok=True)
    (deck / "media").mkdir(parents=True, exist_ok=True)
    normalized_pages = []
    for page in plan["pages"]:
        item = dict(page)
        item["sourceSections"] = plan_source_sections(page)
        item["sourceSubsections"] = plan_source_subsections(page)
        item["sourceDetails"] = plan_source_details(page)
        normalized_pages.append(item)
    normalized = {**plan, "pages": normalized_pages}
    (deck / "plan.json").write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    size = [960, 540]
    theme = {}
    if template_info:
        from pptd_engine.document import load_yaml
        from .kimi_template import template_style_profile, template_theme

        try:
            template = load_yaml(Path(template_info["entry"]))
            if isinstance(template.get("size"), list) and len(template["size"]) == 2:
                size = template["size"]
            theme = template_theme(
                template_style_profile(Path(template_info["pptd"]))
            )
        except (OSError, ValueError):
            pass
    document = {
        "version": "v2",
        "title": str(plan.get("deckTitle") or "演示文稿"),
        "size": size,
        **({"theme": theme} if theme else {}),
        "pages": [page["path"] for page in normalized_pages],
    }
    entry = deck / "deck.pptd"
    entry.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return entry


def clear_page_drafts(workspace: Path) -> None:
    """Enforce the planning boundary if the model ignored the no-page rule."""
    pages = workspace / "deck" / "pages"
    if not pages.is_dir():
        return
    for path in pages.glob("*.page"):
        if path.is_file() and not path.is_symlink():
            path.unlink()
