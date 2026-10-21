"""Unit and gated integration coverage for the agentic kimi-slides expert."""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

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
            request = SimpleNamespace(
                prompt="p", audience="a", pageCount=None, references="", attachments=[],
            )
            requirements = kimi_agent.plan_requirements(request, {})
            message = kimi_agent._planning_message(
                request,
                info,
                {},
                requirements,
            )
            self.assertIn("template-pptd", message)
            self.assertIn("严禁", message)
            self.assertIn("Using a template", message)
            with_references = kimi_agent._planning_message(
                SimpleNamespace(
                    prompt="p", audience="a", pageCount=None,
                    references="附件事实：完成率 83%。", attachments=[],
                ),
                info,
                {"characters": len("附件事实：完成率 83%。"), "outline": ""},
                kimi_agent.plan_requirements(
                    SimpleNamespace(pageCount=None),
                    {"characters": len("附件事实：完成率 83%。"), "outline": ""},
                ),
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
            request = SimpleNamespace(
                prompt="p",
                audience="a",
                pageCount=None,
                references=references,
                attachments=[],
            )
            payload = json.loads(kimi_agent._planning_message(
                request,
                None,
                info,
                kimi_agent.plan_requirements(request, info),
            ))
            self.assertEqual(payload["references"], "")
            self.assertEqual(payload["referenceFile"], "sources/reference.md")
            self.assertIn("reference-outline.md", payload["note"])

    def test_long_source_requires_a_complete_multi_chapter_plan(self):
        from types import SimpleNamespace

        outline = "\n".join([
            "# 军用计算报告.docx",
            "# 一、军用计算业务发展现状与趋势分析",
            "# 二、未来军用计算业务发展需求",
            "# 集团军用计算业务发展基础与现状",
            "# 四、集团军用计算业务高质量发展规划",
            "# 五、全方位保障措施",
            "## 1.1 全球军事智能化加速演进",
        ])
        request = SimpleNamespace(pageCount=None)
        info = {"characters": 47086, "outline": outline}
        requirements = kimi_agent.plan_requirements(request, info)
        self.assertEqual(len(requirements["principalSections"]), 5)
        self.assertEqual(requirements["minimumPlanPages"], 17)
        self.assertEqual(requirements["maximumPlanPages"], 32)
        self.assertEqual(requirements["fieldLimits"]["content"], 240)

        five_page_plan = {
            "deckTitle": "军用计算",
            "pages": [
                {"path": f"pages/{index}.page", "title": title,
                 "readerTask": "说明", "content": "内容", "visualPlan": "卡片",
                 "sourceSections": sections}
                for index, (title, sections) in enumerate([
                    ("封面", []),
                    ("目录", []),
                    ("第一章", [requirements["principalSections"][0]]),
                    ("第二章", [requirements["principalSections"][1]]),
                    ("第三章", [requirements["principalSections"][2]]),
                ], 1)
            ],
        }
        issues = kimi_agent.audit_plan(five_page_plan, requirements)
        self.assertTrue(any("计划页数不足" in issue for issue in issues))
        self.assertTrue(any("四、集团" in issue and "五、全方位" in issue for issue in issues))
        self.assertTrue(any("至少需要 2 页" in issue for issue in issues))

        oversized = {**five_page_plan, "pages": [dict(page) for page in five_page_plan["pages"]]}
        oversized["pages"][0]["content"] = "过长" * 121
        self.assertTrue(any(
            "content 过长" in issue
            for issue in kimi_agent.audit_plan(oversized, requirements)
        ))

        explicit = kimi_agent.plan_requirements(SimpleNamespace(pageCount=5), info)
        explicit_plan = {**five_page_plan, "pages": [dict(page) for page in five_page_plan["pages"]]}
        explicit_plan["pages"][3]["sourceSections"] = [
            requirements["principalSections"][1],
            requirements["principalSections"][2],
            requirements["principalSections"][3],
        ]
        explicit_plan["pages"][4]["sourceSections"] = [requirements["principalSections"][4]]
        self.assertEqual(kimi_agent.audit_plan(explicit_plan, explicit), [])

    def test_complete_plan_is_accepted_and_materialized_before_page_batches(self):
        outline = "\n".join([
            "# 一、现状与趋势",
            "# 二、发展需求",
            "# 三、基础与现状",
            "# 四、发展规划",
            "# 五、保障措施",
        ])
        request = SimpleNamespace(pageCount=None)
        info = {"characters": 47086, "outline": outline}
        requirements = kimi_agent.plan_requirements(request, info)
        sections = requirements["principalSections"]
        pages = [
            {"path": "pages/01-cover.page", "title": "封面", "readerTask": "建立主题",
             "content": "报告主题", "visualPlan": "封面", "sourceSections": []},
            {"path": "pages/02-toc.page", "title": "目录", "readerTask": "总览",
             "content": "五章", "visualPlan": "目录", "sourceSections": []},
        ]
        for index in range(3, requirements["minimumPlanPages"]):
            section = sections[(index - 3) % len(sections)]
            pages.append({
                "path": f"pages/{index:02d}-body.page",
                "title": f"正文 {index}",
                "readerTask": f"论证 {section}",
                "content": f"{section} 的事实与结论",
                "visualPlan": "证据页",
                "sourceSections": [section],
            })
        pages.append({
            "path": "pages/99-closing.page", "title": "致谢", "readerTask": "收束",
            "content": "感谢观看", "visualPlan": "致谢", "sourceSections": [],
        })
        plan = {"deckTitle": "完整报告", "pages": pages}
        self.assertEqual(kimi_agent.audit_plan(plan, requirements), [])

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            entry = kimi_agent._write_plan_deck(workspace, plan, None)
            document, paths, existing, missing = kimi_agent._deck_progress(entry)
            self.assertEqual(document["pages"], [page["path"] for page in pages])
            self.assertEqual(len(paths), len(pages))
            self.assertEqual(existing, [])
            self.assertEqual(missing, paths)
            saved = json.loads((workspace / "deck" / "plan.json").read_text(encoding="utf-8"))
            self.assertEqual(len(saved["pages"]), len(pages))

            planning = json.loads(kimi_agent._planning_message(
                request, None, info, requirements,
            ))
            self.assertEqual(planning["planRequirements"]["minimumPlanPages"], 17)
            self.assertEqual(planning["planRequirements"]["maximumPlanPages"], 32)
            self.assertIn("只做完整内容规划", planning["note"])
            self.assertIn("禁止创建或修改任何 deck/pages", planning["note"])
            self.assertIn("紧凑的页面契约", planning["note"])
            self.assertIn("content ≤ 240 字", planning["note"])
            self.assertNotIn("write at most the first five planned pages", planning["note"])

            execution = json.loads(kimi_agent._execution_message(
                request,
                None,
                info,
                plan,
                {"batch": 1, "batchPages": paths[:5], "existingPages": [], "missingPages": paths},
            ))
            self.assertEqual(len(execution["batchPagePlans"]), 5)
            self.assertEqual(execution["batchPagePlans"][0]["path"], paths[0])
            self.assertIn("完整计划已经通过宿主校验", execution["note"])
            self.assertIn("写入 continuation.batchPages", execution["note"])
            self.assertIn("YAML 无法解析，必须完整重写", execution["note"])
            self.assertEqual(execution["pageValidationIssues"], [])

    def test_run_kimi_plans_once_then_writes_pages_in_batches(self):
        class Client:
            async def close(self):
                pass

        model = SimpleNamespace(
            model="fake",
            max_retries=0,
            budget_enabled=True,
            client=Client(),
        )
        calls = {
            "planning": 0,
            "execution": 0,
            "batches": [],
            "draft_present": [],
            "validation_issues": [],
        }
        plan = {
            "deckTitle": "分批测试",
            "pages": [
                {"path": f"pages/{index}.page", "title": title,
                 "readerTask": "说明", "content": "内容", "visualPlan": "文本",
                 "sourceSections": []}
                for index, title in enumerate(("封面", "目录", "正文", "致谢"), 1)
            ],
        }

        class Agent:
            def __init__(self, workspace):
                self.workspace = workspace

            async def reply(self, message):
                text = message.get_text_content()
                if "当前阶段只做完整内容规划" in text:
                    calls["planning"] += 1
                    target = self.workspace / "deck" / "plan.json"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
                    draft = self.workspace / "deck" / "pages" / "draft.page"
                    draft.parent.mkdir(parents=True, exist_ok=True)
                    draft.write_text("elements: []\n", encoding="utf-8")
                    return None
                calls["execution"] += 1
                calls["draft_present"].append(
                    (self.workspace / "deck" / "pages" / "draft.page").exists()
                )
                payload = json.loads(text)
                calls["validation_issues"].append(payload["pageValidationIssues"])
                batch = payload["continuation"]["batchPages"]
                calls["batches"].append(batch)
                for index, path in enumerate(batch, 1):
                    target = self.workspace / "deck" / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if calls["execution"] == 1 and index == 1:
                        target.write_text(
                            "elements:\n"
                            "- elementId: malformed\n"
                            "  inset:\n"
                            "    - 0.0\n"
                            "  - 0.0\n",
                            encoding="utf-8",
                        )
                        continue
                    target.write_text(
                        "elements:\n"
                        f"- elementId: title-{calls['execution']}-{index}\n"
                        "  elementType: text\n"
                        "  bounds: [40, 40, 400, 60]\n"
                        "  content:\n"
                        "    text: 分批页面\n"
                        "    wrap: true\n"
                        "    align: [left, top]\n",
                        encoding="utf-8",
                    )
                return None

        async def run(request, *, data_root, progress, on_event=None, project_id, job_id):
            return await kimi_agent.run_kimi(
                request,
                data_root=data_root,
                progress=progress,
                on_event=on_event,
                project_id=project_id,
                job_id=job_id,
            )

        request = SimpleNamespace(
            prompt="生成演示",
            audience="团队",
            pageCount=None,
            references="",
            attachments=[],
            scenario="auto",
            templateId=None,
        )
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(kimi_agent, "make_model", return_value=model), \
             patch.object(kimi_agent, "_make_agent", side_effect=lambda _model, ws, _sid, _iters: Agent(ws)), \
             patch.object(kimi_agent, "_render_pages", return_value=None), \
             patch.object(kimi_agent, "final_layout_issues", return_value=[]), \
             patch.object(kimi_agent, "visual_overview_issues", return_value=[]):
            import asyncio
            result = asyncio.run(run(
                request,
                data_root=Path(tmp),
                progress=lambda *_: None,
                project_id="planned-batches",
                job_id="job-1",
            ))

        self.assertEqual(calls["planning"], 1)
        self.assertEqual(calls["execution"], 3)
        self.assertEqual(calls["draft_present"], [False, False, False])
        self.assertEqual([len(batch) for batch in calls["batches"]], [2, 2, 1])
        self.assertEqual(
            [len(issues) for issues in calls["validation_issues"]],
            [0, 1, 0],
        )
        self.assertEqual(
            calls["validation_issues"][1][0]["path"],
            "pages/1.page",
        )

    def test_run_kimi_repairs_layout_issue_before_commit(self):
        class Client:
            async def close(self):
                pass

        model = SimpleNamespace(model="fake", max_retries=0, budget_enabled=True, client=Client())
        calls = {"layout_repairs": 0}
        plan = {
            "deckTitle": "排版修复测试",
            "pages": [
                {"path": f"pages/{index}.page", "title": title,
                 "readerTask": "说明", "content": "内容", "visualPlan": "文本",
                 "sourceSections": []}
                for index, title in enumerate(("封面", "目录", "正文", "致谢"), 1)
            ],
        }
        page_markup = (
            "elements:\n"
            "- elementId: title-{index}\n"
            "  elementType: text\n"
            "  bounds: [40, 40, 400, 60]\n"
            "  content:\n"
            "    text: 页面内容\n"
            "    wrap: true\n"
            "    align: [left, top]\n"
        )

        class Agent:
            def __init__(self, workspace):
                self.workspace = workspace

            async def reply(self, message):
                text = message.get_text_content()
                if "当前阶段只做完整内容规划" in text:
                    target = self.workspace / "deck" / "plan.json"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
                    return None
                if "本轮只处理这些页面" in text:
                    calls["layout_repairs"] += 1
                    target = self.workspace / "deck" / "pages" / "1.page"
                    target.write_text(page_markup.format(index="repaired"), encoding="utf-8")
                    return None
                payload = json.loads(text)
                for index, path in enumerate(payload["continuation"]["batchPages"], 1):
                    target = self.workspace / "deck" / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(page_markup.format(index=index), encoding="utf-8")
                return None

        request = SimpleNamespace(
            prompt="生成演示", audience="团队", pageCount=None, references="",
            attachments=[], scenario="auto", templateId=None,
        )
        layout_calls = {"count": 0}

        def layout(*_args, **_kwargs):
            layout_calls["count"] += 1
            if layout_calls["count"] == 1:
                return [{"page": "pages/1.page", "location": "pages/1.page#title",
                         "elementId": "title-1", "code": "text-overflow", "message": "需要修复"}]
            return []

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(kimi_agent, "make_model", return_value=model), \
             patch.object(kimi_agent, "_make_agent", side_effect=lambda _model, ws, _sid, _iters: Agent(ws)), \
             patch.object(kimi_agent, "_render_pages", return_value=None), \
             patch.object(kimi_agent, "final_layout_issues", side_effect=layout), \
             patch.object(kimi_agent, "visual_overview_issues", return_value=[]):
            import asyncio
            result = asyncio.run(kimi_agent.run_kimi(
                request, data_root=Path(tmp), progress=lambda *_: None,
                project_id="layout-repair", job_id="job-layout",
            ))

        self.assertEqual(calls["layout_repairs"], 1)
        self.assertEqual(layout_calls["count"], 2)
        self.assertEqual(result["warnings"], [])
        self.assertEqual(len(result["pages"]), 4)
        self.assertEqual(result["revision"], 1)

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

    def test_partial_deck_reports_missing_pages(self):
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

    def test_malformed_page_is_not_reported_as_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = Path(tmp) / "deck" / "deck.pptd"
            (entry.parent / "pages").mkdir(parents=True)
            entry.write_text(
                "version: v2\nsize: [960, 540]\n"
                "pages: [pages/one.page, pages/two.page]\n",
                encoding="utf-8",
            )
            (entry.parent / "pages" / "one.page").write_text(
                "elements:\n"
                "- elementId: broken\n"
                "  inset:\n"
                "    - 0.0\n"
                "  - 0.0\n",
                encoding="utf-8",
            )
            (entry.parent / "pages" / "two.page").write_text(
                "elements: []\n", encoding="utf-8"
            )
            document, paths, existing, missing, invalid = kimi_agent._deck_status(entry)
            with self.assertRaisesRegex(ValueError, r"pages/one\.page:\d+:\d+"):
                kimi_agent._load_deck(entry)
        self.assertEqual(document["pages"], paths)
        self.assertEqual(existing, ["pages/two.page"])
        self.assertEqual(missing, [])
        self.assertEqual(len(invalid), 1)
        self.assertEqual(invalid[0]["path"], "pages/one.page")
        self.assertEqual(invalid[0]["code"], "yaml-invalid")
        self.assertIn("pages/one.page:", invalid[0]["location"])

    def test_white_text_on_white_background_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            rendered = Path(tmp)
            (rendered / "pages").mkdir()
            Image.new("RGB", (960, 540), "white").save(
                rendered / "pages" / "page-1.png"
            )
            document = {"size": [960, 540], "theme": {}}
            pages = [{
                "path": "pages/one.page",
                "data": {"elements": [{
                    "elementId": "invisible",
                    "elementType": "text",
                    "bounds": [40, 40, 400, 60],
                    "content": {
                        "text": '<span style="color:#FFFFFF">不可见标题</span>',
                    },
                }]},
            }]
            issues = kimi_agent._contrast_issues(document, pages, rendered)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["code"], "text-contrast")
        self.assertEqual(issues[0]["elementId"], "invisible")

    def test_configured_background_replaces_body_page_backgrounds_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = root / "deck" / "deck.pptd"
            entry.parent.mkdir(parents=True)
            background = root / "background.png"
            background.write_bytes(b"configured-background")
            document = {"size": [960, 540]}
            pages = [
                {"path": "pages/1.page", "data": {"elements": []}},
                {"path": "pages/2.page", "data": {"elements": [{
                    "elementId": "old-full-image",
                    "elementType": "image",
                    "src": "media/old.jpg",
                    "bounds": [0, 0, 960, 540],
                }]}},
                {"path": "pages/3.page", "data": {"elements": []}},
            ]
            kimi_agent._apply_configured_background(
                entry.parent, document, pages, background
            )
            self.assertNotIn("background", pages[0]["data"])
            self.assertNotIn("background", pages[2]["data"])
            self.assertEqual(len(pages[1]["data"]["elements"]), 1)
            configured = pages[1]["data"]["elements"][0]
            self.assertEqual(configured["elementId"], "host-background")
            self.assertEqual(configured["bounds"], [0, 0, 960, 540])
            self.assertTrue(configured["src"].endswith(".png"))
            self.assertTrue((entry.parent / configured["src"]).is_file())

    def test_continuation_batch_target_covers_all_remaining_batches(self):
        completed = 2
        for remaining_batches in range(7, 0, -1):
            target = kimi_agent._batch_page_target(
                23 - completed, remaining_batches, minimum=2
            )
            completed += target
        self.assertGreaterEqual(completed, 23)
        self.assertEqual(kimi_agent._batch_page_target(21, 7, 2), 3)
        self.assertEqual(kimi_agent._batch_page_target(4, 7, 2), 2)
        self.assertEqual(kimi_agent._batch_page_target(21, 7, 5), 5)
        self.assertEqual(kimi_agent._batch_page_target(0, 7, 2), 0)

    def test_recovery_checkpoint_reports_partial_agent_deck(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = kimi_agent._workspace(root, "project", "job")
            entry = workspace / "deck" / "deck.pptd"
            (entry.parent / "pages").mkdir(parents=True)
            entry.write_text(
                "version: v2\nsize: [960, 540]\n"
                "pages: [pages/one.page, pages/two.page, pages/three.page]\n",
                encoding="utf-8",
            )
            (entry.parent / "pages" / "one.page").write_text(
                "elements: []\n", encoding="utf-8"
            )
            checkpoint = kimi_agent.recovery_checkpoint(root, "project", "job")
        self.assertEqual(checkpoint["completedPages"], 1)
        self.assertEqual(checkpoint["totalPages"], 3)
        self.assertEqual(checkpoint["missingPages"], 2)
        self.assertEqual(checkpoint["summary"], "已保留 1/3 页")

    def test_recovery_checkpoint_does_not_count_malformed_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = kimi_agent._workspace(root, "project", "job")
            entry = workspace / "deck" / "deck.pptd"
            (entry.parent / "pages").mkdir(parents=True)
            entry.write_text(
                "version: v2\nsize: [960, 540]\n"
                "pages: [pages/one.page, pages/two.page, pages/three.page]\n",
                encoding="utf-8",
            )
            (entry.parent / "pages" / "one.page").write_text(
                "elements: []\n", encoding="utf-8"
            )
            (entry.parent / "pages" / "two.page").write_text(
                "elements:\n"
                "- elementId: broken\n"
                "  offset:\n"
                "    - 0.0\n"
                "  - -3.0\n",
                encoding="utf-8",
            )
            checkpoint = kimi_agent.recovery_checkpoint(root, "project", "job")
        self.assertEqual(checkpoint["completedPages"], 1)
        self.assertEqual(checkpoint["totalPages"], 3)
        self.assertEqual(checkpoint["missingPages"], 2)
        self.assertEqual(checkpoint["invalidPages"], 1)
        self.assertEqual(checkpoint["summary"], "已保留 1/3 页")

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

    def test_long_cover_title_uses_two_lines_and_moves_the_subtitle_down(self):
        page = {"elements": [
            {"elementId": "title", "elementType": "text", "bounds": [0, 0, 960, 540],
             "content": {"text": '<p style="text-align:center;line-height:1.2"><strong><span style="font-family:MiSans;font-size:66.0px;color:#FFFFFF;letter-spacing:4.0px">模板标题</span></strong></p><p style="text-align:center;line-height:1.2"></p><p style="text-align:center;line-height:1.2"></p>',
                         "align": ["left", "middle"]}},
            {"elementId": "subtitle", "elementType": "text", "bounds": [0, 244, 960, 36],
             "content": {"text": '<p style="text-align:center"><span style="font-size:24.0px">副标题</span></p>'}},
            {"elementId": "date", "elementType": "text", "bounds": [0, 321, 960, 55],
             "content": {"text": '<p style="text-align:center"><span style="font-size:24.0px">2026.9</span></p>'}},
        ]}
        title = page["elements"][0]
        title["content"] = kimi_agent._relabel(title["content"], "集团军用计算业务高质量发展专项论证报告")
        kimi_agent._fit_cover_slots(page, [960, 540])
        markup = title["content"]["text"]
        self.assertEqual(markup.count("<p"), 2)
        self.assertIn("集团军用计算业务", markup)
        self.assertIn("高质量发展专项论证报告", markup)
        self.assertLess(float(re.search(r"font-size:([0-9.]+)px", markup).group(1)), 66)
        self.assertGreaterEqual(page["elements"][1]["bounds"][1], title["bounds"][1] + title["bounds"][3] + 16)
        self.assertGreater(page["elements"][2]["bounds"][1], page["elements"][1]["bounds"][1])

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
            self.assertEqual(body[0], {
                "elementId": "host-background", "elementType": "image", "src": "media/body.jpg",
                "bounds": [0, 0, 960, 540], "fit": {"mode": "fill"},
                "crop": {"left": 0, "top": 0, "right": 0, "bottom": 0}})
            self.assertNotIn("background", pages[1]["data"])
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
            self.assertEqual(pages[1]["data"]["elements"][0]["src"], "media/canonical.jpg")
            self.assertEqual(pages[2]["data"]["elements"][0]["src"], "media/canonical.jpg")
            self.assertEqual(pages[2]["data"]["elements"][0]["fit"], {"mode": "fill"})
            self.assertEqual(pages[3]["data"]["elements"][0]["src"], "media/canonical.jpg")
            self.assertNotIn("background", pages[2]["data"])
            self.assertNotIn("background", pages[3]["data"])
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
