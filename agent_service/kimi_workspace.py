"""Bounded file workspace used by the PPT expert agent."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil

import yaml

from .docx_markdown import docx_to_markdown
from .number_fidelity import numeric_tokens


def _compact_planning_text(text: str, limit: int = 2400) -> str:
    """Keep the parts of one chapter that are useful for page planning.

    The planner only needs the chapter's main claims and concrete evidence. It
    does not need every paragraph from a long source report.
    """
    paragraphs = []
    for block in re.split(r"\n\s*\n", str(text or "")):
        value = " ".join(part.strip() for part in block.splitlines() if part.strip())
        if not value or value.startswith("#"):
            continue
        paragraphs.append(value)
    if not paragraphs:
        return ""

    selected = []

    def add(value: str) -> None:
        value = value.strip()
        if value and value not in selected:
            selected.append(value)

    for value in paragraphs[:4]:
        add(value)
    for value in paragraphs:
        if re.search(r"\d", value):
            add(value)
        if len(selected) >= 10:
            break
    for value in paragraphs[-2:]:
        add(value)
    result = "\n".join(selected)
    if len(result) <= limit:
        return result
    return result[:limit].rsplit("\n", 1)[0].rstrip()


def _section_excerpts(text: str, limit: int = 2400) -> dict[str, str]:
    """Split a structured reading copy into compact principal-section briefs."""
    excerpts = {}
    current_title = ""
    current_lines: list[str] = []
    for line in str(text or "").splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            if current_title:
                excerpts[current_title] = _compact_planning_text(
                    "\n".join(current_lines), limit
                )
            current_title = match.group(1).strip()
            current_lines = []
        elif current_title:
            current_lines.append(line)
    if current_title:
        excerpts[current_title] = _compact_planning_text(
            "\n".join(current_lines), limit
        )
    return {title: value for title, value in excerpts.items() if value}


def _subsection_excerpts(text: str, limit: int = 6000) -> dict[str, str]:
    """Preserve each second-level section as an addressable evidence unit."""
    excerpts = {}
    chapter_intro: list[str] = []
    current_title = ""
    current_lines: list[str] = []
    chapter_has_unit = False

    def flush() -> None:
        if current_title:
            excerpts[current_title] = "\n".join(current_lines).strip()[:limit]

    for line in str(text or "").splitlines():
        first_level = re.match(r"^#\s+(.+?)\s*$", line)
        if first_level:
            flush()
            chapter_intro = []
            chapter_has_unit = False
            current_title = ""
            current_lines = []
            continue
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            flush()
            current_title = match.group(1).strip()
            current_lines = [] if chapter_has_unit else list(chapter_intro)
            chapter_has_unit = True
        elif current_title:
            current_lines.append(line)
        else:
            chapter_intro.append(line)
    flush()
    return {title: value for title, value in excerpts.items() if value}


def workspace(data_root: Path, project_id: str, job_id: str) -> Path:
    override = os.getenv("PPT_KIMI_WORKSPACE", "").strip()
    base = Path(override).expanduser().resolve() if override else data_root
    return base / "kimi-workspace" / project_id / job_id


def prepare_template(data_root: Path, template_id, target_workspace: Path) -> Path | None:
    """Copy the uploaded company template into the agent workspace and return it."""
    if not template_id or not isinstance(template_id, str):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", template_id):
        raise ValueError("无效的模板标识")
    source = data_root / "templates" / template_id / "template.pptx"
    if not source.is_file():
        raise ValueError(f"未找到公司模板文件：{source}")
    destination = target_workspace / "template" / "template.pptx"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())
    return destination


def prepare_reference_material(
    data_root: Path,
    project_id: str,
    request,
    target_workspace: Path,
) -> dict:
    """Copy originals into the workspace and build a structured reading copy."""
    folder = data_root / "reference-files" / project_id
    sources = target_workspace / "sources"
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
            safe_stem = (
                re.sub(r"[^A-Za-z0-9_-]+", "-", Path(name).stem).strip("-")
                or f"source-{index}"
            )
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
            files.append(
                {
                    "name": name,
                    "original": str(original_copy.relative_to(target_workspace)),
                    "readingCopy": str(reading.relative_to(target_workspace)),
                    "characters": len(text),
                }
            )
            sections.append(f"# {name}\n\n{text}".strip())
    references = getattr(request, "references", "") or ""
    if not files and references:
        reading = sources / "reference.md"
        reading.write_text(references, encoding="utf-8")
        files.append(
            {
                "name": "参考资料",
                "original": None,
                "readingCopy": str(reading.relative_to(target_workspace)),
                "characters": len(references),
            }
        )
        sections.append(references)
    if not sections:
        return {"files": [], "outline": "", "characters": 0}
    combined = "\n\n".join(sections)
    source_numeric_tokens = numeric_tokens(combined)
    reference_md = sources / "reference.md"
    reference_md.write_text(combined, encoding="utf-8")
    outline_lines = [
        line.strip()
        for line in combined.splitlines()
        if re.match(r"^#{1,4}\s+", line.strip())
    ]
    outline = "\n".join(outline_lines[:500])
    (sources / "reference-outline.md").write_text(outline + "\n", encoding="utf-8")
    excerpts = _section_excerpts(combined, limit=4000)
    source_units = _subsection_excerpts(combined)
    source_unit_briefs = {
        title: _compact_planning_text(value, limit=700)
        for title, value in source_units.items()
    }
    if excerpts:
        per_section_limit = max(
            400,
            min(2400, 15000 // max(1, len(excerpts))),
        )
        planning_brief = "\n\n".join(
            f"## {title}\n{value[:per_section_limit]}"
            for title, value in excerpts.items()
        )[:16000]
    else:
        planning_brief = _compact_planning_text(combined, limit=12000)
    return {
        "files": files,
        "outline": outline[:20000],
        "characters": len(combined),
        "referenceFile": str(reference_md.relative_to(target_workspace)),
        "outlineFile": "sources/reference-outline.md",
        "planningBrief": planning_brief,
        "sectionExcerpts": excerpts,
        "subsectionExcerpts": source_units,
        "sourceUnitBriefs": source_unit_briefs,
        "numericTokens": source_numeric_tokens,
    }


def yaml_parse_issue(page_path: str, exc: BaseException) -> dict:
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


def page_has_content(page_data: dict) -> bool:
    """A declared page is complete only after it has renderable elements."""
    elements = page_data.get('elements') if isinstance(page_data, dict) else None
    return isinstance(elements, list) and len(elements) > 0


def deck_status(entry: Path, *, require_content: bool = False):
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
            page_data = load_yaml(page_path)
        except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
            invalid.append(yaml_parse_issue(path, exc))
        else:
            if require_content and not page_has_content(page_data):
                missing.append(path)
                invalid.append(
                    {
                        'path': path,
                        'location': path,
                        'code': 'page-empty',
                        'message': f'{path} 只有页面元数据，尚未写入可渲染元素',
                    }
                )
            elif require_content:
                from .pptd_validation import validate_document

                structural = validate_document(
                    {
                        'document': {**document, 'pages': [path]},
                        'pages': [{'path': path, 'data': page_data}],
                    },
                    resource_root=entry.parent,
                )
                if structural:
                    missing.append(path)
                    invalid.extend(
                        {
                            **issue,
                            'path': path,
                            'pagePath': path,
                        }
                        for issue in structural
                    )
                else:
                    existing.append(path)
            else:
                existing.append(path)
    return document, paths, existing, missing, invalid


def deck_progress(entry: Path, *, require_content: bool = False):
    """Read the declared deck without treating malformed pages as complete."""
    document, paths, existing, _, _ = deck_status(
        entry,
        require_content=require_content,
    )
    missing = [path for path in paths if path not in existing]
    return document, paths, existing, missing


def batch_page_target(missing_count: int, remaining_batches: int, minimum: int) -> int:
    """Distribute all missing pages across the batches that are still available."""
    if missing_count <= 0:
        return 0
    if remaining_batches <= 0:
        return missing_count
    return max(minimum, (missing_count + remaining_batches - 1) // remaining_batches)


def recovery_checkpoint(data_root: Path, project_id: str, job_id: str) -> dict | None:
    """Return a public checkpoint summary for an agent-written partial deck."""
    target_workspace = workspace(data_root, project_id, job_id)
    deck_dir = target_workspace / "deck"
    entries = sorted(deck_dir.glob("*.pptd")) if deck_dir.is_dir() else []
    if not entries:
        return None
    try:
        _, paths, existing, missing, invalid = deck_status(
            entries[0],
            require_content=True,
        )
    except (OSError, ValueError, yaml.YAMLError):
        return None
    if not paths:
        return None
    invalid_paths = {
        str(issue.get('path') or issue.get('pagePath') or issue.get('location'))
        for issue in invalid
        if isinstance(issue, dict)
    }
    checkpoint = {
        "stage": "pages",
        "completedPages": len(existing),
        "totalPages": len(paths),
        "missingPages": len(set(missing) | invalid_paths),
        "summary": f"已保留 {len(existing)}/{len(paths)} 页",
    }
    if invalid:
        checkpoint["invalidPages"] = len(invalid_paths)
    return checkpoint


def restore_workspace(
    data_root: Path,
    project_id: str,
    job_id: str,
    parent_job_id: str | None,
):
    """Resume a failed agent run from its last written deck files."""
    if not parent_job_id:
        return
    source = workspace(data_root, project_id, parent_job_id)
    destination = workspace(data_root, project_id, job_id)
    if source.is_dir() and not destination.exists():
        shutil.copytree(source, destination, dirs_exist_ok=True)


def reply_text(reply_msg) -> str:
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
    return re.sub(r"<think>[\s\S]*?</think>", "", text).strip()


def find_entry(target_workspace: Path) -> Path:
    """Only the agent's new deliverable counts; template analysis never does."""
    deck_dir = target_workspace / "deck"
    entries = sorted(deck_dir.glob("*.pptd")) if deck_dir.is_dir() else []
    if not entries:
        raise ValueError(
            "PPT 专家没有在 deck/ 目录产出新的 .pptd 交付物（模板分析产物不会被当作答案）。"
        )
    return entries[0]


def load_document(entry: Path) -> dict:
    """Read the deck manifest without requiring its declared pages to exist yet."""
    from pptd_engine.document import load_yaml

    return load_yaml(entry)


def load_deck(entry: Path):
    from pptd_engine.document import load_yaml

    document = load_document(entry)
    pages = []
    for ref in document.get("pages", []):
        try:
            data = load_yaml(entry.parent / ref)
        except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
            issue = yaml_parse_issue(ref, exc)
            raise ValueError(issue["message"]) from exc
        pages.append({"path": ref, "data": data})
    return document, pages


def collect_assets(entry: Path) -> dict[str, Path]:
    media = entry.parent / "media"
    assets: dict[str, Path] = {}
    if media.is_dir():
        for path in media.rglob("*"):
            if path.is_file() and not path.is_symlink():
                assets[path.relative_to(entry.parent).as_posix()] = path
    return assets


def write_deck(
    directory: Path,
    entry: Path,
    document: dict,
    pages: list[dict],
) -> Path:
    """Materialize the final deck so browser rendering uses exactly what ships."""
    directory.mkdir(parents=True, exist_ok=True)
    media = entry.parent / "media"
    if media.is_dir():
        shutil.copytree(media, directory / "media", dirs_exist_ok=True)
    for page in pages:
        target = directory / str(page["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            yaml.safe_dump(page["data"], allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    target = directory / entry.name
    target.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return target


def write_pages(entry: Path, pages: list[dict]) -> None:
    """Persist page changes back to the agent's editable source deck."""
    for page in pages:
        target = entry.parent / str(page["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            yaml.safe_dump(page["data"], allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )


def issue_summary(issues: list[dict], limit: int = 8) -> str:
    lines = [
        f"- {issue.get('location')}: {issue.get('message')}"
        for issue in issues[:limit]
    ]
    if len(issues) > limit:
        lines.append(f"- 另有 {len(issues) - limit} 条同类问题，请逐一自查同类元素。")
    return "\n".join(lines)


def issue_page(issue: dict) -> str:
    return str(issue.get("page") or issue.get("location", "").split("#", 1)[0])


def page_hashes(entry: Path, paths: list[str]) -> dict[str, str]:
    result = {}
    for path in paths:
        target = entry.parent / path
        result[path] = (
            hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else ""
        )
    return result
