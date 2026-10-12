"""AgentScope 2 tracing exported to the configured Studio OTLP HTTP receiver."""
import asyncio
from contextlib import contextmanager
import logging
import os
from urllib.parse import urlsplit

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import StatusCode
import requests

logger = logging.getLogger(__name__)
_provider = None
_endpoint = ""


def setup():
    global _provider, _endpoint
    base = os.getenv("AGENTSCOPE_STUDIO_URL", "").strip().rstrip("/")
    if _provider is not None or not base:
        return
    parsed = urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("AGENTSCOPE_STUDIO_URL 必须是 Studio 后端 HTTP 地址，不含账号、查询参数或 /tracing 页面路径。")
    if parsed.path not in ("", "/v1/traces"):
        raise ValueError("AGENTSCOPE_STUDIO_URL 请填写 Studio 后端地址，如 http://192.168.0.10:3001。")
    _endpoint = base if parsed.path == "/v1/traces" else base + "/v1/traces"
    session = requests.Session()
    session.trust_env = False
    exporter = OTLPSpanExporter(endpoint=_endpoint, timeout=3, session=session)
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider(resource=Resource.create({"service.name": "ppt-studio-agent"}))
        trace.set_tracer_provider(provider)
    provider.add_span_processor(BatchSpanProcessor(exporter, schedule_delay_millis=1000, max_export_batch_size=64))
    _provider = provider
    logger.info("AgentScope tracing enabled: %s", _endpoint)


def health():
    # Configuration is not a claim of successful delivery to Studio.
    return {"enabled": _provider is not None, "endpoint": _endpoint or None}


@contextmanager
def generation(request, job_id):
    with trace.get_tracer("ppt_studio").start_as_current_span(
        f"PPT {request.kind}",
        # Background jobs outlive the request that enqueued them. Give each
        # its own root instead of inheriting an already-ended HTTP span.
        context=Context(),
        attributes={"gen_ai.operation.name": "invoke_generic_function",
                    "gen_ai.conversation.id": request.project.id,
                    "agentscope.function.name": "PPT.generate",
                    "ppt.project.id": request.project.id,
                    "ppt.job.id": job_id,
                    "ppt.job.kind": request.kind},
    ) as span:
        try:
            yield span
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                span.set_attribute("ppt.cancelled", True)
                span.set_status(StatusCode.ERROR, "Generation cancelled")
            raise
        else:
            span.set_status(StatusCode.OK)


async def flush():
    if _provider is not None:
        await asyncio.to_thread(_provider.force_flush, timeout_millis=4000)


async def shutdown():
    if _provider is not None:
        await flush()
        await asyncio.to_thread(_provider.shutdown)
