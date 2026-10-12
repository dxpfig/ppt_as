"""Real AgentScope planning and writing; configuration errors never simulate AI."""
import json
import math
import os
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlparse
from dotenv import load_dotenv

from agentscope.agent import Agent, ReActConfig, ContextConfig
from agentscope.credential import AnthropicCredential, OpenAICredential
from agentscope.message import Msg
from agentscope.model import AnthropicChatModel, OpenAIChatModel
from agentscope.middleware import TracingMiddleware
from agentscope.state import AgentState

from .models import DeckContent, JobRequest, Plan
from .prompt_library import PromptLibrary, SCENARIOS
from .model_context import BudgetedOpenAIChatModel, BudgetedAnthropicChatModel, context_settings, PROTECTED_REQUEST_FIELDS

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


def configuration():
    provider = os.getenv("PPT_LLM_PROVIDER", "anthropic" if os.getenv("ANTHROPIC_AUTH_TOKEN") or os.getenv("ANTHROPIC_API_KEY") else "openai")
    if provider == "anthropic":
        token = os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")
        model = os.getenv("PPT_LLM_MODEL") or os.getenv("ANTHROPIC_MODEL") or os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL")
    else:
        token = os.getenv("OPENAI_API_KEY")
        model = os.getenv("PPT_LLM_MODEL") or os.getenv("OPENAI_MODEL")
    return provider, token, model


def health():
    provider, token, model = configuration()
    return {"llmConfigured": bool(token and model), "model": model or "未配置", "provider": provider}


def timeout_setting(name, default):
    try:
        value = float(os.getenv(name) or default)
        if not math.isfinite(value) or not 1 <= value <= 14400:
            raise ValueError()
        return value
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须为 1–14400 秒。") from exc


def make_model(*, output_mode="default", observable_retries=False):
    provider, token, model = configuration()
    if output_mode not in ("default", "auto", "adaptive", "direct"):
        raise ValueError("未知模型输出模式。")
    official_m3 = provider == "openai" and model == "MiniMax-M3" and urlparse(os.getenv("OPENAI_BASE_URL", "")).hostname in ("api.minimax.io", "api.minimaxi.com")
    if output_mode == "direct" and not official_m3:
        raise ValueError("直接输出模式仅对已验证的官方 MiniMax-M3 接口可用。")
    retries = 0 if observable_retries else 1
    if not token or not model:
        raise ValueError("尚未配置大模型。请设置 PPT_LLM_PROVIDER、PPT_LLM_MODEL 与对应 API Key 后重启服务。")
    timeout = timeout_setting('PPT_LLM_TIMEOUT_SECONDS', 180)
    try:
        max_tokens = int(os.getenv('PPT_LLM_MAX_TOKENS') or 16000)
        if not 1 <= max_tokens <= 65536: raise ValueError()
    except ValueError as exc:
        raise ValueError('PPT_LLM_MAX_TOKENS 必须为 1–65536 的整数。') from exc
    if provider == "anthropic":
        kwargs = {"timeout": timeout, "max_retries": 0}
        if os.getenv("ANTHROPIC_AUTH_TOKEN") and not os.getenv("ANTHROPIC_API_KEY"):
            kwargs["auth_token"] = token
        budget_enabled, window, margin, _, context_mode, explicit_window = context_settings(os.getenv('ANTHROPIC_BASE_URL'))
        model_instance = BudgetedAnthropicChatModel(
            credential=AnthropicCredential(api_key=token, base_url=os.getenv("ANTHROPIC_BASE_URL") or None),
            model=model, stream=False, max_retries=retries,
            parameters=AnthropicChatModel.Parameters(max_tokens=max_tokens), client_kwargs=kwargs,
            context_size=window, budget_enabled=budget_enabled, safety_tokens=margin,
            context_mode=context_mode, explicit_window=explicit_window,
        )
        model_instance.pptd_request_timeout = timeout
        return model_instance
    if provider != "openai":
        raise ValueError("PPT_LLM_PROVIDER 仅支持 anthropic 或 openai。")
    # Official M3 accepts this explicit switch; reasoning_effort is ignored by
    # M3 and reasoning_split alone does not reduce its thinking token budget.
    controlled = official_m3 and output_mode != "default"
    effective = ("adaptive" if output_mode == "adaptive" else "direct") if controlled else "provider-default"
    stream_setting = os.getenv('PPT_LLM_STREAM', '').strip().lower()
    if stream_setting not in ('', 'true', 'false', '1', '0'):
        raise ValueError('PPT_LLM_STREAM 必须为 true 或 false。')
    stream = controlled if not stream_setting else stream_setting in ('true', '1')
    try:
        extra = json.loads(os.getenv('PPT_LLM_EXTRA_BODY') or '{}')
        if not isinstance(extra, dict) or PROTECTED_REQUEST_FIELDS.intersection(extra): raise ValueError()
    except (TypeError, ValueError) as exc:
        raise ValueError('PPT_LLM_EXTRA_BODY 必须为 JSON 对象，且不能覆盖模型、消息、工具、流式或输出额度参数。') from exc
    if controlled:
        extra.update(thinking={"type": "disabled" if effective == "direct" else "adaptive"}, reasoning_split=True)
    elif extra:
        effective = 'configured'
    budget_enabled, window, margin, tokenizer, context_mode, explicit_window = context_settings(os.getenv('OPENAI_BASE_URL'))
    model_instance = BudgetedOpenAIChatModel(
        credential=OpenAICredential(api_key=token, base_url=os.getenv("OPENAI_BASE_URL") or None),
        model=model, stream=stream, max_retries=retries,
        parameters=OpenAIChatModel.Parameters(max_tokens=max_tokens),
        client_kwargs={"timeout": timeout, "max_retries": 0},
        extra_body=extra or None,
        context_size=window, budget_enabled=budget_enabled, safety_tokens=margin, tokenizer_url=tokenizer,
        context_mode=context_mode, explicit_window=explicit_window,
    )
    model_instance.pptd_output_mode = effective
    model_instance.pptd_request_timeout = timeout
    return model_instance


def make_agent(name, prompt, model, session_id=None):
    return Agent(name=name, system_prompt=prompt, model=model,
                 middlewares=[TracingMiddleware()],
                 state=AgentState(session_id=session_id) if session_id else None,
                 react_config=ReActConfig(max_iters=3, structured_output_grace_iters=2, interruption_raise_cancelled_error=True),
                 context_config=ContextConfig(compression_tool_enabled=False, trigger_ratio=.9,
                                              compression_fallback_to_truncation=False))


async def generate(request: JobRequest, progress):
    prompts = PromptLibrary()
    model = make_model()
    try:
        context = {"request": request.prompt, "audience": request.audience, "pageCount": request.pageCount,
                   "imageSource": request.project.imageSource, "theme": request.project.theme,
                   "usingCompanyTemplate": bool(request.templateId or request.project.templateId),
                   "references": request.references or request.project.references,
                   "conversation": [{"role": m.get("role"), "text": str(m.get("text", ""))[:6000]} for m in request.project.messages[-20:]]}
        async def ask(name, role, schema, data, scenario="general"):
            agent = make_agent(name, prompts.system(role, scenario), model, request.project.id)
            result = await agent.reply(Msg(name="user", content=[{"type": "text", "text": json.dumps(data, ensure_ascii=False)}], role="user"), structured_schema=schema)
            return schema.model_validate(result.structured_output)
        scenario = "general"
        plan = None
        if request.kind == "outline":
            progress(0, "策划 Agent 正在确定叙事、页面目标与版式")
            plan = await ask("PPTPlanner", "planner", Plan, context)
            if request.pageCount is not None and len(plan.pages) != request.pageCount:
                raise ValueError(f"规划返回 {len(plan.pages)} 页，与要求的 {request.pageCount} 页不符，请重试。")
            scenario = plan.scenario
            context["plan"] = plan.model_dump()
            progress(1, "内容 Agent 正在按页面设计撰写内容")
            content = await ask("PPTWriter", "writer", DeckContent, context, scenario)
        else:
            progress(1, "编辑 Agent 正在根据要求修改内容")
            selected = [slide for slide in request.project.slides if slide.id == request.selectedSlideId] if request.selectedSlideId else request.project.slides
            if not selected:
                raise ValueError("未找到要修改的幻灯片。")
            metadata = getattr(request.project, "generationMeta", None) or {}
            scenario = metadata.get("scenario", "general") if isinstance(metadata, dict) else "general"
            if scenario not in SCENARIOS:
                scenario = "general"
            context.update(pageCount=len(selected), existingSlides=[s.model_dump() for s in selected])
            content = await ask("PPTEditor", "editor", DeckContent, context, scenario)
        expected = (request.pageCount or len(plan.pages)) if request.kind == "outline" else len(selected)
        if len(content.slides) != expected:
            raise ValueError(f"模型返回 {len(content.slides)} 页，与要求的 {expected} 页不符，请重试。")
        issues = content_issues(content)
        if issues:
            progress(2, "正在检查内容密度与版式适配")
            content = await ask("PPTRefiner", "repair", DeckContent, {**context, "draft": content.model_dump(), "issues": issues}, scenario)
            if len(content.slides) != expected:
                raise ValueError("版式修订改变了请求页数，请重试。")
        slides = [dict(s.model_dump(), id=uuid4().hex) for s in content.slides]
        if request.kind == "revise":
            replacements = {}
            for old, new in zip(selected, slides):
                new.update(id=old.id, imageDisabled=old.imageDisabled)
                if old.image and new.get("imageQuery") == old.imageQuery:
                    new["image"] = old.image.model_dump()
                replacements[old.id] = new
            slides = [replacements.get(old.id, old.model_dump()) for old in request.project.slides]
        progress(2, "内容校验完成，可确认后生成演示文件")
        metadata = {"promptVersion": prompts.version, "scenario": scenario, "contentWarnings": content_issues(content)}
        if plan:
            metadata.update(narrative=plan.narrative, visualDirection=plan.visualDirection)
        return {"title": request.project.title if request.kind == "revise" else content.title, "slides": slides, "reply": content.reply, "generationMeta": metadata}
    finally:
        await model.client.close()


def content_issues(content):
    issues = []
    for i, slide in enumerate(content.slides, 1):
        if len(slide.title) > 45 or sum(map(len, slide.points)) > 420:
            issues.append(f"第 {i} 页文字密度过高，请精简重复表述。")
        if slide.layout == "comparison" and len(slide.points) != 2:
            issues.append(f"第 {i} 页对比版式需要恰好两个选项，或改为重点阐述。")
        if slide.layout == "process" and not 2 <= len(slide.points) <= 5:
            issues.append(f"第 {i} 页流程版式需要 2–5 个步骤，或改为重点阐述。")
    return issues
