"""Budget the actual provider messages, including output space and repair history."""
import hashlib
import json
import os
import asyncio
import copy
from contextlib import contextmanager
from contextvars import ContextVar
from collections import OrderedDict
from urllib.parse import urlsplit

from agentscope.model import OpenAIChatModel, AnthropicChatModel

PROTECTED_REQUEST_FIELDS = {'model', 'messages', 'stream', 'stream_options', 'max_tokens',
                            'max_completion_tokens', 'tools', 'tool_choice', 'functions',
                            'function_call', 'system', 'input', 'prompt', 'instructions', 'response_format'}


class ContextBudgetError(ValueError):
    failure_kind = 'context-limit'

    def __init__(self, budget):
        self.budget = budget
        counting = budget.get('counting')
        label = '输入保守估算上界' if counting == 'conservative-utf8' else '输入服务端估算' if counting == 'responses-input-tokens' else '输入'
        window = '回退窗口' if budget.get('contextSource') == 'fallback' else '模型窗口'
        super().__init__(f"{label} {budget['inputTokens']} tokens，输出预留 {budget['outputTokens']}，"
                         f"安全余量 {budget['safetyTokens']}，超过可用上下文预算 "
                         f"{budget['inputLimit']}（{window} {budget['contextLimit']}）")


class ContextCountError(ValueError):
    failure_kind = 'context-count'

    def __init__(self):
        super().__init__('无法取得有效的服务端 token 计数，已停止提交请求；请检查计数服务后恢复。')


def responses_count_payload(payload):
    """Translate complete Chat messages to the standard Responses count format.

    Never silently omit an unknown message field or media type. These requests
    need a compatible counter instead of a misleading partial token count.
    """
    if set(payload) - {'model', 'messages', 'tools'}:
        raise ContextCountError()

    def content(value, role):
        if isinstance(value, str):
            return value
        if not isinstance(value, list):
            raise ContextCountError()
        result = []
        for block in value:
            if block.get('type') == 'text' and set(block) <= {'type', 'text'} and isinstance(block.get('text'), str):
                result.append({'type': 'output_text' if role == 'assistant' else 'input_text', 'text': block['text']})
            elif block.get('type') == 'image_url' and set(block) <= {'type', 'image_url'}:
                image = block['image_url']
                if not isinstance(image, dict) or set(image) - {'url', 'detail'} or not isinstance(image.get('url'), str):
                    raise ContextCountError()
                result.append({'type': 'input_image', 'image_url': image['url'],
                               **({'detail': image['detail']} if 'detail' in image else {})})
            else:
                raise ContextCountError()
        return result

    result = {'model': payload['model'], 'input': []}
    for message in payload['messages']:
        role = message.get('role')
        if (set(message) - {'role', 'name', 'content', 'tool_calls', 'tool_call_id', 'reasoning_content'} or
                role not in ('system', 'developer', 'user', 'assistant', 'tool')):
            raise ContextCountError()
        if 'name' in message and not isinstance(message['name'], str):
            raise ContextCountError()
        if message.get('reasoning_content'):
            if role != 'assistant' or not isinstance(message['reasoning_content'], str):
                raise ContextCountError()
            result['input'].append({'type': 'reasoning', 'summary': [
                {'type': 'summary_text', 'text': message['reasoning_content']}]})
        if role == 'tool':
            if not isinstance(message.get('tool_call_id'), str) or message.get('tool_calls'):
                raise ContextCountError()
            result['input'].append({'type': 'function_call_output', 'call_id': message['tool_call_id'],
                                    'output': content(message.get('content'), role)})
            continue
        if 'tool_call_id' in message or (message.get('tool_calls') and role != 'assistant'):
            raise ContextCountError()
        if message.get('content') is not None:
            result['input'].append({'type': 'message', 'role': role, 'content': content(message['content'], role)})
        for call in message.get('tool_calls') or []:
            function = call.get('function', {})
            if (set(call) - {'id', 'type', 'function'} or call.get('type') != 'function' or
                    set(function) != {'name', 'arguments'} or
                    not all(isinstance(v, str) for v in (call.get('id'), function.get('name'), function.get('arguments')))):
                raise ContextCountError()
            result['input'].append({'type': 'function_call', 'call_id': call['id'], **function})
    if payload.get('tools'):
        result['tools'] = []
        for tool in payload['tools']:
            if tool.get('type') != 'function' or set(tool) != {'type', 'function'}:
                raise ContextCountError()
            function = tool['function']
            if not isinstance(function, dict) or set(function) - {'name', 'description', 'parameters', 'strict'}:
                raise ContextCountError()
            result['tools'].append({'type': 'function', **copy.deepcopy(function)})
    return result


def responses_name_overhead(payload):
    # Responses has no Chat `name` field. Count its serialized bytes plus
    # framing conservatively instead of silently losing named-agent metadata.
    return sum(len(json.dumps({'name': message['name']}, ensure_ascii=False).encode()) + 16
               for message in payload['messages'] if 'name' in message)


def integer_setting(name, default, minimum=1, maximum=4_194_304):
    try:
        value = int(os.getenv(name) or default)
        if not minimum <= value <= maximum:
            raise ValueError()
        return value
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{name} 必须为 {minimum}–{maximum} 的整数。') from exc


def context_settings(base_url):
    mode = os.getenv('PPT_LLM_CONTEXT_MODE', 'auto').strip() or 'auto'
    if mode not in ('auto', 'configured', 'off'):
        raise ValueError('PPT_LLM_CONTEXT_MODE 必须为 auto、configured 或 off。')
    enabled = mode != 'off'
    window = integer_setting('PPT_LLM_CONTEXT_TOKENS', 128000)
    margin = integer_setting('PPT_LLM_CONTEXT_MARGIN', 8192, minimum=0)
    endpoint = os.getenv('PPT_LLM_TOKENIZER_URL', '').strip()
    if endpoint:
        target, origin = urlsplit(endpoint), urlsplit(base_url or 'https://api.openai.com/v1')
        def authority(u):
            return u.scheme, u.hostname, u.port or (443 if u.scheme == 'https' else 80)
        if (target.scheme not in ('http', 'https') or not target.hostname or target.username or
                target.password or target.query or target.fragment or authority(target) != authority(origin)):
            raise ValueError('PPT_LLM_TOKENIZER_URL 必须与模型接口同源，且不能包含凭据或查询参数。')
    return enabled, window, margin, endpoint, mode, bool(os.getenv('PPT_LLM_CONTEXT_TOKENS'))


class ContextBudgetMixin:
    """Keep AgentScope's tracing/streaming while guarding every actual call.

    Provider counts are cached by the complete formatted request (not by length).
    No prompts, credentials, or raw server errors are exposed in diagnostics.
    Disabled budgeting leaves provider defaults intact.
    Each provider formats and counts exactly the messages it sends.
    """
    native_counting = False
    output_token_parameter = 'max_completion_tokens'

    def __init__(self, *, budget_enabled=False, safety_tokens=8192, tokenizer_url='',
                 context_mode='configured', explicit_window=False, **kwargs):
        super().__init__(**kwargs)
        self.budget_enabled = budget_enabled
        self.safety_tokens = safety_tokens
        self._requested_safety = safety_tokens
        self._requested_output = self.parameters.max_tokens or 16000
        self._output_limit = self._requested_output
        self.tokenizer_url = tokenizer_url
        self.context_mode = context_mode
        self.explicit_window = explicit_window
        self.context_info = {'source': 'configured' if explicit_window else 'fallback',
                             'contextTokens': self.context_size, 'model': self.model}
        self._discovered = False
        self._discovery_lock = asyncio.Lock()
        self._token_counts = OrderedDict()
        self._response_schema = ContextVar('pptd_response_schema', default=None)
        self._supports_json_schema = False
        self._responses_counting = False

    @contextmanager
    def json_response(self, schema):
        """Scope output constraints to one call, including concurrent page agents."""
        token = self._response_schema.set(copy.deepcopy(schema))
        try:
            yield
        finally:
            self._response_schema.reset(token)

    def _resize_budget(self, limit):
        # Configuration is a ceiling. Reserve at most a quarter of a small
        # window for output, and at most 1/32 for safety. Never enlarge output
        # beyond the configured or server-advertised maximum.
        self.context_size = limit
        self.safety_tokens = min(self._requested_safety, max(1, limit // 32))
        self.parameters.max_tokens = max(1, min(self._requested_output, self._output_limit, limit // 4))
        self.context_info.update(contextTokens=limit, outputTokens=self.parameters.max_tokens,
                                 safetyTokens=self.safety_tokens)

    async def discover_context(self):
        """Discover once per model instance/job, never reuse another model's limit."""
        async with self._discovery_lock:
            if self._discovered or not self.budget_enabled:
                return dict(self.context_info)
            if self.context_mode == 'auto':
                try:
                    entry = await self._metadata()
                    # vLLM's OpenAI API implements response_format/json_schema.
                    # Do not assume this extension for arbitrary compatible APIs.
                    self._supports_json_schema = entry.get('owned_by') == 'vllm'
                    containers = [entry, entry.get('limits', {})]
                    limits = [data.get(key) for data in containers if isinstance(data, dict)
                              for key in ('max_model_len', 'context_length', 'context_window', 'max_context_length')]
                    limit = next((v for v in limits if type(v) is int and v > 0), None)
                    if limit:
                        self.context_size = min(limit, self.context_size) if self.explicit_window else limit
                        self.context_info['source'] = 'model-api+configured-cap' if self.explicit_window else 'model-api'
                    else:
                        self.context_info['discoveryWarning'] = '模型服务未返回上下文上限字段，使用配置或回退预算；该预算不是服务端确认值。'
                    outputs = [data.get(key) for data in containers if isinstance(data, dict)
                               for key in ('max_output_tokens', 'max_completion_tokens')]
                    self._output_limit = next((v for v in outputs if type(v) is int and v > 0), self._requested_output)
                    if not self.tokenizer_url and entry.get('owned_by') == 'vllm':
                        # vLLM's tokenizer lives beside /v1, retaining any reverse-proxy prefix.
                        base = str(self.client.base_url).rstrip('/')
                        self.tokenizer_url = (base[:-3] if base.endswith('/v1') else base) + '/tokenize'
                except Exception:
                    # /models has no standard context field. Unknown providers
                    # retain an explicitly labelled configured/fallback budget.
                    self.context_info['discoveryWarning'] = '模型服务未提供可读取的上下文上限，使用配置或保守回退值。'
                if not self.native_counting and not self.tokenizer_url:
                    await self._discover_counter()
            self._resize_budget(self.context_size)
            self.context_info.update(contextTokens=self.context_size,
                                     counting='provider-tokenizer' if self.native_counting else 'server-tokenizer' if self.tokenizer_url else 'responses-input-tokens' if self._responses_counting else 'conservative-utf8',
                                     structuredOutput='json-schema' if self._supports_json_schema else 'prompt-validation')
            self._discovered = True
            return dict(self.context_info)

    async def _discover_counter(self):
        """Optional provider capability; subclasses opt in using a harmless probe."""

    async def _budget(self, messages, tools=None, *, model_name=None, output_tokens=None, extra_body=None):
        await self.discover_context()
        payload = await self._context_payload(messages, tools, model_name, extra_body)
        fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if self.native_counting or self.tokenizer_url or self._responses_counting:
            if fingerprint in self._token_counts:
                count, server_limit = self._token_counts[fingerprint]
                self._token_counts.move_to_end(fingerprint)
            else:
                try:
                    count, server_limit = await self._remote_count(payload)
                    if type(count) is not int or count < 0 or type(server_limit) is not int or server_limit < 1:
                        raise ValueError('Invalid token count')
                except Exception as exc:
                    raise ContextCountError() from exc
                self._token_counts[fingerprint] = (count, server_limit)
                while len(self._token_counts) > 16:
                    self._token_counts.popitem(last=False)
        else:
            # Explicitly conservative UTF-8 bound for text-only profiles without
            # a tokenizer. Multimodal requests require the real processor.
            if any(isinstance(m.get('content'), list) and any(b.get('type') != 'text' for b in m['content']) for m in payload['messages']):
                raise ContextCountError()
            count = len(json.dumps(payload, ensure_ascii=False).encode())
            server_limit = self.context_size
        limit = min(self.context_size, server_limit)
        if limit < self.context_size:
            self._resize_budget(limit)
            self.context_info['source'] += '+tokenizer-cap'
        reserve = min(output_tokens, self.parameters.max_tokens) if output_tokens is not None else self.parameters.max_tokens or 0
        # Stop before AgentScope's 90% compression threshold. Large windows may
        # require more room than the explicit output + margin reservation.
        input_limit = min(limit - reserve - self.safety_tokens, int(limit * .9) - 1)
        budget = {'inputTokens': count, 'outputTokens': reserve, 'safetyTokens': self.safety_tokens,
                  'contextLimit': limit, 'inputLimit': input_limit,
                  'counting': self.context_info.get('counting'), 'contextSource': self.context_info['source']}
        if count > input_limit:
            raise ContextBudgetError(budget)
        return count

    async def count_tokens(self, messages, tools=None):
        if not self.budget_enabled:
            return await super().count_tokens(messages, tools)
        return await self._budget(messages, tools)

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **generate_kwargs):
        if self.budget_enabled:
            if model_name != self.model:
                raise ValueError('切换模型需创建新的模型实例，以重新发现上下文能力。')
            await self._budget(messages, tools, model_name=model_name,
                output_tokens=generate_kwargs.get('max_completion_tokens', generate_kwargs.get('max_tokens')),
                extra_body=generate_kwargs.get('extra_body'))
            override = generate_kwargs.pop('max_completion_tokens', generate_kwargs.pop('max_tokens', None))
            if override is not None:
                generate_kwargs[self.output_token_parameter] = min(override, self.parameters.max_tokens)
        schema = self._response_schema.get()
        if schema is not None and self._supports_json_schema:
            generate_kwargs['response_format'] = {'type':'json_schema', 'json_schema':{
                'name':'pptd_response', 'strict':True, 'schema':schema}}
        return await super()._call_api(model_name, messages, tools, tool_choice, **generate_kwargs)


class BudgetedOpenAIChatModel(ContextBudgetMixin, OpenAIChatModel):
    async def _responses_count(self, payload):
        response = await self.client.with_options(timeout=20, max_retries=0).post(
            'responses/input_tokens', cast_to=dict, body=responses_count_payload(payload),
            options={'follow_redirects': False})
        count = response.get('input_tokens')
        if response.get('object') != 'response.input_tokens' or type(count) is not int or count < 0:
            raise ContextCountError()
        return count + responses_name_overhead(payload), self.context_size

    async def _discover_counter(self):
        # The Models API doesn't advertise counters. Detect the standard same-
        # origin endpoint with synthetic text, without model-name assumptions.
        try:
            await self._responses_count({'model': self.model, 'messages': [
                {'role': 'user', 'content': 'Token counting capability check.'}]})
        except Exception:
            return
        self._responses_counting = True

    async def _metadata(self):
        response = await self.client.with_options(timeout=10, max_retries=0).get('models', cast_to=dict)
        return next((row for row in response.get('data', [])
                     if isinstance(row, dict) and row.get('id') == self.model), {})

    async def _context_payload(self, messages, tools, model_name, extra_body):
        payload = {'model': model_name or self.model, 'messages': await self.formatter.format(messages)}
        formatted_tools, _ = self._format_tools(tools, None)
        if formatted_tools:
            payload['tools'] = formatted_tools
        extra = self.extra_body if extra_body is None else extra_body
        if extra and PROTECTED_REQUEST_FIELDS.intersection(extra):
            raise ValueError('模型扩展参数不能覆盖已计入预算的消息、工具或输出额度。')
        for key in ('chat_template_kwargs', 'chat_template', 'mm_processor_kwargs', 'media_io_kwargs'):
            if extra and key in extra:
                payload[key] = extra[key]
        return payload

    async def _remote_count(self, payload):
        if self._responses_counting:
            return await self._responses_count(payload)
        response = await self.client.with_options(timeout=20, max_retries=0).post(
            self.tokenizer_url, cast_to=dict, body=payload, options={'follow_redirects': False})
        return response['count'], response['max_model_len']


class BudgetedAnthropicChatModel(ContextBudgetMixin, AnthropicChatModel):
    native_counting = True
    output_token_parameter = 'max_tokens'

    async def _metadata(self):
        entry = await self.client.with_options(timeout=10, max_retries=0).models.retrieve(self.model)
        # Reserving output inside the advertised input window is conservative.
        return {'context_length': entry.max_input_tokens, 'max_output_tokens': entry.max_tokens}

    async def _context_payload(self, messages, tools, model_name, extra_body):
        formatted = await self.formatter.format(messages)
        payload = {'model': model_name or self.model}
        if formatted and formatted[0]['role'] == 'system':
            payload['system'] = formatted[0]['content']
            formatted = formatted[1:]
        payload['messages'] = formatted
        formatted_tools, _ = self._format_tools(tools, None)
        if formatted_tools: payload['tools'] = formatted_tools
        return payload

    async def _remote_count(self, payload):
        result = await self.client.with_options(timeout=20, max_retries=0).messages.count_tokens(**payload)
        return result.input_tokens, self.context_size
