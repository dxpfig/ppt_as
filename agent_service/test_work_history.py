"""Work-record regression tests, isolated from user data and model providers."""
import asyncio
import importlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

service = importlib.import_module("agent_service.app")


class WorkHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        for folder in ("projects", "templates", "jobs", "artifacts", "trash"):
            (root / folder).mkdir()
        self.root_patch = patch.object(service, "ROOT", root)
        self.root_patch.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=service.app), base_url="http://test/api/studio/")
        self.project = {
            "imageSource": "none", "id": "history-a", "title": "季度汇报", "theme": "ink", "phase": "outline",
            "slides": [{"layout": "auto", "section": "", "source": "", "imageIntent": "auto", "imageQuery": "", "imageDisabled": False, "image": None, "id": "slide-a", "title": "第一季度", "subtitle": "业务回顾", "points": ["保留内容"], "notes": "原始讲稿"}],
            "messages": [{"id": "message-a", "role": "user", "text": "面向管理层，保留关键风险"}],
            "versions": [{"id": "version-a", "label": "初稿", "slides": [], "theme": "ink"}],
            "pageCount": 4, "audience": "管理层", "references": "内部参考材料", "templateId": "company-template",
            "contentGenerated": True,
        }

    async def asyncTearDown(self):
        tasks = list(service.TASKS.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        service.TASKS.clear()
        await self.client.aclose()
        self.root_patch.stop()
        self.directory.cleanup()

    async def wait_job(self, identifier):
        for _ in range(100):
            job = (await self.client.get(f"jobs/{identifier}")).json()
            if job["status"] not in ("queued", "running"):
                return job
            await asyncio.sleep(.01)
        self.fail("Test job did not finish")

    async def test_reopen_and_continue_preserves_full_record(self):
        self.assertEqual((await self.client.put("projects/history-a", json=self.project)).status_code, 200)
        reopened = (await self.client.get("projects/history-a")).json()
        for key, value in self.project.items():
            self.assertEqual(reopened[key], value)
        edited = {**reopened, "title": "季度汇报（修订）", "audience": "董事会"}
        edited["slides"][0]["notes"] = "继续编辑后的讲稿"
        await self.client.put("projects/history-a", json=edited)
        await self.client.put("projects/history-b", json={**self.project, "id": "history-b", "title": "另一份演示"})
        restored = (await self.client.get("projects/history-a")).json()
        self.assertEqual(restored["slides"][0]["notes"], "继续编辑后的讲稿")
        self.assertEqual(restored["references"], "内部参考材料")
        records = (await self.client.get("projects")).json()
        self.assertEqual([p["id"] for p in records], ["history-b", "history-a"])

    async def test_delete_cannot_be_resurrected_by_stale_autosave(self):
        await self.client.put("projects/history-a", json=self.project)
        self.assertEqual((await self.client.delete("projects/history-a")).status_code, 200)
        self.assertEqual((await self.client.get("projects")).json(), [])
        self.assertEqual((await self.client.get("projects/history-a")).status_code, 404)
        self.assertEqual((await self.client.put("projects/history-a", json=self.project)).status_code, 410)
        self.assertEqual((await self.client.post("jobs", json={"kind": "render", "project": self.project})).status_code, 410)
        self.assertEqual((await self.client.delete("projects/history-a")).status_code, 200)
        self.assertEqual((await self.client.post("projects/history-a/restore")).status_code, 200)
        restored = (await self.client.get("projects/history-a")).json()
        self.assertEqual(restored["messages"], self.project["messages"])
        self.assertEqual(restored["versions"], self.project["versions"])
        self.assertEqual(restored["templateId"], self.project["templateId"])
        self.assertEqual((await self.client.put("projects/history-a", json=restored)).status_code, 200)

    async def test_job_result_is_saved_without_browser_polling(self):
        async def generate(request, progress):
            self.assertEqual(request.project.references, "内部参考材料")
            self.assertEqual(request.project.messages, self.project["messages"])
            return {"title": request.project.title, "slides": [{**self.project["slides"][0], "title": "继续创作的新标题"}], "reply": "已按原有资料修改"}

        with patch.object(service.agents, "health", return_value={"llmConfigured": True}), patch.object(service.agents, "generate", side_effect=generate):
            response = await self.client.post("jobs", json={"kind": "revise", "project": self.project, "prompt": "继续改写标题"})
            self.assertEqual(response.status_code, 202)
            job = await self.wait_job(response.json()["id"])
        self.assertEqual(job["status"], "completed", job)
        # Reading the record alone is enough to recover the completed result.
        reopened = (await self.client.get("projects/history-a")).json()
        self.assertEqual(reopened["slides"][0]["title"], "继续创作的新标题")
        self.assertEqual(reopened["messages"][-1]["text"], "已按原有资料修改")
        self.assertIsNone(reopened["activeJobId"])
        self.assertEqual(job["result"]["project"], reopened)

    async def test_delete_running_record_cancels_job_and_restore_is_idle(self):
        entered = asyncio.Event()

        async def generate(request, progress):
            entered.set()
            await asyncio.Event().wait()

        with patch.object(service.agents, "health", return_value={"llmConfigured": True}), patch.object(service.agents, "generate", side_effect=generate):
            response = await self.client.post("jobs", json={"kind": "revise", "project": self.project})
            identifier = response.json()["id"]
            await asyncio.wait_for(entered.wait(), timeout=2)
            self.assertEqual((await self.client.put("projects/history-a", json=self.project)).status_code, 409)
            self.assertEqual((await self.client.post("jobs", json={"kind": "revise", "project": self.project})).status_code, 409)
            await self.client.delete("projects/history-a")
            self.assertEqual((await self.wait_job(identifier))["status"], "cancelled")
            await self.client.post("projects/history-a/restore")
        restored = (await self.client.get("projects/history-a")).json()
        self.assertIsNone(restored["activeJobId"])
        self.assertNotEqual(restored["phase"], "generating")

    async def test_failed_revision_keeps_existing_work_editable(self):
        async def generate(request, progress):
            raise ValueError("测试模型暂不可用")

        project = {**self.project, "phase": "ready"}
        with patch.object(service.agents, "health", return_value={"llmConfigured": True}), patch.object(service.agents, "generate", side_effect=generate):
            response = await self.client.post("jobs", json={"kind": "revise", "project": project})
            job = await self.wait_job(response.json()["id"])
        self.assertEqual(job["status"], "failed")
        reopened = (await self.client.get("projects/history-a")).json()
        self.assertEqual(reopened["slides"], project["slides"])
        self.assertEqual(reopened["messages"], project["messages"])
        self.assertEqual(reopened["phase"], "ready")
        self.assertIsNone(reopened["activeJobId"])

    async def test_render_stores_artifact_and_version_without_browser(self):
        async def render_pdf(path):
            return ["slide-1.png"]

        with patch.object(service, "create_deck", return_value=[]), patch.object(service, "render_pdf", side_effect=render_pdf), patch.object(service, "audit_rendered_pdf", return_value=[]):
            response = await self.client.post("jobs", json={"kind": "render", "project": {**self.project, "templateId": None}})
            job = await self.wait_job(response.json()["id"])
        self.assertEqual(job["status"], "completed", job)
        reopened = (await self.client.get("projects/history-a")).json()
        self.assertEqual(reopened["phase"], "ready")
        self.assertEqual(reopened["artifact"]["id"], job["id"])
        self.assertEqual(reopened["versions"][0]["artifact"], reopened["artifact"])
        self.assertEqual(reopened["versions"][1:], self.project["versions"])


if __name__ == "__main__":
    unittest.main()
