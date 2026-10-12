"""Template route: keep the company deck's own pages and only change their text.

Reverse-engineering kimi-slides showed the routing we were missing. A new deck is
authored as PPTD, but a real corporate deck is *edited where it is*, because
converting it is lossy: add-in data (`ppt/tags/`, think-cell), embedded workbooks
and embedded fonts do not survive the round trip and the loss is silent.

So this route never rebuilds the template:

1. read the template's page/paragraph inventory (`prep`),
2. let the model choose which template pages the new deck keeps and write the new
   text for the paragraphs it names (cover title/date/presenter, body copy, ...),
3. rewrite only those paragraphs, drop the unused pages, verify, render previews.

The published deck is the company template itself with new words: background,
right-top logo, right-bottom footer, masters, theme, fonts and add-ins stay
byte-identical.
"""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from agentscope.message import Msg

from . import surgical
from . import template_spec
from . import template_reference
from .kimi_rules import KimiRules
from .pptd_generation import _await_model, _message_content, _parse_json_response
from .agents import make_agent

MAX_PARAGRAPHS_PER_PAGE = 80
MAX_PAGES = 60
TEXT_LIMIT = 5000


class TemplateParagraph(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=40)
    text: str = Field(max_length=TEXT_LIMIT)


class TemplatePage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    number: int = Field(ge=1, le=400)
    title: str = Field(min_length=1, max_length=200)
    paragraphs: list[TemplateParagraph] = Field(min_length=1, max_length=MAX_PARAGRAPHS_PER_PAGE)


class TemplateDeck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    pages: list[TemplatePage] = Field(min_length=1, max_length=MAX_PAGES)


def inventory(pptx: Path) -> dict:
    """Page numbers, stable paragraph addresses and the template's own words."""
    info = surgical.inspect(pptx)
    pages = []
    for page in info["slides"]:
        paragraphs = [{"id": item["id"], "shape": item["shape"], "text": item["text"], "lines": item["lines"]}
                      for item in page["paragraphs"] if item["text"].strip()]
        if paragraphs:
            pages.append({"number": page["number"], "idPrefix": f"s{page['number']}#",
                          "paragraphs": paragraphs[:MAX_PARAGRAPHS_PER_PAGE]})
    return {"census": info["census"], "addins": info["addins"], "pageCount": len(info["slides"]), "pages": pages}


def validate_deck(deck: Any, pages: dict) -> list[dict]:
    """Every named page and paragraph must exist in the template inventory."""
    issues: list[dict] = []

    def fail(code: str, location: str, message: str) -> None:
        issues.append({"code": code, "location": location, "message": message})

    if not isinstance(deck, dict):
        fail("InvalidDeck", "deck", "模板路线需要 {title, pages} 对象。")
        return issues
    known = {page["number"]: {item["id"] for item in page["paragraphs"]} for page in pages["pages"]}
    numbers = [page.get("number") for page in deck.get("pages") or [] if isinstance(page, dict)]
    if len(set(numbers)) != len(numbers):
        fail("DuplicatePage", "pages", "同一模板页不能出现两次。")
    for index, page in enumerate(deck.get("pages") or []):
        if not isinstance(page, dict):
            fail("InvalidPage", f"pages[{index}]", "页必须是对象。")
            continue
        number = page.get("number")
        if number not in known:
            fail("UnknownTemplatePage", f"pages[{index}].number",
                 f"模板没有第 {number} 页；可选：{sorted(known)[:8]}…")
            continue
        identities = [item.get("id") for item in page.get("paragraphs") or [] if isinstance(item, dict)]
        wrong_page = [identity for identity in identities
                      if isinstance(identity, str) and not identity.startswith(f"s{number}#")]
        if wrong_page:
            fail("AddressPageMismatch", f"pages[{index}].paragraphs",
                 f"id 必须属于第 {number} 页（前缀 s{number}#），这些是别的页：{wrong_page[:5]}")
        unknown = [identity for identity in identities if identity not in known[number]]
        if unknown:
            fail("UnknownParagraph", f"pages[{index}].paragraphs", f"这些地址不属于模板第 {number} 页：{unknown[:5]}")
        if len(set(identities)) != len(identities):
            fail("DuplicateParagraph", f"pages[{index}].paragraphs", "同一段不能改两次。")
    if numbers:
        if numbers[0] != 1:
            fail("CoverRequired", "pages[0].number", "第一页必须使用模板封面（第 1 页），只改它的文字。")
        closing = max(known)
        if len(numbers) > 1 and numbers[-1] != closing:
            fail("ClosingRequired", "pages[-1].number", f"最后一页必须使用模板结束页（第 {closing} 页）。")
    return issues


def operations(deck: dict) -> list[dict]:
    """Flatten the model's per-page text into surgical operations."""
    return [{"id": item["id"], "text": item["text"]}
            for page in deck["pages"] for item in page["paragraphs"]]


def apply_deck(template: Path, deck: dict, folder: Path) -> dict:
    """Rewrite the named paragraphs, then keep only the chosen pages."""
    folder.mkdir(parents=True, exist_ok=True)
    edited = folder / "edited.pptx"
    published = folder / "presentation.pptx"
    applied = surgical.apply(template, operations(deck), edited)
    selected = surgical.select(edited, [page["number"] for page in deck["pages"]], published)
    edited.unlink(missing_ok=True)
    return {"output": str(published), "pages": selected["pages"], "operations": applied["operations"],
            "verification": {"edit": applied["verification"], "select": selected["verification"]},
            "deletedParts": selected["dropped"]}


def prompt_context(request, pages: dict, scenario: str, issues: list[dict] | None = None) -> dict:
    """Everything the model needs to plan the deck, and nothing else."""
    context = {
        "request": request.prompt,
        "audience": request.audience,
        "pageCount": getattr(request, "pageCount", None),
        "scenario": scenario,
        "references": request.references or "",
        "template": pages,
        "instructions": {
            "address": ("number 是**模板页号**（清单里的 number），不是新 deck 的页序；"
                        "该页 paragraphs 的每个 id 必须以 s<number># 开头，跨页复用会直接判错。"),
            "cover": "第一页使用模板封面（第 1 页），只改标题、副标题、日期、汇报人。",
            "closing": "最后一页使用模板结束页，只改它的文字。",
            "chrome": "页脚公司名、版权行、Logo 文字、页码属于公司固定元素，原样保留，不要列进 paragraphs。",
            "length": "每条新文字与模板原文长度相当：小标签保持短，正文可同量级。",
            "pages": "按本次需求选择保留哪些模板页及其顺序；页数由你的选择决定。",
        },
    }
    if issues:
        context["validationErrors"] = issues
    return context


async def ask_deck(model, rules: KimiRules, request, pages: dict, scenario: str, *,
                   issues: list[dict] | None = None, session_id=None, on_event=None):
    """One strict JSON round trip; the caller repairs from the returned issues."""
    contract = ("\n\nJSON TRANSPORT CONTRACT\nReturn exactly one JSON object as assistant text, no tool call, "
                "no XML, no explanation, no code fence. Schema:\n"
                + json.dumps(TemplateDeck.model_json_schema(), ensure_ascii=False))
    system = rules.system("template", scenario, template=True) + contract
    agent = make_agent("TemplateDeck", system, model, session_id)
    message = Msg(name="user", content=_message_content(prompt_context(request, pages, scenario, issues)), role="user")
    # The inventory is large, so allow a longer single request than the PPTD
    # stages - but never the provider's full timeout, which can leave a job
    # hanging for half an hour before anyone sees a reason.
    timeout = min(float(getattr(model, "pptd_request_timeout", 180)), 480)
    response = await _await_model(agent, message, {"phase": "pages"}, on_event, timeout=timeout)
    parsed = _parse_json_response(response.get_text_content(), reasoning_prefix="minimax" in str(getattr(model, "model", "")).lower())
    return TemplateDeck.model_validate(parsed, strict=True).model_dump()


async def ask_plan(model, rules: KimiRules, request, spec: dict, tokens: dict, scenario: str, *,
                   issues: list[dict] | None = None, session_id=None, on_event=None,
                   rows: dict[str, int] | None = None):
    """One strict JSON round trip for the rebuild plan."""
    from .agents import make_agent
    contract = ('\n\nJSON TRANSPORT CONTRACT\nReturn exactly one JSON object as assistant text, no tool call, '
                'no XML, no explanation, no code fence. Schema:\n'
                + json.dumps(template_spec.BuildPlan.model_json_schema(), ensure_ascii=False))
    system = rules.system('template-build', scenario, template=True) + contract
    agent = make_agent('TemplateBuild', system, model, session_id)
    payload = {
        'request': request.prompt,
        'audience': request.audience,
        'pageCount': getattr(request, 'pageCount', None),
        'templateSpec': spec,
        'design': tokens,
        # Spelled out so the model never has to infer which pages copy the template
        # and which slot names they carry: only these three pages take pageType.
        'faithfulPages': [
            {'pageType': page_type['id'], 'templatePage': (page_type.get('templatePages') or [None])[0],
             'mustFill': [slot['name'] for slot in page_type.get('slots') or [] if not slot.get('repeatable')],
             'mayRepeat': [slot['name'] for slot in page_type.get('slots') or [] if slot.get('repeatable')],
             'maxRows': (rows or {}).get(page_type['id'])}
            for page_type in spec.get('pageTypes') or []
            if page_type['id'] in template_spec.faithful_page_types(spec)],
        'bodyLayouts': {layout: template_spec.LAYOUT_CAPACITY[layout]
                        for layout in template_spec.LAYOUT_CAPACITY},
    }
    if issues:
        payload['validationErrors'] = issues
    message = Msg(name='user', content=_message_content(payload), role='user')
    response = await _await_model(agent, message, {'phase': 'plan'}, on_event,
                                  timeout=min(float(getattr(model, 'pptd_request_timeout', 180)), 480))
    parsed = _parse_json_response(response.get_text_content(),
                                  reasoning_prefix='minimax' in str(getattr(model, 'model', '')).lower())
    return template_spec.BuildPlan.model_validate(parsed, strict=True).model_dump()


async def build_deck(request, progress, *, root: Path, template_id: str, model,
                     rules: KimiRules | None = None, on_event=None, attempts: int = 3):
    """Rebuild route: plan against the template's spec, then compose new pages.

    The spec has already measured the template (page types, slots, capacities,
    repeating units). The model only chooses pages and writes text inside those
    limits, and the composer grows repeating slots to the content's own count.
    """
    from .kimi_rules import SCENARIOS
    rules = rules or KimiRules()
    scenario = getattr(request, 'scenario', 'auto')
    if scenario not in SCENARIOS:
        scenario = 'management-report'
    # A reference measured by an older importer is re-derived before it styles a
    # new deck: what it could not read then would come back as wrong colouring now.
    rederived = template_reference.stale(root, template_id)
    reference = None if rederived else template_reference.load(root, template_id)
    template_pptx = Path(root) / 'templates' / str(template_id) / 'template.pptx'
    if not template_pptx.is_file():
        raise TemplateIncomplete('未找到公司模板文件，请重新上传。')
    if not reference:
        # A project may jump straight to "rebuild" without importing the template
        # first: importing is bookkeeping this route can do for itself.
        progress(0, '正在重新解析公司模板（模板解析能力已更新）' if rederived
                 else '正在解析公司模板（首次使用需要一次结构分析）')
        await asyncio.to_thread(template_reference.build, template_pptx, Path(root), str(template_id))
        reference = template_reference.load(root, template_id)
        if not reference:
            raise TemplateIncomplete('模板解析失败，请检查模板文件后重试。')
    folder = template_reference.reference_path(root, template_id) / 'overview'
    # The induced spec was read from the previous pages, so it is measured again
    # together with them.
    spec = None if rederived else template_spec.load_spec(folder)
    if not spec:
        # Induction reads the rendered overview and the measured tokens; both are
        # produced here the first time this template is rebuilt.
        progress(0, '首次重建：正在渲染模板总览并归纳版面规范')
        await template_spec.ensure(folder, template_pptx)
        spec = await template_spec.induce(model, rules, folder=folder, summary=reference['summary'],
                                          tokens=reference['summary'].get('design') or {},
                                          components_found=reference['summary'].get('components') or [],
                                          session_id=getattr(getattr(request, 'project', None), 'id', None),
                                          scenario=scenario)
        if not spec:
            raise TemplateIncomplete('未能归纳模板规范，请重试。')
    sources = reference['sources']
    pages = [json.loads(sources[key]) for key in sorted(sources)]
    progress(0, f"已读取模板规范（{len(spec['pageTypes'])} 个页型），正在规划本次页面与内容")
    # The kit is what a composed body page keeps: background, top-right logo,
    # bottom-right company mark, title treatment and the type tokens. It is built
    # first so the plan is measured against the very boxes the composer will draw.
    from . import template_kit
    kit = await asyncio.to_thread(template_kit.extract, pages, reference['summary'].get('design') or {},
                                  reference['summary'].get('components') or [])
    # How many rows the template's own list pages hold, so the plan keeps its
    # contents page inside the room the designer drew for it.
    rows = {page_type['id']: template_spec.repeat_capacity(pages[(page_type.get('templatePages') or [1])[0] - 1])
            for page_type in spec['pageTypes']
            if page_type['id'] in template_spec.faithful_page_types(spec)
            and (page_type.get('templatePages') or [0])[0] <= len(pages)}
    rows = {key: value for key, value in rows.items() if value}
    issues: list[dict] = []
    plan = None
    previous: set[str] = set()
    for attempt in range(attempts):
        try:
            plan = await ask_plan(model, rules, request, spec, reference['summary'].get('design') or {},
                                  scenario, issues=issues or None,
                                  session_id=getattr(getattr(request, 'project', None), 'id', None),
                                  on_event=on_event, rows=rows)
        except Exception as error:  # provider failures must fail with a reason
            issues = [{'code': 'ModelUnavailable', 'location': 'plan',
                       'message': f'{type(error).__name__}: {error}'[:300]}]
        else:
            issues = template_spec.validate_plan(plan, spec, reference['summary'], kit, pages)
            if not issues:
                break
        fingerprint = json.dumps([issue.get('message') for issue in issues], ensure_ascii=False)
        if fingerprint in previous:
            raise TemplateIncomplete('模板重建计划连续返回相同问题，已停止。', plan, issues)
        previous.add(fingerprint)
        progress(1, f"第 {attempt + 1} 轮调整：{len(issues)} 项需要修正")
    if issues or plan is None:
        raise TemplateIncomplete('未能生成符合模板规范的计划。', plan, issues)
    progress(2, f"已确定 {len(plan['pages'])} 页：封面与目录沿用模板，正文按模板页眉页脚与背景自由排版")
    built = await asyncio.to_thread(template_spec.build, pages, spec, plan, kit)
    progress(3, '已按模板组装完成，准备渲染与导出验收')
    return {'plan': plan, 'document': built['document'], 'pages': built['pages'],
            'assets': reference['media']}


class TemplateIncomplete(ValueError):
    """The model could not produce a valid deck for this template."""

    def __init__(self, message: str, deck: dict | None = None, issues: list[dict] | None = None):
        super().__init__(message)
        self.deck = deck
        self.issues = issues or []


async def generate(request, progress, *, template_path: Path, model, rules: KimiRules | None = None,
                   folder: Path, on_event=None, attempts: int = 3):
    """Inventory -> model deck -> surgical apply -> verify. No PPTD in the middle."""
    from .kimi_rules import SCENARIOS
    rules = rules or KimiRules()
    template_path = Path(template_path)
    pages = await asyncio.to_thread(inventory, template_path)
    if not pages["pages"]:
        raise TemplateIncomplete("模板里没有可编辑文字，无法套用。")
    scenario = getattr(request, "scenario", "auto")
    if scenario not in SCENARIOS:
        scenario = "management-report"
    progress(0, f"已读取模板 {pages['pageCount']} 页，准备按本次需求选择页面与内容")
    deck = None
    issues: list[dict] = []
    previous: set[str] = set()
    for attempt in range(attempts):
        try:
            deck = await ask_deck(model, rules, request, pages, scenario, issues=issues or None,
                                  session_id=getattr(getattr(request, "project", None), "id", None), on_event=on_event)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # A slow or unavailable provider must fail this job with a readable
            # reason instead of retrying silently for tens of minutes. Provider
            # SDK errors (APITimeoutError, APIConnectionError, ...) are not
            # OSError subclasses, so this deliberately catches broadly and the
            # caller still reports the failure rather than hanging.
            detail = str(error) or type(error).__name__
            issues = [{"code": "ModelUnavailable", "location": "deck",
                       "message": f"{type(error).__name__}: {detail}"[:300]}]
        else:
            issues = validate_deck(deck, pages)
            if not issues:
                break
        fingerprint = json.dumps([issue.get("message") for issue in issues], ensure_ascii=False)
        if fingerprint in previous:
            raise TemplateIncomplete("模板内容连续返回相同问题，已停止。", deck, issues)
        previous.add(fingerprint)
        progress(1, f"第 {attempt + 1} 轮调整：{len(issues)} 项需要修正")
    if issues:
        raise TemplateIncomplete("未能生成符合模板的页面与文字。", deck, issues)
    progress(2, f"已确定 {len(deck['pages'])} 页：正在按模板原位替换文字")
    report = await asyncio.to_thread(apply_deck, template_path, deck, Path(folder))
    progress(3, "文字已按模板套用，页面与原文件其余部分保持逐字节一致")
    return {"deck": deck, "report": report}
