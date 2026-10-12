"""Trace real AgentScope middleware with deterministic model responses, no network."""
import asyncio
import unittest
from unittest.mock import patch

from agentscope.credential import OpenAICredential
from agentscope.message import Msg, TextBlock
from agentscope.model import OpenAIChatModel, ChatResponse, ChatUsage
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from . import agents, telemetry
from .models import JobRequest, Project


class LocalModel(OpenAIChatModel):
    async def _call_api(self, *args, **kwargs):
        await asyncio.sleep(0)
        return ChatResponse(content=[TextBlock(text="已完成策划")], is_last=True,
                            usage=ChatUsage(input_tokens=12, output_tokens=5, time=0.01))


class TelemetryTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.exporter = InMemorySpanExporter()
        cls.provider = TracerProvider()
        cls.provider.add_span_processor(SimpleSpanProcessor(cls.exporter))
        trace.set_tracer_provider(cls.provider)

    async def asyncSetUp(self):
        self.exporter.clear()

    async def test_concurrent_agents_keep_parent_trace_and_conversation(self):
        async def run(project_id):
            request = JobRequest(kind="outline", project=Project(id=project_id))
            model = LocalModel(credential=OpenAICredential(api_key="test-secret"), model="trace-test", stream=False)
            try:
                with telemetry.generation(request, project_id + "-job"):
                    agent = agents.make_agent("PPTPlanner", "回答一句话", model, project_id)
                    await agent.reply(Msg(name="user", role="user", content=[{"type": "text", "text": "测试主题"}]))
            finally:
                await model.client.close()
        await asyncio.gather(run("project-a"), run("project-b"))
        spans = self.exporter.get_finished_spans()
        self.assertEqual(len(spans), 6)
        for project_id in ("project-a", "project-b"):
            group = [s for s in spans if s.attributes.get("gen_ai.conversation.id") == project_id]
            self.assertEqual(len(group), 3)
            root = next(s for s in group if s.name == "PPT outline")
            agent = next(s for s in group if s.name == "invoke_agent PPTPlanner")
            model = next(s for s in group if s.name.startswith("chat "))
            self.assertEqual(agent.parent.span_id, root.context.span_id)
            self.assertEqual(model.parent.span_id, agent.context.span_id)
            self.assertEqual(model.attributes["gen_ai.usage.input_tokens"], 12)
            self.assertEqual(model.attributes["gen_ai.usage.output_tokens"], 5)
            self.assertIn("测试主题", model.attributes["gen_ai.input.messages"])
            self.assertIn("已完成策划", model.attributes["gen_ai.output.messages"])
        self.assertNotIn("test-secret", str([dict(s.attributes) for s in spans]))

    async def test_model_failure_marks_model_agent_and_job(self):
        model = LocalModel(credential=OpenAICredential(api_key="test-secret"), model="trace-test", stream=False)
        request = JobRequest(kind="revise", project=Project(id="failure"))
        try:
            with self.assertRaises(ValueError), patch.object(model, "_call_api", side_effect=ValueError("test failure")):
                with telemetry.generation(request, "failed-job"):
                    agent = agents.make_agent("PPTEditor", "回答一句话", model, request.project.id)
                    await agent.reply(Msg(name="user", role="user", content=[{"type": "text", "text": "编辑"}]))
        finally:
            await model.client.close()
        spans = self.exporter.get_finished_spans()
        self.assertEqual(len(spans), 3)
        self.assertTrue(all(s.status.status_code == StatusCode.ERROR for s in spans))

    async def test_cancellation_marks_root(self):
        request = JobRequest(kind="outline", project=Project(id="cancel"))
        with self.assertRaises(asyncio.CancelledError):
            with telemetry.generation(request, "cancel-job"):
                raise asyncio.CancelledError()
        span = self.exporter.get_finished_spans()[0]
        self.assertEqual(span.status.status_code, StatusCode.ERROR)
        self.assertTrue(span.attributes["ppt.cancelled"])

    async def test_job_is_independent_of_enqueue_http_request(self):
        request = JobRequest(kind="outline", project=Project(id="http-parent"))
        with trace.get_tracer("http-test").start_as_current_span("POST /jobs") as http:
            with telemetry.generation(request, "background") as job:
                self.assertNotEqual(job.get_span_context().trace_id, http.get_span_context().trace_id)
        job = next(s for s in self.exporter.get_finished_spans() if s.name == "PPT outline")
        self.assertIsNone(job.parent)


if __name__ == "__main__":
    unittest.main()
