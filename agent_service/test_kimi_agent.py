"""Unit and gated integration coverage for the agentic kimi-slides expert."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from agent_service import kimi_agent


class KimiAgentUnitTests(unittest.TestCase):
    def test_system_prompt_layers_skill_and_adapter(self):
        prompt = kimi_agent.kimi_system_prompt(Path("/tmp/kimi-ws"))
        self.assertIn("name: kimi-slides", prompt)
        self.assertIn("HOST ADAPTER", prompt)
        self.assertIn("kimi_slides", prompt)

    def test_reply_text_strips_reasoning(self):
        class Block:
            text = "<think>internal</think>\n演示已生成。"

        class Msg:
            text = None
            content = [Block()]

        self.assertEqual(kimi_agent._reply_text(Msg()), "演示已生成。")
        self.assertEqual(kimi_agent._reply_text(None), "")

    def test_find_entry_prefers_deck_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "deck").mkdir()
            (root / "deck" / "deck.pptd").write_text("version: v2\n", encoding="utf-8")
            (root / "other.pptd").write_text("version: v2\n", encoding="utf-8")
            self.assertEqual(kimi_agent._find_entry(root).name, "deck.pptd")

    def test_load_deck_and_collect_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = Path(tmp) / "deck" / "deck.pptd"
            (entry.parent / "pages").mkdir(parents=True)
            (entry.parent / "media").mkdir()
            entry.write_text("version: v2\nsize: [960, 540]\npages: [pages/one.page]\n", encoding="utf-8")
            (entry.parent / "pages" / "one.page").write_text("elements: []\n", encoding="utf-8")
            (entry.parent / "media" / "a.png").write_bytes(b"x")
            document, pages = kimi_agent._load_deck(entry)
            self.assertEqual(document["pages"], ["pages/one.page"])
            assets = kimi_agent._collect_assets(entry)
            self.assertIn("media/a.png", assets)

    def test_prepare_template_and_message(self):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template = root / "templates" / "abc123" / "template.pptx"
            template.parent.mkdir(parents=True)
            template.write_bytes(b"PPTX")
            workspace = root / "ws"
            workspace.mkdir()
            path = kimi_agent._prepare_template(root, "abc123", workspace)
            self.assertEqual(path.name, "template.pptx")
            info = {"pptx": str(path), "pptd": str(workspace / "template-pptd"),
                    "entry": str(workspace / "template-pptd" / "template.pptd"),
                    "screenshots": str(workspace / "template-screenshots")}
            message = kimi_agent._initial_message(
                SimpleNamespace(prompt="p", audience="a", pageCount=None, references="", attachments=[]),
                info,
            )
            self.assertIn("template-pptd", message)
            self.assertIn("严禁", message)
            self.assertIn("Using a template", message)

    def test_find_entry_requires_new_deck(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "template-pptd").mkdir()
            (root / "template-pptd" / "template.pptd").write_text("version: v2\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                kimi_agent._find_entry(root)
            (root / "deck").mkdir()
            (root / "deck" / "deck.pptd").write_text("version: v2\n", encoding="utf-8")
            self.assertEqual(kimi_agent._find_entry(root).name, "deck.pptd")

    def test_extract_and_apply_company_chrome(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template = root / "template-pptd"
            (template / "media").mkdir(parents=True)
            (template / "media" / "top.png").write_bytes(b"top")
            (template / "media" / "logo.png").write_bytes(b"logo")
            (template / "template.pptd").write_text(
                "version: v2\nsize: [960, 540]\npages: [pages/a.page, pages/b.page]\n", encoding="utf-8")
            (template / "pages").mkdir()
            page = (
                "elements:\n"
                "- elementId: t\n  elementType: image\n  src: media/top.png\n  bounds: [758, -22, 210, 130]\n"
                "- elementId: l\n  elementType: image\n  src: media/logo.png\n  bounds: [730, 518, 74, 13]\n"
                "- elementId: n\n  elementType: text\n  content: {text: 长沙湘计海盾科技有限公司}\n  bounds: [805, 516, 156, 15]\n"
            )
            (template / "pages" / "a.page").write_text(page, encoding="utf-8")
            (template / "pages" / "b.page").write_text(page, encoding="utf-8")

            chrome = kimi_agent._extract_chrome(template)
            self.assertEqual(len(chrome), 3)
            self.assertTrue(any(e.get("src") == "media/top.png" for e in chrome))
            self.assertTrue(any(e.get("src") == "media/logo.png" for e in chrome))

            deck = root / "deck"
            (deck / "media").mkdir(parents=True)
            (deck / "deck.pptd").write_text("version: v2\nsize: [960, 540]\npages: [pages/one.page]\n", encoding="utf-8")
            (deck / "pages").mkdir()
            (deck / "pages" / "one.page").write_text(
                "elements:\n- elementId: x\n  elementType: text\n  content: {text: hi}\n  bounds: [40, 40, 400, 60]\n",
                encoding="utf-8")
            document = {"size": [960, 540]}
            pages = [{"path": "pages/one.page", "data": {"elements": [
                {"elementId": "x", "elementType": "text", "content": {"text": "hi"}, "bounds": [40, 40, 400, 60]}]}}]
            kimi_agent._apply_chrome(deck, document, pages, chrome, template)
            ids = [e["elementId"] for e in pages[0]["data"]["elements"]]
            self.assertEqual(len(ids), 4)
            self.assertEqual(len(set(ids)), 4)
            self.assertTrue((deck / "media" / "top.png").is_file())
            self.assertTrue((deck / "media" / "logo.png").is_file())

    def test_template_cover_and_closing_are_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template = root / "template-pptd"
            (template / "media").mkdir(parents=True)
            (template / "media" / "cover.jpg").write_bytes(b"cover")
            (template / "media" / "end.jpg").write_bytes(b"end")
            (template / "template.pptd").write_text(
                "version: v2\nsize: [960, 540]\npages: [pages/c.page, pages/e.page]\n", encoding="utf-8")
            (template / "pages").mkdir()
            (template / "pages" / "c.page").write_text(
                "elements:\n"
                "- elementId: bg\n  elementType: image\n  src: media/cover.jpg\n  bounds: [0, 0, 960, 540]\n"
                '- elementId: t\n  elementType: text\n  bounds: [0, 100, 960, 80]\n  content: {text: \'<p style="text-align:center"><span style="font-family:MiSans;font-size:66.0px">模板标题</span></p>\'}\n'
                '- elementId: s\n  elementType: text\n  bounds: [0, 244, 960, 36]\n  content: {text: \'<p style="text-align:center"><span style="font-size:24.0px">模板副标题</span></p>\'}\n',
                encoding="utf-8")
            (template / "pages" / "e.page").write_text(
                "elements:\n"
                "- elementId: bg\n  elementType: image\n  src: media/end.jpg\n  bounds: [0, 0, 960, 540]\n"
                '- elementId: t\n  elementType: text\n  bounds: [0, 119, 960, 128]\n  content: {text: \'<p style="text-align:center"><span style="font-size:100.0px">请领导讲话！</span></p>\'}\n',
                encoding="utf-8")

            deck = root / "deck"
            (deck / "media").mkdir(parents=True)
            (deck / "pages").mkdir()
            (deck / "deck.pptd").write_text("version: v2\nsize: [960, 540]\npages: [pages/one.page, pages/two.page]\n", encoding="utf-8")
            (deck / "pages" / "one.page").write_text(
                "elements:\n"
                '- elementId: t\n  elementType: text\n  bounds: [40, 60, 800, 70]\n  content: {text: \'<p><span style="font-size:60.0px">新标题</span></p>\'}\n'
                '- elementId: s\n  elementType: text\n  bounds: [40, 200, 800, 30]\n  content: {text: "新副标题"}\n',
                encoding="utf-8")
            (deck / "pages" / "two.page").write_text(
                "elements:\n- elementId: b\n  elementType: text\n  bounds: [40, 40, 800, 300]\n  content: {text: '正文内容'}\n",
                encoding="utf-8")
            document = {"size": [960, 540], "pages": ["pages/one.page", "pages/two.page"]}
            pages = [
                {"path": "pages/one.page", "data": kimi_agent._load_deck(deck / "deck.pptd")[1][0]["data"]},
                {"path": "pages/two.page", "data": kimi_agent._load_deck(deck / "deck.pptd")[1][1]["data"]},
            ]
            kimi_agent._apply_template_cover_closing(template, deck, document, pages)

            cover = pages[0]["data"]
            self.assertTrue(any(e.get("src") == "media/cover.jpg" for e in cover["elements"]))
            cover_text = " ".join(kimi_agent._visible_text(e.get("content") or {}) for e in cover["elements"] if e.get("elementType") == "text")
            self.assertIn("新标题", cover_text)
            self.assertNotIn("模板标题", cover_text)
            self.assertEqual(len(pages), 3)
            closing_text = " ".join(kimi_agent._visible_text(e.get("content") or {}) for e in pages[-1]["data"]["elements"] if e.get("elementType") == "text")
            self.assertIn("请领导讲话", closing_text)
            self.assertEqual(document["pages"], [p["path"] for p in pages])
            self.assertTrue((deck / "media" / "end.jpg").is_file())


@unittest.skipUnless(
    os.getenv("PPT_KIMI_RUN_E2E") == "1",
    "set PPT_KIMI_RUN_E2E=1 and configure an LLM to run the agentic end-to-end test",
)
class KimiAgentIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_agentic_generation_commits_document(self):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as tmp:
            request = SimpleNamespace(
                prompt="做一份2页的关于睡眠重要性的演示", audience="成年人",
                pageCount=None, references="", attachments=[],
            )
            result = await kimi_agent.run_kimi(
                request, data_root=Path(tmp), progress=lambda *_: None,
                on_event=None, project_id="e2e", job_id="e2e",
            )
            self.assertGreaterEqual(result["revision"], 1)
            self.assertTrue(result["pages"])
            self.assertNotIn("pptx", result)


if __name__ == "__main__":
    unittest.main()
