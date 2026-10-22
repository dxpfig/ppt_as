"""Kimi-slides 1:1 agentic PPT expert.

Unlike the deterministic ``pptd_generation`` pipeline, this agent gives the model
real file and CLI tools and lets it follow ``docs/kimi-ppt/original/SKILL.md``
itself: classify the task, read only the rules it needs, write pages in parallel,
and close the ``kimi-slides check`` / ``screenshot`` loop before finishing.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from agentscope.agent import Agent, ContextConfig, ReActConfig
from agentscope.event import (
    ExceedMaxItersEvent,
    ModelCallStartEvent,
    ReplyStartEvent,
    ThinkingBlockStartEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
)
from agentscope.message import Msg
from agentscope.middleware import TracingMiddleware
from agentscope.permission import PermissionBehavior, PermissionDecision, PermissionMode
from agentscope.state import AgentState
from agentscope.tool import Bash, Edit, FunctionTool, Glob, Grep, Read, Toolkit, Write

from .model_gateway import make_model, timeout_setting
from .execution_policy import expert_policy
from .layout_autofit import autofit_state
from .pptd_compat import normalize_pages
from .pptd_validation import normalize_table_tracks, validate_document
from .repair_coordinator import issue_fingerprint
from .kimi_validation import (
    _browser_layout_issues,
    _browser_layout_report,
    _contrast_issues,
    _emit_note,
    _render_pages,
    _renderer_ready,
    _slide_artifact_dir,
    final_layout_report,
    visual_overview_issues,
)
from .kimi_template import (
    _apply_background,
    _apply_chrome,
    _apply_configured_background,
    _apply_host_transforms,
    _apply_template_cover_closing,
    _clear_corner_conflicts,
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
from .kimi_plan import (
    audit_plan,
    clear_page_drafts as _clear_page_drafts,
    load_plan as _load_plan,
    plan_requirements,
    write_plan_deck as _write_plan_deck,
)
from .kimi_workspace import (
    batch_page_target as _batch_page_target,
    collect_assets as _collect_assets,
    deck_progress as _deck_progress,
    deck_status as _deck_status,
    find_entry as _find_entry,
    issue_page as _issue_page,
    issue_summary as _issue_summary,
    load_deck as _load_deck,
    page_hashes as _page_hashes,
    prepare_reference_material as _prepare_reference_material,
    prepare_template as _prepare_template,
    recovery_checkpoint,
    reply_text as _reply_text,
    restore_workspace,
    workspace as _workspace,
    write_deck as _write_deck,
    write_pages as _write_pages,
)
from .kimi_prompts import (
    execution_message as _execution_message,
    planning_message as _planning_message,
)


ROOT = Path(__file__).resolve().parents[1]
ORIGINAL = ROOT / "docs" / "kimi-ppt" / "original"


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


def _make_agent(
    model,
    workspace: Path,
    session_id: str | None,
    max_iters: int,
    *,
    allowed_tools: tuple[str, ...] | None = None,
):
    available = {
        'Read': Read(),
        'Write': Write(),
        'Edit': Edit(),
        'Grep': Grep(),
        'Glob': Glob(),
        'Bash': Bash(cwd=str(workspace)),
        'kimi_slides': _cli_tool(workspace),
    }
    selected = list(available) if allowed_tools is None else list(allowed_tools)
    unknown = set(selected) - set(available)
    if unknown:
        raise ValueError(f'未知的 PPT 专家工具：{", ".join(sorted(unknown))}')
    toolkit = Toolkit(tools=[available[name] for name in selected])
    state = AgentState(session_id=session_id) if session_id else AgentState()
    state.permission_context.mode = PermissionMode.BYPASS
    return Agent(
        name="KimiSlidesExpert",
        system_prompt=kimi_system_prompt(workspace),
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
            trigger_ratio=0.9,
            compression_fallback_to_truncation=False,
        ),
    )


async def _reply_with_progress(
    agent,
    message,
    *,
    emit,
    phase: str,
    activity: str,
    max_steps: int,
):
    """Expose model and tool activity without pretending steps equal progress."""
    if not hasattr(agent, 'reply_stream'):
        return await agent.reply(message)
    final = None
    model_calls = 0
    tool_names: dict[str, str] = {}
    async for item in agent.reply_stream(message, yield_final_msg=True):
        if isinstance(item, Msg):
            final = item
        elif isinstance(item, ReplyStartEvent):
            await emit(phase, 'waiting', f'{activity}：正在分析当前上下文')
        elif isinstance(item, ThinkingBlockStartEvent):
            await emit(phase, 'waiting', f'{activity}：模型正在判断下一步')
        elif isinstance(item, ModelCallStartEvent):
            model_calls += 1
            await emit(
                phase,
                'waiting',
                f'{activity}：模型请求 {model_calls}/{max_steps}',
            )
        elif isinstance(item, ToolCallStartEvent):
            tool_names[item.tool_call_id] = item.tool_call_name
            label = {
                'Read': '正在读取文件',
                'Write': '正在写入页面文件',
                'Edit': '正在修改页面文件',
                'Grep': '正在检索文件内容',
                'Glob': '正在定位文件',
                'Bash': '正在运行命令',
                'kimi_slides': '正在运行 PPT 检查或截图工具',
            }.get(item.tool_call_name, f'正在执行工具 {item.tool_call_name}')
            await emit(phase, 'started', f'{activity}：{label}')
        elif isinstance(item, ToolResultEndEvent):
            name = tool_names.get(item.tool_call_id, '工具')
            await emit(
                phase,
                'waiting',
                f'{activity}：{name} 执行完成，模型正在判断下一步',
            )
        elif isinstance(item, ExceedMaxItersEvent):
            await emit(
                phase,
                'waiting',
                f'{activity}：本轮 {max_steps} 步预算已用完，正在整理结果',
            )
    if final is None:
        raise ValueError(f'{activity}没有返回可用的 Agent 结果。')
    return final


def _http_status(error: BaseException) -> int | None:
    for owner in (error, getattr(error, 'response', None)):
        value = getattr(owner, 'status_code', None)
        if isinstance(value, int):
            return value
    return None


def _retry_delay() -> float:
    try:
        value = float(os.getenv('PPT_LLM_RETRY_DELAY_SECONDS', '1.5'))
    except ValueError:
        return 1.5
    return value if 0.5 <= value <= 15 else 1.5


def _observe_model_retries(model, emit) -> None:
    """Surface hidden provider retries without leaking provider response bodies."""
    original = getattr(model, '_call_api', None)
    if original is None or getattr(model, '_ppt_retry_observed', False):
        return
    retries = 0

    async def observed(*args, **kwargs):
        nonlocal retries
        try:
            return await original(*args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            status = _http_status(error)
            if status in {429, 500, 502, 503, 504, 529}:
                retries += 1
                delay = max(1.0, float(getattr(model, 'retry_delay', 1.0)))
                await emit(
                    getattr(model, '_ppt_event_phase', 'validate'),
                    'retrying',
                    f'模型服务暂时繁忙（HTTP {status}），'
                    f'{delay:g} 秒后重试（第 {retries} 次）',
                )
            raise

    model._call_api = observed
    model._ppt_retry_observed = True


REPAIR_STATE_FILE = '.repair-state.json'


def _repair_state_path(workspace: Path) -> Path:
    return workspace / REPAIR_STATE_FILE


def _load_repair_state(workspace: Path) -> dict:
    default = {
        'version': 1,
        'attempts': {},
        'noops': {},
        'autofitAttempts': 0,
        'baselineIssueCount': 0,
        'issueCount': 0,
        'issueFingerprint': '',
    }
    path = _repair_state_path(workspace)
    if not path.is_file():
        return default
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default
    if not isinstance(value, dict):
        return default

    def counters(name: str) -> dict[str, int]:
        source = value.get(name)
        if not isinstance(source, dict):
            return {}
        return {
            str(key): number
            for key, number in source.items()
            if isinstance(number, int) and number >= 0
        }

    for name in ('autofitAttempts', 'baselineIssueCount', 'issueCount'):
        number = value.get(name)
        default[name] = number if isinstance(number, int) and number >= 0 else 0
    default['attempts'] = counters('attempts')
    default['noops'] = counters('noops')
    fingerprint = value.get('issueFingerprint')
    default['issueFingerprint'] = fingerprint if isinstance(fingerprint, str) else ''
    return default


def _save_repair_state(workspace: Path, state: dict) -> None:
    path = _repair_state_path(workspace)
    value = {
        **state,
        'version': 1,
        'updatedAt': time.time(),
    }
    temporary = path.with_suffix('.tmp')
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    temporary.replace(path)


def _repair_progress(state: dict, issue_count: int) -> int | None:
    baseline = max(int(state.get('baselineIssueCount') or 0), issue_count)
    if baseline <= 0:
        return None
    return max(0, min(95, round((baseline - issue_count) / baseline * 100)))


def _page_sources(entry: Path, paths: list[str]) -> list[dict]:
    sources = []
    for path in paths:
        target = entry.parent / path
        try:
            text = target.read_text(encoding='utf-8')
        except (OSError, UnicodeError):
            continue
        sources.append({'path': path, 'source': text})
    return sources


def _run_cli(command: str, args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        [sys.executable, "-m", "pptd_engine.cli", command, *args],
        cwd=str(cwd), env=_cli_env(), capture_output=True, text=True, timeout=900,
    )
    if proc.returncode:
        raise ValueError(f"模板预处理 {command} 失败：{(proc.stderr or proc.stdout)[-2000:]}")
    return proc.stdout or ""


def _prepare_template_analysis(workspace: Path, template_pptx: Path) -> dict:
    """Convert and screenshot the company template so the agent can analyze it."""
    pptd_dir = workspace / "template-pptd"
    shots_dir = workspace / "template-screenshots"
    entry = pptd_dir / f"{template_pptx.stem}.pptd"
    if not entry.is_file():
        _run_cli("convert", [str(template_pptx), "-o", str(pptd_dir)], workspace)
    if not any((shots_dir / "pages").glob("*.png")):
        _run_cli("screenshot", [str(pptd_dir), "-o", str(shots_dir)], workspace)
    return {
        "pptx": str(template_pptx),
        "pptd": str(pptd_dir),
        "entry": str(pptd_dir / f"{template_pptx.stem}.pptd"),
        "screenshots": str(shots_dir),
    }


async def run_kimi(request, *, data_root: Path, progress, on_event=None, project_id: str, job_id: str):
    """Run the agentic loop, validate, and commit the produced deck."""
    workspace = kimi_workspace.workspace(data_root, project_id, job_id)
    workspace.mkdir(parents=True, exist_ok=True)
    policy = expert_policy()
    max_iters = policy.max_iterations
    batch_iters = min(max_iters, max(8, int(os.getenv("PPT_KIMI_BATCH_ITERS", "18"))))
    plan_iters = min(max_iters, max(12, policy.planning_iterations))
    plan_attempts = policy.planning_attempts
    batch_limit = max(1, int(os.getenv("PPT_KIMI_BATCHES", "8")))
    batch_page_size = policy.page_batch_size
    repair_rounds = policy.repair_rounds
    layout_page_attempts = policy.layout_page_attempts
    layout_page_batch = policy.layout_page_batch
    layout_iterations = policy.layout_iterations
    template_path = kimi_workspace.prepare_template(
        data_root,
        getattr(request, "templateId", None),
        workspace,
    )
    template_info = None
    if template_path:
        template_info = await asyncio.to_thread(_prepare_template_analysis, workspace, template_path)
    model = make_model(output_mode="adaptive")
    # The agentic loop performs many independent model calls. Give transient
    # overloads a bounded retry window without changing prompt/context behavior.
    model.max_retries = max(int(getattr(model, 'max_retries', 0) or 0), 3)
    model.retry_delay = _retry_delay()
    # The agentic ReAct loop makes many short model calls. Per-call remote token
    # counting is both slow and rate-limit-prone, and AgentScope's own context
    # compression already bounds the loop. Disable the deterministic-pipeline
    # budget for this route so one busy counting endpoint cannot abort the job.
    model.budget_enabled = False

    async def emit(phase, status, message, **fields):
        if on_event:
            result = on_event({"phase": phase, "status": status, "message": message, **fields})
            if asyncio.iscoroutine(result):
                await result

    _observe_model_retries(model, emit)

    try:
        reference_info = await asyncio.to_thread(
            kimi_workspace.prepare_reference_material, data_root, project_id, request, workspace
        )
        reference_characters = reference_info.get("characters") or len(getattr(request, "references", "") or "")
        reference_summary = (
            f"，已加入 {reference_characters:,} 字参考资料。"
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
            for plan_attempt in range(plan_attempts):
                await emit(
                    "plan",
                    "started" if plan_attempt == 0 else "retrying",
                    "正在规划完整叙事与章节覆盖"
                    + (f"（计划校验未通过，第 {plan_attempt + 1} 次）" if plan_attempt else ""),
                )
                planning_text = _planning_message(request, template_info, reference_info, requirements)
                if plan_attempt:
                    planning_text = (
                        "宿主拒绝了上一版 deck/plan.json：\n- "
                        + "\n- ".join(last_issues[:30])
                        + "\n请重写完整 deck/plan.json，仍然禁止写任何页面。\n\n"
                        + planning_text
                    )
                planning_agent = _make_agent(model, workspace, project_id, plan_iters)
                model._ppt_event_phase = 'plan'
                await _reply_with_progress(
                    planning_agent,
                    Msg(
                        name="user",
                        role="user",
                        content=[{"type": "text", "text": planning_text}],
                    ),
                    emit=emit,
                    phase='plan',
                    activity='PPT 内容策划',
                    max_steps=plan_iters,
                )
                plan = kimi_plan.load_plan(plan_path)
                if plan:
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
        _, planned_paths, _, _ = kimi_workspace.deck_progress(source_entry)
        batch_limit = max(batch_limit, (len(planned_paths) + 4) // 5 + 2)
        await emit(
            "plan",
            "completed",
            f"完整计划已通过校验：{len(planned_paths)} 页，"
            f"覆盖 {len(requirements['principalSections'])} 个一级章节",
            covered=0,
            total=len(planned_paths),
        )
        for batch_index in range(batch_limit):
            continuation = None
            if source_entry is not None:
                _, paths, existing, missing, invalid = kimi_workspace.deck_status(source_entry)
                pending = [path for path in paths if path not in existing]
                if not pending:
                    break
                target_pages = kimi_workspace.batch_page_target(
                    len(pending), batch_limit - batch_index, batch_page_size
                )
                target_pages = min(5, target_pages)
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
            agent = _make_agent(model, workspace, project_id, batch_iters)
            message = Msg(
                name="user",
                role="user",
                content=[{
                    "type": "text",
                    "text": _execution_message(
                        request,
                        template_info,
                        reference_info,
                        plan,
                        continuation,
                    ),
                }],
            )
            model._ppt_event_phase = 'pages'
            reply = kimi_workspace.reply_text(await _reply_with_progress(
                agent,
                message,
                emit=emit,
                phase='pages',
                activity='逐页创作',
                max_steps=batch_iters,
            ))
            source_entry = kimi_workspace.find_entry(workspace)
            _, paths, existing, _, _ = kimi_workspace.deck_status(source_entry)
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
        _, planned_paths, existing_paths, _, invalid_paths = kimi_workspace.deck_status(source_entry)
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
        document: dict = {}
        pages: list[dict] = []
        entry: Path | None = None
        for structure_attempt in range(repair_rounds + 1):
            document, pages = kimi_workspace.load_deck(source_entry)
            _apply_host_transforms(source_entry, document, pages, template_info)
            shutil.rmtree(final_dir, ignore_errors=True)
            entry = kimi_workspace.write_deck(final_dir, source_entry, document, pages)
            issues = validate_document({"document": document, "pages": pages}, resource_root=entry.parent)
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
            model._ppt_event_phase = 'validate'
            reply = kimi_workspace.reply_text(await _reply_with_progress(
                repair_agent,
                message,
                emit=emit,
                phase='validate',
                activity='结构修复',
                max_steps=min(batch_iters, 14),
            ))

        if entry is None:
            raise ValueError("PPT 专家没有生成可检查的 PPTD 交付稿。")
        repair_state = _load_repair_state(workspace)
        page_attempts = dict(repair_state['attempts'])
        page_noops = dict(repair_state['noops'])
        autofit_attempts = repair_state['autofitAttempts']
        last_issue_count = repair_state['issueCount']
        last_issue_fingerprint = repair_state['issueFingerprint']
        model._ppt_event_phase = 'validate'
        while True:
            document, pages = kimi_workspace.load_deck(source_entry)
            _apply_host_transforms(source_entry, document, pages, template_info)
            shutil.rmtree(final_dir, ignore_errors=True)
            entry = kimi_workspace.write_deck(final_dir, source_entry, document, pages)
            issues = validate_document({"document": document, "pages": pages}, resource_root=entry.parent)
            if issues:
                detail = "; ".join(f"{i.get('location')}: {i.get('message')}" for i in issues[:8])
                raise ValueError(f"排版修复引入了结构问题：{detail}")
            await emit("validate", "started", "正在用真实浏览器排版检查整份交付稿（含封面、致谢与公司元素）。")
            rendered = await asyncio.to_thread(_render_pages, entry)
            try:
                layout, layouts = await asyncio.to_thread(
                    final_layout_report,
                    entry,
                    document,
                    pages,
                    rendered,
                )
                visual = []
                if not layout:
                    visual = await visual_overview_issues(
                        document, pages, rendered, on_event=on_event,
                        scenario=getattr(request, "scenario", None) or "auto",
                        cache_dir=data_root / "review-cache",
                    )
            finally:
                if rendered is not None:
                    shutil.rmtree(rendered, ignore_errors=True)
            layout = layout + visual
            if not layout:
                outstanding = []
                repair_state.update(
                    attempts=page_attempts,
                    noops=page_noops,
                    autofitAttempts=autofit_attempts,
                    issueCount=0,
                    issueFingerprint='',
                )
                _save_repair_state(workspace, repair_state)
                break

            source_paths = list(document.get("pages") or [])
            if repair_state['baselineIssueCount'] <= 0:
                repair_state['baselineIssueCount'] = len(layout)
            current_progress = _repair_progress(repair_state, len(layout))

            if autofit_attempts < policy.layout_autofit_budget:
                fitted_pages = deepcopy(pages)
                fitted = autofit_state(
                    {'pages': fitted_pages},
                    layouts,
                    layout,
                )
                if any(fitted.values()):
                    autofit_attempts += 1
                    _write_pages(source_entry, fitted_pages)
                    repair_state.update(
                        attempts=page_attempts,
                        noops=page_noops,
                        autofitAttempts=autofit_attempts,
                        issueCount=len(layout),
                        issueFingerprint=issue_fingerprint(layout),
                    )
                    last_issue_count = len(layout)
                    last_issue_fingerprint = issue_fingerprint(layout)
                    _save_repair_state(workspace, repair_state)
                    await emit(
                        'validate',
                        'retrying',
                        f'已对 {sum(fitted.values())} 个溢出元素执行确定性自适应，'
                        '正在重新检查；暂不调用修复 Agent。',
                        issueCount=len(layout),
                        progress=current_progress,
                    )
                    if progress:
                        progress(70, '确定性排版自适应完成，正在重新检查')
                    continue

            current_fingerprint = issue_fingerprint(layout)
            if (
                last_issue_fingerprint
                and current_fingerprint == last_issue_fingerprint
                and len(layout) >= last_issue_count
            ):
                outstanding = layout
                repair_state.update(
                    attempts=page_attempts,
                    noops=page_noops,
                    autofitAttempts=autofit_attempts,
                    issueCount=len(layout),
                    issueFingerprint=current_fingerprint,
                )
                _save_repair_state(workspace, repair_state)
                await emit(
                    'validate',
                    'waiting',
                    f'问题集合没有改善（仍为 {len(layout)} 项），'
                    '已停止重复修复并保留可编辑稿。',
                    issues=layout[:12],
                    issueCount=len(layout),
                    progress=current_progress,
                )
                break

            last_issue_count = len(layout)
            last_issue_fingerprint = current_fingerprint
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
                repair_state.update(
                    attempts=page_attempts,
                    noops=page_noops,
                    autofitAttempts=autofit_attempts,
                    issueCount=len(layout),
                    issueFingerprint=current_fingerprint,
                )
                _save_repair_state(workspace, repair_state)
                await emit(
                    "validate", "waiting",
                    f"仍有 {len(layout)} 项排版问题，已保留可编辑稿；可继续从检查点恢复。",
                    issues=layout[:12],
                    issueCount=len(layout),
                    progress=current_progress,
                )
                break

            batch = pending[:layout_page_batch]
            batch_issues = [
                issue for issue in layout if kimi_workspace.issue_page(issue) in batch
            ]
            page_numbers = [source_paths.index(path) + 1 for path in batch]
            before = kimi_workspace.page_hashes(source_entry, source_paths)
            for path in batch:
                page_attempts[path] = page_attempts.get(path, 0) + 1

            await emit(
                "validate", "retrying",
                f"排版检查发现 {len(layout)} 项问题；正在修复第 "
                f"{', '.join(str(number) for number in page_numbers)} 页。",
                issues=batch_issues[:12],
                issueCount=len(layout),
                progress=current_progress,
            )
            if progress:
                progress(70, f"正在修复第 {', '.join(str(number) for number in page_numbers)} 页的排版问题")
            screenshot_dir = workspace / f"layout-repair-{page_attempts[batch[0]]}-{page_numbers[0]}"
            noop_note = "".join(
                f"\n注意：上一轮没有实际修改 {path}，本轮必须修改该页面。"
                for path in batch if page_noops.get(path)
            )
            target_sources = _page_sources(source_entry, batch)
            message = Msg(name="user", role="user", content=[{"type": "text", "text": (
                "宿主的真实浏览器排版检查（渲染最终交付稿，已包含公司模板的封面/致谢与角落元素）发现以下问题：\n"
                + kimi_workspace.issue_summary(batch_issues, limit=len(batch_issues))
                + f"\n本轮只处理这些页面：{', '.join(batch)}（页码 {', '.join(str(number) for number in page_numbers)}）。"
                + f"\n请先运行 `kimi_slides screenshot deck -p {','.join(str(number) for number in page_numbers)} "
                  f"-o {screenshot_dir}` 查看实际渲染效果，再编辑对应的 .page 文件。"
                + "\n目标页面的当前源码已放在 targetPages 中，不要先用 Grep、Glob 或逐页 Read 探索。"
                  "截图后必须直接调用 Edit 或 Write；修改后只运行一次本轮页面的 "
                  "`kimi_slides check deck --level auto`，不要重复读取整个工作区。"
                + "\n\n目标页面源码：\n"
                + json.dumps({'targetPages': target_sources}, ensure_ascii=False)
                + "\n"
                  "保持文字与数据完整，不要改动 template-pptd/ 下的分析素材。"
                + noop_note
            )}])
            repair_agent = _make_agent(
                model,
                workspace,
                project_id,
                layout_iterations,
                allowed_tools=('Read', 'Write', 'Edit', 'kimi_slides'),
            )
            model._ppt_event_phase = 'validate'
            reply = kimi_workspace.reply_text(await _reply_with_progress(
                repair_agent,
                message,
                emit=emit,
                phase='validate',
                activity=f"第 {', '.join(str(number) for number in page_numbers)} 页排版修复",
                max_steps=layout_iterations,
            ))
            after = kimi_workspace.page_hashes(source_entry, source_paths)
            changed_pages = [
                path for path in source_paths
                if after[path] != before[path]
            ]
            changed_batch = [path for path in batch if path in changed_pages]
            if not changed_pages:
                for path in batch:
                    page_noops[path] = page_noops.get(path, 0) + 1
                outstanding = layout
                repair_state.update(
                    attempts=page_attempts,
                    noops=page_noops,
                    autofitAttempts=autofit_attempts,
                    issueCount=len(layout),
                    issueFingerprint=current_fingerprint,
                )
                _save_repair_state(workspace, repair_state)
                await emit(
                    'validate',
                    'waiting',
                    '本轮 Agent 没有修改任何页面，已停止自动修复；'
                    '不会再次执行无意义的整稿检查。',
                    issues=layout[:12],
                    issueCount=len(layout),
                    progress=current_progress,
                )
                break
            for path in batch:
                if path not in changed_pages:
                    page_noops[path] = page_noops.get(path, 0) + 1
                else:
                    page_noops[path] = 0
            repair_state.update(
                attempts=page_attempts,
                noops=page_noops,
                autofitAttempts=autofit_attempts,
                issueCount=len(layout),
                issueFingerprint=current_fingerprint,
                lastChangedPages=changed_batch,
            )
            _save_repair_state(workspace, repair_state)
        from .pptd_store import DocumentStore

        store = DocumentStore(data_root)
        revision = store.read(project_id)["revision"] if store.exists(project_id) else 0
        committed = store.commit(
            project_id, document, pages, "AI 生成（PPT 专家）", revision,
            assets=kimi_workspace.collect_assets(entry),
        )
        await emit(
            "validate",
            "completed",
            (
                f'排版检查保留 {len(outstanding)} 项未解决问题，'
                '已提交可编辑文档供确认。'
                if outstanding
                else '结构检查与真实排版检查通过，已提交可编辑文档。'
            ),
            issueCount=len(outstanding),
            progress=100,
        )
        if outstanding:
            reply = (reply + "\n\n## 排版检查未通过项\n\n"
                     "以下问题已定位到页面与元素；文档仍可编辑预览，请确认或调整后再导出：\n\n"
                     + kimi_workspace.issue_summary(outstanding)).strip()
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
                "workspace": str(workspace),
            },
        }
    finally:
        await model.client.close()


def kimi_health() -> dict:
    from .model_gateway import health

    return health()
