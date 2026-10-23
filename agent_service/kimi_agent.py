"""Kimi-slides 1:1 agentic PPT expert.

Unlike the deterministic ``pptd_generation`` pipeline, this agent gives the model
real file and CLI tools and lets it follow ``docs/kimi-ppt/original/SKILL.md``
itself: classify the task, read only the rules it needs, write pages in parallel,
and close the ``kimi-slides check`` / ``screenshot`` loop before finishing.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import html
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import yaml
from agentscope.agent import Agent, ContextConfig, ReActConfig
from agentscope.event import (
    ExceedMaxItersEvent,
    ModelCallEndEvent,
    ModelCallStartEvent,
    ReplyStartEvent,
    ThinkingBlockStartEvent,
    ToolCallDeltaEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
)
from agentscope.message import Msg, TextBlock, ToolResultState
from agentscope.middleware import TracingMiddleware
from agentscope.permission import PermissionBehavior, PermissionDecision, PermissionMode
from agentscope.state import AgentState
from agentscope.tool import (
    Bash,
    Edit,
    FunctionTool,
    Glob,
    Grep,
    Read,
    ToolChunk,
    Toolkit,
    Write,
)

from .benchmark_timing import BenchmarkTrace
from .model_gateway import make_model, timeout_setting
from .number_fidelity import numeric_tokens
from . import template_cache
from .execution_policy import expert_policy
from .pptd_compat import normalize_pages
from .pptd_validation import normalize_table_tracks, validate_document
from .kimi_validation import (
    _browser_layout_issues,
    _browser_layout_report,
    _contrast_issues,
    _emit_note,
    _render_pages,
    _renderer_ready,
    _slide_artifact_dir,
    final_layout_issues,
    visual_overview_issues,
)
from .kimi_template import (
    _apply_background,
    _apply_chrome,
    _apply_configured_background,
    _apply_host_transforms,
    _apply_template_cover_closing,
    _clear_corner_conflicts,
    build_template_closing,
    build_template_cover,
    build_template_agenda,
    build_template_body_flow,
    build_template_cards,
    build_template_comparison,
    build_template_dense_regression,
    build_template_metrics_matrix,
    build_template_table_process,
    template_style_profile,
    _extract_body_background,
    _extract_chrome,
    _fill_template_page,
    _fit_cover_slots,
    _normalize_table_fonts,
    _relabel,
    _visible_text,
)
from . import kimi_plan
from . import kimi_workspace
from . import visual_repair
from .kimi_plan import (
    audit_plan,
    clear_page_drafts as _clear_page_drafts,
    load_plan as _load_plan,
    plan_requirements,
    write_plan_deck as _write_plan_deck,
)
from .kimi_preview import DraftPreviewPublisher
from .kimi_workspace import (
    batch_page_target as _batch_page_target,
    collect_assets as _collect_assets,
    deck_progress as _deck_progress,
    deck_status as _deck_status,
    find_entry as _find_entry,
    issue_page as _issue_page,
    issue_summary as _issue_summary,
    load_deck as _load_deck,
    load_document as _load_document,
    page_hashes as _page_hashes,
    prepare_reference_material as _prepare_reference_material,
    prepare_template as _prepare_template,
    recovery_checkpoint,
    reply_text as _reply_text,
    restore_workspace,
    workspace as _workspace,
    write_deck as _write_deck,
)
from .kimi_prompts import (
    execution_message as _execution_message,
    page_layout_system_prompt as _page_layout_system_prompt,
    page_spec_message as _page_spec_message,
    planning_message as _planning_message,
    planning_system_prompt as _planning_system_prompt,
)
from . import layout_engine


ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = ROOT / "docs" / "kimi-ppt" / "original"
logger = logging.getLogger(__name__)


class PlanningDeadlineError(ValueError):
    failure_kind = "plan-timeout"


class PageWriteStalled(ValueError):
    """The page agent spent its exploration budget without writing a page."""


class PageWriteInvalid(ValueError):
    """A written page did not satisfy the authoritative PPTD schema."""

    def __init__(self, issues: dict[str, list[dict]]):
        self.issues = issues
        details = []
        for path, values in list(issues.items())[:4]:
            message = '；'.join(
                str(item.get('message') or item.get('code') or '结构无效')
                for item in values[:3]
            )
            details.append(f'{path}: {message}')
        super().__init__('页面结构校验失败：' + ' | '.join(details))


class PageGateFailed(ValueError):
    """A page did not pass structure, rendering and AI inspection."""


def _apply_host_transformed_page(
    entry: Path,
    document: dict,
    page_path: str,
    page_data: dict,
    template_info: dict | None,
) -> dict:
    """Apply host template layers to one page while preserving deck position."""
    if not template_info:
        return page_data
    working_document = deepcopy(document)
    working_pages = [
        {
            'path': path,
            'data': (
                deepcopy(page_data)
                if path == page_path
                else {'elements': []}
            ),
        }
        for path in document.get('pages', [])
        if isinstance(path, str)
    ]
    _apply_host_transforms(
        entry,
        working_document,
        working_pages,
        template_info,
    )
    selected = next(
        (
            page
            for page in working_pages
            if page.get('path') == page_path
        ),
        None,
    )
    if selected is None or not isinstance(selected.get('data'), dict):
        raise PageGateFailed(f'{page_path} 无法套用宿主模板')
    return selected['data']


async def _repair_page_until_pass(
    request,
    *,
    workspace: Path,
    entry: Path,
    deck_document: dict,
    page_path: str,
    page_index: int,
    page_data: dict,
    publisher: DraftPreviewPublisher,
    emit,
    max_repair_rounds: int,
) -> dict:
    """Fail closed until one page passes before the caller generates the next."""
    current = page_data
    trace = BenchmarkTrace.for_workspace(workspace, workspace.name)
    for attempt in range(max(0, int(max_repair_rounds)) + 1):
        checked = await publisher.check_page(page_index, page_path)
        if not checked.get('ok'):
            raise PageGateFailed(
                f'第 {page_index + 1} 页无法完成渲染检查：'
                f"{checked.get('error') or '未知渲染错误'}"
            )
        item = checked.get('item') or {}
        review_status = item.get('reviewStatus')
        issues = list(checked.get('issues') or [])
        trace.record_issues(
            'pageRepairGate',
            issues,
            pageIndex=page_index,
            pagePath=page_path,
            attempt=attempt,
            reviewStatus=review_status,
        )
        if review_status == 'unavailable':
            detail = item.get('reviewError') or '视觉模型不可用'
            raise PageGateFailed(
                f'第 {page_index + 1} 页 AI 检视未执行：{detail}'
            )
        if review_status == 'unreviewed' or review_status not in {
            'passed',
            'issues',
        }:
            raise PageGateFailed(
                f'第 {page_index + 1} 页没有获得有效的 AI 检视结论'
            )
        if review_status == 'passed' and not issues:
            await emit(
                'validate',
                'completed',
                f'第 {page_index + 1} 页已通过结构、渲染和 AI 检视。',
                pagePath=page_path,
                pageIndex=page_index,
                title=item.get('title'),
                issueCount=0,
            )
            return current
        if attempt >= max_repair_rounds:
            raise PageGateFailed(
                f'第 {page_index + 1} 页在 {attempt} 轮修复后仍未通过：'
                + kimi_workspace.issue_summary(issues, limit=8)
            )

        normalized = [
            {
                **issue,
                'page': issue.get('page') or page_path,
                'pagePath': issue.get('pagePath') or page_path,
            }
            for issue in issues
        ]
        if not normalized:
            raise PageGateFailed(
                f'第 {page_index + 1} 页检视判定未通过，但没有返回可修复的问题详情'
            )
        await emit(
            'validate',
            'retrying',
            f'第 {page_index + 1} 页检视发现 {len(normalized)} 项问题，'
            '正在修复；修复通过前不会生成下一页。',
            pagePath=page_path,
            pageIndex=page_index,
            title=item.get('title'),
            attempt=attempt + 1,
            issueCount=len(normalized),
        )
        state = {
            'document': {
                **deck_document,
                'pages': [page_path],
            },
            'pages': [{'path': page_path, 'data': current}],
        }

        async def repair_event(event):
            values = dict(event)
            await emit(
                values.pop('phase', 'visual'),
                values.pop('status', 'waiting'),
                values.pop('message', ''),
                **values,
            )

        with trace.span(
            'repairModel',
            pageIndex=page_index,
            pagePath=page_path,
            attempt=attempt + 1,
            issueCount=len(normalized),
        ):
            candidate = await visual_repair.repair_deck(
                request,
                state,
                normalized,
                on_event=repair_event,
                rendered_pages={
                    page_path: checked['image'].read_bytes(),
                },
                layout_measurements=checked.get('layouts') or {},
            )
        selected = next(
            (
                page
                for page in candidate.get('pages', [])
                if page.get('path') == page_path
            ),
            None,
        )
        if selected is None or not isinstance(selected.get('data'), dict):
            raise PageGateFailed(
                f'第 {page_index + 1} 页修复没有返回有效页面数据'
            )
        current = selected['data']
        structural = validate_document(
            {
                'document': {
                    **deck_document,
                    'pages': [page_path],
                },
                'pages': [{'path': page_path, 'data': current}],
            },
            resource_root=entry.parent,
        )
        if structural:
            raise PageGateFailed(
                f'第 {page_index + 1} 页修复引入了结构问题：'
                + kimi_workspace.issue_summary(structural, limit=8)
            )
        kimi_workspace.write_pages(
            entry,
            [{'path': page_path, 'data': current}],
        )
        await emit(
            'validate',
            'waiting',
            f'第 {page_index + 1} 页修复稿已写入，正在重新渲染并复审。',
            pagePath=page_path,
            pageIndex=page_index,
            title=item.get('title'),
            attempt=attempt + 1,
        )
    raise PageGateFailed(f'第 {page_index + 1} 页未通过单页门禁')


def _page_structure_issues(workspace: Path, relative: str) -> list[dict]:
    from pptd_engine.document import load_yaml

    entry = workspace / 'deck' / 'deck.pptd'
    target = workspace / relative
    try:
        document = load_yaml(entry)
        page_data = load_yaml(target)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
        return [{
            **kimi_workspace.yaml_parse_issue(relative, exc),
            'pagePath': Path(relative).relative_to('deck').as_posix(),
        }]
    page_path = Path(relative).relative_to('deck').as_posix()
    issues = validate_document(
        {
            'document': {**document, 'pages': [page_path]},
            'pages': [{'path': page_path, 'data': page_data}],
        },
        resource_root=entry.parent,
    )
    return [
        {
            **issue,
            'pagePath': page_path,
            'pageIndex': (
                document.get('pages', []).index(page_path)
                if page_path in document.get('pages', [])
                else None
            ),
        }
        for issue in issues
    ]


def _json_object(value: str) -> dict:
    text = str(value or '').strip()
    if text.startswith('```'):
        lines = text.splitlines()
        if lines and lines[0].startswith('```'):
            lines = lines[1:]
        if lines and lines[-1].strip() == '```':
            lines = lines[:-1]
        text = '\n'.join(lines).strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        start, end = text.find('{'), text.rfind('}')
        if start < 0 or end <= start:
            raise ValueError('模型没有返回 JSON 对象')
        parsed = json.loads(text[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError('模型返回的页面规格必须是 JSON 对象')
    return parsed


def _cli_env() -> dict:
    """Keep the repo importable when the CLI runs with the workspace as cwd."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _skill_text() -> str:
    return (ORIGINAL / "SKILL.md").read_text(encoding="utf-8")


def kimi_system_prompt(workspace: Path) -> str:
    """Layer the archived skill verbatim with a local host adapter."""
    adapter = f"""

HOST ADAPTER: local kimi-slides PPT expert
You are an agentic PPT expert in a local application. The archived Kimi
``kimi-slides`` skill above is your authoritative workflow; follow its steps
1-5 and all its rules. Local equivalents for the Kimi App private pieces:

- ``kimi_slides`` tool with subcommands convert / check / screenshot replaces
  the proprietary ``kimi-slides`` CLI. Never claim to run that private binary.
- The reference files (pptd.md, cli.md, slides_categories.md and the seven
  scenario guides, fonts.md, shapes.md) live under
  {ORIGINAL}. Read exactly the files you need with the Read tool; do not
  preload the whole 200KB.
- Your working directory is {workspace}. Produce the deck there with this
  layout: ``deck/deck.pptd`` (entry), ``deck/pages/*.page``, and
  ``deck/media/*`` for local images. Do not write to /tmp or /work.
- The deliverable is ONLY a brand-new deck under ``{workspace}/deck/``. Any
  converted template (``template-pptd/``) and its screenshots
  (``template-screenshots/``) are analysis material: never deliver them, and
  never copy the template's own text, data, page order or page count into the
  answer. Generate content for the user's request and reuse only the template's
  design language (colors, fonts, layout, reusable components).
- ``<KIMI_REF .../>`` is not emitted as text. Finishing with a valid deck under
  the working directory is how you deliver; the host commits it and shows the
  editable card. The host owns export and versioning, so do not run any PPTX
  export; PPTX is only exported from the editor.
- Research and media tools are not attached by default. Use only information
  the user supplied. Never invent URLs, data, cases, sources or local image
  paths. If a real image is needed and none was supplied, use a shape/icon/text
  composition and note the missing asset in your final reply.
- Validation loop: after writing pages, run ``kimi_slides check deck`` (with
  ``--level auto`` to apply deterministic fixes), then ``kimi_slides screenshot
  deck -o deck-screenshots`` and inspect the page PNGs with your image input
  when available. Fix the relevant ``.page`` files and re-check.
- When the host supplies a fixed page-writing batch, do not run the
  ``kimi_slides`` CLI or any screenshot loop. Write each requested page once and
  stop. The host validates, renders and reviews every page immediately after it
  is written.
- The host separates planning from writing. A planning call writes only the
  complete ``deck/plan.json`` and no page. Page-writing calls receive a fixed
  batch from that approved plan. A five-page write cap is never a five-page
  plan cap and must not shorten a long report.
- The host renders the finished deck in a real browser and blocks delivery on
  located overflow, overlap, clipping and text/background contrast failures.
  Text drawn with a colour that nearly matches what is behind it is a defect,
  not a style choice.
- Company template discipline: the template already owns its page background
  and its top-right and bottom-right corner marks, and the host re-applies them.
  Do not invent your own page background, gradient or bottom wave band, and
  never place your own footer, navigation, page number or section list in the
  right-hand corner band (roughly the right 25% of the width, top 12% or bottom
  15%): the company logo and name live there. If you want a running section
  label, keep it left of that band.
- Keep parallel siblings consistent: numbered steps, category chips and cards
  that sit in one row use the same fill colour. Do not fade one brand colour
  across a row; use one fill, and distinguish states with a single accent
  instead.
- Do not separate parallel items with decorative rule lines. If two blocks need
  to be told apart, use spacing, a card, or one accent; a full-width hairline
  between every list item reads as clutter, and reviewers reject it.
- Use one size for body-level text across a page, tables included: a list body
  at 12px next to a table body at 11px is an inconsistency, not a hierarchy.
  Headings may be larger; body copy should match.
- For a complete report or a long source document, preserve its major argument,
  not merely its topic. A professional deck normally needs a real cover, an agenda
  that matches the later sections, a progression through evidence and decisions,
  and a formal closing. Do not compress several major chapters into one generic
  summary page.
- Choose page count from the source hierarchy and reader workload. For structured
  long reports, a practical default is roughly 14–20 substantive pages; use more
  only when the source genuinely requires it. Do not compress principal sections
  merely to reduce pages, and do not inflate the deck with a standalone section
  divider for every chapter. The explicit user page count always wins.
- Before writing pages, turn the source outline into an argument map: what the
  reader must understand first, which facts/numbers/tables support each claim,
  and what decision or next action follows. Preserve concrete entities, dates,
  metrics, comparisons and source tables when they materially support a claim.
- Vary page structure according to the content: comparison matrix for alternatives,
  timeline for phases, process/diagram for architecture, table for structured data,
  numbered cards for parallel requirements, and lede plus evidence for arguments.
  Do not repeat one card/list layout for more than two consecutive pages.
- When the source contains a comparison, responsibility matrix, phased plan or
  structured project/accountability table, preserve its relationships as an
  editable table or matrix when that communicates the argument better than prose.
  Give the most decision-relevant metrics a clear visual anchor; never fabricate
  a number or turn a qualitative statement into measured data.
- Every page carries one controlling judgement. If a page needs more than one
  competing headline, split it or move secondary material to another page.
- Reference files may be long. Read `sources/reference-outline.md` first, then
  inspect relevant sections of `sources/reference.md` and the original file as
  needed. Never ignore a principal section because it is later in the document.
  The original file is authoritative if Markdown conversion appears incomplete.
- The source hierarchy is a hard classification contract. Keep every named
  entity, number, unit, date, project, category count and factual relationship
  exactly as supplied. Do not move a fact into a different subsection, merge
  distinct numbered categories, or change an enumerated list into a different
  count. When space is tight, compress wording without dropping the fact.
- Output exactly the deck files; the final assistant message should be a brief
  natural-language summary. No shell/code output is required in the reply.
"""
    return _skill_text() + adapter


def _cli_tool(workspace: Path) -> FunctionTool:
    def run(command: str, args: list[str]) -> str:
        if command not in ("convert", "check", "screenshot"):
            return "error: unknown kimi_slides command; use convert/check/screenshot"
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            return "error: args must be a list of strings"
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pptd_engine.cli", command, *args],
                cwd=str(workspace), env=_cli_env(), capture_output=True, text=True, timeout=360,
            )
        except subprocess.TimeoutExpired:
            return "error: kimi_slides timed out"
        except Exception as exc:  # pragma: no cover - defensive boundary
            return f"error: {exc}"
        output = (proc.stdout or "").strip()
        if proc.returncode:
            output = "\n".join(part for part in (output, proc.stderr or "") if part)
            return f"exit {proc.returncode}:\n{output}" if output else f"exit {proc.returncode}"
        return output or "(no output)"

    return FunctionTool(
        func=run,
        name="kimi_slides",
        description="Run the local kimi-slides CLI: convert a PPTX to PPTD, check a PPTD directory, or render screenshots. command is one of convert/check/screenshot; args are the remaining CLI arguments.",
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "enum": ["convert", "check", "screenshot"]},
                "args": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["command", "args"],
        },
        is_read_only=False,
        permission=PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="kimi_slides is always allowed inside the bounded agent workspace.",
        ),
    )


def _workspace_path(workspace: Path, file_path: str, *, restrict: bool):
    if not isinstance(file_path, str) or not file_path.strip():
        return None
    path = Path(file_path).expanduser()
    resolved = path.resolve() if path.is_absolute() else (workspace / path).resolve()
    if restrict:
        try:
            resolved.relative_to(workspace.resolve())
        except ValueError:
            return None
    return str(resolved)


class _WorkspaceRead(Read):
    """Read relative paths from the agent workspace without failed tool calls."""

    def __init__(self, workspace: Path):
        super().__init__()
        self.workspace = workspace

    async def call(
        self,
        file_path: str,
        offset: int = 1,
        limit: int = 2000,
        pages: str | None = None,
        _agent_state=None,
    ) -> ToolChunk:
        resolved = _workspace_path(self.workspace, file_path, restrict=False)
        if resolved is None:
            return ToolChunk(
                content=[TextBlock(text="Error: invalid file path")],
                state=ToolResultState.ERROR,
            )
        return await super().call(
            resolved,
            offset=offset,
            limit=limit,
            pages=pages,
            _agent_state=_agent_state,
        )


class _WorkspaceWrite(Write):
    """Write relative paths inside the agent workspace."""

    def __init__(
        self,
        workspace: Path,
        allowed_paths: set[str] | None = None,
        sealed_paths: set[str] | None = None,
    ):
        super().__init__()
        self.workspace = workspace
        self.allowed_paths = allowed_paths
        self.sealed_paths = sealed_paths

    async def call(self, file_path: str, content: str, _agent_state=None) -> ToolChunk:
        resolved = _workspace_path(self.workspace, file_path, restrict=True)
        relative = (
            Path(resolved).resolve().relative_to(self.workspace.resolve()).as_posix()
            if resolved is not None
            else ''
        )
        if (
            resolved is None
            or (
                self.allowed_paths is not None
                and relative not in self.allowed_paths
            )
        ):
            return ToolChunk(
                content=[
                    TextBlock(
                        text=(
                            "Error: file_path is not an allowed page in the "
                            f"current batch: {file_path}"
                        )
                    )
                ],
                state=ToolResultState.ERROR,
            )
        if self.sealed_paths is not None and relative in self.sealed_paths:
            return ToolChunk(
                content=[
                    TextBlock(
                        text=(
                            "Error: this page is already structurally valid. "
                            "Stop writing and finish the batch."
                        )
                    )
                ],
                state=ToolResultState.ERROR,
            )
        target = Path(resolved)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(
                f'.{target.name}.{uuid4().hex}.tmp'
            )
            temporary.write_text(content, encoding='utf-8')
            temporary.replace(target)
            if _agent_state is not None:
                await _agent_state.tool_context.cache_file(
                    file_path=str(target),
                    lines=content.splitlines(keepends=True),
                    mtime=target.stat().st_mtime,
                )
        except OSError as exc:
            return ToolChunk(
                content=[
                    TextBlock(text=f'Error: failed to write {relative}: {exc}')
                ],
                state=ToolResultState.ERROR,
            )
        return ToolChunk(
            content=[
                TextBlock(
                    text=(
                        f'The page {relative} has been written successfully '
                        f'({len(content.splitlines())} lines).'
                    )
                )
            ],
            state=ToolResultState.RUNNING,
            metadata={'file_path': str(target)},
        )


class _WorkspaceEdit(Edit):
    """Edit relative paths inside the agent workspace."""

    def __init__(self, workspace: Path, allowed_paths: set[str] | None = None):
        super().__init__()
        self.workspace = workspace
        self.allowed_paths = allowed_paths

    async def call(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        _agent_state=None,
    ) -> ToolChunk:
        resolved = _workspace_path(self.workspace, file_path, restrict=True)
        relative = (
            Path(resolved).resolve().relative_to(self.workspace.resolve()).as_posix()
            if resolved is not None
            else ''
        )
        if (
            resolved is None
            or (
                self.allowed_paths is not None
                and relative not in self.allowed_paths
            )
        ):
            return ToolChunk(
                content=[
                    TextBlock(
                        text=(
                            "Error: file_path is not an allowed page in the "
                            f"current batch: {file_path}"
                        )
                    )
                ],
                state=ToolResultState.ERROR,
            )
        return await super().call(
            resolved,
            old_string,
            new_string,
            replace_all=replace_all,
            _agent_state=_agent_state,
        )


def _make_agent(
    model,
    workspace: Path,
    session_id: str | None,
    max_iters: int,
    *,
    allowed_tools: tuple[str, ...] | None = None,
    extra_tools: dict | None = None,
    system_prompt: str | None = None,
    context_trigger_ratio: float = 0.9,
    allowed_page_paths: set[str] | None = None,
    sealed_page_paths: set[str] | None = None,
):
    available = {
        "Read": _WorkspaceRead(workspace),
        "Write": _WorkspaceWrite(
            workspace,
            allowed_page_paths,
            sealed_page_paths,
        ),
        "Edit": _WorkspaceEdit(workspace, allowed_page_paths),
        "Grep": Grep(),
        "Glob": Glob(),
        "Bash": Bash(cwd=str(workspace)),
        "kimi_slides": _cli_tool(workspace),
        **(extra_tools or {}),
    }
    selected = list(available) if allowed_tools is None else list(allowed_tools)
    unknown = set(selected) - set(available)
    if unknown:
        raise ValueError(f"未知的 PPT 专家工具：{', '.join(sorted(unknown))}")
    toolkit = Toolkit(
        tools=[available[name] for name in selected]
    )
    state = AgentState(session_id=session_id) if session_id else AgentState()
    state.permission_context.mode = PermissionMode.BYPASS
    return Agent(
        name="KimiSlidesExpert",
        system_prompt=system_prompt or kimi_system_prompt(workspace),
        model=model,
        toolkit=toolkit,
        middlewares=[TracingMiddleware()],
        state=state,
        react_config=ReActConfig(
            max_iters=max_iters,
            structured_output_grace_iters=2,
            interruption_raise_cancelled_error=True,
        ),
        context_config=ContextConfig(
            compression_tool_enabled=False,
            trigger_ratio=context_trigger_ratio,
            compression_fallback_to_truncation=False,
        ),
    )


async def _reply_with_progress(
    agent,
    message,
    *,
    emit,
    phase,
    activity,
    max_steps=14,
    workspace: Path | None = None,
    stop_when=None,
    tool_result_detail=None,
    no_progress_after: int | None = None,
    validate_write=None,
    sealed_page_paths: set[str] | None = None,
):
    """Consume AgentScope events into safe, user-visible progress updates."""
    if not hasattr(agent, 'reply_stream'):
        return await agent.reply(message)
    final = None
    model_calls = 0
    wrote_page = False
    attempted_page_write = False
    invalid_writes: dict[str, list[dict]] = {}
    model_started_at = None
    tool_names = {}
    tool_inputs: dict[str, str] = {}
    async for item in agent.reply_stream(message, yield_final_msg=True):
        if isinstance(item, Msg):
            final = item
        elif isinstance(item, ReplyStartEvent):
            await emit(
                phase,
                'waiting',
                f'{activity}：正在分析当前上下文',
                progress=1,
            )
        elif isinstance(item, ThinkingBlockStartEvent):
            await emit(
                phase,
                'waiting',
                f'{activity}：模型正在分析页面与检查结果',
            )
        elif isinstance(item, ModelCallStartEvent):
            model_calls += 1
            model_started_at = time.monotonic()
            await emit(
                phase,
                'waiting',
                f'{activity}：正在等待模型返回下一步（第 {model_calls} 次模型请求）',
                progress=min(95, round(model_calls / max(1, max_steps) * 100)),
            )
            if (
                no_progress_after is not None
                and model_calls > no_progress_after
                and not wrote_page
                and not attempted_page_write
            ):
                raise PageWriteStalled(
                    f'{activity}连续 {no_progress_after} 次模型请求未写入页面'
                )
        elif isinstance(item, ModelCallEndEvent):
            duration = (
                max(0.0, time.monotonic() - model_started_at)
                if model_started_at is not None
                else 0.0
            )
            usage = {
                "phase": phase,
                "request": model_calls,
                "durationSeconds": round(duration, 3),
                "inputTokens": int(item.input_tokens or 0),
                "outputTokens": int(item.output_tokens or 0),
                "cacheInputTokens": int(item.cache_input_tokens or 0),
                "cacheCreationInputTokens": int(
                    item.cache_creation_input_tokens or 0
                ),
            }
            logger.info("model_call %s", json.dumps(usage, ensure_ascii=False))
            if workspace is not None:
                try:
                    with (workspace / ".model-usage.jsonl").open(
                        "a", encoding="utf-8"
                    ) as stream:
                        stream.write(json.dumps(usage, ensure_ascii=False) + "\n")
                except OSError:
                    pass
        elif isinstance(item, ToolCallStartEvent):
            tool_names[item.tool_call_id] = item.tool_call_name
            tool_inputs[item.tool_call_id] = ''
            label = {
                'Read': '正在读取文件',
                'Write': '正在写入页面文件',
                'Edit': '正在修改页面文件',
                'Grep': '正在检索文件内容',
                'Glob': '正在定位文件',
                'Bash': '正在运行命令',
                'kimi_slides': '正在运行 PPT 检查或截图工具',
                'submit_plan': '正在提交并校验完整计划',
            }.get(item.tool_call_name, f'正在执行工具 {item.tool_call_name}')
            await emit(
                phase,
                'started',
                f'{activity}：{label}',
                progress=min(95, round(max(model_calls, 1) / max(1, max_steps) * 100)),
            )
        elif isinstance(item, ToolCallDeltaEvent):
            tool_inputs[item.tool_call_id] = (
                tool_inputs.get(item.tool_call_id, '') + item.delta
            )
        elif isinstance(item, ToolResultEndEvent):
            name = tool_names.get(item.tool_call_id, '工具')
            tool_state = str(getattr(item.state, 'value', item.state))
            tool_failed = tool_state in {
                ToolResultState.ERROR.value,
                ToolResultState.DENIED.value,
                ToolResultState.INTERRUPTED.value,
            }
            detail = _tool_detail(
                name,
                tool_inputs.pop(item.tool_call_id, ''),
                workspace,
            )
            if (
                name in ('Write', 'Edit')
                and not tool_failed
                and detail.startswith('deck/pages/')
                and detail.endswith('.page')
            ):
                attempted_page_write = True
                issues = (
                    validate_write(detail)
                    if validate_write is not None
                    else []
                )
                if issues:
                    invalid_writes[detail] = issues
                    wrote_page = False
                    await emit(
                        phase,
                        'retrying',
                        f'{activity}：{name} 已落盘，但 PPTD 结构无效'
                        f'（{len(issues)} 项）',
                        progress=min(
                            95,
                            round(
                                max(model_calls, 1)
                                / max(1, max_steps)
                                * 100
                            ),
                        ),
                        detail=detail,
                        issueCount=len(issues),
                    )
                else:
                    invalid_writes.pop(detail, None)
                    wrote_page = True
                    if sealed_page_paths is not None:
                        sealed_page_paths.add(detail)
            if tool_result_detail is not None:
                extra_detail = tool_result_detail(name)
                if extra_detail:
                    detail = "；".join(
                        value for value in (detail, extra_detail) if value
                    )
            detail_fields = {'detail': detail} if detail else {}
            await emit(
                phase,
                'retrying' if tool_failed else 'waiting',
                (
                    f'{activity}：{name} 执行失败，模型需要修正参数'
                    if tool_failed
                    else f'{activity}：{name} 执行完成，模型正在判断下一步'
                ),
                progress=min(95, round(max(model_calls, 1) / max(1, max_steps) * 100)),
                **detail_fields,
            )
            if stop_when is not None and stop_when():
                return True
        elif isinstance(item, ExceedMaxItersEvent):
            await emit(
                phase,
                'waiting',
                f'{activity}：本轮 Agent 步骤预算已用完，正在整理结果',
                progress=95,
            )
    if final is None:
        raise ValueError(f'{activity}没有返回可用的 Agent 结果。')
    if invalid_writes:
        raise PageWriteInvalid(invalid_writes)
    return final


class _PlanSubmitter:
    """Validate and persist a plan as soon as the model submits it."""

    def __init__(
        self,
        workspace: Path,
        requirements: dict,
        max_submissions: int = 2,
    ):
        self.workspace = workspace
        self.requirements = requirements
        self.max_submissions = max_submissions
        self.submissions = 0
        self.accepted = False
        self.last_issues: list[str] = []
        self.history: list[dict] = []
        self.tool = FunctionTool(
            func=self.submit,
            name="submit_plan",
            description=(
                "Validate and save the complete PPT page plan. Use this tool "
                "once with the full page plan. Prefer top-level pages, deckTitle, "
                "subtitle, date and size arguments; a nested plan object is also accepted. "
                "On rejection, repair the returned issues and submit the complete plan once more."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "plan": {
                        "description": "Optional nested complete plan object or JSON string.",
                    },
                    "deckTitle": {"type": "string"},
                    "subtitle": {"type": "string"},
                    "date": {"type": "string"},
                    "size": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                    "pages": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "title": {"type": "string"},
                                "readerTask": {"type": "string"},
                                "content": {"type": "string"},
                                "visualPlan": {"type": "string"},
                                "sourceSections": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "sourceSubsections": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                        },
                    },
                },
                "additionalProperties": True,
            },
            is_read_only=False,
            permission=PermissionDecision(
                behavior=PermissionBehavior.ALLOW,
                message="submit_plan is the only planning-stage write.",
            ),
        )

    @property
    def finished(self) -> bool:
        return self.accepted or self.submissions >= self.max_submissions

    def describe_result(self, tool_name: str) -> str:
        if tool_name != "submit_plan" or not self.history:
            return ""
        item = self.history[-1]
        state = "通过" if item.get("accepted") else "未通过"
        issues = item.get("issues") or []
        text = f"submit_plan 第 {item['attempt']} 次：{state}"
        if issues:
            text += "；" + "；".join(issues[:3])
            if len(issues) > 3:
                text += f"；另有 {len(issues) - 3} 项"
        elif item.get("pageCount"):
            text += f"，共 {item['pageCount']} 页"
        return text[:900]

    def _save_rejected(self, plan, issues: list[str]) -> None:
        target = (
            self.workspace
            / "deck"
            / f"plan.rejected-{self.submissions}.json"
        )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(
                    {"issues": issues, "plan": plan},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        except (OSError, TypeError, ValueError):
            pass

    def _record(self, accepted: bool, issues: list[str], plan=None) -> None:
        self.history.append(
            {
                "attempt": self.submissions,
                "accepted": accepted,
                "issues": list(issues),
                "pageCount": (
                    len(plan.get("pages", []))
                    if isinstance(plan, dict)
                    else 0
                ),
            }
        )

    def submit(
        self,
        plan=None,
        *,
        pages=None,
        deckTitle=None,
        subtitle=None,
        date=None,
        size=None,
        **_extra,
    ):
        if self.accepted:
            return json.dumps(
                {"accepted": True, "ignoredDuplicate": True},
                ensure_ascii=False,
            )
        if self.submissions >= self.max_submissions:
            return json.dumps(
                {
                    "accepted": False,
                    "issues": [f"最多提交 {self.max_submissions} 次完整计划"],
                },
                ensure_ascii=False,
            )
        self.submissions += 1
        if plan is None:
            nested = _extra.pop("plan", None)
            if nested is not None:
                plan = nested
            else:
                plan = {
                    key: value
                    for key, value in {
                        "deckTitle": deckTitle,
                        "subtitle": subtitle,
                        "date": date,
                        "size": size,
                        "pages": pages,
                    }.items()
                    if value is not None
                }
        elif pages is not None and isinstance(plan, dict):
            plan = {**plan, "pages": pages}
        if plan is None:
            plan = {
                "deckTitle": deckTitle,
                "subtitle": subtitle,
                "date": date,
                "size": size,
                "pages": pages,
            }
            plan = {key: value for key, value in plan.items() if value is not None}
        if isinstance(plan, str):
            try:
                plan = json.loads(plan)
            except ValueError:
                plan = None
        if isinstance(plan, list):
            plan = {"pages": plan}
        if isinstance(plan, dict) and isinstance(plan.get("plan"), (dict, str)):
            plan = plan["plan"]
            if isinstance(plan, str):
                try:
                    plan = json.loads(plan)
                except ValueError:
                    plan = None
        if isinstance(plan, dict) and isinstance(plan.get("pages"), str):
            try:
                plan = {**plan, "pages": json.loads(plan["pages"])}
            except ValueError:
                plan = {**plan, "pages": None}
        if not isinstance(plan, dict):
            self.last_issues = ["plan 必须是完整的 JSON 对象"]
            self._save_rejected(plan, self.last_issues)
            self._record(False, self.last_issues)
            return json.dumps(
                {"accepted": False, "issues": self.last_issues},
                ensure_ascii=False,
            )
        agenda_inserted = kimi_plan.ensure_required_agenda_page(
            plan,
            self.requirements,
        )
        fields_normalized = kimi_plan.normalize_plan_field_limits(
            plan,
            self.requirements,
        )
        kimi_plan.attach_source_details(plan, self.requirements)
        self.last_issues = audit_plan(plan, self.requirements)
        logger.info(
            "submit_plan attempt=%d accepted=%s issue_count=%d issues=%s",
            self.submissions,
            not self.last_issues,
            len(self.last_issues),
            json.dumps(self.last_issues[:12], ensure_ascii=False),
        )
        if self.last_issues:
            self._save_rejected(plan, self.last_issues)
            self._record(False, self.last_issues, plan)
            return json.dumps(
                {
                    "accepted": False,
                    "issues": self.last_issues[:30],
                    "remainingSubmissions": max(
                        0, self.max_submissions - self.submissions
                    ),
                },
                ensure_ascii=False,
            )
        target = self.workspace / "deck" / "plan.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
        self.accepted = True
        self._record(True, [], plan)
        return json.dumps(
            {
                "accepted": True,
                "pageCount": len(plan.get("pages", [])),
                "agendaInserted": agenda_inserted,
                "fieldsNormalized": fields_normalized,
            },
            ensure_ascii=False,
        )


def _tool_detail(name: str, raw_input: str, workspace: Path | None) -> str:
    """Describe only the concrete tool target, never tool result content."""
    try:
        payload = json.loads(raw_input or '{}')
    except (TypeError, ValueError):
        return ''
    if not isinstance(payload, dict):
        return ''

    def visible_path(value) -> str:
        text = str(value or '').strip()
        if not text:
            return ''
        path = Path(text)
        if workspace and path.is_absolute():
            try:
                return str(path.resolve().relative_to(workspace.resolve()))
            except (OSError, ValueError):
                return f'外部文件 · {path.name}'
        return text

    def short(value, limit=160) -> str:
        text = ' '.join(str(value or '').split())
        return text if len(text) <= limit else text[: limit - 1] + '…'

    path = visible_path(payload.get('file_path') or payload.get('path'))
    if name in ('Read', 'Write', 'Edit'):
        return path
    if name == 'Grep':
        pattern = short(payload.get('pattern'), 80)
        return ' · '.join(item for item in (f'检索 “{pattern}”' if pattern else '', path) if item)
    if name == 'Glob':
        pattern = short(payload.get('pattern'), 80)
        return ' · '.join(item for item in (f'匹配 {pattern}' if pattern else '', path) if item)
    if name == 'Bash':
        description = short(payload.get('description'), 120)
        command = short(payload.get('command'), 160)
        return ' · '.join(item for item in (description, command) if item)
    if name == 'kimi_slides':
        command = str(payload.get('command') or '').strip()
        args = payload.get('args')
        rendered = ' '.join(str(item) for item in args) if isinstance(args, list) else ''
        return short(f'{command} {rendered}'.strip())
    return path


def _run_cli(command: str, args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        [sys.executable, "-m", "pptd_engine.cli", command, *args],
        cwd=str(cwd), env=_cli_env(), capture_output=True, text=True, timeout=900,
    )
    if proc.returncode:
        raise ValueError(f"模板预处理 {command} 失败：{(proc.stderr or proc.stdout)[-2000:]}")
    return proc.stdout or ""


def _prepare_template_analysis(
    workspace: Path,
    template_pptx: Path,
    *,
    screenshots: bool = True,
) -> dict:
    """Return content-addressed template conversion and optional screenshots."""
    return template_cache.prepare(
        workspace.parents[2],
        template_pptx,
        screenshots=screenshots,
    )


def _single_page_cover_content(request) -> dict | None:
    """Extract an explicit one-page cover contract from labelled user text."""
    if getattr(request, 'pageCount', None) != 1:
        return None
    prompt = str(getattr(request, 'prompt', '') or '')
    if '封面' not in prompt:
        return None
    labels = {
        '主标题': 'title',
        '副标题': 'subtitle',
        '汇报单位': 'organization',
        '日期': 'date',
    }
    values = {}
    for line in prompt.splitlines():
        match = re.match(
            r'^\s*(主标题|副标题|汇报单位|日期)\s*[：:]\s*(.+?)\s*$',
            line,
        )
        if match:
            values[labels[match.group(1)]] = match.group(2)
    if not values.get('title'):
        return None
    values['designContract'] = [
        '封面必须复用公司模板封面。',
        '主标题、副标题、单位和日期保留原文并各出现一次。',
        '标题不得与副标题或日期重叠。',
    ]
    return values


def _single_page_closing_content(request) -> dict | None:
    """Extract an explicit one-page closing contract from labelled user text."""
    if getattr(request, 'pageCount', None) != 1:
        return None
    prompt = str(getattr(request, 'prompt', '') or '')
    if '结束页' not in prompt:
        return None
    match = re.search(
        r'^\s*主文字\s*[：:]\s*(.+?)\s*$',
        prompt,
        re.MULTILINE,
    )
    if not match:
        return None
    text = match.group(1).strip()
    if not any(
        marker in text.lower()
        for marker in ('感谢', '谢谢', '请领导', '敬请', 'thank you', 'thanks', 'q&a')
    ):
        return None
    return {
        'title': text,
        'designContract': [
            '结束页必须复用公司模板结束页。',
            '主文字保留原文且只出现一次。',
            '不得追加第二个结束页或致谢页。',
        ],
    }


def _single_page_agenda_content(request) -> dict | None:
    """Extract an explicit one-page agenda from numbered prompt lines."""
    if getattr(request, 'pageCount', None) != 1:
        return None
    prompt = str(getattr(request, 'prompt', '') or '')
    if '目录' not in prompt:
        return None
    items = []
    for line in prompt.splitlines():
        match = re.match(r'^\s*\d+\s*[.、]\s*(.+?)\s*$', line)
        if match:
            value = match.group(1).strip()
            if value and value not in items:
                items.append(value)
    if len(items) < 2:
        return None
    return {
        'title': '目录',
        'items': items,
        'designContract': [
            '目录必须复用公司模板目录行样式。',
            '章节编号和标题按输入顺序显示。',
            '全部目录项不得压住公司页脚。',
        ],
    }


def _single_page_cards_content(request) -> dict | None:
    """Extract an explicit one-page equal-card contract."""
    if getattr(request, 'pageCount', None) != 1:
        return None
    prompt = str(getattr(request, 'prompt', '') or '')
    if not ('目标卡' in prompt or ('三张' in prompt and '卡片' in prompt)):
        return None
    title_match = re.search(
        r'页面标题\s*[：:]\s*([^。\n]+)',
        prompt,
    )
    items = []
    for line in prompt.splitlines():
        match = re.match(r'^\s*\d+\s*[.、]\s*(.+?)\s*$', line)
        if match:
            value = match.group(1).strip()
            if value and value not in items:
                items.append(value)
    if not title_match or len(items) < 2:
        return None
    return {
        'title': title_match.group(1).strip(),
        'items': items,
        'designContract': [
            '三项必须使用等宽、等高等间距的横向卡片。',
            '每张卡片按编号和目标标题垂直排列。',
            '不得移除卡片或改成平均分栏以外的非卡片布局。',
        ],
    }


def _comparison_items(value: str) -> list[str]:
    return [
        item.strip().rstrip('。')
        for item in re.split(r'[；;]', value)
        if item.strip().rstrip('。')
    ]


def _single_page_comparison_content(request) -> dict | None:
    """Extract an explicit one-page two-column comparison contract."""
    if getattr(request, 'pageCount', None) != 1:
        return None
    prompt = str(getattr(request, 'prompt', '') or '')
    if '双栏' not in prompt or '底部结论' not in prompt:
        return None
    title_match = re.search(
        r'页面标题\s*[：:]\s*([^。\n]+)',
        prompt,
    )
    left_match = re.search(
        r'左栏[“"]([^”"]+)[”"]\s*[：:]\s*([^\n]+)',
        prompt,
    )
    right_match = re.search(
        r'右栏[“"]([^”"]+)[”"]\s*[：:]\s*([^\n]+)',
        prompt,
    )
    conclusion_match = re.search(
        r'底部结论\s*[：:]\s*([^。\n]+)',
        prompt,
    )
    if not all((title_match, left_match, right_match, conclusion_match)):
        return None
    left_items = _comparison_items(left_match.group(2))
    right_items = _comparison_items(right_match.group(2))
    if not left_items or not right_items:
        return None
    return {
        'title': title_match.group(1).strip(),
        'leftTitle': left_match.group(1).strip(),
        'leftItems': left_items,
        'rightTitle': right_match.group(1).strip(),
        'rightItems': right_items,
        'conclusion': conclusion_match.group(1).strip(),
        'designContract': [
            '页面必须使用左右双栏范围对比布局。',
            '左右两栏卡片必须底部对齐。',
            '底部验收结论必须独立成条，不覆盖正文或页脚。',
            '保留全部范围和结论文字。',
        ],
    }


def _single_page_metrics_content(request) -> dict | None:
    """Extract an explicit KPI + 2x2 matrix + conclusion page."""
    if getattr(request, 'pageCount', None) != 1:
        return None
    prompt = str(getattr(request, 'prompt', '') or '')
    if 'KPI' not in prompt or '2×2 矩阵' not in prompt or '结论' not in prompt:
        return None
    title_match = re.search(
        r'页面标题\s*[：:]\s*([^。\n]+)',
        prompt,
    )
    kpi_match = re.search(
        r'KPI\s*[：:]\s*([^\n]+)',
        prompt,
    )
    matrix_match = re.search(
        r'2×2\s*矩阵\s*[：:]\s*([^\n]+)',
        prompt,
    )
    conclusion_match = re.search(
        r'结论\s*[：:]\s*([^。\n]+)',
        prompt,
    )
    if not all((title_match, kpi_match, matrix_match, conclusion_match)):
        return None
    kpis = _comparison_items(kpi_match.group(1))
    matrix_items = _comparison_items(matrix_match.group(1))
    if len(kpis) != 3 or len(matrix_items) != 4:
        return None
    return {
        'title': title_match.group(1).strip(),
        'kpis': kpis,
        'matrixItems': matrix_items,
        'conclusion': conclusion_match.group(1).strip(),
        'designContract': [
            '三个 KPI 卡必须等宽并对齐。',
            '四个矩阵单元格必须按 2×2 排列且等宽等高。',
            '底部结论独立显示，不覆盖矩阵或页脚。',
            '保留全部 KPI 原文、矩阵标题和结论。',
        ],
    }


def _single_page_table_process_content(request) -> dict | None:
    """Extract an explicit table + three-stage process + request page."""
    if getattr(request, 'pageCount', None) != 1:
        return None
    prompt = str(getattr(request, 'prompt', '') or '')
    if '表格' not in prompt or '流程' not in prompt or '请示框' not in prompt:
        return None
    title_match = re.search(
        r'页面标题\s*[：:]\s*([^。\n]+)',
        prompt,
    )
    stage_match = re.search(
        r'流程\s*[：:]\s*([^\n]+)',
        prompt,
    )
    request_match = re.search(
        r'请示框\s*[：:]\s*([^。\n]+)',
        prompt,
    )
    rows = []
    for line in prompt.splitlines():
        if line.count('|') != 2 or line.lstrip().startswith('表格'):
            continue
        values = [value.strip() for value in line.split('|')]
        if all(values):
            rows.append(values)
    if not all((title_match, stage_match, request_match)) or len(rows) != 3:
        return None
    stages = [
        value.strip().rstrip('。')
        for value in re.split(r'→', stage_match.group(1))
        if value.strip().rstrip('。')
    ]
    if len(stages) != 3:
        return None
    return {
        'title': title_match.group(1).strip(),
        'columns': ['任务', '关键动作', '阶段'],
        'rows': rows,
        'stages': stages,
        'requestText': request_match.group(1).strip(),
        'designContract': [
            '表格必须包含三列且保留全部单元格文字。',
            '流程必须显示为三个节点和两个箭头。',
            '请示框必须作为唯一视觉强调区。',
            '表格、流程和请示框不得相互覆盖或压住页脚。',
        ],
    }


def _single_page_dense_regression_content(request) -> dict | None:
    """Extract the explicit high-density diagnostic page contract."""
    if getattr(request, 'pageCount', None) != 1:
        return None
    prompt = str(getattr(request, 'prompt', '') or '')
    if (
        '高密度信息场景' not in prompt
        or '指标区标题' not in prompt
        or '对照表' not in prompt
    ):
        return None
    title_match = re.search(r'页面标题\s*[：:]\s*([^\n]+)', prompt)
    subtitle_match = re.search(r'副标题\s*[：:]\s*([^\n]+)', prompt)
    core_match = re.search(r'核心判断条\s*[：:]\s*([^\n]+)', prompt)
    metrics_title_match = re.search(
        r'指标区标题\s*[：:]\s*([^\n]+)',
        prompt,
    )
    header_match = re.search(r'表头为[“"]([^”"]+)[”"]', prompt)
    if not all((
        title_match,
        subtitle_match,
        core_match,
        metrics_title_match,
        header_match,
    )):
        return None
    stages = []
    metrics = []
    table_rows = []
    in_metrics = False
    in_table = False
    for line in prompt.splitlines():
        stripped = line.strip()
        if stripped.startswith('- ') and '｜' in stripped:
            stages.append(stripped[2:].strip())
            continue
        if stripped.startswith('四张等宽指标卡'):
            in_metrics = True
            in_table = False
            continue
        if '表头为' in stripped:
            in_metrics = False
            in_table = True
            continue
        metric_match = re.match(
            r'^\s*\d+\.\s*([^|]+?)\s*\|\s*(.+?)\s*$',
            line,
        )
        if in_metrics and metric_match:
            metrics.append({
                'value': metric_match.group(1).strip(),
                'label': metric_match.group(2).strip(),
            })
            continue
        if in_table and line.count('|') == 2:
            values = [value.strip() for value in line.split('|')]
            if all(values):
                table_rows.append(values)
    if (
        len(stages) != 3
        or len(metrics) != 4
        or len(table_rows) != 3
    ):
        return None
    columns = [
        value.strip()
        for value in re.split(r'[、，,]', header_match.group(1))
        if value.strip()
    ]
    if len(columns) != 3:
        return None
    return {
        'title': title_match.group(1).strip(),
        'subtitle': subtitle_match.group(1).strip(),
        'stages': stages,
        'coreJudgment': core_match.group(1).strip(),
        'metricsTitle': metrics_title_match.group(1).strip(),
        'metrics': metrics,
        'tableColumns': columns,
        'tableRows': table_rows,
        'designContract': [
            '本页是高密度诊断页，必须完整保留全部文字、数字、单位、日期和专有名词。',
            '左侧显示三阶段状态和核心判断，右侧显示四张等宽指标卡和四行三列对照表。',
            '指标卡不得出现孤字换行，表格不得越界、裁切或重叠。',
            '只允许一个标题，不得追加页面。',
        ],
        'visualMaxTokens': 2048,
    }


def planned_page_role(index: int, total: int, page: dict, plan: dict) -> str:
    """Classify a planned page without relying on the single-page request shape."""
    if total < 2:
        return 'content'
    title = str(page.get('title') or '')
    visual = str(page.get('visualPlan') or '')
    content = str(page.get('content') or '')
    combined = f'{title} {visual} {content}'.lower()
    if index == 0 and (
        title == str(plan.get('deckTitle') or '')
        or '封面' in combined
        or 'cover' in combined
    ):
        return 'cover'
    if index == total - 1 and any(
        marker in combined
        for marker in (
            '感谢',
            '谢谢',
            '请领导',
            '敬请',
            '结束页',
            '收尾页',
            'q&a',
            'thank you',
        )
    ):
        return 'closing'
    if index <= 2 and any(
        marker in combined
        for marker in ('目录', '提纲', '论证主线', 'agenda')
    ):
        return 'agenda'
    return 'content'


def planned_body_layout(page: dict) -> str:
    """Choose a deterministic body composition from the approved design brief."""
    brief = (
        f"{page.get('title') or ''} "
        f"{page.get('visualPlan') or ''} "
        f"{page.get('content') or ''}"
    ).lower()
    if any(
        marker in brief
        for marker in ('双栏', '左右', '两侧', '对比', '对照')
    ):
        return 'columns'
    if any(
        marker in brief
        for marker in (
            '卡片',
            '宫格',
            '象限',
            '矩阵',
            '横排',
            '三列',
            '双列',
            '谱系',
            '积木',
        )
    ):
        return 'cards'
    return 'editorial'


def planned_layout_archetype(page: dict) -> str:
    """Recommend a semantic composition without constraining the page model."""
    brief = (
        f"{page.get('title') or ''} "
        f"{page.get('visualPlan') or ''} "
        f"{page.get('content') or ''}"
    ).lower()
    rules = (
        (
            'timeline',
            ('时间轴', '时间线', '阶梯', '分阶段', '演进轴', '路线图'),
        ),
        ('table', ('表格', '对照表', '数据表', '矩阵表')),
        (
            'process',
            ('流程', '箭头', '步骤', '推进路径', '实施路径'),
        ),
        (
            'comparison',
            ('双栏', '左右', '两侧', '对比', '对照', '优劣'),
        ),
        (
            'diagram',
            ('架构', '积木', '链路', '金字塔', '分层', '堆叠', '能力地图'),
        ),
        (
            'matrix',
            ('矩阵', '象限', '宫格', '四宫格', '九宫格'),
        ),
        (
            'metrics',
            ('指标', 'kpi', '数字卡', '数据锚点', '数据条'),
        ),
        ('cards', ('卡片', '横排', '三列', '双列', '谱系')),
    )
    for archetype, markers in rules:
        if any(marker in brief for marker in markers):
            return archetype
    return 'editorial'


def _plain_page_text(page_data: dict) -> str:
    values = []
    for element in page_data.get('elements') or []:
        if not isinstance(element, dict):
            continue
        if element.get('elementType') == 'text':
            content = element.get('content')
            if isinstance(content, dict):
                values.append(
                    html.unescape(
                        re.sub(r'<[^>]+>', ' ', str(content.get('text') or ''))
                    )
                )
        elif element.get('elementType') == 'table':
            for row in element.get('rows') or []:
                for cell in row or []:
                    if isinstance(cell, dict):
                        values.append(str(cell.get('text') or ''))
    return ' '.join(values)


def content_layout_issues(
    page_data: dict,
    page_plan: dict,
    layout_history: list[dict] | None = None,
) -> list[dict]:
    """Reject body pages that lose planned data or repeat a stale composition."""
    issues = []
    path = str(page_data.get('meta', {}).get('sourcePath') or '')
    text = re.sub(r'\s+', '', _plain_page_text(page_data))
    expected_numbers = numeric_tokens(str(page_plan.get('content') or ''))
    missing_numbers = [
        token
        for token in expected_numbers
        if token and token not in text
    ]
    if missing_numbers:
        issues.append({
            'code': 'planned-number-missing',
            'location': f'{path}.elements',
            'message': (
                '正文布局遗漏了计划中的数字或单位：'
                + '、'.join(missing_numbers[:12])
                + '。必须回到 page.content 和 sourceEvidence 补全，不能删减数据。'
            ),
            'severity': 'error',
        })
    elements = [
        element
        for element in page_data.get('elements') or []
        if isinstance(element, dict)
    ]
    content_elements = [
        element
        for element in elements
        if element.get('elementId') not in {'page-title', 'page-subtitle'}
    ]
    rich_table = any(
        element.get('elementType') == 'table'
        and isinstance(element.get('rows'), list)
        and len(element['rows']) >= 4
        for element in content_elements
    )
    if (
        len(str(page_plan.get('content') or '')) >= 80
        and len(elements) < 8
        and not rich_table
    ):
        issues.append({
            'code': 'planned-layout-too-sparse',
            'location': f'{path}.elements',
            'message': (
                '正文页布局过稀。普通正文页至少需要八个可见元素，'
                '或使用至少四行的结构化表格；请用分区标题、证据、'
                '指标、连接关系或结论承载计划内容。'
            ),
            'severity': 'error',
        })
    family = layout_engine.layout_family(page_data)
    previous = list(layout_history or [])[-2:]
    if (
        len(previous) == 2
        and all(isinstance(item, dict) for item in previous)
        and all(item.get('family') == family for item in previous)
    ):
        issues.append({
            'code': 'layout-repetition',
            'location': f'{path}.elements',
            'message': (
                f'连续三页使用“{family}”构图，版面节奏过于单一。'
                '请保持全部内容不变，改用与最近两页不同的结构。'
            ),
            'severity': 'error',
        })
    return issues


def _planned_agenda_items(
    page: dict,
    requirements: dict,
) -> list[str]:
    values = [
        str(value).strip()
        for value in requirements.get('principalSections') or []
        if str(value).strip()
    ]
    if 2 <= len(values) <= 8:
        return [
            re.sub(
                r'^\s*[0-9]*[一二三四五六七八九十百]+[、.．]\s*',
                '',
                value,
            ).strip()
            for value in values
        ]
    numbered = re.findall(
        r'[①②③④⑤⑥⑦⑧⑨⑩]\s*([^；;。\n]+)',
        str(page.get('content') or ''),
    )
    if 2 <= len(numbered) <= 8:
        return [value.strip() for value in numbered]
    chinese_numbered = re.findall(
        r'(?:^|[。；;\n])\s*[一二三四五六七八九十百]+、\s*'
        r'([^：:。；;\n]+)',
        str(page.get('content') or ''),
    )
    if 2 <= len(chinese_numbered) <= 8:
        return [value.strip() for value in chinese_numbered]
    values = [
        re.sub(
            r'^\s*[0-9]*[一二三四五六七八九十百]+[、.．]\s*',
            '',
            part,
        ).strip()
        for part in re.split(r'[；;\n]', str(page.get('content') or ''))
    ]
    return [
        value.split('（', 1)[0].strip()
        for value in values
        if value
    ][:8]


def build_planned_role_page(
    *,
    index: int,
    total: int,
    page: dict,
    plan: dict,
    requirements: dict,
    template_info: dict | None,
    deck_dir: Path,
    style_profile: dict | None = None,
    deterministic_content: bool = False,
) -> tuple[str, dict] | None:
    """Build a planned cover/agenda/closing page directly from its plan role."""
    role = planned_page_role(index, total, page, plan)
    if not template_info:
        return None
    template_dir = Path(template_info['pptd'])
    if role == 'content':
        if not deterministic_content:
            return None
        content = str(page.get('content') or '').strip()
        if not content:
            return None
        data = build_template_body_flow(
            template_dir,
            deck_dir,
            title=str(page.get('title') or '正文'),
            subtitle=str(page.get('readerTask') or plan.get('subtitle') or ''),
            content=content,
            style_profile=style_profile,
            layout=planned_body_layout(page),
        )
    elif role == 'cover':
        data = build_template_cover(
            template_dir,
            deck_dir,
            title=str(page.get('title') or plan.get('deckTitle') or '演示文稿'),
            subtitle=str(plan.get('subtitle') or page.get('subtitle') or ''),
            organization=str(
                plan.get('organization')
                or page.get('organization')
                or ''
            ),
            date=str(plan.get('date') or page.get('date') or ''),
        )
    elif role == 'agenda':
        items = _planned_agenda_items(page, requirements)
        if len(items) < 2:
            return None
        data = build_template_agenda(
            template_dir,
            deck_dir,
            items=items,
        )
    else:
        data = build_template_closing(
            template_dir,
            deck_dir,
            text=str(page.get('title') or '感谢观看'),
        )
    return role, data


def planned_role_visual_contract(
    role: str,
    layout: str = 'editorial',
) -> list[str]:
    """Visual contract carried into page reviews for planned template roles."""
    if role == 'cover':
        return [
            '封面必须复用公司模板封面。',
            '主标题、副标题及可用日期各出现一次。',
            '标题不得与副标题或日期重叠。',
        ]
    if role == 'agenda':
        return [
            '目录必须复用公司模板目录行样式。',
            '章节编号和标题按计划顺序显示。',
            '全部目录项不得压住公司页脚。',
        ]
    if role == 'closing':
        return [
            '结束页必须复用公司模板结束页。',
            '主文字保留原文且只出现一次。',
            '不得追加第二个结束页或致谢页。',
        ]
    if role == 'content':
        layout_text = {
            'cards': '正文采用模板风格卡片网格，卡片不能遮挡固定装饰。',
            'columns': '正文采用模板风格双栏对照，左右内容不得串栏。',
            'editorial': '正文采用模板风格纵向编辑式信息流。',
        }.get(layout, '正文采用模板风格信息流。')
        return [
            '正文使用模板正文背景、页脚和右上角装饰。',
            layout_text,
            '计划中的全部文字、数字和专有名词必须保留。',
        ]
    return []


async def _run_single_page_template_fast_path(
    request,
    *,
    data_root: Path,
    workspace: Path,
    template_info: dict,
    content: dict,
    page_kind: str,
    trace: BenchmarkTrace,
    progress,
    on_event,
    project_id: str,
) -> dict:
    """Build one template page without planning or a page model."""
    from pptd_engine.document import load_yaml
    from .pptd_store import DocumentStore

    async def emit(phase, status, message, **fields):
        if on_event:
            result = on_event({
                'phase': phase,
                'status': status,
                'message': message,
                **fields,
            })
            if asyncio.iscoroutine(result):
                await result

    page_label = {
        'cover': '封面',
        'closing': '结束页',
        'agenda': '目录',
        'cards': '目标卡页',
        'comparison': '双栏对比页',
        'metrics': 'KPI矩阵页',
        'tableProcess': '表格流程页',
        'denseRegression': '高密度回归页',
    }[page_kind]
    if progress:
        progress(5, f'单页快速路径：复用模板{page_label}')
    await emit(
        'context',
        'completed',
        f'已读取单页{page_label}内容和固定模板。',
    )
    await emit(
        'plan',
        'completed',
        f'单页{page_label}计划已固定：1 页。',
        covered=0,
        total=1,
    )
    template_entry = Path(template_info['entry'])
    template_document = load_yaml(template_entry)
    page_path = (
        'pages/cover.page'
        if page_kind == 'cover'
        else 'pages/closing.page'
        if page_kind == 'closing'
        else 'pages/cards.page'
        if page_kind == 'cards'
        else 'pages/comparison.page'
        if page_kind == 'comparison'
        else 'pages/metrics.page'
        if page_kind == 'metrics'
        else 'pages/table-process.page'
        if page_kind == 'tableProcess'
        else 'pages/dense-regression.page'
        if page_kind == 'denseRegression'
        else 'pages/agenda.page'
    )
    document = {
        'version': 'v2',
        'title': content['title'],
        'size': template_document.get('size', [960, 540]),
        'theme': deepcopy(template_document.get('theme') or {}),
        'pages': [page_path],
    }
    strategy = f'template-{page_kind}'
    with trace.span('hostTransform', scope='page', strategy=strategy):
        if page_kind == 'cover':
            page = build_template_cover(
                Path(template_info['pptd']),
                workspace / 'deck',
                title=content['title'],
                subtitle=content.get('subtitle', ''),
                organization=content.get('organization', ''),
                date=content.get('date', ''),
            )
        elif page_kind == 'closing':
            page = build_template_closing(
                Path(template_info['pptd']),
                workspace / 'deck',
                text=content['title'],
            )
        elif page_kind == 'agenda':
            page = build_template_agenda(
                Path(template_info['pptd']),
                workspace / 'deck',
                items=content['items'],
            )
        elif page_kind == 'cards':
            page = build_template_cards(
                Path(template_info['pptd']),
                workspace / 'deck',
                title=content['title'],
                items=content['items'],
            )
        elif page_kind == 'comparison':
            page = build_template_comparison(
                Path(template_info['pptd']),
                workspace / 'deck',
                title=content['title'],
                left_title=content['leftTitle'],
                left_items=content['leftItems'],
                right_title=content['rightTitle'],
                right_items=content['rightItems'],
                conclusion=content['conclusion'],
            )
        elif page_kind == 'metrics':
            page = build_template_metrics_matrix(
                Path(template_info['pptd']),
                workspace / 'deck',
                title=content['title'],
                kpis=content['kpis'],
                matrix_items=content['matrixItems'],
                conclusion=content['conclusion'],
            )
        elif page_kind == 'tableProcess':
            page = build_template_table_process(
                Path(template_info['pptd']),
                workspace / 'deck',
                title=content['title'],
                columns=content['columns'],
                rows=content['rows'],
                stages=content['stages'],
                request_text=content['requestText'],
            )
        else:
            page = build_template_dense_regression(
                Path(template_info['pptd']),
                workspace / 'deck',
                title=content['title'],
                subtitle=content['subtitle'],
                stages=content['stages'],
                core_judgment=content['coreJudgment'],
                metrics_title=content['metricsTitle'],
                metrics=content['metrics'],
                table_columns=content['tableColumns'],
                table_rows=content['tableRows'],
            )
    pages = [{'path': page_path, 'data': page}]
    if page_kind in {
        'agenda',
        'cards',
        'comparison',
        'metrics',
        'tableProcess',
        'denseRegression',
    }:
        with trace.span('hostTransform', scope='page', strategy='agenda-chrome'):
            template_dir = Path(template_info['pptd'])
            _apply_chrome(
                workspace / 'deck',
                document,
                pages,
                _extract_chrome(template_dir),
                template_dir,
            )
    with trace.span('compile', scope='page', strategy=strategy):
        entry = kimi_workspace.write_deck(
            workspace / 'deck',
            template_entry,
            document,
            pages,
        )
    with trace.span('structureValidation', scope='page'):
        structural = validate_document(
            {'document': document, 'pages': pages},
            resource_root=entry.parent,
        )
    trace.record_issues('pageStructure', structural, pagePath=page_path)
    if structural:
        raise PageGateFailed(
            f'单页{page_label}结构无效：'
            + kimi_workspace.issue_summary(structural, limit=8)
        )
    await emit(
        'pages',
        'completed',
        f'第 1 页{page_label}已从模板生成。',
        pagePath=page_path,
        pageIndex=0,
        title=content['title'],
    )
    await emit(
        'validate',
        'started',
        f'正在检查{page_label}渲染与几何边界。',
    )
    with trace.span('browserRender', scope='page'):
        rendered = await asyncio.to_thread(_render_pages, entry)
    if rendered is None:
        raise PageGateFailed('单页模板渲染不可用。')
    try:
        with trace.span('browserLayout', scope='page'):
            issues = await asyncio.to_thread(
                final_layout_issues,
                entry,
                document,
                pages,
                rendered,
            )
        trace.record_issues('deckLayout', issues, pagePath=page_path)
        visual = []
        if not issues:
            with trace.span('visionReview', scope='page'):
                visual = await visual_overview_issues(
                document,
                pages,
                rendered,
                on_event=on_event,
                    scenario=getattr(request, 'scenario', None) or 'auto',
                    cache_dir=data_root / 'review-cache',
                    design_contract=content.get('designContract'),
                    max_tokens=content.get('visualMaxTokens'),
                )
            trace.record_issues(
                'visionReview',
                visual,
                pagePath=page_path,
            )
        issues = [*issues, *visual]
        preview_dir = workspace / 'draft-previews'
        preview_dir.mkdir(parents=True, exist_ok=True)
        rendered_page = rendered / 'pages' / 'page-1.png'
        preview_path = preview_dir / 'page-1.png'
        if rendered_page.is_file():
            shutil.copy2(rendered_page, preview_path)
            review_status = 'issues' if issues else 'passed'
            manifest = {
                'status': 'draft',
                'completedPages': 1,
                'reviewedPages': int(review_status == 'passed'),
                'issuePages': int(review_status == 'issues'),
                'totalPages': 1,
                'updatedAt': time.time(),
                'pages': [{
                    'path': page_path,
                    'index': 0,
                    'title': content['title'],
                    'status': 'ready',
                    'previewFile': 'page-1.png',
                    'text': content['title'],
                    'reviewStatus': review_status,
                    'issueCount': len(issues),
                    'issues': [
                        {
                            key: issue.get(key)
                            for key in (
                                'code',
                                'elementId',
                                'location',
                                'severity',
                                'message',
                            )
                            if issue.get(key) is not None
                        }
                        for issue in issues[:20]
                    ],
                    'reviewModel': getattr(request, 'visionModel', ''),
                }],
            }
            (workspace / '.draft-preview.json').write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
                encoding='utf-8',
            )
    finally:
        shutil.rmtree(rendered, ignore_errors=True)

    store = DocumentStore(data_root)
    revision = store.read(project_id)['revision'] if store.exists(project_id) else 0
    committed = store.commit(
        project_id,
        document,
        pages,
        f'单页{page_label}快速生成',
        revision,
        assets=kimi_workspace.collect_assets(entry),
    )
    warnings = [
        {
            'location': issue.get('location'),
            'code': issue.get('code'),
            'message': issue.get('message'),
        }
        for issue in issues
    ]
    if issues:
        await emit(
            'validate',
            'waiting',
            f'单页模板已生成，但仍有 {len(issues)} 项门禁问题。',
            issueCount=len(issues),
        )
    else:
        await emit(
            'validate',
            'completed',
            '单页结构、浏览器几何和 AI 视觉检查通过。',
        )
    content_elements = (
        1 + len(content.get('items') or []) * 2
        if page_kind == 'agenda'
        else 2 + len(content.get('items') or []) * 3
        if page_kind == 'cards'
        else (
            7
            + len(content.get('leftItems') or [])
            + len(content.get('rightItems') or [])
        )
        if page_kind == 'comparison'
        else 20
        if page_kind == 'metrics'
        else 14
        if page_kind == 'tableProcess'
        else 24
        if page_kind == 'denseRegression'
        else len(page.get('elements') or [])
    )
    return {
        'document': committed['document'],
        'pages': committed['pages'],
        'revision': committed['revision'],
        'reply': (
            '单页已生成，可编辑预览与源文件已就绪。'
            if not issues
            else f'单页已生成，仍有 {len(issues)} 项检查问题待确认。'
        ),
        'warnings': warnings,
        'generationMeta': {
            'workflow': 'kimi-slides',
            'strategy': f'{strategy}-fast-path',
            'pageCount': 1,
            'contentElements': content_elements,
            'workspace': str(workspace),
        },
    }


async def run_kimi(request, *, data_root: Path, progress, on_event=None, project_id: str, job_id: str):
    """Run the agentic loop, validate, and commit the produced deck."""
    workspace = kimi_workspace.workspace(data_root, project_id, job_id)
    workspace.mkdir(parents=True, exist_ok=True)
    trace = BenchmarkTrace(data_root, job_id)
    trace_completed = False
    policy = expert_policy()
    max_iters = policy.max_iterations
    batch_iters = min(max_iters, max(8, int(os.getenv("PPT_KIMI_BATCH_ITERS", "12"))))
    plan_iters = min(max_iters, max(4, policy.planning_iterations))
    plan_attempts = policy.planning_attempts
    plan_timeout = min(
        420.0,
        timeout_setting("PPT_KIMI_PLAN_TIMEOUT_SECONDS", 360),
    )
    batch_limit = max(1, int(os.getenv("PPT_KIMI_BATCHES", "8")))
    batch_page_size = policy.page_batch_size
    repair_rounds = policy.repair_rounds
    layout_page_attempts = policy.layout_page_attempts
    layout_page_batch = policy.layout_page_batch
    cover_content = _single_page_cover_content(request)
    closing_content = (
        _single_page_closing_content(request)
        if cover_content is None
        else None
    )
    agenda_content = (
        _single_page_agenda_content(request)
        if cover_content is None and closing_content is None
        else None
    )
    cards_content = (
        _single_page_cards_content(request)
        if (
            cover_content is None
            and closing_content is None
            and agenda_content is None
        )
        else None
    )
    comparison_content = (
        _single_page_comparison_content(request)
        if (
            cover_content is None
            and closing_content is None
            and agenda_content is None
            and cards_content is None
        )
        else None
    )
    metrics_content = (
        _single_page_metrics_content(request)
        if (
            cover_content is None
            and closing_content is None
            and agenda_content is None
            and cards_content is None
            and comparison_content is None
        )
        else None
    )
    table_process_content = (
        _single_page_table_process_content(request)
        if (
            cover_content is None
            and closing_content is None
            and agenda_content is None
            and cards_content is None
            and comparison_content is None
            and metrics_content is None
        )
        else None
    )
    dense_regression_content = (
        _single_page_dense_regression_content(request)
        if (
            cover_content is None
            and closing_content is None
            and agenda_content is None
            and cards_content is None
            and comparison_content is None
            and metrics_content is None
            and table_process_content is None
        )
        else None
    )
    fast_page = (
        ('cover', cover_content)
        if cover_content is not None
        else ('closing', closing_content)
        if closing_content is not None
        else ('agenda', agenda_content)
        if agenda_content is not None
        else ('cards', cards_content)
        if cards_content is not None
        else ('comparison', comparison_content)
        if comparison_content is not None
        else ('metrics', metrics_content)
        if metrics_content is not None
        else ('tableProcess', table_process_content)
        if table_process_content is not None
        else ('denseRegression', dense_regression_content)
        if dense_regression_content is not None
        else None
    )
    with trace.span('templateLoad'):
        template_path = kimi_workspace.prepare_template(
            data_root,
            getattr(request, "templateId", None),
            workspace,
        )
        template_info = None
        if template_path:
            template_info = await asyncio.to_thread(
                _prepare_template_analysis,
                workspace,
                template_path,
                screenshots=fast_page is None,
            )
    if fast_page is not None and template_info is not None:
        page_kind, page_content = fast_page
        try:
            result = await _run_single_page_template_fast_path(
                request,
                data_root=data_root,
                workspace=workspace,
                template_info=template_info,
                content=page_content,
                page_kind=page_kind,
                trace=trace,
                progress=progress,
                on_event=on_event,
                project_id=project_id,
            )
        except BaseException:
            trace.finish('failed')
            raise
        trace.finish('completed')
        return result
    model = make_model(output_mode="adaptive")
    # The agentic loop performs many independent model calls. Give transient
    # overloads a bounded retry window without changing prompt/context behavior.
    model.max_retries = max(int(getattr(model, 'max_retries', 0) or 0), 3)
    # The agentic ReAct loop makes many short model calls. Per-call remote token
    # counting is both slow and rate-limit-prone, and AgentScope's own context
    # compression already bounds the loop. Disable the deterministic-pipeline
    # budget for this route so one busy counting endpoint cannot abort the job.
    model.budget_enabled = False
    page_reasoning_mode = (
        os.getenv("PPT_KIMI_PAGE_REASONING_MODE", "adaptive").strip()
        or "adaptive"
    )
    page_model = make_model(
        output_mode=page_reasoning_mode,
        reasoning_mode=page_reasoning_mode,
        observable_retries=True,
    )
    page_model.max_retries = model.max_retries
    page_model.budget_enabled = False

    async def emit(phase, status, message, **fields):
        if on_event:
            result = on_event({"phase": phase, "status": status, "message": message, **fields})
            if asyncio.iscoroutine(result):
                await result

    try:
        planning_started = time.monotonic()
        reference_info = await asyncio.to_thread(
            kimi_workspace.prepare_reference_material, data_root, project_id, request, workspace
        )
        reference_characters = reference_info.get("characters") or len(getattr(request, "references", "") or "")
        planning_characters = len(reference_info.get("planningBrief", ""))
        reference_summary = (
            f"，原始资料 {reference_characters:,} 字，"
            f"已生成 {planning_characters:,} 字规划摘要。"
            if reference_characters
            else "，未提供文本参考资料。"
        )
        await emit("context", "started", "PPT 专家正在读取上下文并规划" + reference_summary)
        if progress:
            progress(5, "PPT 专家正在按原生工作流生成")
        reply = ""
        document: dict = {}
        pages: list[dict] = []
        final_dir = workspace / "final"
        entry: Path | None = None
        outstanding: list[dict] = []
        requirements = kimi_plan.plan_requirements(request, reference_info)
        plan_path = workspace / "deck" / "plan.json"
        plan = kimi_plan.load_plan(plan_path)
        if plan:
            kimi_plan.attach_source_details(plan, requirements)
            plan_issues = kimi_plan.audit_plan(plan, requirements)
        elif plan_path.is_file():
            plan_issues = [
                "deck/plan.json 已存在，但不是完整合法的 JSON；"
                "通常表示计划内容过长，在单次写入时被截断"
            ]
        else:
            plan_issues = ["尚未生成 deck/plan.json"]
        if plan is not None and not plan_issues:
            kimi_plan.write_plan_deck(workspace, plan, template_info)
        else:
            last_issues = plan_issues
            plan_started_at = time.monotonic()
            for plan_attempt in range(plan_attempts):
                remaining = plan_timeout - (time.monotonic() - plan_started_at)
                if plan_attempt and remaining < 150:
                    break
                if remaining <= 0:
                    raise PlanningDeadlineError(
                        f"内容策划超过 {int(plan_timeout)} 秒，已停止继续请求模型"
                    )
                await emit(
                    "plan",
                    "started" if plan_attempt == 0 else "retrying",
                    "正在规划完整叙事与章节覆盖"
                    + (f"（计划校验未通过，第 {plan_attempt + 1} 次）" if plan_attempt else ""),
                )
                planning_text = _planning_message(request, template_info, reference_info, requirements)
                if plan_attempt:
                    planning_text = (
                        "宿主拒绝了上一版计划：\n- "
                        + "\n- ".join(last_issues[:30])
                        + "\n请修复完整计划并再次调用 submit_plan；不要写页面文件。\n\n"
                        + planning_text
                    )
                submitter = _PlanSubmitter(workspace, requirements)
                planning_agent = _make_agent(
                    model,
                    workspace,
                    project_id,
                    plan_iters,
                    allowed_tools=("submit_plan",),
                    extra_tools={"submit_plan": submitter.tool},
                    system_prompt=_planning_system_prompt(),
                    context_trigger_ratio=0.7,
                )
                try:
                    await asyncio.wait_for(
                        _reply_with_progress(
                            planning_agent,
                            Msg(
                                name="user",
                                role="user",
                                content=[{"type": "text", "text": planning_text}],
                            ),
                            emit=emit,
                            phase="plan",
                            activity="PPT 内容策划",
                            max_steps=plan_iters,
                            workspace=workspace,
                            stop_when=lambda: submitter.finished,
                            tool_result_detail=submitter.describe_result,
                        ),
                        timeout=remaining,
                    )
                except TimeoutError as exc:
                    raise PlanningDeadlineError(
                        f"内容策划超过 {int(plan_timeout)} 秒，已停止继续请求模型"
                    ) from exc
                plan = kimi_plan.load_plan(plan_path)
                if submitter.last_issues and not submitter.accepted:
                    last_issues = submitter.last_issues
                elif plan:
                    kimi_plan.attach_source_details(plan, requirements)
                    last_issues = kimi_plan.audit_plan(plan, requirements)
                elif plan_path.is_file():
                    last_issues = [
                        "deck/plan.json 已存在，但不是完整合法的 JSON；"
                        "通常表示计划内容过长，在单次写入时被截断"
                    ]
                else:
                    last_issues = ["未生成 deck/plan.json"]
                if not last_issues:
                    kimi_plan.clear_page_drafts(workspace)
                    kimi_plan.write_plan_deck(workspace, plan, template_info)
                    break
            if plan is None or last_issues:
                raise ValueError(
                    "完整内容计划未通过宿主校验："
                    + "；".join(last_issues[:12])
                )
        source_entry = kimi_workspace.find_entry(workspace)
        body_style_profile = (
            template_style_profile(Path(template_info['pptd']))
            if template_info
            else None
        )
        _, planned_paths, _, _ = kimi_workspace.deck_progress(
            source_entry,
            require_content=True,
        )
        batch_limit = max(
            batch_limit,
            (len(planned_paths) + batch_page_size - 1) // batch_page_size + 1,
        )
        await emit(
            "plan",
            "completed",
            f"完整计划已通过校验：{len(planned_paths)} 页，"
            f"覆盖 {len(requirements['principalSections'])} 个一级章节、"
            f"{len(requirements.get('requiredSubsections') or [])} 个二级章节",
            covered=0,
            total=len(planned_paths),
        )
        trace.record_ms(
            'planning',
            (time.monotonic() - planning_started) * 1000,
            totalPages=len(planned_paths),
        )
        planned_page_contracts: dict[str, list[str]] = {}
        content_layout_history: list[dict] = []
        deterministic_content = os.getenv(
            'PPT_KIMI_DETERMINISTIC_CONTENT',
            '0',
        ).strip().lower() in {'1', 'true', 'yes', 'on'}
        async def inspect_page(path, image_path, state, structural_issues):
            if structural_issues:
                return {
                    "status": "issues",
                    "issues": structural_issues,
                }
            if os.getenv("PPT_KIMI_VISUAL_REVIEW", "1") == "0":
                return {"status": "unavailable", "issues": []}
            from .review_service import review_page

            design_contract = planned_page_contracts.get(path)
            report = await review_page(
                state,
                workspace,
                path,
                scenario=getattr(request, "scenario", None) or "auto",
                usage=getattr(request, "usage", None) or "presentation",
                cache_dir=data_root / "review-cache",
                image_name=image_path.relative_to(workspace).as_posix(),
                design_contract=design_contract,
                max_tokens=4096 if design_contract else None,
            )
            issues = report.get("issues") or []
            return {
                "status": "issues" if issues else "passed",
                "issues": issues,
                "model": report.get("model", ""),
            }

        def validate_page_write(relative):
            return _page_structure_issues(workspace, relative)

        preview_publisher = DraftPreviewPublisher(
            workspace,
            source_entry,
            job_id,
            on_event=emit,
            inspect_page=inspect_page,
            concurrency=policy.visual_review_concurrency,
        )
        # Generation is deliberately serial. Each page is rendered and
        # visually inspected through this publisher before the next page starts.
        await preview_publisher.start(poll=False)
        try:
            # A resumed workspace may already contain pages from an earlier
            # job. Re-open that gate before writing any new page so recovery
            # cannot bypass the per-page acceptance contract.
            checked_document, checked_paths, checked_existing, _, _ = (
                kimi_workspace.deck_status(
                    source_entry,
                    require_content=True,
                )
            )
            if checked_existing:
                from pptd_engine.document import load_yaml

                for existing_path in checked_paths:
                    if existing_path not in checked_existing:
                        continue
                    existing_index = checked_paths.index(existing_path)
                    existing_data = load_yaml(source_entry.parent / existing_path)
                    existing_data = _apply_host_transformed_page(
                        source_entry,
                        checked_document,
                        existing_path,
                        existing_data,
                        template_info,
                    )
                    transformed_issues = validate_document(
                        {
                            'document': {
                                **checked_document,
                                'pages': [existing_path],
                            },
                            'pages': [{
                                'path': existing_path,
                                'data': existing_data,
                            }],
                        },
                        resource_root=source_entry.parent,
                    )
                    if transformed_issues:
                        raise PageGateFailed(
                            f'第 {existing_index + 1} 页套用模板后结构无效：'
                            + kimi_workspace.issue_summary(
                                transformed_issues,
                                limit=8,
                            )
                        )
                    kimi_workspace.write_pages(
                        source_entry,
                        [{
                            'path': existing_path,
                            'data': existing_data,
                        }],
                    )
                    await emit(
                        'validate',
                        'started',
                        f'第 {existing_index + 1} 页来自恢复稿，'
                        '正在重新执行单页门禁。',
                        pagePath=existing_path,
                        pageIndex=existing_index,
                    )
                    await _repair_page_until_pass(
                        request,
                        workspace=workspace,
                        entry=source_entry,
                        deck_document=checked_document,
                        page_path=existing_path,
                        page_index=existing_index,
                        page_data=existing_data,
                        publisher=preview_publisher,
                        emit=emit,
                        max_repair_rounds=layout_page_attempts,
                    )
            for batch_index in range(batch_limit):
                continuation = None
                if source_entry is not None:
                    _, paths, existing, missing, invalid = (
                        kimi_workspace.deck_status(
                            source_entry,
                            require_content=True,
                        )
                    )
                    pending = [path for path in paths if path not in existing]
                    if not pending:
                        break
                    target_pages = kimi_workspace.batch_page_target(
                        len(pending), batch_limit - batch_index, batch_page_size
                    )
                    target_pages = min(batch_page_size, target_pages)
                    continuation = {
                        "batch": batch_index + 1,
                        "totalPages": len(paths),
                        "existingPages": existing,
                        "missingPages": pending,
                        "absentPages": missing,
                        "invalidPages": invalid,
                        "batchPages": pending[:target_pages],
                        "planFile": "deck/plan.json" if (source_entry.parent / "plan.json").is_file() else None,
                    }
                    await emit(
                        "pages",
                        "started",
                        f"继续原生 Kimi 编排：已完成 {len(existing)}/{len(paths)} 页，"
                        f"正在补写 {len(pending)} 页（本段目标 {target_pages} 页）。",
                        covered=len(existing),
                        total=len(paths),
                    )
                plan_by_path = {
                    item.get('path'): item
                    for item in plan.get('pages', [])
                    if isinstance(item, dict) and isinstance(item.get('path'), str)
                }
                # Page batches are written incrementally; loading the full deck
                # here would misreport an unwritten page as invalid YAML.
                deck_document = kimi_workspace.load_document(source_entry)
                page_paths = deck_document.get('pages') or []
                source_units = (
                    reference_info.get('subsectionExcerpts') or {}
                )
                for path in continuation.get('batchPages', []):
                    index = page_paths.index(path)
                    item = plan_by_path.get(path) or {
                        'path': path,
                        'title': f'第 {index + 1} 页',
                    }
                    evidence = []
                    for title in item.get('sourceSubsections') or []:
                        value = source_units.get(str(title))
                        if value:
                            evidence.append(f'### {title}\n{value}')
                    page_context = {
                        **item,
                        'pageIndex': index,
                        'pageNumber': index + 1,
                        'pageCount': len(page_paths),
                        'recommendedArchetype': planned_layout_archetype(item),
                        'layoutHistory': [
                            dict(value)
                            for value in content_layout_history[-2:]
                        ],
                        'templateStyle': body_style_profile,
                        'deckTitle': plan.get('deckTitle'),
                        'narrative': plan.get('narrative'),
                        'previousTitle': (
                            plan_by_path.get(page_paths[index - 1], {}).get('title')
                            if index > 0
                            else None
                        ),
                        'nextTitle': (
                            plan_by_path.get(page_paths[index + 1], {}).get('title')
                            if index + 1 < len(page_paths)
                            else None
                        ),
                    }
                    previous_spec = None
                    issues = []
                    page_data = None
                    page_role = planned_page_role(
                        index,
                        len(page_paths),
                        item,
                        plan,
                    )
                    role_started = time.monotonic()
                    planned_role = build_planned_role_page(
                        index=index,
                        total=len(page_paths),
                        page=item,
                        plan=plan,
                        requirements=requirements,
                        template_info=template_info,
                        deck_dir=source_entry.parent,
                        style_profile=body_style_profile,
                        deterministic_content=deterministic_content,
                    )
                    if planned_role is not None:
                        role, page_data = planned_role
                        page_role = role
                        planned_page_contracts[path] = (
                            planned_role_visual_contract(
                                role,
                                (
                                    planned_body_layout(item)
                                    if role == 'content'
                                    else 'editorial'
                                ),
                            )
                        )
                        trace.record_ms(
                            'hostTransform',
                            (time.monotonic() - role_started) * 1000,
                            pageIndex=index,
                            pagePath=path,
                            strategy=f'planned-{role}',
                        )
                        with trace.span(
                            'structureValidation',
                            pageIndex=index,
                            pagePath=path,
                            strategy=f'planned-{role}',
                        ):
                            issues = validate_document(
                                {
                                    'document': {
                                        **deck_document,
                                        'pages': [path],
                                    },
                                    'pages': [{
                                        'path': path,
                                        'data': page_data,
                                    }],
                                },
                                resource_root=source_entry.parent,
                            )
                        trace.record_issues(
                            'pageStructure',
                            issues,
                            pageIndex=index,
                            pagePath=path,
                            strategy=f'planned-{role}',
                        )
                        await emit(
                            'pages',
                            'waiting',
                            f'第 {index + 1}/{len(page_paths)} 页识别为{role}，'
                            '直接使用模板角色构建，跳过页面模型。',
                            pageIndex=index,
                            pagePath=path,
                            title=item.get('title'),
                        )
                    for attempt in range(0 if planned_role is not None else 2):
                        await emit(
                            'pages',
                            'started',
                            f'正在生成第 {index + 1}/{len(page_paths)} 页 JSON 布局'
                            + ('（修正结构）' if attempt else ''),
                            pageIndex=index,
                            pagePath=path,
                            title=item.get('title'),
                        )
                        agent = _make_agent(
                            page_model,
                            workspace,
                            project_id,
                            1,
                            allowed_tools=(),
                            system_prompt=_page_layout_system_prompt(),
                        )
                        try:
                            with trace.span(
                                'pageModel',
                                pageIndex=index,
                                pagePath=path,
                                attempt=attempt + 1,
                            ):
                                reply = kimi_workspace.reply_text(
                                    await _reply_with_progress(
                                        agent,
                                        Msg(
                                            name='user',
                                            role='user',
                                            content=[{
                                                'type': 'text',
                                                'text': _page_spec_message(
                                                    page_context,
                                                    '\n\n'.join(evidence),
                                                    previous_spec,
                                                    issues,
                                                    content_layout_history,
                                                    body_style_profile,
                                                ),
                                            }],
                                        ),
                                        emit=emit,
                                        phase='pages',
                                        activity=f'第 {index + 1} 页布局',
                                        max_steps=1,
                                    )
                                )
                            spec = _json_object(reply)
                            with trace.span(
                                'compile',
                                pageIndex=index,
                                pagePath=path,
                                attempt=attempt + 1,
                            ):
                                page_data = layout_engine.compile_page(
                                    spec,
                                    page_index=index,
                                    page_path=path,
                                    style_profile=body_style_profile,
                                )
                            with trace.span(
                                'structureValidation',
                                pageIndex=index,
                                pagePath=path,
                                attempt=attempt + 1,
                            ):
                                issues = validate_document(
                                    {
                                        'document': {
                                            **deck_document,
                                            'pages': [path],
                                        },
                                        'pages': [{
                                            'path': path,
                                            'data': page_data,
                                        }],
                                    },
                                    resource_root=source_entry.parent,
                                )
                                if not issues:
                                    issues = content_layout_issues(
                                        page_data,
                                        item,
                                        content_layout_history,
                                    )
                            trace.record_issues(
                                'pageStructure',
                                issues,
                                pageIndex=index,
                                pagePath=path,
                                attempt=attempt + 1,
                            )
                            if not issues:
                                break
                            previous_spec = spec
                            await emit(
                                'validate',
                                'retrying',
                                f'第 {index + 1} 页布局结构有 '
                                f'{len(issues)} 项问题，正在修正。',
                                pageIndex=index,
                                pagePath=path,
                                title=item.get('title'),
                                issueCount=len(issues),
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            issues = [{
                                'code': 'PageSpecInvalid',
                                'location': path,
                                'message': str(exc)[:1000],
                                'severity': 'error',
                            }]
                            await emit(
                                'validate',
                                'retrying',
                                f'第 {index + 1} 页 JSON 规格无效，正在修正。',
                                pageIndex=index,
                                pagePath=path,
                                title=item.get('title'),
                                issueCount=1,
                            )
                    if page_data is None or issues:
                        detail = kimi_workspace.issue_summary(
                            issues,
                            limit=8,
                        )
                        raise ValueError(
                            f'第 {index + 1} 页未生成合法布局：{detail}'
                        )
                    with trace.span(
                        'hostTransform',
                        pageIndex=index,
                        pagePath=path,
                    ):
                        page_data = _apply_host_transformed_page(
                            source_entry,
                            deck_document,
                            path,
                            page_data,
                            template_info,
                        )
                    with trace.span(
                        'structureValidation',
                        pageIndex=index,
                        pagePath=path,
                        transformed=True,
                    ):
                        transformed_issues = validate_document(
                            {
                                'document': {
                                    **deck_document,
                                    'pages': [path],
                                },
                                'pages': [{
                                    'path': path,
                                    'data': page_data,
                                }],
                            },
                            resource_root=source_entry.parent,
                        )
                    trace.record_issues(
                        'hostTransform',
                        transformed_issues,
                        pageIndex=index,
                        pagePath=path,
                    )
                    if transformed_issues:
                        raise PageGateFailed(
                            f'第 {index + 1} 页套用模板后结构无效：'
                            + kimi_workspace.issue_summary(
                                transformed_issues,
                                limit=8,
                            )
                        )
                    kimi_workspace.write_pages(
                        source_entry,
                        [{'path': path, 'data': page_data}],
                    )
                    page_data = await _repair_page_until_pass(
                        request,
                        workspace=workspace,
                        entry=source_entry,
                        deck_document=deck_document,
                        page_path=path,
                        page_index=index,
                        page_data=page_data,
                        publisher=preview_publisher,
                        emit=emit,
                        max_repair_rounds=layout_page_attempts,
                    )
                    if page_role == 'content':
                        content_layout_history.append({
                            'pageIndex': index + 1,
                            'title': str(item.get('title') or ''),
                            'family': layout_engine.layout_family(page_data),
                        })
                    await emit(
                        'pages',
                        'completed',
                        f'第 {index + 1} 页已通过单页门禁并写入。',
                        pageIndex=index,
                        pagePath=path,
                        title=item.get('title'),
                    )
                source_entry = kimi_workspace.find_entry(workspace)
                _, paths, existing, _, _ = kimi_workspace.deck_status(
                    source_entry,
                    require_content=True,
                )
                pending = [path for path in paths if path not in existing]
                await emit(
                    "pages",
                    "started",
                    f"Kimi 本段完成：{len(existing)}/{len(paths)} 页已写入。",
                    covered=len(existing),
                    total=len(paths),
                )
                if not pending:
                    break
            if source_entry is None:
                raise ValueError("PPT 专家没有在 deck/ 目录产出新的 .pptd 交付物。")
            _, planned_paths, existing_paths, _, invalid_paths = (
                kimi_workspace.deck_status(
                    source_entry,
                    require_content=True,
                )
            )
            pending_paths = [path for path in planned_paths if path not in existing_paths]
            if pending_paths:
                missing_detail = ", ".join(pending_paths[:6])
                if len(pending_paths) > 6:
                    missing_detail += f" 等 {len(pending_paths)} 页"
                invalid_detail = ""
                if invalid_paths:
                    invalid_detail = "；YAML 问题：" + "；".join(
                        issue["message"] for issue in invalid_paths[:6]
                    )
                raise ValueError(
                    f"PPT 专家连续 {batch_limit} 段仍未写完全部页面："
                    f"{len(existing_paths)}/{len(planned_paths)} 页可解析，"
                    f"待完成 {missing_detail}{invalid_detail}"
                )
        finally:
            await preview_publisher.stop()
        document: dict = {}
        pages: list[dict] = []
        entry: Path | None = None
        for structure_attempt in range(repair_rounds + 1):
            document, pages = kimi_workspace.load_deck(source_entry)
            with trace.span(
                'hostTransform',
                scope='deck',
                attempt=structure_attempt,
            ):
                _apply_host_transforms(source_entry, document, pages, template_info)
                shutil.rmtree(final_dir, ignore_errors=True)
                entry = kimi_workspace.write_deck(final_dir, source_entry, document, pages)
            with trace.span(
                'structureValidation',
                scope='deck',
                attempt=structure_attempt,
            ):
                issues = validate_document(
                    {"document": document, "pages": pages},
                    resource_root=entry.parent,
                )
            trace.record_issues(
                'deckStructure',
                issues,
                attempt=structure_attempt,
            )
            if not issues:
                break
            detail = "; ".join(f"{i.get('location')}: {i.get('message')}" for i in issues[:8])
            if structure_attempt >= repair_rounds:
                raise ValueError(f"生成的 PPTD 未通过结构校验：{detail}")
            await emit(
                "validate",
                "retrying",
                f"结构校验发现 {len(issues)} 项问题，请模型修复后重检。",
                issues=issues[:12],
            )
            if progress:
                progress(65, f"结构校验发现 {len(issues)} 项问题，正在修复")
            message = Msg(name="user", role="user", content=[{"type": "text", "text": (
                "宿主的 PPTD 结构校验发现以下问题：\n"
                + kimi_workspace.issue_summary(issues)
                + "\n请只修改对应的 .page 文件修正结构和表格网格，保留全部文字与数据，"
                  "然后重新运行 `kimi_slides check deck --level auto`。"
            )}])
            repair_agent = _make_agent(
                model, workspace, project_id, min(batch_iters, 14)
            )
            with trace.span(
                'repairModel',
                scope='structure',
                attempt=structure_attempt + 1,
                issueCount=len(issues),
            ):
                reply = kimi_workspace.reply_text(await _reply_with_progress(
                    repair_agent,
                    message,
                    emit=emit,
                    phase="validate",
                    activity="结构修复",
                    max_steps=min(batch_iters, 14),
                    workspace=workspace,
                ))

        if entry is None:
            raise ValueError("PPT 专家没有生成可检查的 PPTD 交付稿。")
        page_attempts: dict[str, int] = {}
        page_noops: dict[str, int] = {}
        visual_history: dict = {}
        review_changed_pages = None
        while True:
            document, pages = kimi_workspace.load_deck(source_entry)
            with trace.span(
                'hostTransform',
                scope='deck',
                finalPass=True,
            ):
                _apply_host_transforms(source_entry, document, pages, template_info)
                shutil.rmtree(final_dir, ignore_errors=True)
                entry = kimi_workspace.write_deck(final_dir, source_entry, document, pages)
            with trace.span(
                'structureValidation',
                scope='deck',
                finalPass=True,
            ):
                issues = validate_document(
                    {"document": document, "pages": pages},
                    resource_root=entry.parent,
                )
            trace.record_issues(
                'deckStructure',
                issues,
                finalPass=True,
            )
            if issues:
                detail = "; ".join(f"{i.get('location')}: {i.get('message')}" for i in issues[:8])
                raise ValueError(f"排版修复引入了结构问题：{detail}")
            await emit("validate", "started", "正在用真实浏览器排版检查整份交付稿（含封面、致谢与公司元素）。")
            with trace.span(
                'browserRender',
                scope='deck',
                finalPass=True,
            ):
                rendered = await asyncio.to_thread(_render_pages, entry)
            try:
                with trace.span(
                    'browserLayout',
                    scope='deck',
                    finalPass=True,
                ):
                    layout = await asyncio.to_thread(
                        final_layout_issues,
                        entry,
                        document,
                        pages,
                        rendered,
                    )
                trace.record_issues(
                    'deckLayout',
                    layout,
                    finalPass=True,
                )
                visual = []
                if not layout:
                    with trace.span(
                        'visionReview',
                        scope='deck',
                        finalPass=True,
                    ):
                        visual = await visual_overview_issues(
                            document, pages, rendered, on_event=on_event,
                            scenario=getattr(request, "scenario", None) or "auto",
                            cache_dir=data_root / "review-cache",
                            previous_review=visual_history or None,
                            only_pages=review_changed_pages,
                            report_holder=visual_history,
                        )
                    trace.record_issues(
                        'visionReview',
                        visual,
                        finalPass=True,
                    )
            finally:
                if rendered is not None:
                    shutil.rmtree(rendered, ignore_errors=True)
            layout = layout + visual
            if not layout:
                outstanding = []
                break

            source_paths = list(document.get("pages") or [])
            issue_pages = []
            for issue in layout:
                path = kimi_workspace.issue_page(issue)
                if path in source_paths and path not in issue_pages:
                    issue_pages.append(path)
            pending = [
                path for path in issue_pages
                if page_attempts.get(path, 0) < layout_page_attempts
            ]
            if not pending:
                outstanding = layout
                await emit(
                    "validate", "waiting",
                    f"仍有 {len(layout)} 项排版问题，已保留可编辑稿；可继续从检查点恢复。",
                    issues=layout[:12],
                )
                break

            batch = pending[:layout_page_batch]
            batch_issues = [
                issue for issue in layout if kimi_workspace.issue_page(issue) in batch
            ]
            page_numbers = [source_paths.index(path) + 1 for path in batch]
            before = kimi_workspace.page_hashes(source_entry, batch)
            for path in batch:
                page_attempts[path] = page_attempts.get(path, 0) + 1

            await emit(
                "validate", "retrying",
                f"排版检查发现 {len(layout)} 项问题；正在修复第 "
                f"{', '.join(str(number) for number in page_numbers)} 页。",
                issues=batch_issues[:12],
                progress=0,
            )
            if progress:
                progress(70, f"正在修复第 {', '.join(str(number) for number in page_numbers)} 页的排版问题")
            screenshot_dir = workspace / f"layout-repair-{page_attempts[batch[0]]}-{page_numbers[0]}"
            noop_note = "".join(
                f"\n注意：上一轮没有实际修改 {path}，本轮必须修改该页面。"
                for path in batch if page_noops.get(path)
            )
            message = Msg(name="user", role="user", content=[{"type": "text", "text": (
                "宿主的真实浏览器排版检查（渲染最终交付稿，已包含公司模板的封面/致谢与角落元素）发现以下问题：\n"
                + kimi_workspace.issue_summary(batch_issues, limit=len(batch_issues))
                + f"\n本轮只处理这些页面：{', '.join(batch)}（页码 {', '.join(str(number) for number in page_numbers)}）。"
                + f"\n请先运行 `kimi_slides screenshot deck -p {','.join(str(number) for number in page_numbers)} "
                  f"-o {screenshot_dir}` 查看实际渲染效果，再编辑对应的 .page 文件。"
                + f"\n修改后只运行 `kimi_slides check deck -p {','.join(str(number) for number in page_numbers)} --level auto` "
                  "和本轮页面的 screenshot 验证。"
                  "不要在本批次内运行整份 20 页检查；宿主会在本批返回后统一复检整份文档。"
                  "保持文字与数据完整，不要改动 template-pptd/ 下的分析素材。"
                + noop_note
            )}])
            repair_agent = _make_agent(
                model, workspace, project_id, min(batch_iters, 14)
            )
            with trace.span(
                'repairModel',
                scope='layout',
                pageNumbers=page_numbers,
                issueCount=len(layout),
            ):
                reply = kimi_workspace.reply_text(await _reply_with_progress(
                    repair_agent,
                    message,
                    emit=emit,
                    phase="validate",
                    activity=(
                        f"第 {', '.join(str(number) for number in page_numbers)} 页"
                        "排版修复"
                    ),
                    max_steps=min(batch_iters, 14),
                    workspace=workspace,
                ))
            after = kimi_workspace.page_hashes(source_entry, batch)
            changed_batch = [
                path for path in batch
                if after[path] != before[path]
            ]
            if changed_batch:
                review_changed_pages = changed_batch
                changed_numbers = [
                    source_paths.index(path) + 1
                    for path in changed_batch
                ]
                page_spec = ','.join(str(number) for number in changed_numbers)
                await emit(
                    "validate",
                    "waiting",
                    f"正在先检查本轮修改的第 {page_spec} 页，确认后再运行整份检查。",
                    progress=90,
                )
                from pptd_engine import check as check_module

                page_check = await asyncio.to_thread(
                    check_module.check_deck,
                    source_entry.parent,
                    page_spec,
                    "all",
                    "keep",
                )
                await emit(
                    "validate",
                    "waiting",
                    (
                        f"本轮第 {page_spec} 页检查发现 {len(page_check)} 项提醒；"
                        "宿主继续执行整份复检。"
                        if page_check
                        else f"本轮第 {page_spec} 页检查通过，宿主继续执行整份复检。"
                    ),
                    progress=100,
                )
            for path in batch:
                if after[path] == before[path]:
                    page_noops[path] = page_noops.get(path, 0) + 1
                else:
                    page_noops[path] = 0
        from .pptd_store import DocumentStore

        store = DocumentStore(data_root)
        revision = store.read(project_id)["revision"] if store.exists(project_id) else 0
        committed = store.commit(
            project_id, document, pages, "AI 生成（PPT 专家）", revision,
            assets=kimi_workspace.collect_assets(entry),
        )
        await emit("validate", "completed", "结构检查与真实排版检查通过，已提交可编辑文档。")
        if outstanding:
            reply = (reply + "\n\n## 排版检查未通过项\n\n"
                     "以下问题已定位到页面与元素；文档仍可编辑预览，请确认或调整后再导出：\n\n"
                     + kimi_workspace.issue_summary(outstanding)).strip()
        trace_completed = True
        trace.finish('completed')
        return {
            "document": committed["document"],
            "pages": committed["pages"],
            "revision": committed["revision"],
            "reply": reply,
            "warnings": [{"location": issue.get("location"), "code": issue.get("code"),
                          "message": issue.get("message")} for issue in outstanding],
            "generationMeta": {
                "workflow": "kimi-slides",
                "model": getattr(model, "model", None) or getattr(model, "_model", None),
                "pageReasoningMode": page_reasoning_mode,
                "workspace": str(workspace),
            },
        }
    finally:
        if not trace_completed:
            trace.finish('failed')
        if page_model is not model:
            await page_model.client.close()
        await model.client.close()


def kimi_health() -> dict:
    from .model_gateway import health

    return health()
