"""Page-scoped repair, visual review, and redesign actions."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import time
from uuid import uuid4

from . import pptd_exports, template_reference, visual_repair, visual_review
from .agents import make_model
from .kimi_rules import KimiRules, SCENARIOS
from .orchestrator import PAGE_ACTION_KINDS
from .pptd_generation import PageResult, _ask
from .pptd_validation import validate_document
from .render_service import RenderedPage, page_issues, render_page
from .visual_pipeline import CandidateSource


_page_issues = page_issues
_render_page = render_page


def _report(page_path: str, action: str, *, revision: int, status: str,
            issues: list[dict], observations: list[dict] | None = None,
            model: str = "") -> dict:
    return {
        "action": action,
        "pagePath": page_path,
        "revision": revision,
        "status": status,
        "issues": issues,
        "observations": observations or [],
        "model": model,
    }


async def _review_rendered(rendered: RenderedPage, request, page_path: str) -> dict:
    scenario = getattr(request, "scenario", "auto")
    if scenario not in SCENARIOS:
        scenario = "management-report"
    report = await visual_review.review_page(
        rendered.state,
        Path(rendered.source_root).parent,
        page_path,
        scenario=scenario,
        usage=getattr(request, "usage", "presentation"),
    )
    issues = [*rendered.issues, *report.get("issues", [])]
    unique = {}
    for issue in issues:
        key = (
            issue.get("pagePath"),
            issue.get("code"),
            tuple(issue.get("elementIds") or []),
            issue.get("message"),
        )
        unique[key] = issue
    return _report(
        page_path,
        "review",
        revision=rendered.state["revision"],
        status="issues" if unique else "passed",
        issues=list(unique.values()),
        observations=report.get("observations", []),
        model=report.get("model", ""),
    )


async def review_page(store, project_id: str, page_path: str, request) -> dict:
    with tempfile.TemporaryDirectory(prefix=".page-review-", dir=store.root) as temporary:
        rendered = await _render_page(store, project_id, Path(temporary), page_path)
        return await _review_rendered(rendered, request, page_path)


async def repair_page(store, project_id: str, page_path: str, request) -> dict:
    with tempfile.TemporaryDirectory(prefix=".page-repair-", dir=store.root) as temporary:
        root = Path(temporary)
        rendered = await _render_page(store, project_id, root / "initial", page_path)
        review = await _review_rendered(rendered, request, page_path)
        issues = review["issues"]
        if not issues:
            return _report(
                page_path,
                "repair",
                revision=rendered.state["revision"],
                status="passed",
                issues=[],
                observations=review.get("observations", []),
                model=review.get("model", ""),
            )

        async def validate_candidate(candidate):
            candidate_root = root / f"candidate-{len(list(root.glob('candidate-*')))}"
            checked = await _render_page(
                CandidateSource(rendered.source_root, candidate),
                project_id,
                candidate_root,
                page_path,
            )
            if checked.issues:
                raise pptd_exports.ValidationError(
                    checked.issues,
                    previews={page_path: checked.image},
                    layouts={page_path: checked.layout},
                )

        candidate = await visual_repair.repair_deck(
            request,
            rendered.state,
            issues,
            rendered_pages={page_path: rendered.image},
            layout_measurements={page_path: rendered.layout},
            validate_candidate=validate_candidate,
        )
        validate_document(candidate)
        checked = await _render_page(
            CandidateSource(rendered.source_root, candidate),
            project_id,
            root / "final",
            page_path,
        )
        if checked.issues:
            raise ValueError("页面修复后仍有排版问题，原稿未被覆盖。")
        final_review = await _review_rendered(checked, request, page_path)
        committed = store.commit(
            project_id,
            candidate["document"],
            candidate["pages"],
            f"页面修复：{page_path}",
            rendered.state["revision"],
        )
        return _report(
            page_path,
            "repair",
            revision=committed["revision"],
            status=final_review["status"],
            issues=final_review["issues"],
            observations=final_review.get("observations", []),
            model=final_review.get("model", ""),
        )


async def _design_candidate(rendered: RenderedPage, page_path: str, request, data_root: Path) -> dict:
    scenario = getattr(request, "scenario", "auto")
    if scenario not in SCENARIOS:
        metadata = getattr(request.project, "generationMeta", None) or {}
        scenario = metadata.get("scenario", "management-report") if isinstance(metadata, dict) else "management-report"
    if scenario not in SCENARIOS:
        scenario = "management-report"
    page = next(item for item in rendered.state["pages"] if item["path"] == page_path)
    reference = template_reference.load(
        data_root,
        getattr(request, "templateId", None) or getattr(request.project, "templateId", None),
    )
    context = {
        "task": "Redesign exactly one existing page. Preserve all factual content, numbers, chart data, formulas, sources and semantic labels; change layout, hierarchy, spacing and visual expression according to the user request.",
        "request": request.prompt,
        "scenario": scenario,
        "document": {
            key: rendered.state["document"].get(key)
            for key in ("version", "size", "theme", "title")
        },
        "pagePath": page_path,
        "currentPage": page["data"],
        "userDesignRequest": request.prompt,
        "returnContract": "Return {data:<complete redesigned page>} for this page only. Keep the page inside the canvas and use editable PPTD elements.",
    }
    if reference:
        context["templateReference"] = reference["summary"]
        context["_contextSources"] = reference.get("sources", {})
    model = make_model(output_mode=getattr(request, "outputMode", "auto"), observable_retries=True)
    try:
        answer = await _ask(
            model,
            "page",
            KimiRules(),
            context,
            scenario=scenario,
            session_id=request.project.id,
            response_schema=PageResult,
        )
    finally:
        await model.client.close()
    data = answer["data"]
    candidate = {
        "document": deepcopy(rendered.state["document"]),
        "pages": deepcopy(rendered.state["pages"]),
        "revision": rendered.state["revision"],
    }
    next(page for page in candidate["pages"] if page["path"] == page_path)["data"] = data
    validate_document(candidate)
    return candidate


async def design_page(store, project_id: str, page_path: str, request, data_root: Path) -> dict:
    if not str(getattr(request, "prompt", "") or "").strip():
        raise ValueError("请输入页面设计想法。")
    with tempfile.TemporaryDirectory(prefix=".page-design-", dir=store.root) as temporary:
        root = Path(temporary)
        initial = await _render_page(store, project_id, root / "initial", page_path)
        candidate = await _design_candidate(initial, page_path, request, data_root)
        rendered = await _render_page(
            CandidateSource(initial.source_root, candidate),
            project_id,
            root / "candidate",
            page_path,
        )
        if rendered.issues:
            async def validate_candidate(next_candidate):
                checked = await _render_page(
                    CandidateSource(initial.source_root, next_candidate),
                    project_id,
                    root / f"repair-{len(list(root.glob('repair-*')))}",
                    page_path,
                )
                if checked.issues:
                    raise pptd_exports.ValidationError(
                        checked.issues,
                        previews={page_path: checked.image},
                        layouts={page_path: checked.layout},
                    )

            candidate = await visual_repair.repair_deck(
                request,
                candidate,
                rendered.issues,
                rendered_pages={page_path: rendered.image},
                layout_measurements={page_path: rendered.layout},
                validate_candidate=validate_candidate,
            )
            validate_document(candidate)
            rendered = await _render_page(
                CandidateSource(initial.source_root, candidate),
                project_id,
                root / "final",
                page_path,
            )
            if rendered.issues:
                raise ValueError("页面设计稿仍有排版问题，原稿未被覆盖。")
        review = await _review_rendered(rendered, request, page_path)
        candidate_id = uuid4().hex
        candidate_dir = design_candidate_dir(store.root, project_id, candidate_id)
        candidate_dir.mkdir(parents=True)
        CandidateSource(initial.source_root, candidate).snapshot(
            project_id, candidate_dir / "source"
        )
        (candidate_dir / "preview.png").write_bytes(rendered.image)
        metadata = {
            "id": candidate_id,
            "projectId": project_id,
            "pagePath": page_path,
            "baseRevision": initial.state["revision"],
            "createdAt": time.time(),
            "status": "pending",
        }
        (candidate_dir / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        report = _report(
            page_path,
            "design",
            revision=initial.state["revision"],
            status=review["status"],
            issues=review["issues"],
            observations=review.get("observations", []),
            model=review.get("model", ""),
        )
        report["candidate"] = {
            "id": candidate_id,
            "baseRevision": initial.state["revision"],
            "previewUrl": f"/api/studio/projects/{project_id}/pptd/design-candidates/{candidate_id}/preview",
            "applyUrl": f"/api/studio/projects/{project_id}/pptd/design-candidates/{candidate_id}/apply",
            "discardUrl": f"/api/studio/projects/{project_id}/pptd/design-candidates/{candidate_id}",
        }
        return report


def design_candidate_dir(root: Path, project_id: str, candidate_id: str) -> Path:
    if not project_id or not project_id.replace("-", "").replace("_", "").isalnum():
        raise ValueError("无效的项目标识。")
    if not candidate_id or not candidate_id.isalnum():
        raise ValueError("无效的设计候选标识。")
    return Path(root).resolve() / "design-candidates" / project_id / candidate_id


def read_design_candidate(root: Path, project_id: str, candidate_id: str) -> tuple[Path, dict]:
    folder = design_candidate_dir(root, project_id, candidate_id)
    metadata_path = folder / "metadata.json"
    if not metadata_path.is_file():
        raise ValueError("设计候选不存在或已失效。")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("projectId") != project_id or metadata.get("id") != candidate_id:
        raise ValueError("设计候选元数据不匹配。")
    return folder, metadata


def apply_design_candidate(
    store,
    project_id: str,
    candidate_id: str,
    expected_revision: int,
) -> dict:
    folder, metadata = read_design_candidate(store.root, project_id, candidate_id)
    if metadata.get("status") != "pending":
        raise ValueError("该设计候选已经处理。")
    if metadata.get("baseRevision") != expected_revision:
        raise ValueError("页面设计期间文档已更新，请重新生成设计稿。")
    state = store._read_source(folder / "source")
    committed = store.commit(
        project_id,
        state["document"],
        state["pages"],
        f"页面设计：{metadata.get('pagePath')}",
        expected_revision,
    )
    metadata["status"] = "applied"
    (folder / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return committed


def discard_design_candidate(store, project_id: str, candidate_id: str) -> None:
    folder, metadata = read_design_candidate(store.root, project_id, candidate_id)
    if metadata.get("status") == "applied":
        raise ValueError("已经应用的页面设计不能放弃。")
    shutil.rmtree(folder)


async def run_page_action(kind: str, store, project_id: str, page_path: str, request, data_root: Path) -> dict:
    if kind == "page-review":
        return await review_page(store, project_id, page_path, request)
    if kind == "page-repair":
        return await repair_page(store, project_id, page_path, request)
    if kind == "page-design":
        return await design_page(store, project_id, page_path, request, data_root)
    raise ValueError("未知的页面操作。")
