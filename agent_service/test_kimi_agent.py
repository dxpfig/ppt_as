"""Unit and gated integration coverage for the agentic kimi-slides expert."""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from agent_service import kimi_agent, kimi_preview


class KimiAgentProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_stream_emits_model_and_tool_status_without_reasoning(self):
        from agentscope.event import (
            ModelCallEndEvent,
            ModelCallStartEvent,
            ReplyStartEvent,
            ToolCallDeltaEvent,
            ToolCallStartEvent,
            ToolResultEndEvent,
        )
        from agentscope.message import Msg, ToolResultState

        class StreamingAgent:
            async def reply_stream(self, _message, **_kwargs):
                yield ReplyStartEvent(
                    session_id="session",
                    reply_id="reply",
                    name="PPT",
                )
                yield ModelCallStartEvent(
                    reply_id="reply",
                    model_name="fake",
                )
                yield ModelCallEndEvent(
                    reply_id="reply",
                    input_tokens=1234,
                    output_tokens=56,
                )
                yield ToolCallStartEvent(
                    reply_id="reply",
                    tool_call_id="tool-1",
                    tool_call_name="kimi_slides",
                )
                yield ToolResultEndEvent(
                    reply_id="reply",
                    tool_call_id="tool-1",
                    state=ToolResultState.SUCCESS,
                )
                yield ToolCallStartEvent(
                    reply_id="reply",
                    tool_call_id="tool-2",
                    tool_call_name="Edit",
                )
                yield ToolCallDeltaEvent(
                    reply_id="reply",
                    tool_call_id="tool-2",
                    delta=json.dumps({
                        "file_path": "/tmp/kimi-ws/deck/pages/6.page",
                        "old_string": "private old content",
                        "new_string": "private new content",
                    }),
                )
                yield ToolResultEndEvent(
                    reply_id="reply",
                    tool_call_id="tool-2",
                    state=ToolResultState.SUCCESS,
                )
                yield Msg(
                    name="assistant",
                    role="assistant",
                    content=[{"type": "text", "text": "完成"}],
                )

        events = []

        async def emit(phase, status, message, **fields):
            events.append(
                {
                    'phase': phase,
                    'status': status,
                    'message': message,
                    **fields,
                }
            )

        result = await kimi_agent._reply_with_progress(
            StreamingAgent(),
            Msg(
                name="user",
                role="user",
                content=[{"type": "text", "text": "修复页面"}],
            ),
            emit=emit,
            phase="validate",
            activity="第 6 页排版修复",
            workspace=Path("/tmp/kimi-ws"),
        )
        self.assertEqual(kimi_agent._reply_text(result), "完成")
        messages = [event['message'] for event in events]
        self.assertTrue(any("等待模型返回下一步" in item for item in messages))
        self.assertTrue(any("正在运行 PPT 检查或截图工具" in item for item in messages))
        self.assertTrue(any("正在修改页面文件" in item for item in messages))
        self.assertTrue(any("执行完成" in item for item in messages))
        self.assertTrue(
            any(
                0 < event.get('progress', 0) <= 95
                for event in events
            )
        )
        self.assertNotIn("reasoning", json.dumps(events))
        edit_completion = next(
            event for event in events
            if event["message"].endswith("Edit 执行完成，模型正在判断下一步")
        )
        self.assertEqual(edit_completion["detail"], "deck/pages/6.page")
        self.assertNotIn("private old content", json.dumps(events))
        self.assertNotIn("private new content", json.dumps(events))

    async def test_agent_stream_stops_immediately_after_plan_submission(self):
        from agentscope.event import (
            ModelCallEndEvent,
            ModelCallStartEvent,
            ToolCallStartEvent,
            ToolResultEndEvent,
        )
        from agentscope.message import Msg, ToolResultState

        class StreamingAgent:
            async def reply_stream(self, _message, **_kwargs):
                yield ModelCallStartEvent(reply_id="reply", model_name="fake")
                yield ModelCallEndEvent(
                    reply_id="reply",
                    input_tokens=3210,
                    output_tokens=45,
                )
                yield ToolCallStartEvent(
                    reply_id="reply",
                    tool_call_id="plan-1",
                    tool_call_name="submit_plan",
                )
                yield ToolResultEndEvent(
                    reply_id="reply",
                    tool_call_id="plan-1",
                    state=ToolResultState.SUCCESS,
                )
                yield Msg(
                    name="assistant",
                    role="assistant",
                    content=[{"type": "text", "text": "不应继续读取"}],
                )

        events = []

        async def emit(phase, status, message, **fields):
            events.append((phase, status, message, fields))

        with tempfile.TemporaryDirectory() as tmp:
            result = await kimi_agent._reply_with_progress(
                StreamingAgent(),
                Msg(name="user", role="user", content=[{"type": "text", "text": "规划"}]),
                emit=emit,
                phase="plan",
                activity="PPT 内容策划",
                max_steps=6,
                workspace=Path(tmp),
                stop_when=lambda: True,
                tool_result_detail=lambda _name: (
                    "submit_plan 第 1 次：未通过；缺少 pages"
                ),
            )
            usage = json.loads(
                (Path(tmp) / ".model-usage.jsonl").read_text(encoding="utf-8")
            )
        self.assertTrue(result)
        self.assertTrue(any("正在提交并校验完整计划" in item[2] for item in events))
        self.assertFalse(any("不应继续读取" in item[2] for item in events))
        self.assertTrue(any(
            "缺少 pages" in str(item[3].get("detail", ""))
            for item in events
        ))
        self.assertEqual(usage["inputTokens"], 3210)
        self.assertEqual(usage["outputTokens"], 45)

    async def test_page_gate_repairs_and_rechecks_before_returning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deck = root / 'deck'
            (deck / 'pages').mkdir(parents=True)
            image = root / 'page.png'
            image.write_bytes(b'png')
            page_data = {
                'elements': [{
                    'elementId': 'title',
                    'elementType': 'text',
                    'bounds': [40, 40, 400, 60],
                    'content': {
                        'text': '待修复页面',
                        'wrap': True,
                        'align': ['left', 'top'],
                    },
                }],
            }
            entry = deck / 'deck.pptd'
            entry.write_text(
                'version: v2\nsize: [960, 540]\n'
                'pages: [pages/one.page]\n',
                encoding='utf-8',
            )
            (deck / 'pages' / 'one.page').write_text(
                'elements: []\n',
                encoding='utf-8',
            )

            class Publisher:
                def __init__(self):
                    self.calls = 0

                async def check_page(self, index, path):
                    self.calls += 1
                    if self.calls == 1:
                        return {
                            'ok': True,
                            'item': {
                                'index': index,
                                'path': path,
                                'title': '第一页',
                                'reviewStatus': 'issues',
                                'reviewError': None,
                            },
                            'issues': [{
                                'page': path,
                                'location': f'{path}#title',
                                'elementId': 'title',
                                'code': 'text-overflow',
                                'message': '标题溢出',
                            }],
                            'image': image,
                            'layouts': {},
                        }
                    return {
                        'ok': True,
                        'item': {
                            'index': index,
                            'path': path,
                            'title': '第一页',
                            'reviewStatus': 'passed',
                            'reviewError': None,
                        },
                        'issues': [],
                    }

            publisher = Publisher()
            events = []
            repaired = {
                **page_data,
                'elements': [{
                    **page_data['elements'][0],
                    'bounds': [40, 40, 520, 60],
                }],
            }

            async def repair(_request, state, issues, **_kwargs):
                self.assertEqual(state['document']['pages'], ['pages/one.page'])
                self.assertEqual(issues[0]['code'], 'text-overflow')
                return {
                    **state,
                    'pages': [{
                        'path': 'pages/one.page',
                        'data': repaired,
                    }],
                }

            async def emit(_phase, _status, message, **fields):
                events.append({'message': message, **fields})

            with patch.object(
                kimi_agent.visual_repair,
                'repair_deck',
                side_effect=repair,
            ):
                result = await kimi_agent._repair_page_until_pass(
                    SimpleNamespace(
                        project=SimpleNamespace(id='project'),
                    ),
                    workspace=root,
                    entry=entry,
                    deck_document={
                        'version': 'v2',
                        'size': [960, 540],
                        'pages': ['pages/one.page'],
                    },
                    page_path='pages/one.page',
                    page_index=0,
                    page_data=page_data,
                    publisher=publisher,
                    emit=emit,
                    max_repair_rounds=1,
                )
            written = (deck / 'pages' / 'one.page').read_text(encoding='utf-8')

        self.assertEqual(publisher.calls, 2)
        self.assertEqual(result['elements'][0]['bounds'], [40, 40, 520, 60])
        self.assertIn('520', written)
        self.assertTrue(any('修复通过前不会生成下一页' in item['message'] for item in events))
        self.assertTrue(any('已通过结构、渲染和 AI 检视' in item['message'] for item in events))


class KimiAgentUnitTests(unittest.TestCase):
    def setUp(self):
        preview_render = patch.object(
            kimi_preview,
            '_render_pages',
            return_value=None,
        )
        preview_render.start()
        self.addCleanup(preview_render.stop)

    def test_grouped_institute_ids_expand_without_relaxing_other_units(self):
        from agent_service.number_fidelity import numeric_tokens

        source = numeric_tokens("电科15所、32所和52所，中船709所、716所。")
        compact = numeric_tokens("电科15/32/52所，中船709/716所。")
        self.assertEqual(compact, source)
        self.assertIn("709所", source)
        self.assertNotIn("709亿", source)
        self.assertNotIn("709亿", compact)
        self.assertIn("709亿", numeric_tokens("市场规模可达709亿元。"))

    def test_system_prompt_layers_skill_and_adapter(self):
        prompt = kimi_agent.kimi_system_prompt(Path("/tmp/kimi-ws"))
        self.assertIn("name: kimi-slides", prompt)
        self.assertIn("HOST ADAPTER", prompt)
        self.assertIn("kimi_slides", prompt)
        self.assertIn("do not run the", prompt)

    def test_page_agent_stops_after_repeated_exploration_without_writes(self):
        from agentscope.event import ModelCallStartEvent, ReplyStartEvent

        class StreamAgent:
            async def reply_stream(self, _message, *, yield_final_msg=True):
                yield ReplyStartEvent(
                    session_id="test",
                    reply_id="reply",
                    name="assistant",
                )
                for index in range(6):
                    yield ModelCallStartEvent(
                        reply_id="reply",
                        model_name=f"model-{index}",
                    )

        events = []

        async def emit(phase, status, message, **fields):
            events.append((phase, status, message, fields))

        async def run():
            await kimi_agent._reply_with_progress(
                StreamAgent(),
                SimpleNamespace(),
                emit=emit,
                phase="pages",
                activity="逐页创作",
                max_steps=12,
                no_progress_after=5,
            )

        with self.assertRaises(kimi_agent.PageWriteStalled):
            asyncio.run(run())
        self.assertEqual(len(events), 7)

    def test_workspace_file_tools_resolve_relative_paths_safely(self):
        from agentscope.state import AgentState

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / 'deck' / 'pages').mkdir(parents=True)
            write = kimi_agent._WorkspaceWrite(workspace)
            edit = kimi_agent._WorkspaceEdit(workspace)
            read = kimi_agent._WorkspaceRead(workspace)

            asyncio.run(
                write.call(
                    'deck/pages/a.page',
                    'elements:\n- elementId: title\n  text: old\n',
                )
            )
            target = workspace / 'deck' / 'pages' / 'a.page'
            self.assertIn('old', target.read_text(encoding='utf-8'))

            asyncio.run(
                edit.call(
                    'deck/pages/a.page',
                    'old',
                    'new',
                )
            )
            self.assertIn('new', target.read_text(encoding='utf-8'))
            self.assertIn(
                'new',
                asyncio.run(read.call('deck/pages/a.page')).content[0].text,
            )

            state = AgentState()
            asyncio.run(
                write.call(
                    'deck/pages/a.page',
                    'elements:\n- elementId: title\n  text: replaced\n',
                    _agent_state=state,
                )
            )
            self.assertIn('replaced', target.read_text(encoding='utf-8'))

            gated = kimi_agent._WorkspaceWrite(
                workspace,
                {'deck/pages/a.page'},
            )
            denied = asyncio.run(
                gated.call(
                    'deck/pages/other.page',
                    'elements:\n- elementId: title\n',
                )
            )
            self.assertEqual(denied.state, 'error')
            self.assertFalse((workspace / 'deck' / 'pages' / 'other.page').exists())

            sealed_write = kimi_agent._WorkspaceWrite(
                workspace,
                {'deck/pages/a.page'},
                {'deck/pages/a.page'},
            )
            sealed = asyncio.run(
                sealed_write.call(
                    'deck/pages/a.page',
                    'elements:\n- elementId: title\n',
                )
            )
            self.assertEqual(sealed.state, 'error')
            self.assertIn('replaced', target.read_text(encoding='utf-8'))

            result = asyncio.run(write.call('../outside.page', 'bad'))
            self.assertEqual(result.state, 'error')
            self.assertFalse((workspace.parent / 'outside.page').exists())

    def test_deck_status_does_not_treat_metadata_stub_as_generated(self):
        with tempfile.TemporaryDirectory() as tmp:
            entry = Path(tmp) / 'deck.pptd'
            (Path(tmp) / 'pages').mkdir()
            entry.write_text(
                'version: v2\nsize: [960, 540]\npages: [pages/one.page]\n',
                encoding='utf-8',
            )
            (Path(tmp) / 'pages' / 'one.page').write_text(
                'title: 空壳页\nmeta:\n  page: 1\nelements: []\n',
                encoding='utf-8',
            )

            _, _, existing, missing, invalid = kimi_agent._deck_status(
                entry,
                require_content=True,
            )
            self.assertEqual(existing, [])
            self.assertEqual(missing, ['pages/one.page'])
            self.assertEqual(invalid[0]['code'], 'page-empty')

    def test_page_structure_check_rejects_non_pptd_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            deck = workspace / 'deck'
            (deck / 'pages').mkdir(parents=True)
            (deck / 'deck.pptd').write_text(
                'version: v2\nsize: [960, 540]\npages: [pages/one.page]\n',
                encoding='utf-8',
            )
            target = deck / 'pages' / 'one.page'
            target.write_text(
                'page:\n'
                '  elements:\n'
                '    - type: text\n'
                '      x: 10\n'
                '      y: 10\n'
                '      text: 错误结构\n',
                encoding='utf-8',
            )
            issues = kimi_agent._page_structure_issues(
                workspace,
                'deck/pages/one.page',
            )
            self.assertTrue(issues)
            self.assertTrue(all(issue['pagePath'] == 'pages/one.page' for issue in issues))

    def test_page_structure_check_catches_yaml_parser_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            deck = workspace / 'deck'
            (deck / 'pages').mkdir(parents=True)
            (deck / 'deck.pptd').write_text(
                'version: v2\nsize: [960, 540]\npages: [pages/one.page]\n',
                encoding='utf-8',
            )
            (deck / 'pages' / 'one.page').write_text(
                'elements:\n'
                '- elementId: title\n'
                '  elementType: text\n'
                '  bounds: [1, 2, 3, 4]\n'
                '  content:\n'
                '    text: "broken"}\n',
                encoding='utf-8',
            )
            issues = kimi_agent._page_structure_issues(
                workspace,
                'deck/pages/one.page',
            )
            self.assertEqual(issues[0]['code'], 'yaml-invalid')
            self.assertEqual(issues[0]['pagePath'], 'pages/one.page')

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
            self.assertIn("submit_plan", message)
            self.assertIn("不要读取模板文件或截图", message)
            self.assertNotIn("template-pptd", message)
            self.assertNotIn("template-screenshots", message)
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
            self.assertEqual(payload["references"], "")
            self.assertEqual(payload["referenceCharacters"], len("附件事实：完成率 83%。"))
            self.assertEqual(payload["planningBrief"], "附件事实：完成率 83%。")
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
            self.assertIsNone(payload["referenceFile"])
            self.assertIn("第 1 章", payload["planningBrief"])
            self.assertLessEqual(len(payload["planningBrief"]), 12000)
            self.assertNotIn("sources/reference.md", payload["note"])
            self.assertIn("不要读取文件", payload["note"])

    def test_planning_brief_keeps_chapter_evidence_without_full_text(self):
        from types import SimpleNamespace

        references = (
            "# 一、现状\n\n"
            "## 1.1 细分现状\n\n"
            + "背景段落。" * 300
            + "\n\n市场规模达到 142.6 亿元，同比增长 11.2%。"
            + "\n\n# 二、规划\n\n"
            "## 2.1 细分规划\n\n"
            + "规划段落。" * 300
            + "\n\n2028 年完成型号突破。"
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            info = kimi_agent._prepare_reference_material(
                workspace,
                "brief",
                SimpleNamespace(references=references),
                workspace,
            )
        brief = info["planningBrief"]
        self.assertLess(len(brief), len(references))
        self.assertIn("142.6 亿元", brief)
        self.assertIn("2028 年", brief)
        self.assertEqual(set(info["sectionExcerpts"]), {"一、现状", "二、规划"})
        self.assertEqual(
            set(info["sourceUnitBriefs"]),
            {"1.1 细分现状", "2.1 细分规划"},
        )

    def test_long_plan_requires_a_second_page_agenda(self):
        from types import SimpleNamespace

        outline = "\n".join([
            "# 一、现状",
            "## 1.1 全球现状",
            "# 二、规划",
            "## 2.1 发展目标",
        ])
        info = {"characters": 18000, "outline": outline}
        requirements = kimi_agent.plan_requirements(
            SimpleNamespace(pageCount=None),
            info,
        )
        self.assertTrue(requirements["requiresAgendaPage"])
        self.assertEqual(requirements["agendaPagePosition"], 2)
        plan = {
            "deckTitle": "测试报告",
            "pages": [
                {
                    "path": "pages/01-cover.page",
                    "title": "测试报告",
                    "readerTask": "建立主题",
                    "content": "报告主题",
                    "visualPlan": "模板封面",
                    "sourceSections": [],
                },
                {
                    "path": "pages/02-content.page",
                    "title": "现状",
                    "readerTask": "理解现状",
                    "content": "现状事实",
                    "visualPlan": "证据页",
                    "sourceSections": ["一、现状"],
                },
            ],
        }
        self.assertTrue(
            kimi_agent.kimi_plan.ensure_required_agenda_page(
                plan,
                requirements,
            )
        )
        self.assertEqual(plan["pages"][1]["title"], "目录")
        self.assertEqual(plan["pages"][1]["kind"], "agenda")
        self.assertEqual(
            plan["pages"][1]["content"],
            "一、现状；二、规划",
        )

    def test_content_pages_keep_the_page_model_by_default(self):
        page = {
            "path": "pages/03.page",
            "title": "建设路线",
            "readerTask": "理解阶段安排",
            "content": "2026 年完成验证，2028 年完成定型。",
            "visualPlan": "三阶段时间轴",
            "sourceSections": ["四、发展规划"],
            "sourceSubsections": [],
        }
        self.assertIsNone(
            kimi_agent.build_planned_role_page(
                index=2,
                total=5,
                page=page,
                plan={"deckTitle": "测试"},
                requirements={"principalSections": ["四、发展规划"]},
                template_info={"pptd": "/missing-template"},
                deck_dir=Path("/missing-deck"),
            )
        )
        self.assertEqual(
            kimi_agent.planned_layout_archetype(page),
            "timeline",
        )

    def test_page_spec_carries_evidence_history_and_template_style(self):
        payload = json.loads(kimi_agent._page_spec_message(
            {
                "title": "建设路线",
                "visualPlan": "三阶段时间轴",
                "recommendedArchetype": "timeline",
            },
            "2026 年完成验证，2028 年完成定型。",
            layout_history=[
                {"pageIndex": 3, "family": "cards"},
                {"pageIndex": 4, "family": "comparison"},
            ],
            style_profile={
                "fontFamily": "微软雅黑",
                "colors": {"accent": "#E50011"},
                "sizes": {"title": 30, "body": 13},
            },
        ))
        self.assertIn("2028", payload["sourceEvidence"])
        self.assertEqual(
            [item["family"] for item in payload["layoutHistory"]],
            ["cards", "comparison"],
        )
        self.assertEqual(
            payload["templateStyle"]["fontFamily"],
            "微软雅黑",
        )
        self.assertTrue(
            payload["requirements"]["avoidLastTwoLayoutFamilies"]
        )

    def test_content_quality_rejects_missing_numbers_and_repeated_layouts(self):
        page = {
            "meta": {"sourcePath": "pages/03.page"},
            "elements": [
                {
                    "elementId": "title",
                    "elementType": "text",
                    "bounds": [40, 30, 800, 50],
                    "content": {"text": "建设路线", "fontSize": 28},
                },
                {
                    "elementId": "body-1",
                    "elementType": "text",
                    "bounds": [40, 120, 800, 70],
                    "content": {"text": "完成关键技术验证。", "fontSize": 13},
                },
                {
                    "elementId": "body-2",
                    "elementType": "text",
                    "bounds": [40, 210, 800, 70],
                    "content": {"text": "进入型号适配。", "fontSize": 13},
                },
                {
                    "elementId": "body-3",
                    "elementType": "text",
                    "bounds": [40, 300, 800, 70],
                    "content": {"text": "形成规模化交付。", "fontSize": 13},
                },
            ],
        }
        issues = kimi_agent.content_layout_issues(
            page,
            {
                "content": (
                    "2026 年完成原型验证并确认关键链路指标，"
                    "2028 年完成型号适配，最终形成规模化交付能力。"
                    "页面需要同时说明验证内容、关键指标、阶段衔接、"
                    "型号适配边界、规模化交付条件以及后续推进责任。"
                )
            },
            [
                {"pageIndex": 1, "family": "editorial"},
                {"pageIndex": 2, "family": "editorial"},
            ],
        )
        codes = {item["code"] for item in issues}
        self.assertIn("planned-number-missing", codes)
        self.assertIn("planned-layout-too-sparse", codes)
        self.assertIn("layout-repetition", codes)

    def test_layout_compiler_inherits_template_typography_and_palette(self):
        from agent_service import layout_engine

        page = layout_engine.compile_page(
            {
                "title": "建设路线",
                "archetype": "timeline",
                "root": {
                    "type": "text",
                    "role": "body",
                    "text": "2026 年完成验证。",
                },
            },
            page_index=2,
            page_path="pages/03.page",
            style_profile={
                "fontFamily": "MiSans",
                "colors": {
                    "accent": "#123456",
                    "text": "#222222",
                    "surface": "#FAFAFA",
                },
                "sizes": {"title": 32, "body": 14},
            },
        )
        self.assertEqual(page["meta"]["archetype"], "timeline")
        self.assertEqual(
            page["elements"][0]["content"]["fontFamily"],
            "MiSans",
        )
        self.assertEqual(
            page["elements"][0]["content"]["color"],
            "#123456",
        )
        self.assertEqual(
            page["elements"][1]["content"]["fontFamily"],
            "MiSans",
        )

    def test_card_children_flow_vertically_instead_of_overlapping(self):
        from agent_service import layout_engine

        page = layout_engine.compile_page(
            {
                "title": "研发进展",
                "root": {
                    "type": "card",
                    "padding": 16,
                    "gap": 8,
                    "children": [
                        {
                            "type": "text",
                            "role": "caption",
                            "text": "节点 01｜2026年",
                        },
                        {
                            "type": "text",
                            "role": "heading",
                            "text": "原型验证",
                        },
                        {
                            "type": "text",
                            "role": "metric",
                            "text": "关键链路时延下降 30%",
                        },
                        {
                            "type": "text",
                            "role": "body",
                            "text": "完成原型验证，进入技术验证阶段。",
                        },
                    ],
                },
            },
            page_index=0,
            page_path="pages/01.page",
        )
        texts = [
            element
            for element in page["elements"]
            if element.get("elementType") == "text"
            and element.get("elementId") != "p1-1-page-title"
        ]
        self.assertEqual(len(texts), 4)
        for upper, lower in zip(texts, texts[1:]):
            self.assertLessEqual(
                upper["bounds"][1] + upper["bounds"][3],
                lower["bounds"][1] + 0.01,
            )

    def test_exact_page_plan_rejects_a_missing_agenda(self):
        from types import SimpleNamespace

        outline = "\n".join([
            "# 一、现状",
            "# 二、规划",
        ])
        requirements = kimi_agent.plan_requirements(
            SimpleNamespace(pageCount=4),
            {"characters": 1000, "outline": outline},
        )
        self.assertTrue(requirements["requiresAgendaPage"])
        pages = [
            {
                "path": f"pages/{index}.page",
                "title": title,
                "readerTask": "说明",
                "content": "内容",
                "visualPlan": "正文",
                "sourceSections": sections,
            }
            for index, (title, sections) in enumerate(
                [
                    ("封面", []),
                    ("现状", ["一、现状"]),
                    ("规划", ["二、规划"]),
                    ("致谢", []),
                ],
                1,
            )
        ]
        plan = {"deckTitle": "测试", "pages": pages}
        self.assertFalse(
            kimi_agent.kimi_plan.ensure_required_agenda_page(
                plan,
                requirements,
            )
        )
        self.assertTrue(any(
            "必须包含目录页" in issue
            for issue in kimi_agent.audit_plan(plan, requirements)
        ))

    def test_plan_field_limits_are_normalized_before_validation(self):
        requirements = {
            "fieldLimits": {
                "title": 60,
                "readerTask": 120,
                "content": 320,
                "visualPlan": 200,
            }
        }
        content = (
            "第一章说明现状与趋势。" * 30
            + "第二章说明发展需求。" * 20
        )
        plan = {
            "pages": [{
                "path": "pages/01.page",
                "title": "结论",
                "readerTask": "理解结论",
                "content": content,
                "visualPlan": "证据页",
                "sourceSections": [],
            }]
        }
        self.assertTrue(
            kimi_agent.kimi_plan.normalize_plan_field_limits(
                plan,
                requirements,
            )
        )
        self.assertLessEqual(len(plan["pages"][0]["content"]), 320)
        self.assertTrue(plan["pages"][0]["content"].endswith("。"))

    def test_plan_must_cover_every_second_level_section_and_keep_owner(self):
        from types import SimpleNamespace

        outline = "\n".join(
            [
                "# 一、现状",
                "## 1.1 全球现状",
                "### 1.1.1 全球演进",
                "## 1.2 国内现状",
                "### 1.2.1 国内产业",
                "# 二、规划",
                "## 2.1 发展目标",
                "### 2.1.1 阶段目标",
            ]
        )
        requirements = kimi_agent.plan_requirements(
            SimpleNamespace(pageCount=2),
            {"characters": 1000, "outline": outline},
        )
        requirements["minimumPlanPages"] = 2
        pages = [
            {
                "path": "pages/1.page",
                "title": "现状",
                "readerTask": "理解现状",
                "content": "现状事实",
                "visualPlan": "时间轴",
                "sourceSections": ["一、现状"],
                "sourceSubsections": ["1.1 全球现状"],
            },
            {
                "path": "pages/2.page",
                "title": "规划",
                "readerTask": "理解规划",
                "content": "规划事实",
                "visualPlan": "路线图",
                "sourceSections": ["二、规划"],
                "sourceSubsections": ["2.1 发展目标"],
            },
        ]
        issues = kimi_agent.audit_plan({"pages": pages}, requirements)
        self.assertTrue(any("缺少：1.2 国内现状" in issue for issue in issues))

        pages[0]["sourceSubsections"] = ["1.1 全球现状", "1.2 国内现状"]
        kimi_agent.kimi_plan.attach_source_details(
            {"pages": pages},
            requirements,
        )
        self.assertEqual(kimi_agent.audit_plan({"pages": pages}, requirements), [])

        pages[1]["sourceSubsections"] = ["1.2 国内现状"]
        pages[1]["sourceSections"] = ["二、规划"]
        kimi_agent.kimi_plan.attach_source_details(
            {"pages": pages},
            requirements,
        )
        issues = kimi_agent.audit_plan({"pages": pages}, requirements)
        self.assertTrue(any("错误归属" in issue for issue in issues))

    def test_page_batch_receives_source_evidence_for_assigned_subsections(self):
        from types import SimpleNamespace

        request = SimpleNamespace(
            prompt="生成",
            audience="团队",
            pageCount=1,
            references="",
            attachments=[],
        )
        plan = {
            "pages": [
                {
                    "path": "pages/1.page",
                    "title": "现状",
                    "readerTask": "理解现状",
                    "content": "现状结论",
                    "visualPlan": "时间轴",
                    "sourceSections": ["一、现状"],
                    "sourceSubsections": ["1.1 全球现状"],
                }
            ]
        }
        payload = json.loads(
            kimi_agent._execution_message(
                request,
                None,
                {
                    "subsectionExcerpts": {
                        "1.1 全球现状": "算力成为第三大战斗力要素，增长率 6.9%。"
                    }
                },
                plan,
                {"batch": 1, "batchPages": ["pages/1.page"]},
            )
        )
        self.assertIn("第三大战斗力要素", payload["batchPagePlans"][0]["sourceEvidence"])
        self.assertIn("不得改写枚举数量", payload["note"])
        self.assertIn("elementId: title", payload["pptdPageTemplate"])
        self.assertIn("禁止使用 page: 外层", payload["note"])

    def test_plan_submitter_validates_then_stops_after_acceptance(self):
        from types import SimpleNamespace

        request = SimpleNamespace(pageCount=2)
        requirements = kimi_agent.plan_requirements(request, {})
        requirements["minimumPlanPages"] = 2
        invalid = {"pages": [{"path": "pages/1.page"}]}
        valid = {
            "deckTitle": "测试",
            "pages": [
                {
                    "path": f"pages/{index}.page",
                    "title": f"第 {index} 页",
                    "readerTask": "理解结论",
                    "content": "事实与结论",
                    "visualPlan": "卡片",
                    "sourceSections": [],
                }
                for index in (1, 2)
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            submitter = kimi_agent._PlanSubmitter(Path(tmp), requirements)
            rejected = json.loads(submitter.submit(invalid))
            self.assertFalse(rejected["accepted"])
            self.assertFalse(submitter.finished)
            self.assertIn("submit_plan 第 1 次：未通过", submitter.describe_result("submit_plan"))
            rejected_path = Path(tmp) / "deck" / "plan.rejected-1.json"
            self.assertTrue(rejected_path.is_file())
            self.assertTrue(json.loads(rejected_path.read_text())["issues"])
            accepted = json.loads(submitter.submit(valid))
            self.assertTrue(accepted["accepted"])
            self.assertTrue(submitter.finished)
            saved = json.loads((Path(tmp) / "deck" / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, valid)

    def test_plan_submit_tool_accepts_flat_model_arguments(self):
        from types import SimpleNamespace

        requirements = kimi_agent.plan_requirements(
            SimpleNamespace(pageCount=1),
            {},
        )
        requirements["minimumPlanPages"] = 1
        page = {
            "path": "pages/1.page",
            "title": "结论",
            "readerTask": "理解结论",
            "content": "事实与结论",
            "visualPlan": "卡片",
            "sourceSections": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            submitter = kimi_agent._PlanSubmitter(Path(tmp), requirements)
            chunk = asyncio.run(
                submitter.tool.call(deckTitle="测试", pages=[page])
            )
            self.assertIn("accepted", chunk.content[0].text)
            saved = json.loads(
                (Path(tmp) / "deck" / "plan.json").read_text(encoding="utf-8")
            )
        self.assertEqual(saved["deckTitle"], "测试")
        self.assertEqual(saved["pages"], [page])

    def test_plan_submit_tool_accepts_nested_and_json_string_plans(self):
        from types import SimpleNamespace

        requirements = kimi_agent.plan_requirements(
            SimpleNamespace(pageCount=1),
            {},
        )
        requirements["minimumPlanPages"] = 1
        page = {
            "path": "pages/1.page",
            "title": "结论",
            "readerTask": "理解结论",
            "content": "事实与结论",
            "visualPlan": "卡片",
            "sourceSections": [],
        }
        plan = {"deckTitle": "测试", "pages": [page]}
        for mode, value in (
            ("nested", {"plan": plan}),
            ("string", {"plan": json.dumps(plan, ensure_ascii=False)}),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                submitter = kimi_agent._PlanSubmitter(Path(tmp), requirements)
                chunk = asyncio.run(submitter.tool.call(**value))
                self.assertIn("accepted", chunk.content[0].text)
                saved = json.loads(
                    (Path(tmp) / "deck" / "plan.json").read_text(encoding="utf-8")
                )
                self.assertEqual(saved["pages"], [page])

    def test_long_source_requires_a_complete_multi_chapter_plan(self):
        from types import SimpleNamespace

        outline = "\n".join([
            "# 军用计算报告.docx",
            "# 一、军用计算业务发展现状与趋势分析",
            "## 1.1 全球军事智能化加速演进",
            "# 二、未来军用计算业务发展需求",
            "# 集团军用计算业务发展基础与现状",
            "# 四、集团军用计算业务高质量发展规划",
            "# 五、全方位保障措施",
        ])
        request = SimpleNamespace(pageCount=None)
        info = {
            "characters": 47086,
            "outline": outline,
            "numericTokens": ["2026年", "6.9%", "1.94万亿"],
        }
        requirements = kimi_agent.plan_requirements(request, info)
        self.assertEqual(len(requirements["principalSections"]), 5)
        self.assertEqual(requirements["minimumPlanPages"], 17)
        self.assertEqual(requirements["recommendedPlanPages"], 17)
        self.assertEqual(requirements["maximumPlanPages"], 32)
        self.assertEqual(requirements["fieldLimits"]["content"], 320)
        self.assertIsNone(requirements["exactPlanPages"])

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
        oversized["pages"][0]["content"] = "过长" * 161
        self.assertTrue(any(
            "content 过长" in issue
            for issue in kimi_agent.audit_plan(oversized, requirements)
        ))
        invented = {
            **five_page_plan,
            "pages": [dict(page) for page in five_page_plan["pages"]],
        }
        invented["pages"][0]["content"] = "虚构增长 99.9%。"
        self.assertTrue(any(
            "不存在的数字：99.9%" in issue
            for issue in kimi_agent.audit_plan(invented, requirements)
        ))

        explicit = kimi_agent.plan_requirements(SimpleNamespace(pageCount=5), info)
        self.assertEqual(explicit["preferredPlanPages"], 5)
        self.assertEqual(explicit["minimumPlanPages"], 5)
        self.assertEqual(explicit["recommendedPlanPages"], 5)
        self.assertEqual(explicit["maximumPlanPages"], 5)
        self.assertEqual(explicit["exactPlanPages"], 5)
        explicit_plan = {**five_page_plan, "pages": [dict(page) for page in five_page_plan["pages"]]}
        explicit_plan["pages"][3]["sourceSections"] = [
            requirements["principalSections"][1],
            requirements["principalSections"][2],
            requirements["principalSections"][3],
        ]
        explicit_plan["pages"][2]["sourceSubsections"] = [
            requirements["requiredSubsections"][0]
        ]
        explicit_plan["pages"][4]["sourceSections"] = [requirements["principalSections"][4]]
        explicit_issues = kimi_agent.audit_plan(explicit_plan, explicit)
        self.assertFalse(any("计划页数不足" in issue for issue in explicit_issues))
        self.assertFalse(any("用户指定" in issue for issue in explicit_issues))

    def test_explicit_single_page_count_is_an_exact_plan_contract(self):
        from types import SimpleNamespace

        requirements = kimi_agent.plan_requirements(
            SimpleNamespace(pageCount=1),
            {},
        )
        self.assertEqual(requirements["minimumPlanPages"], 1)
        self.assertEqual(requirements["recommendedPlanPages"], 1)
        self.assertEqual(requirements["maximumPlanPages"], 1)
        self.assertEqual(requirements["exactPlanPages"], 1)
        plan = {
            "deckTitle": "单页封面",
            "pages": [{
                "path": "pages/01.page",
                "title": "封面",
                "readerTask": "建立主题",
                "content": "单页内容",
                "visualPlan": "封面",
                "sourceSections": [],
            }],
        }
        self.assertEqual(kimi_agent.audit_plan(plan, requirements), [])
        plan["pages"].append({
            "path": "pages/02.page",
            "title": "多余页",
            "readerTask": "额外内容",
            "content": "不应生成",
            "visualPlan": "正文",
            "sourceSections": [],
        })
        self.assertTrue(any(
            "计划页数超过上限" in issue
            for issue in kimi_agent.audit_plan(plan, requirements)
        ))

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
            self.assertIn("content ≤ 320 字", planning["note"])
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
            "pageCalls": [],
        }
        agent_options = []
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
                from agentscope.message import Msg

                text = message.get_text_content()
                if "当前阶段只做完整内容规划" in text:
                    calls["planning"] += 1
                    target = self.workspace / "deck" / "plan.json"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
                    draft = self.workspace / "deck" / "pages" / "draft.page"
                    draft.parent.mkdir(parents=True, exist_ok=True)
                    draft.write_text("elements: []\n", encoding="utf-8")
                    return Msg(
                        name="assistant",
                        role="assistant",
                        content=[{"type": "text", "text": "planned"}],
                    )
                calls["execution"] += 1
                payload = json.loads(text)
                page = payload["page"]
                calls["pageCalls"].append(page["path"])
                if calls["execution"] == 1:
                    result = "{invalid json"
                else:
                    result = json.dumps(
                        {
                            "title": page["title"],
                            "kind": "content",
                            "root": {
                                "type": "text",
                                "role": "body",
                                "text": "分批页面",
                            },
                        },
                        ensure_ascii=False,
                    )
                return Msg(
                    name="assistant",
                    role="assistant",
                    content=[{"type": "text", "text": result}],
                )

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
        async def page_gate(_publisher, index, path):
            return {
                'ok': True,
                'item': {
                    'path': path,
                    'index': index,
                    'title': plan['pages'][index]['title'],
                    'reviewStatus': 'passed',
                    'reviewError': None,
                },
                'issues': [],
            }
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(kimi_agent, "make_model", return_value=model), \
             patch.object(
                 kimi_agent,
                 "_make_agent",
                 side_effect=lambda _model, ws, _sid, _iters, **kwargs: (
                     agent_options.append(kwargs),
                     Agent(ws),
                 )[1],
             ), \
             patch.object(kimi_agent, "_render_pages", return_value=None), \
             patch.object(
                 kimi_agent.DraftPreviewPublisher,
                 "check_page",
                 new=page_gate,
             ), \
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
        self.assertEqual(agent_options[0]["allowed_tools"], ("submit_plan",))
        self.assertEqual(agent_options[0]["context_trigger_ratio"], 0.7)
        self.assertIn("submit_plan", agent_options[0]["extra_tools"])
        self.assertIn("content-planning", agent_options[0]["system_prompt"])
        self.assertEqual(calls["execution"], 5)
        self.assertEqual(
            calls["pageCalls"],
            ["pages/1.page", "pages/1.page", "pages/2.page", "pages/3.page", "pages/4.page"],
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
                from agentscope.message import Msg

                text = message.get_text_content()
                if "当前阶段只做完整内容规划" in text:
                    target = self.workspace / "deck" / "plan.json"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
                    return Msg(
                        name="assistant",
                        role="assistant",
                        content=[{"type": "text", "text": "planned"}],
                    )
                if "本轮只处理这些页面" in text:
                    calls["layout_repairs"] += 1
                    calls["layout_prompt"] = text
                    target = self.workspace / "deck" / "pages" / "1.page"
                    target.write_text(page_markup.format(index="repaired"), encoding="utf-8")
                    return Msg(
                        name="assistant",
                        role="assistant",
                        content=[{"type": "text", "text": "repaired"}],
                    )
                payload = json.loads(text)
                page = payload["page"]
                return Msg(
                    name="assistant",
                    role="assistant",
                    content=[{
                        "type": "text",
                        "text": json.dumps(
                            {
                                "title": page["title"],
                                "kind": "content",
                                "root": {
                                    "type": "text",
                                    "role": "body",
                                    "text": "页面内容",
                                },
                            },
                            ensure_ascii=False,
                        ),
                    }],
                )

        request = SimpleNamespace(
            prompt="生成演示", audience="团队", pageCount=None, references="",
            attachments=[], scenario="auto", templateId=None,
        )
        layout_calls = {"count": 0}
        page_checks = []

        def layout(*_args, **_kwargs):
            layout_calls["count"] += 1
            if layout_calls["count"] == 1:
                return [{"page": "pages/1.page", "location": "pages/1.page#title",
                         "elementId": "title-1", "code": "text-overflow", "message": "需要修复"}]
            return []

        def page_check(_directory, page_spec, _severity, _level):
            page_checks.append(page_spec)
            return []
        async def page_gate(_publisher, index, path):
            return {
                'ok': True,
                'item': {
                    'path': path,
                    'index': index,
                    'title': plan['pages'][index]['title'],
                    'reviewStatus': 'passed',
                    'reviewError': None,
                },
                'issues': [],
            }

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(kimi_agent, "make_model", return_value=model), \
             patch.object(kimi_agent, "_make_agent", side_effect=lambda _model, ws, _sid, _iters, **_kwargs: Agent(ws)), \
             patch.object(kimi_agent, "_render_pages", return_value=None), \
             patch.object(
                 kimi_agent.DraftPreviewPublisher,
                 "check_page",
                 new=page_gate,
             ), \
             patch.object(kimi_agent, "final_layout_issues", side_effect=layout), \
             patch("pptd_engine.check.check_deck", side_effect=page_check), \
             patch.object(kimi_agent, "visual_overview_issues", return_value=[]):
            import asyncio
            result = asyncio.run(kimi_agent.run_kimi(
                request, data_root=Path(tmp), progress=lambda *_: None,
                project_id="layout-repair", job_id="job-layout",
            ))

        self.assertEqual(calls["layout_repairs"], 1)
        self.assertIn("check deck -p 1 --level auto", calls["layout_prompt"])
        self.assertIn("不要在本批次内运行整份", calls["layout_prompt"])
        self.assertEqual(page_checks, ["1"])
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
                "elements:\n"
                "- elementId: title\n"
                "  elementType: text\n"
                "  bounds: [40, 40, 400, 60]\n"
                "  content:\n"
                "    text: 已完成页面\n",
                encoding="utf-8",
            )
            _, paths, existing, missing = kimi_agent._deck_progress(entry)
            document = kimi_agent._load_document(entry)
        self.assertEqual(paths, ["pages/one.page", "pages/two.page"])
        self.assertEqual(existing, ["pages/one.page"])
        self.assertEqual(missing, ["pages/two.page"])
        self.assertEqual(document["pages"], paths)

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
                "elements:\n"
                "- elementId: title\n"
                "  elementType: text\n"
                "  bounds: [40, 40, 400, 60]\n"
                "  content:\n"
                "    text: 已完成页面\n",
                encoding="utf-8",
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
                "elements:\n"
                "- elementId: title\n"
                "  elementType: text\n"
                "  bounds: [40, 40, 400, 60]\n"
                "  content:\n"
                "    text: 已完成页面\n",
                encoding="utf-8",
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
            kimi_agent._apply_chrome(deck, document, pages, chrome, template)
            repeated_ids = [
                e["elementId"]
                for e in pages[0]["data"]["elements"]
            ]
            self.assertEqual(repeated_ids, ids)
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
