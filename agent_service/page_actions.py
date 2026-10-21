"""Page-scoped repair, visual review, and redesign actions."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile

import pymupdf

from . import pptd_exports, template_reference, visual_repair, visual_review
from .agents import make_model
from .kimi_rules import KimiRules, SCENARIOS
from .pptd_generation import PageResult, _ask
from .pptd_validation import validate_document
from .visual_pipeline import CandidateSource


PAGE_ACTION_KINDS = {
    "page-repair",
    "page-review",
    "page-design",
}


@dataclass
class RenderedPage:
    state: dict
    source_root: Path
    image: bytes
    issues: list[dict]
    layout: dict


def _issue_page(issue: dict) -> str:
    path = issue.get("pagePath") or issue.get("page")
    if isinstance(path, str) and path:
        return path
    location = issue.get("location")
    return location.split("#", 1)[0] if isinstance(location, str) else ""


def _page_issues(issues: list[dict], page_path: str) -> list[dict]:
    selected = []
    for issue in issues:
        if _issue_page(issue) != page_path:
            continue
        identifiers = issue.get("elementIds")
        if not isinstance(identifiers, list):
            identifiers = [issue["elementId"]] if issue.get("elementId") else []
        selected.append({
            **issue,
            "pagePath": page_path,
            "elementIds": identifiers,
        })
    return selected


async def _render_page(source, project_id: str, folder: Path, page_path: str) -> RenderedPage:
    """Render one page with the same browser pipeline used by final export."""
    folder = Path(folder).resolve()
    folder.mkdir(parents=True)
    source_dir = folder / "source"
    browser_dir = folder / "browser"
    browser_dir.mkdir()
    state = source.snapshot(project_id, source_dir)
    paths = state["document"].get("pages", [])
    if page_path not in paths:
        raise ValueError("当前页面不属于这个演示。")
    page_index = paths.index(page_path)
    env = os.environ.copy()
    browser = pptd_exports.chromium_path()
    if browser:
        env["PPTD_CHROMIUM"] = browser
    issues: list[dict] = []
    previews: dict[str, bytes] = {}
    layouts: dict[str, dict] = {}
    entry = source_dir / "document.pptd"
    try:
        async with pptd_exports._snapshot_renderer(entry, state["revision"]) as url:
            await pptd_exports._validated_process(
                browser_dir,
                "node",
                pptd_exports.WEB_DIR / "export_pdf.cjs",
                url,
                browser_dir,
                env=env,
                timeout=360,
            )
    except pptd_exports.ValidationError as error:
        issues = list(error.issues)
        previews = dict(error.previews or {})
        layouts = dict(error.layouts or {})

    image = previews.get(page_path)
    if image is None:
        report_path = browser_dir / "pdf-pages.json"
        if not report_path.is_file():
            raise ValueError("页面渲染没有返回可审阅的结果。")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        item = next((page for page in report.get("pages", []) if page.get("index") == page_index), None)
        if not item:
            raise ValueError("页面渲染结果缺少当前页。")
        with pymupdf.open(browser_dir / item["file"]) as pdf:
            image = pdf[0].get_pixmap(matrix=pymupdf.Matrix(1.2, 1.2), alpha=False).tobytes("png")
        layouts[page_path] = {"canvasSize": state["document"]["size"], "elements": item.get("textLayout", [])}

    (folder / "slide-1.png").write_bytes(image)
    return RenderedPage(
        state=state,
        source_root=source_dir,
        image=image,
        issues=_page_issues(issues, page_path),
        layout=layouts.get(page_path, {}),
    )


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
        committed = store.commit(
            project_id,
            candidate["document"],
            candidate["pages"],
            f"页面设计：{page_path}",
            initial.state["revision"],
        )
        return _report(
            page_path,
            "design",
            revision=committed["revision"],
            status=review["status"],
            issues=review["issues"],
            observations=review.get("observations", []),
            model=review.get("model", ""),
        )


async def run_page_action(kind: str, store, project_id: str, page_path: str, request, data_root: Path) -> dict:
    if kind == "page-review":
        return await review_page(store, project_id, page_path, request)
    if kind == "page-repair":
        return await repair_page(store, project_id, page_path, request)
    if kind == "page-design":
        return await design_page(store, project_id, page_path, request, data_root)
    raise ValueError("未知的页面操作。")
