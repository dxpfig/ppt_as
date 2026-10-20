"""Unit and gated integration coverage for the agentic kimi-slides expert."""
from __future__ import annotations

import json
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
            with_references = kimi_agent._initial_message(
                SimpleNamespace(
                    prompt="p", audience="a", pageCount=None,
                    references="附件事实：完成率 83%。", attachments=[],
                ),
                info,
            )
            payload = json.loads(with_references)
            self.assertEqual(payload["references"], "附件事实：完成率 83%。")
            self.assertEqual(payload["referenceCharacters"], len("附件事实：完成率 83%。"))
            self.assertIn("首要事实来源", payload["note"])

    def test_long_reference_is_externalized_with_structure_for_the_agent(self):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            references = "\n\n".join(
                f"## 第 {index} 章\n\n" + f"这是第 {index} 章的重要事实与数据。" * 80
                for index in range(1, 30)
            )
            info = kimi_agent._prepare_reference_material(
                root,
                "project",
                SimpleNamespace(references=references),
                workspace,
            )
            self.assertGreater(len(references), 24000)
            self.assertTrue((workspace / "sources" / "reference.md").is_file())
            self.assertTrue((workspace / "sources" / "reference-outline.md").is_file())
            self.assertIn("第 1 章", info["outline"])
            payload = json.loads(kimi_agent._initial_message(
                SimpleNamespace(
                    prompt="p",
                    audience="a",
                    pageCount=None,
                    references=references,
                    attachments=[],
                ),
                None,
                info,
            ))
            self.assertEqual(payload["references"], "")
            self.assertEqual(payload["referenceFile"], "sources/reference.md")
            self.assertIn("reference-outline.md", payload["note"])

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

    def test_partial_deck_reports_missing_pages_without_loading_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = Path(tmp) / "deck" / "deck.pptd"
            (entry.parent / "pages").mkdir(parents=True)
            entry.write_text(
                "version: v2\nsize: [960, 540]\n"
                "pages: [pages/one.page, pages/two.page]\n",
                encoding="utf-8",
            )
            (entry.parent / "pages" / "one.page").write_text(
                "elements: []\n", encoding="utf-8"
            )
            _, paths, existing, missing = kimi_agent._deck_progress(entry)
        self.assertEqual(paths, ["pages/one.page", "pages/two.page"])
        self.assertEqual(existing, ["pages/one.page"])
        self.assertEqual(missing, ["pages/two.page"])

    def test_workspace_can_restore_an_incomplete_agent_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = kimi_agent._workspace(root, "project", "parent")
            child = kimi_agent._workspace(root, "project", "child")
            (parent / "deck" / "pages").mkdir(parents=True)
            (parent / "deck" / "pages" / "one.page").write_text(
                "elements: []\n", encoding="utf-8"
            )
            kimi_agent.restore_workspace(root, "project", "child", "parent")
            self.assertTrue((child / "deck" / "pages" / "one.page").is_file())

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
            self.assertIn("感谢观看", closing_text)
            self.assertNotIn("请领导讲话", closing_text)
            self.assertEqual(document["pages"], [p["path"] for p in pages])
            self.assertTrue((deck / "media" / "end.jpg").is_file())

    def test_relabel_keeps_trailing_empty_paragraphs(self):
        content = {"text": '<p style="text-align:center;line-height:1.2">'
                           '<span style="font-family:MiSans;font-size:66.0px;color:#FFFFFF">关于AI服务器</span></p>'
                           '<p style="text-align:center;line-height:1.2"></p>'
                           '<p style="text-align:center;line-height:1.2"></p>'}
        updated = kimi_agent._relabel(content, "新标题")
        markup = updated["text"]
        self.assertEqual(markup.count("<p"), 3)
        self.assertIn("color:#FFFFFF", markup)
        self.assertIn("新标题", markup)
        self.assertNotIn("关于AI服务器", markup)
        # The trailing empty lines must keep the slot's size, otherwise the
        # browser collapses them and the centred title slides onto the subtitle.
        self.assertEqual(markup.count("&nbsp;</span>"), 2)
        self.assertEqual(markup.count("font-size:66.0px"), 3)

    def test_fill_template_page_skips_a_slot_that_cannot_fit(self):
        template = {"elements": [
            {"elementId": "big", "elementType": "text", "bounds": [0, 100, 960, 130],
             "content": {"text": '<p><span style="font-size:100.0px">模板</span></p>'}},
            {"elementId": "date", "elementType": "text", "bounds": [400, 321, 175, 46],
             "content": {"text": '<p><span style="font-size:32.0px">2026年1月</span></p>'}},
        ]}
        filled = kimi_agent._fill_template_page(template, ["感谢观看", "欢迎一起交流 AI 生成 PPT 的实战经验"], keep_unfilled=False)
        self.assertIn("感谢观看", filled["elements"][0]["content"]["text"])
        self.assertNotIn("欢迎一起交流", filled["elements"][1]["content"]["text"])

    def test_body_background_is_copied_from_the_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template = root / "template-pptd"
            (template / "media").mkdir(parents=True)
            (template / "media" / "body.jpg").write_bytes(b"body")
            (template / "media" / "cover.jpg").write_bytes(b"cover")
            (template / "template.pptd").write_text(
                "version: v2\nsize: [960, 540]\npages: [pages/a.page, pages/b.page, pages/c.page, pages/d.page]\n",
                encoding="utf-8")
            (template / "pages").mkdir()
            (template / "pages" / "a.page").write_text(
                "elements:\n- elementId: cover\n  elementType: image\n  src: media/cover.jpg\n  bounds: [0, 0, 960, 540]\n",
                encoding="utf-8")
            for name in ("b", "c"):
                (template / "pages" / f"{name}.page").write_text(
                    "elements:\n- elementId: bg\n  elementType: image\n  src: media/body.jpg\n  bounds: [0, 0, 960, 540]\n",
                    encoding="utf-8")
            (template / "pages" / "d.page").write_text(
                "elements:\n- elementId: end\n  elementType: image\n  src: media/cover.jpg\n  bounds: [0, 0, 960, 540]\n",
                encoding="utf-8")

            background = kimi_agent._extract_body_background(template)
            self.assertIsNotNone(background)
            self.assertEqual(background["src"], "media/body.jpg")

            deck = root / "deck"
            (deck / "media").mkdir(parents=True)
            (deck / "deck.pptd").write_text(
                "version: v2\nsize: [960, 540]\npages: [pages/one.page, pages/two.page, pages/three.page]\n",
                encoding="utf-8")
            (deck / "pages").mkdir()
            (deck / "pages" / "one.page").write_text("elements: []\n", encoding="utf-8")
            (deck / "pages" / "two.page").write_text(
                "elements:\n"
                "- elementId: wave-band\n  elementType: shape\n  shapeName: custom\n  path: M0,0 L10,10\n  bounds: [0, 490, 960, 50]\n"
                "- elementId: title\n  elementType: text\n  bounds: [60, 40, 400, 40]\n  content: {text: 标题}\n",
                encoding="utf-8")
            (deck / "pages" / "three.page").write_text("elements: []\n", encoding="utf-8")
            document = {"size": [960, 540], "pages": ["pages/one.page", "pages/two.page", "pages/three.page"]}
            pages = [
                {"path": "pages/one.page", "data": {"elements": []}},
                {"path": "pages/two.page", "data": {"elements": [
                    {"elementId": "wave-band", "elementType": "shape", "shapeName": "custom", "path": "M0,0", "bounds": [0, 490, 960, 50]},
                    {"elementId": "title", "elementType": "text", "bounds": [60, 40, 400, 40], "content": {"text": "标题"}},
                ]}},
                {"path": "pages/three.page", "data": {"elements": []}},
            ]
            kimi_agent._apply_background(deck, document, pages, background, template)
            body = pages[1]["data"]["elements"]
            self.assertNotIn("wave-band", [e["elementId"] for e in body])
            self.assertEqual(pages[1]["data"]["background"], {
                "elementId": "bg", "elementType": "image", "src": "media/body.jpg",
                "bounds": [0, 0, 960, 540]})
            self.assertTrue((deck / "media" / "body.jpg").is_file())

    def test_page_background_is_repointed_at_the_template_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template = root / "template-pptd"
            (template / "media").mkdir(parents=True)
            (template / "media" / "canonical.jpg").write_bytes(b"canonical")
            (template / "media" / "other.jpg").write_bytes(b"other")
            background = {"elementId": "bg", "elementType": "image", "src": "media/canonical.jpg",
                          "bounds": [0, 0, 960, 540]}
            deck = root / "deck"
            (deck / "media").mkdir(parents=True)
            (deck / "media" / "mine.jpg").write_bytes(b"other")
            document = {"size": [960, 540]}
            pages = [
                {"path": "pages/cover.page", "data": {"elements": [], "background": {"type": "solid", "color": "#FFFFFF"}}},
                {"path": "pages/plain.page", "data": {"elements": [], "background": {"type": "solid", "color": "#FFFFFF"}}},
                {"path": "pages/wrong.page", "data": {"elements": [], "background": {
                    "type": "image", "src": "media/mine.jpg", "fit": {"mode": "fill"}}}},
                {"path": "pages/same.page", "data": {"elements": [], "background": {
                    "type": "image", "src": "media/canonical.jpg", "fit": {"mode": "fill"}}}},
                {"path": "pages/closing.page", "data": {"elements": [], "background": {"type": "solid", "color": "#FFFFFF"}}},
            ]
            (deck / "media" / "canonical.jpg").write_bytes(b"canonical")
            kimi_agent._apply_background(deck, document, pages, background, template)
            self.assertEqual(pages[0]["data"]["background"], {"type": "solid", "color": "#FFFFFF"})
            self.assertEqual(pages[1]["data"]["background"]["src"], "media/canonical.jpg")
            self.assertEqual(pages[2]["data"]["background"]["src"], "media/canonical.jpg")
            self.assertEqual(pages[2]["data"]["background"]["fit"], {"mode": "fill"})
            self.assertEqual(pages[3]["data"]["background"]["src"], "media/canonical.jpg")
            self.assertEqual(pages[4]["data"]["background"], {"type": "solid", "color": "#FFFFFF"})
            self.assertTrue((deck / "media" / "canonical.jpg").is_file())

    def test_tables_without_a_font_keep_the_deck_typeface(self):
        document = {"theme": {"textStyles": {"body": {"fontFamily": "微软雅黑"}}}}
        pages = [{"path": "pages/one.page", "data": {"elements": [
            {"elementId": "t", "elementType": "table", "style": {
                "firstRowStyle": {"fontSize": 12},
                "bodyStyles": [{"fontSize": 11}, {"fontSize": 11}]}},
        ]}}]
        kimi_agent._normalize_table_fonts(document, pages)
        style = pages[0]["data"]["elements"][0]["style"]
        self.assertEqual(style["firstRowStyle"]["fontFamily"], "微软雅黑")
        self.assertTrue(all(part["fontFamily"] == "微软雅黑" for part in style["bodyStyles"]))

    def test_corner_conflicts_are_relocated_out_of_the_company_mark(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            template = root / "template-pptd"
            (template / "media").mkdir(parents=True)
            (template / "media" / "logo.png").write_bytes(b"logo")
            chrome = [
                {"elementId": "logo", "elementType": "image", "src": "media/logo.png", "bounds": [730, 518, 74, 13]},
                {"elementId": "name", "elementType": "text", "bounds": [805, 516, 156, 15],
                 "content": {"text": "长沙湘计海盾科技有限公司"}},
            ]
            elements = [
                {"elementId": "footer", "elementType": "text", "bounds": [40, 500, 880, 24],
                 "content": {"text": "01 背景与趋势"}},
                {"elementId": "card", "elementType": "shape", "bounds": [40, 140, 280, 100]},
            ]
            moved = kimi_agent._clear_corner_conflicts(elements, chrome, template, [960, 540])
            self.assertEqual(moved, ["footer"])
            self.assertLessEqual(elements[0]["bounds"][0] + elements[0]["bounds"][2], 730)
            self.assertEqual(elements[1]["bounds"], [40, 140, 280, 100])


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
