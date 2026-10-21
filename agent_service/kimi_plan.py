"""Deterministic planning contract for the PPT expert before page writing."""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re

import yaml


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


def plan_requirements(request, reference_info: dict | None) -> dict:
    """Derive the minimum page contract before any page is written."""
    reference_info = reference_info or {}
    characters = int(
        reference_info.get("characters")
        or len(getattr(request, "references", "") or "")
    )
    sections = source_sections(reference_info.get("outline", ""))
    requested = getattr(request, "pageCount", None)
    if type(requested) is int and requested > 0:
        minimum_pages = requested
        exact_pages = True
    elif characters >= 12000:
        by_volume = (characters + 3499) // 3500
        by_chapters = len(sections) * 2
        substantive = min(28, max(8, by_volume, by_chapters))
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


def audit_plan(plan: dict, requirements: dict) -> list[str]:
    """Reject an incomplete or under-sized plan before page generation."""
    issues = []
    pages = plan.get("pages") if isinstance(plan, dict) else None
    if not isinstance(pages, list) or not pages:
        return ["plan.json 缺少非空 pages 数组"]
    minimum = int(requirements.get("minimumPlanPages") or 0)
    if requirements.get("exactPlanPages"):
        if len(pages) != minimum:
            issues.append(
                f"用户指定 {minimum} 页，但 plan.json 规划了 {len(pages)} 页"
            )
    elif len(pages) < minimum:
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
            not requirements.get("exactPlanPages")
            and int(requirements.get("characters") or 0) >= 12000
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
