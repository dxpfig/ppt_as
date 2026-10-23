"""Single-page benchmark runner contracts."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import yaml
from PIL import Image

from agent_service import review_service, visual_review
from agent_service.benchmark_timing import BenchmarkTrace
from agent_service.kimi_agent import (
    _planned_agenda_items,
    _single_page_agenda_content,
    _single_page_cards_content,
    _single_page_closing_content,
    _single_page_comparison_content,
    _single_page_cover_content,
    _single_page_dense_regression_content,
    _single_page_metrics_content,
    _single_page_table_process_content,
    planned_body_layout,
    planned_page_role,
    planned_role_visual_contract,
)
from agent_service.kimi_template import (
    _apply_template_cover_closing,
    _planned_body_blocks,
    _template_body_page_refs,
    build_template_agenda,
    build_template_body_flow,
    build_template_cards,
    build_template_closing,
    build_template_comparison,
    build_template_cover,
    build_template_dense_regression,
    build_template_metrics_matrix,
    build_template_table_process,
    template_style_profile,
    template_theme,
)
from agent_service.visual_review import (
    VisionConfig,
    _single_page_contract_system,
    _validate_verdict,
    vision_extra_body,
    vision_review_token_budget,
)
from agent_service.single_page_benchmark import (
    ROOT,
    _quality_result,
    document_text,
    issue_counts,
    load_plan,
    selected_cases,
    stage_totals,
)


class BenchmarkFixtureTests(unittest.TestCase):
    def test_fixed_plan_has_ten_core_cases_and_inputs(self):
        cases, inputs = load_plan(
            ROOT / 'benchmarks/single-page-template/cases.v1.json',
            ROOT / 'benchmarks/single-page-template/inputs.v1.json',
        )
        mandatory = [
            item
            for item in cases['cases']
            if item.get('includeInFullSuite', True)
        ]
        self.assertEqual(len(mandatory), 10)
        self.assertEqual(len(selected_cases(cases, None)), 10)
        self.assertEqual(
            {item['id'] for item in cases['cases']},
            set(inputs),
        )
        self.assertEqual(
            cases['execution']['generationModel'],
            'MiniMax-M3.1-Flash',
        )
        self.assertEqual(
            cases['execution']['visionModel'],
            'MiniMax-M3.1-Flash',
        )
        self.assertIn('prompt', inputs['COV-01'])
        self.assertEqual(inputs['COV-01']['projectTitle'], '集团军用计算业务高质量发展专项论证报告')

    def test_page_two_regression_case_is_available_but_not_in_full_suite(self):
        cases, inputs = load_plan(
            ROOT / 'benchmarks/single-page-template/cases.v1.json',
            ROOT / 'benchmarks/single-page-template/inputs.v1.json',
        )
        selected = selected_cases(cases, ['REG-P02'])
        self.assertEqual([item['id'] for item in selected], ['REG-P02'])
        self.assertFalse(selected[0]['includeInFullSuite'])
        self.assertEqual(
            inputs['REG-P02']['singleTitle'],
            '算力成为第三大战斗力要素，国内自主可控体系加速成熟',
        )
        self.assertIn(
            '2026年全国国防支出预算增幅',
            inputs['REG-P02']['requiredText'],
        )
        self.assertTrue(
            selected[0]['deckRequirements']['mustGenerateAgendaPage']
        )
        self.assertEqual(
            inputs['REG-P02']['deckRequirements']['agendaItems'],
            [
                '军用计算业务发展现状与趋势分析',
                '未来军用计算业务发展需求',
                '集团军用计算业务发展基础与现状',
                '集团军用计算业务高质量发展规划',
                '全方位保障措施',
            ],
        )

    def test_issue_counts_separate_mechanical_and_visual_findings(self):
        counts = issue_counts([
            {'code': 'text-overlap'},
            {'code': 'text-overflow'},
            {'code': 'text-overflow'},
            {'code': 'text-clipped'},
            {'code': 'text-contrast'},
            {'code': 'visual-spacing'},
        ])
        self.assertEqual(
            counts,
            {
                'overlap': 1,
                'clipped': 1,
                'overflow': 2,
                'contrast': 1,
                'visual': 1,
            },
        )

    def test_trace_stage_totals_keep_each_attempt(self):
        trace = {
            'spans': [
                {'stage': 'browserRender', 'durationMs': 1200},
                {'stage': 'browserRender', 'durationMs': 800},
                {'stage': 'unrelated', 'durationMs': 9999},
            ]
        }
        self.assertEqual(stage_totals(trace)['browserRender'], 2000)


class BenchmarkQualityTests(unittest.TestCase):
    def case(self):
        return {
            'id': 'COV-01',
            'hardElementLimit': 10,
            'firstPassHardMs': 100000,
        }

    def input_case(self):
        return {
            'requiredText': ['标准封面', '2026年10月'],
            'forbiddenText': ['旧标题'],
            'singleTitle': '标准封面',
        }

    def document(self):
        return {
            'pages': [
                {
                    'path': 'pages/cover.page',
                    'data': {
                        'elements': [
                            {
                                'elementId': 'title',
                                'elementType': 'text',
                                'content': {'text': '标准封面'},
                            },
                            {
                                'elementId': 'date',
                                'elementType': 'text',
                                'content': {'text': '2026年10月'},
                            },
                        ]
                    },
                }
            ]
        }

    def trace(self, repair=False):
        spans = [
            {
                'stage': 'visionReview',
                'status': 'completed',
                'durationMs': 1000,
            }
        ]
        if repair:
            spans.append({
                'stage': 'repairModel',
                'status': 'completed',
                'durationMs': 800,
            })
        return {
            'spans': spans,
            'issueSets': [
                {
                    'stage': 'pageGate',
                    'recordedAt': 1,
                    'issues': [],
                },
                {
                    'stage': 'visionReview',
                    'recordedAt': 2,
                    'issues': [],
                },
            ],
        }

    def test_clean_first_round_passes(self):
        quality, _, _ = _quality_result(
            self.case(),
            self.input_case(),
            {'status': 'completed', 'execution': {'events': []}},
            self.document(),
            {'pages': [{'reviewStatus': 'passed', 'issues': []}]},
            self.trace(),
            False,
            50000,
        )
        self.assertTrue(quality['firstPass'])
        self.assertEqual(quality['checks']['failures'], [])

    def test_repair_cannot_count_as_first_pass(self):
        quality, _, _ = _quality_result(
            self.case(),
            self.input_case(),
            {'status': 'completed', 'execution': {'events': []}},
            self.document(),
            {'pages': [{'reviewStatus': 'passed', 'issues': []}]},
            self.trace(repair=True),
            False,
            50000,
        )
        self.assertFalse(quality['firstPass'])
        self.assertIn('repair-used', quality['checks']['failures'])

    def test_review_must_pass_and_reference_real_elements(self):
        quality, _, _ = _quality_result(
            self.case(),
            self.input_case(),
            {'status': 'completed', 'execution': {'events': []}},
            self.document(),
            {
                'pages': [{
                    'reviewStatus': 'issues',
                    'issues': [{
                        'code': 'visual-spacing',
                        'elementId': 'missing-element',
                    }],
                }]
            },
            self.trace(),
            False,
            50000,
        )
        self.assertFalse(quality['firstPass'])
        self.assertIn('visual-review-not-passed', quality['checks']['failures'])
        self.assertIn('visual-missing-element', quality['checks']['failures'])

    def test_explicit_vision_extra_body_overrides_inherited_thinking(self):
        env = {
            'PPT_LLM_PROVIDER': 'openai',
            'PPT_LLM_MODEL': 'local',
            'OPENAI_BASE_URL': 'http://local/v1',
            'PPT_LLM_REASONING_MODE': 'adaptive',
            'PPT_LLM_EXTRA_BODY': '{}',
            'PPT_VISION_EXTRA_BODY': '{"thinking":{"type":"disabled"}}',
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                vision_extra_body(
                    VisionConfig(
                        'openai',
                        'local',
                        'key',
                        'http://local/v1/',
                    )
                ),
                {'thinking': {'type': 'disabled'}},
            )

    def test_visual_token_budget_scales_only_for_dense_pages(self):
        env = {
            'PPT_VISION_MAX_TOKENS': '4096',
            'PPT_VISION_COMPLEX_MAX_TOKENS': '8192',
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(vision_review_token_budget(13), 4096)
            self.assertEqual(vision_review_token_budget(14), 8192)
            self.assertEqual(vision_review_token_budget(24), 8192)


class CoverFastPathTests(unittest.TestCase):
    def test_multi_page_plan_roles_are_detected_from_plan_content(self):
        pages = [
            {'title': '集团报告', 'visualPlan': '模板封面', 'content': ''},
            {'title': '目录：五章论证主线', 'visualPlan': '目录', 'content': ''},
            {'title': '1.1 发展现状', 'visualPlan': '正文', 'content': ''},
            {'title': '感谢观看', 'visualPlan': '模板收尾页', 'content': ''},
        ]
        plan = {'deckTitle': '集团报告'}
        self.assertEqual(
            [
                planned_page_role(index, len(pages), page, plan)
                for index, page in enumerate(pages)
            ],
            ['cover', 'agenda', 'content', 'closing'],
        )

    def test_planned_agenda_items_come_from_source_chapters(self):
        self.assertEqual(
            _planned_agenda_items(
                {'content': ''},
                {
                    'principalSections': [
                        '一、军用计算业务发展现状与趋势分析',
                        '二、未来军用计算业务发展需求',
                        '4五、集团军用计算业务发展基础与现状',
                    ]
                },
            ),
            [
                '军用计算业务发展现状与趋势分析',
                '未来军用计算业务发展需求',
                '集团军用计算业务发展基础与现状',
            ],
        )
        self.assertEqual(
            planned_role_visual_contract('cover'),
            [
                '封面必须复用公司模板封面。',
                '主标题、副标题及可用日期各出现一次。',
                '标题不得与副标题或日期重叠。',
            ],
        )
        self.assertIn(
            '全部目录项不得压住公司页脚。',
            planned_role_visual_contract('agenda'),
        )

    def test_planned_body_layout_comes_from_visual_plan(self):
        self.assertEqual(
            planned_body_layout({
                'title': '业务范围',
                'visualPlan': '左右双栏对照',
                'content': '',
            }),
            'columns',
        )
        self.assertEqual(
            planned_body_layout({
                'title': '保障措施',
                'visualPlan': '三列保障卡片',
                'content': '',
            }),
            'cards',
        )
        self.assertEqual(
            planned_body_layout({
                'title': '正文',
                'visualPlan': '纵向说明',
                'content': '正文内容。',
            }),
            'editorial',
        )
        self.assertEqual(
            _planned_agenda_items(
                {
                    'content': (
                        '四个章节依次为：①发展现状与趋势；'
                        '②未来业务需求；③集团基础与竞争格局；'
                        '④高质量发展规划。四章呈递进逻辑。'
                    )
                },
                {'principalSections': []},
            ),
            [
                '发展现状与趋势',
                '未来业务需求',
                '集团基础与竞争格局',
                '高质量发展规划',
            ],
        )
        self.assertEqual(
            _planned_agenda_items(
                {
                    'content': (
                        '一、发展现状与趋势：回顾基本盘。'
                        '二、未来业务需求：拆解新增要求。'
                        '三、集团基础与竞争格局：梳理资源禀赋。'
                        '四、高质量发展规划：提出实施路径。'
                    )
                },
                {'principalSections': []},
            ),
            [
                '发展现状与趋势',
                '未来业务需求',
                '集团基础与竞争格局',
                '高质量发展规划',
            ],
        )

    def test_explicit_one_page_cover_extracts_labelled_content(self):
        request = SimpleNamespace(
            pageCount=1,
            prompt=(
                '制作一份单页 PPT 封面。\n'
                '主标题：集团军用计算业务高质量发展专项论证报告\n'
                '副标题：发挥国产网信全链条优势\n'
                '汇报单位：长沙湘计海盾科技有限公司\n'
                '日期：2026年10月\n'
            ),
        )
        self.assertEqual(
            _single_page_cover_content(request),
            {
                'title': '集团军用计算业务高质量发展专项论证报告',
                'subtitle': '发挥国产网信全链条优势',
                'organization': '长沙湘计海盾科技有限公司',
                'date': '2026年10月',
                'designContract': [
                    '封面必须复用公司模板封面。',
                    '主标题、副标题、单位和日期保留原文并各出现一次。',
                    '标题不得与副标题或日期重叠。',
                ],
            },
        )
        request.pageCount = 2
        self.assertIsNone(_single_page_cover_content(request))

    def test_template_cover_has_one_copy_of_each_content_slot(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            template_dir = root / 'template'
            (template_dir / 'pages').mkdir(parents=True)
            (template_dir / 'template.pptd').write_text(
                yaml.safe_dump({
                    'version': 'v2',
                    'title': '模板',
                    'size': [960, 540],
                    'theme': {},
                    'pages': ['pages/page-1.page'],
                }, allow_unicode=True),
                encoding='utf-8',
            )
            (template_dir / 'pages' / 'page-1.page').write_text(
                yaml.safe_dump({
                    'elements': [
                        {
                            'elementId': 'title',
                            'elementType': 'text',
                            'bounds': [0, 0, 960, 540],
                            'content': {
                                'text': '<p style="text-align:center"><span style="font-family:MiSans;font-size:66px;color:#FFFFFF">旧标题</span></p>',
                                'align': ['left', 'middle'],
                            },
                        },
                        {
                            'elementId': 'subtitle',
                            'elementType': 'text',
                            'bounds': [0, 244, 960, 36],
                            'content': {
                                'text': '<p><span style="font-family:微软雅黑;font-size:24px;color:#FFFFFF">旧副标题</span></p>',
                            },
                        },
                        {
                            'elementId': 'date',
                            'elementType': 'text',
                            'bounds': [0, 321, 960, 36],
                            'content': {
                                'text': '<p><span style="font-family:微软雅黑;font-size:24px;color:#FFFFFF">2025.1</span></p>',
                            },
                        },
                    ],
                }, allow_unicode=True),
                encoding='utf-8',
            )
            page = build_template_cover(
                template_dir,
                root / 'deck',
                title='集团军用计算业务高质量发展专项论证报告',
                subtitle='发挥国产网信全链条优势',
                organization='长沙湘计海盾科技有限公司',
                date='2026年10月',
            )
        text = document_text({'pages': [{'data': page}]})
        self.assertEqual(text.count('集团军用计算业务高质量发展专项论证报告'), 1)
        self.assertEqual(text.count('发挥国产网信全链条优势'), 1)
        self.assertEqual(text.count('长沙湘计海盾科技有限公司'), 1)
        self.assertEqual(text.count('2026年10月'), 1)
        self.assertNotIn('旧标题', text)
        self.assertEqual(len(page['elements']), 4)

    def test_short_cover_title_gets_safe_non_overlapping_slot(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            template_dir = root / 'template'
            (template_dir / 'pages').mkdir(parents=True)
            (template_dir / 'template.pptd').write_text(
                yaml.safe_dump({
                    'version': 'v2',
                    'size': [960, 540],
                    'theme': {},
                    'pages': ['pages/cover.page'],
                }),
                encoding='utf-8',
            )
            (template_dir / 'pages' / 'cover.page').write_text(
                yaml.safe_dump({
                    'elements': [
                        {
                            'elementId': 'title',
                            'elementType': 'text',
                            'bounds': [0, 0, 960, 540],
                            'content': {
                                'text': '<p><span style="font-size:66px">旧标题</span></p>',
                            },
                        },
                        {
                            'elementId': 'subtitle',
                            'elementType': 'text',
                            'bounds': [0, 244, 960, 36],
                            'content': {
                                'text': '<p><span style="font-size:24px">旧副标题</span></p>',
                            },
                        },
                        {
                            'elementId': 'date',
                            'elementType': 'text',
                            'bounds': [0, 321, 960, 36],
                            'content': {
                                'text': '<p><span style="font-size:24px">2025.1</span></p>',
                            },
                        },
                    ],
                }),
                encoding='utf-8',
            )
            page = build_template_cover(
                template_dir,
                root / 'deck',
                title='发展规划',
                subtitle='自主可控计算体系',
                date='2026年10月',
            )
        title = next(
            item for item in page['elements']
            if item.get('elementId') == 'title'
        )
        subtitle = next(
            item for item in page['elements']
            if item.get('elementId') == 'subtitle'
        )
        self.assertLessEqual(
            title['bounds'][1] + title['bounds'][3],
            subtitle['bounds'][1],
        )

    def test_explicit_one_page_closing_extracts_main_text(self):
        request = SimpleNamespace(
            pageCount=1,
            prompt=(
                '制作一份单页 PPT 结束页，使用公司模板的结束页。\n'
                '主文字：感谢观看\n'
            ),
        )
        self.assertEqual(
            _single_page_closing_content(request),
            {
                'title': '感谢观看',
                'designContract': [
                    '结束页必须复用公司模板结束页。',
                    '主文字保留原文且只出现一次。',
                    '不得追加第二个结束页或致谢页。',
                ],
            },
        )
        request.prompt = '制作一页正文\n主文字：请领导讲话'
        self.assertIsNone(_single_page_closing_content(request))

    def test_explicit_one_page_agenda_extracts_numbered_items(self):
        request = SimpleNamespace(
            pageCount=1,
            prompt=(
                '制作一份单页 PPT 目录。\n'
                '1. 军用计算业务发展现状与趋势\n'
                '2. 未来军用计算业务发展需求\n'
                '3. 集团业务基础与竞争格局\n'
            ),
        )
        self.assertEqual(
            _single_page_agenda_content(request),
            {
                'title': '目录',
                'items': [
                    '军用计算业务发展现状与趋势',
                    '未来军用计算业务发展需求',
                    '集团业务基础与竞争格局',
                ],
                'designContract': [
                    '目录必须复用公司模板目录行样式。',
                    '章节编号和标题按输入顺序显示。',
                    '全部目录项不得压住公司页脚。',
                ],
            },
        )

    def test_template_agenda_extends_rows_without_losing_titles(self):
        items = [
            '军用计算业务发展现状与趋势',
            '未来军用计算业务发展需求',
            '集团业务基础与竞争格局',
            '高质量发展规划与实施路径',
            '组织、人才与资源保障',
        ]
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            template_dir = root / 'template'
            (template_dir / 'pages').mkdir(parents=True)
            (template_dir / 'template.pptd').write_text(
                yaml.safe_dump({
                    'version': 'v2',
                    'size': [960, 540],
                    'theme': {},
                    'pages': ['pages/cover.page', 'pages/agenda.page'],
                }, allow_unicode=True),
                encoding='utf-8',
            )
            (template_dir / 'pages' / 'cover.page').write_text(
                yaml.safe_dump({'elements': []}),
                encoding='utf-8',
            )
            rows = []
            for index, y in enumerate((160, 204), 1):
                rows.extend([
                    {
                        'elementId': f'red-{index}',
                        'elementType': 'shape',
                        'shapeName': 'roundRect',
                        'bounds': [287, y, 62, 34],
                    },
                    {
                        'elementId': f'num-{index}',
                        'elementType': 'text',
                        'bounds': [287, y, 62, 34],
                        'content': {'text': f'0{index}'},
                    },
                    {
                        'elementId': f'white-{index}',
                        'elementType': 'shape',
                        'shapeName': 'roundRect',
                        'bounds': [352, y, 326, 34],
                    },
                    {
                        'elementId': f'title-{index}',
                        'elementType': 'text',
                        'bounds': [358, y + 2, 100, 29],
                        'content': {
                            'text': f'<p><span style="font-family:微软雅黑;font-size:18px;color:#000000">旧标题{index}</span></p>',
                        },
                    },
                ])
            (template_dir / 'pages' / 'agenda.page').write_text(
                yaml.safe_dump({'elements': rows}, allow_unicode=True),
                encoding='utf-8',
            )
            page = build_template_agenda(
                template_dir,
                root / 'deck',
                items=items,
            )
        text = document_text({'pages': [{'data': page}]})
        for item in items:
            self.assertEqual(text.count(item), 1)
        for index in range(1, 6):
            self.assertIn(f'{index:02d}', text)
        title_rows = [
            item for item in page['elements']
            if item.get('elementId', '').startswith('title-')
        ]
        self.assertEqual(len(title_rows), 5)
        self.assertTrue(all(
            item['bounds'][1] + item['bounds'][3] < 540
            for item in title_rows
        ))
        self.assertTrue(all(
            item['bounds'][3] >= 38
            and 'font-size:15' in item['content']['text']
            for item in title_rows
        ))
        self.assertTrue(all(
            item['content']['inset'] == [0.0, 0.0, 0.0, 0.0]
            for item in page['elements']
            if item.get('elementType') == 'text'
        ))

    def test_explicit_goal_cards_extract_title_and_items(self):
        request = SimpleNamespace(
            pageCount=1,
            prompt=(
                '制作一份单页 PPT，三张等宽目标卡。'
                '页面标题：本期交付目标。\n'
                '1. 统一接口与数据口径\n'
                '2. 打通需求到发布链路\n'
                '3. 提升性能与稳定性\n'
            ),
        )
        self.assertEqual(
            _single_page_cards_content(request),
            {
                'title': '本期交付目标',
                'items': [
                    '统一接口与数据口径',
                    '打通需求到发布链路',
                    '提升性能与稳定性',
                ],
                'designContract': [
                    '三项必须使用等宽、等高等间距的横向卡片。',
                    '每张卡片按编号和目标标题垂直排列。',
                    '不得移除卡片或改成平均分栏以外的非卡片布局。',
                ],
            },
        )

    def test_template_goal_cards_are_equal_width_and_non_overlapping(self):
        items = [
            '统一接口与数据口径',
            '打通需求到发布链路',
            '提升性能与稳定性',
        ]
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            template_dir = root / 'template'
            template_dir.mkdir()
            (template_dir / 'template.pptd').write_text(
                yaml.safe_dump({
                    'version': 'v2',
                    'size': [960, 540],
                    'theme': {},
                    'pages': [],
                }, allow_unicode=True),
                encoding='utf-8',
            )
            page = build_template_cards(
                template_dir,
                root / 'deck',
                title='本期交付目标',
                items=items,
            )
        cards = [
            item for item in page['elements']
            if item.get('elementId', '').startswith('goal-card-')
        ]
        self.assertEqual(len(cards), 3)
        self.assertEqual(len({round(card['bounds'][2], 4) for card in cards}), 1)
        self.assertTrue(all(
            cards[index]['bounds'][0] + cards[index]['bounds'][2]
            < cards[index + 1]['bounds'][0]
            for index in range(2)
        ))
        text = document_text({'pages': [{'data': page}]})
        self.assertEqual(text.count('本期交付目标'), 1)
        for item in items:
            self.assertEqual(text.count(item), 1)

    def test_explicit_two_column_comparison_extracts_all_ranges(self):
        request = SimpleNamespace(
            pageCount=1,
            prompt=(
                '双栏范围对比。页面标题：本期范围与验收边界。\n'
                '左栏“本期覆盖”：核心业务模块与对外适配层；接口重构与数据口径统一；发布流程自动化。\n'
                '右栏“本期不做”：多租户改造；历史系统下线。\n'
                '底部结论：功能验收通过、监控全接入、支持灰度和一键回滚。\n'
            ),
        )
        value = _single_page_comparison_content(request)
        self.assertEqual(value['title'], '本期范围与验收边界')
        self.assertEqual(value['leftTitle'], '本期覆盖')
        self.assertEqual(
            value['leftItems'],
            [
                '核心业务模块与对外适配层',
                '接口重构与数据口径统一',
                '发布流程自动化',
            ],
        )
        self.assertEqual(value['rightTitle'], '本期不做')
        self.assertEqual(
            value['rightItems'],
            ['多租户改造', '历史系统下线'],
        )
        self.assertEqual(
            value['conclusion'],
            '功能验收通过、监控全接入、支持灰度和一键回滚',
        )

    def test_template_comparison_has_aligned_columns_and_safe_conclusion(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            template_dir = root / 'template'
            template_dir.mkdir()
            (template_dir / 'template.pptd').write_text(
                yaml.safe_dump({
                    'version': 'v2',
                    'size': [960, 540],
                    'theme': {},
                    'pages': [],
                }, allow_unicode=True),
                encoding='utf-8',
            )
            page = build_template_comparison(
                template_dir,
                root / 'deck',
                title='本期范围与验收边界',
                left_title='本期覆盖',
                left_items=[
                    '核心业务模块与对外适配层',
                    '接口重构与数据口径统一',
                    '发布流程自动化',
                ],
                right_title='本期不做',
                right_items=['多租户改造', '历史系统下线'],
                conclusion='功能验收通过、监控全接入、支持灰度和一键回滚',
            )
        cards = [
            item for item in page['elements']
            if item.get('elementId', '').endswith('-card')
        ]
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[0]['bounds'][1:], cards[1]['bounds'][1:])
        conclusion = next(
            item for item in page['elements']
            if item.get('elementId') == 'comparison-conclusion'
        )
        self.assertLessEqual(
            conclusion['bounds'][1] + conclusion['bounds'][3],
            516,
        )
        text = document_text({'pages': [{'data': page}]})
        for value in (
            '本期覆盖',
            '本期不做',
            '发布流程自动化',
            '历史系统下线',
            '功能验收通过、监控全接入、支持灰度和一键回滚',
        ):
            self.assertEqual(text.count(value), 1)

    def test_explicit_metrics_matrix_extracts_all_content(self):
        request = SimpleNamespace(
            pageCount=1,
            prompt=(
                '页面标题：军用计算能力建设路线。\n'
                'KPI：P95 响应下降 30%；关键链路可用性 ≥ 99.9%；交付周期缩短约 20%。\n'
                '2×2 矩阵：统一算力底座；边缘智能节点；星载智能处理；安全可信体系。\n'
                '结论：以统一底座支撑多场景计算，以模块化产品降低交付成本。\n'
            ),
        )
        value = _single_page_metrics_content(request)
        self.assertEqual(value['title'], '军用计算能力建设路线')
        self.assertEqual(len(value['kpis']), 3)
        self.assertEqual(len(value['matrixItems']), 4)
        self.assertEqual(
            value['conclusion'],
            '以统一底座支撑多场景计算，以模块化产品降低交付成本',
        )

    def test_template_metrics_matrix_has_balanced_twenty_elements(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            template_dir = root / 'template'
            template_dir.mkdir()
            (template_dir / 'template.pptd').write_text(
                yaml.safe_dump({
                    'version': 'v2',
                    'size': [960, 540],
                    'theme': {},
                    'pages': [],
                }, allow_unicode=True),
                encoding='utf-8',
            )
            page = build_template_metrics_matrix(
                template_dir,
                root / 'deck',
                title='军用计算能力建设路线',
                kpis=[
                    'P95 响应下降 30%',
                    '关键链路可用性 ≥ 99.9%',
                    '交付周期缩短约 20%',
                ],
                matrix_items=[
                    '统一算力底座',
                    '边缘智能节点',
                    '星载智能处理',
                    '安全可信体系',
                ],
                conclusion='以统一底座支撑多场景计算，以模块化产品降低交付成本',
            )
        kpi_cards = [
            item for item in page['elements']
            if item.get('elementId', '').startswith('kpi-card-')
        ]
        matrix_cards = [
            item for item in page['elements']
            if item.get('elementId', '').startswith('matrix-card-')
        ]
        self.assertEqual(len(kpi_cards), 3)
        self.assertEqual(len(matrix_cards), 4)
        self.assertEqual(
            len({round(card['bounds'][2], 4) for card in kpi_cards}),
            1,
        )
        self.assertEqual(
            {
                (round(card['bounds'][2], 4), round(card['bounds'][3], 4))
                for card in matrix_cards
            },
            {(392.0, 106.0)},
        )
        self.assertEqual(len(page['elements']), 20)
        text = document_text({'pages': [{'data': page}]})
        for value in (
            '军用计算能力建设路线',
            'P95 响应下降 30%',
            '关键链路可用性 ≥ 99.9%',
            '统一算力底座',
            '安全可信体系',
            '以统一底座支撑多场景计算，以模块化产品降低交付成本',
        ):
            self.assertEqual(text.count(value), 1)

    def test_explicit_table_process_extracts_all_content(self):
        request = SimpleNamespace(
            pageCount=1,
            prompt=(
                '页面标题：十五五前期重点建设任务。\n'
                '表格必须包含表头“任务、关键动作、阶段”和三行数据：\n'
                '统一模块化平台 | 完成五类标准模块 | 2026–2027\n'
                '典型装备适配 | 地面、舰艇、航空验证 | 2027–2028\n'
                '星载智能计算 | 工程样机与在轨验证 | 2028–2030\n'
                '流程：技术验证 → 型号适配 → 规模交付。\n'
                '请示框：建议批准专项立项，并同步启动跨单位联合攻关。\n'
            ),
        )
        value = _single_page_table_process_content(request)
        self.assertEqual(value['title'], '十五五前期重点建设任务')
        self.assertEqual(len(value['rows']), 3)
        self.assertEqual(value['stages'], ['技术验证', '型号适配', '规模交付'])
        self.assertEqual(
            value['requestText'],
            '建议批准专项立项，并同步启动跨单位联合攻关',
        )

    def test_template_table_process_has_fourteen_content_elements(self):
        rows = [
            ['统一模块化平台', '完成五类标准模块', '2026–2027'],
            ['典型装备适配', '地面、舰艇、航空验证', '2027–2028'],
            ['星载智能计算', '工程样机与在轨验证', '2028–2030'],
        ]
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            template_dir = root / 'template'
            template_dir.mkdir()
            (template_dir / 'template.pptd').write_text(
                yaml.safe_dump({
                    'version': 'v2',
                    'size': [960, 540],
                    'theme': {},
                    'pages': [],
                }, allow_unicode=True),
                encoding='utf-8',
            )
            page = build_template_table_process(
                template_dir,
                root / 'deck',
                title='十五五前期重点建设任务',
                columns=['任务', '关键动作', '阶段'],
                rows=rows,
                stages=['技术验证', '型号适配', '规模交付'],
                request_text='建议批准专项立项，并同步启动跨单位联合攻关',
            )
        self.assertEqual(len(page['elements']), 14)
        table = next(
            item for item in page['elements']
            if item.get('elementType') == 'table'
        )
        self.assertEqual(len(table['rows']), 4)
        self.assertEqual(len(table['columnWidths']), 3)
        request = next(
            item for item in page['elements']
            if item.get('elementId') == 'request-box'
        )
        self.assertLessEqual(
            request['bounds'][1] + request['bounds'][3],
            516,
        )
        process_label = next(
            item for item in page['elements']
            if item.get('elementId') == 'process-label'
        )
        process_arrows = [
            item for item in page['elements']
            if item.get('elementId', '').startswith('process-arrow-')
        ]
        self.assertEqual(
            process_label['content']['color'],
            '#4B5563',
        )
        self.assertTrue(all(
            item['content']['color'] == '#4B5563'
            for item in process_arrows
        ))
        self.assertEqual(
            next(
                item['content']['color']
                for item in page['elements']
                if item.get('elementId') == 'request-title'
            ),
            '#9B1C1C',
        )
        text = document_text({'pages': [{'data': page}]})
        for row in rows:
            for cell in row:
                self.assertEqual(text.count(cell), 1)
        self.assertEqual(
            text.count('建议批准专项立项，并同步启动跨单位联合攻关'),
            1,
        )

    def test_planned_body_blocks_preserve_every_character(self):
        content = (
            '第一项事实。第二项数据为6.9%；第三项判断；'
            '第四项任务。第五项风险。第六项建议。'
        )
        blocks = _planned_body_blocks(content)
        self.assertEqual(len(blocks), 4)
        self.assertEqual(''.join(blocks), content)

    def test_template_body_flow_uses_editorial_rows(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            template_dir = root / 'template'
            template_dir.mkdir()
            (template_dir / 'template.pptd').write_text(
                yaml.safe_dump({
                    'version': 'v2',
                    'size': [960, 540],
                    'theme': {},
                    'pages': [],
                }),
                encoding='utf-8',
            )
            page = build_template_body_flow(
                template_dir,
                root / 'deck',
                title='正文概览：一条主线、三大抓手',
                subtitle='快速理解业务主线与落地方式。',
                content=(
                    '自主可控算力体系：以国产软硬件为基础。'
                    '模块化产品：以标准接口快速适配。'
                    '规模化交付：建立批量供货能力。'
                ),
            )
        self.assertEqual(len(page['elements']), 8)
        self.assertFalse(any(
            item.get('elementType') == 'shape'
            and item.get('shapeName') == 'roundRect'
            for item in page['elements']
        ))
        text = document_text({'pages': [{'data': page}]})
        self.assertIn('自主可控算力体系', text)
        self.assertIn('规模化交付', text)

    def test_template_style_profile_and_cards_use_template_tokens(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            template_dir = root / 'template'
            (template_dir / 'pages').mkdir(parents=True)
            (template_dir / 'template.pptd').write_text(
                yaml.safe_dump({
                    'version': 'v2',
                    'size': [960, 540],
                    'pages': [
                        'pages/cover.page',
                        'pages/body.page',
                        'pages/closing.page',
                    ],
                }),
                encoding='utf-8',
            )
            (template_dir / 'pages' / 'cover.page').write_text(
                yaml.safe_dump({'elements': []}),
                encoding='utf-8',
            )
            (template_dir / 'pages' / 'closing.page').write_text(
                yaml.safe_dump({'elements': []}),
                encoding='utf-8',
            )
            (template_dir / 'pages' / 'body.page').write_text(
                yaml.safe_dump({
                    'elements': [
                        {
                            'elementId': 'title',
                            'elementType': 'text',
                            'bounds': [64, 30, 600, 40],
                            'content': {
                                'text': '<p><span style="font-family:微软雅黑;font-size:30px;color:#E50011">标题</span></p>',
                            },
                        },
                        {
                            'elementId': 'body',
                            'elementType': 'text',
                            'bounds': [64, 90, 800, 200],
                            'content': {
                                'text': '<p><span style="font-family:微软雅黑;font-size:13px;color:#000000">正文</span></p>',
                            },
                        },
                        {
                            'elementId': 'card',
                            'elementType': 'shape',
                            'shapeName': 'roundRect',
                            'bounds': [64, 300, 300, 120],
                            'fill': {'type': 'solid', 'color': '#FFFFFF'},
                            'border': {
                                'width': 1,
                                'color': '#F0B4B8',
                                'style': 'solid',
                            },
                        },
                    ]
                }),
                encoding='utf-8',
            )
            profile = template_style_profile(template_dir)
            theme = template_theme(profile)
            from agent_service.kimi_plan import write_plan_deck

            entry = write_plan_deck(
                root / 'workspace',
                {
                    'deckTitle': '正文主题',
                    'pages': [{
                        'path': 'pages/body.page',
                        'title': '正文标题',
                        'readerTask': '理解正文',
                        'content': '正文内容。',
                        'visualPlan': '卡片',
                        'sourceSections': [],
                        'sourceSubsections': [],
                    }],
                },
                {
                    'entry': str(template_dir / 'template.pptd'),
                    'pptd': str(template_dir),
                },
            )
            stored_document = yaml.safe_load(entry.read_text())
            page = build_template_body_flow(
                template_dir,
                root / 'deck',
                title='正文标题',
                subtitle='正文摘要',
                content='第一项内容。第二项内容。',
                style_profile=profile,
                layout='cards',
            )
        self.assertEqual(profile['fontFamily'], '微软雅黑')
        self.assertEqual(profile['colors']['accent'], '#E50011')
        self.assertEqual(profile['colors']['text'], '#000000')
        self.assertEqual(profile['colors']['surface'], '#FFFFFF')
        self.assertEqual(theme['textStyles']['body']['fontFamily'], '微软雅黑')
        self.assertEqual(
            stored_document['theme']['colors']['primary'],
            '#E50011',
        )
        title = next(
            item for item in page['elements']
            if item.get('elementId') == 'body-flow-title'
        )
        card = next(
            item for item in page['elements']
            if item.get('elementId') == 'body-card-1'
        )
        self.assertEqual(title['content']['fontFamily'], '微软雅黑')
        self.assertEqual(title['content']['color'], '#E50011')
        self.assertEqual(card['fill']['color'], '#FFFFFF')
        self.assertEqual(card['border']['color'], '#F0B4B8')

    def test_style_pages_are_detected_without_fixed_template_page_numbers(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            template_dir = root / 'template'
            (template_dir / 'pages').mkdir(parents=True)
            refs = [
                'pages/cover.page',
                'pages/body-a.page',
                'pages/agenda.page',
                'pages/body-b.page',
                'pages/closing.page',
            ]
            (template_dir / 'template.pptd').write_text(
                yaml.safe_dump({
                    'version': 'v2',
                    'size': [960, 540],
                    'pages': refs,
                }),
                encoding='utf-8',
            )
            texts = {
                'cover.page': '封面',
                'body-a.page': '正文甲',
                'agenda.page': '目录',
                'body-b.page': '正文乙',
                'closing.page': '感谢观看',
            }
            for name, text in texts.items():
                (template_dir / 'pages' / name).write_text(
                    yaml.safe_dump({
                        'elements': [
                            {
                                'elementId': 'title',
                                'elementType': 'text',
                                'bounds': [40, 40, 800, 40],
                                'content': {'text': text},
                            },
                            {
                                'elementId': 'body',
                                'elementType': 'text',
                                'bounds': [40, 90, 800, 200],
                                'content': {'text': text},
                            },
                        ]
                    }),
                    encoding='utf-8',
                )
            selected = _template_body_page_refs(template_dir, refs)
        self.assertEqual(
            selected,
            ['pages/body-a.page', 'pages/body-b.page'],
        )

    def test_dense_regression_input_extracts_complete_contract(self):
        value = json.loads(
            (
                ROOT
                / 'benchmarks/single-page-template/inputs.v1.json'
            ).read_text(encoding='utf-8')
        )['cases']['REG-P02']
        parsed = _single_page_dense_regression_content(
            SimpleNamespace(pageCount=1, prompt=value['prompt'])
        )
        self.assertEqual(
            parsed['title'],
            '算力成为第三大战斗力要素，国内自主可控体系加速成熟',
        )
        self.assertEqual(len(parsed['stages']), 3)
        self.assertEqual(len(parsed['metrics']), 4)
        self.assertEqual(len(parsed['tableRows']), 3)
        self.assertEqual(
            parsed['tableColumns'],
            ['对照维度', '国外', '国内'],
        )
        text = '\n'.join([
            parsed['title'],
            parsed['subtitle'],
            *parsed['stages'],
            parsed['coreJudgment'],
            parsed['metricsTitle'],
            *[
                f"{item['value']} {item['label']}"
                for item in parsed['metrics']
            ],
            *parsed['tableColumns'],
            *[cell for row in parsed['tableRows'] for cell in row],
        ])
        for required in value['requiredText']:
            self.assertIn(required, text)

    def test_dense_regression_builder_has_twenty_four_elements(self):
        metrics = [
            {'value': '超300万', 'label': 'GenAI.mil服务军事人员'},
            {'value': '6.9%', 'label': '2026年全国国防支出预算增幅'},
            {'value': '1.94万亿元', 'label': '2026年国防支出预算'},
            {'value': '744TOPS', 'label': '三体计算星座单星最高算力'},
        ]
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            template_dir = root / 'template'
            template_dir.mkdir()
            (template_dir / 'template.pptd').write_text(
                yaml.safe_dump({
                    'version': 'v2',
                    'size': [960, 540],
                    'theme': {},
                    'pages': [],
                }, allow_unicode=True),
                encoding='utf-8',
            )
            page = build_template_dense_regression(
                template_dir,
                root / 'deck',
                title='算力成为第三大战斗力要素，国内自主可控体系加速成熟',
                subtitle='全球军事竞争转向决策优势，在轨智能计算迈入实质性验证与初步组网阶段',
                stages=[
                    '机械化｜平台性能决定优势',
                    '信息化｜信息网络成为关键',
                    '智能化｜算力与火力、机动性并列，成为第三大战斗力要素',
                ],
                core_judgment='核心判断内容完整保留。',
                metrics_title='关键指标与在轨智能计算项目对照',
                metrics=metrics,
                table_columns=['对照维度', '国外', '国内'],
                table_rows=[
                    ['代表项目', '国外项目', '国内项目'],
                    ['典型能力', '国外能力', '国内能力'],
                    ['路线与阶段', '国外路线', '国内路线'],
                ],
            )
        self.assertEqual(len(page['elements']), 24)
        metric_cards = [
            item for item in page['elements']
            if item.get('elementId', '').startswith('dense-metric-card-')
        ]
        self.assertEqual(len(metric_cards), 4)
        self.assertEqual(
            len({round(item['bounds'][2], 4) for item in metric_cards}),
            1,
        )
        table = next(
            item for item in page['elements']
            if item.get('elementType') == 'table'
        )
        self.assertEqual(len(table['rows']), 4)
        self.assertEqual(len(table['columnWidths']), 3)
        self.assertLessEqual(
            table['bounds'][1] + table['bounds'][3],
            516,
        )

    def test_template_closing_reuses_last_page_and_clears_old_text(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            template_dir = root / 'template'
            (template_dir / 'pages').mkdir(parents=True)
            (template_dir / 'template.pptd').write_text(
                yaml.safe_dump({
                    'version': 'v2',
                    'size': [960, 540],
                    'theme': {},
                    'pages': ['pages/cover.page', 'pages/closing.page'],
                }, allow_unicode=True),
                encoding='utf-8',
            )
            for path, text in (
                ('cover.page', '旧封面'),
                ('closing.page', '旧感谢页'),
            ):
                (template_dir / 'pages' / path).write_text(
                    yaml.safe_dump({
                        'elements': [
                            {
                                'elementId': 'main',
                                'elementType': 'text',
                                'bounds': [0, 120, 960, 240],
                                'content': {
                                    'text': f'<p><span style="font-family:MiSans;font-size:100px;color:#FFFFFF">{text}</span></p>',
                                },
                            },
                            {
                                'elementId': 'secondary',
                                'elementType': 'text',
                                'bounds': [0, 400, 960, 40],
                                'content': {
                                    'text': '<p><span style="font-family:MiSans;font-size:18px;color:#FFFFFF">旧附注</span></p>',
                                },
                            },
                        ],
                    }, allow_unicode=True),
                    encoding='utf-8',
                )
            page = build_template_closing(
                template_dir,
                root / 'deck',
                text='感谢观看',
            )
        text = document_text({'pages': [{'data': page}]})
        self.assertEqual(text.count('感谢观看'), 1)
        self.assertNotIn('旧感谢页', text)
        self.assertNotIn('旧附注', text)
        main = next(
            item for item in page['elements']
            if item.get('elementId') == 'main'
        )
        self.assertEqual(main['bounds'][0], 0.0)
        self.assertEqual(main['bounds'][2], 960.0)
        self.assertGreaterEqual(main['bounds'][3], 145.0)

    def test_single_page_verdict_accepts_harmless_empty_field_omissions(self):
        requested = [{
            'pagePath': 'pages/cover.page',
            'pageIndex': 0,
            'elements': [],
        }]
        verdict = _validate_verdict(
            {
                'status': 'passed',
                'observations': [{
                    'pagePath': 'pages/cover.page',
                    'evidence': '清晰',
                }],
            },
            requested,
            requested,
        )
        self.assertEqual(verdict['coveredPages'], ['pages/cover.page'])
        self.assertEqual(verdict['issues'], [])
        self.assertEqual(verdict['deckFindingDecisions'], [])

    def test_contract_review_does_not_require_model_observations(self):
        requested = [{
            'pagePath': 'pages/cards.page',
            'pageIndex': 0,
            'elements': [],
        }]
        verdict = _validate_verdict(
            {
                'status': 'passed',
                'coveredPages': ['pages/cards.page'],
                'issues': [],
                'deckFindingDecisions': [],
            },
            requested,
            requested,
            require_observations=False,
        )
        self.assertEqual(verdict['status'], 'passed')
        self.assertEqual(verdict['observations'], [])

    def test_contract_prompt_excludes_observations(self):
        prompt = _single_page_contract_system(['三张卡片等宽。'])
        self.assertNotIn('"observations"', prompt)
        self.assertIn('"issues"', prompt)


class BenchmarkTimingTests(unittest.TestCase):
    def test_trace_is_enabled_for_web_jobs_by_default(self):
        with TemporaryDirectory() as temporary, patch.dict(
            os.environ,
            {},
            clear=True,
        ):
            trace = BenchmarkTrace(Path(temporary), 'web-job')
            with trace.span('planning'):
                pass
            path = (
                Path(temporary)
                / 'benchmark-traces'
                / 'web-job.json'
            )
            self.assertTrue(path.is_file())

    def test_trace_records_partial_result_without_generation_side_effects(self):
        with TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {'PPT_BENCHMARK_TRACE': '1'}):
                trace = BenchmarkTrace(Path(temporary), 'job-a')
                with trace.span('pageModel', pageIndex=0):
                    pass
                trace.record_issues(
                    'pageGate',
                    [{'code': 'text-overflow', 'elementId': 'body'}],
                    pagePath='pages/a.page',
                )
                trace.finish('completed')
            value = json.loads(
                (
                    Path(temporary)
                    / 'benchmark-traces'
                    / 'job-a.json'
                ).read_text(encoding='utf-8')
            )
        self.assertEqual(value['status'], 'completed')
        self.assertEqual(value['spans'][0]['stage'], 'pageModel')
        self.assertEqual(value['issueSets'][0]['issueCount'], 1)

    def test_trace_instances_merge_spans_without_overwriting_each_other(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict(os.environ, {'PPT_BENCHMARK_TRACE': '1'}):
                first = BenchmarkTrace(root, 'job-a')
                second = BenchmarkTrace(root, 'job-a')
                with first.span('templateLoad'):
                    pass
                with second.span('browserRender'):
                    pass
                first.finish('completed')
            value = json.loads(
                (
                    root
                    / 'benchmark-traces'
                    / 'job-a.json'
                ).read_text(encoding='utf-8')
            )
        self.assertEqual(
            {item['stage'] for item in value['spans']},
            {'templateLoad', 'browserRender'},
        )
        self.assertEqual(value['status'], 'completed')

    def test_template_cover_transform_preserves_prebuilt_unit_slot(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            template_dir = root / 'template-pptd'
            (template_dir / 'pages').mkdir(parents=True)
            (template_dir / 'template.pptd').write_text(
                yaml.safe_dump({
                    'version': 'v2',
                    'size': [960, 540],
                    'pages': ['pages/cover.page', 'pages/closing.page'],
                }),
                encoding='utf-8',
            )
            cover_elements = [
                {
                    'elementId': f'cover-{index}',
                    'elementType': 'text',
                    'bounds': [0, 100 + index * 80, 960, 50],
                    'content': {
                        'text': f'<p><span style="font-size:{40-index * 8}px">旧文本{index}</span></p>',
                    },
                }
                for index in range(3)
            ]
            (template_dir / 'pages' / 'cover.page').write_text(
                yaml.safe_dump({'elements': cover_elements}),
                encoding='utf-8',
            )
            (template_dir / 'pages' / 'closing.page').write_text(
                yaml.safe_dump({
                    'elements': [{
                        'elementId': 'closing-1',
                        'elementType': 'text',
                        'bounds': [0, 200, 960, 100],
                        'content': {'text': '感谢观看'},
                    }]
                }),
                encoding='utf-8',
            )
            page = json.loads(json.dumps(cover_elements))
            page.append({
                'elementId': 'cover-unit',
                'elementType': 'text',
                'bounds': [0, 360, 960, 40],
                'content': {'text': '长沙湘计海盾科技有限公司'},
            })
            pages = [{'path': 'pages/cover.page', 'data': {'elements': page}}]
            document = {'size': [960, 540], 'pages': ['pages/cover.page']}
            _apply_template_cover_closing(
                template_dir,
                root / 'deck',
                document,
                pages,
            )
            ids = {
                element['elementId']
                for element in pages[0]['data']['elements']
            }
        self.assertIn('cover-unit', ids)

    def test_typed_single_content_page_is_not_replaced_by_cover_or_closing(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            template_dir = root / 'template-pptd'
            (template_dir / 'pages').mkdir(parents=True)
            (template_dir / 'template.pptd').write_text(
                yaml.safe_dump({
                    'version': 'v2',
                    'size': [960, 540],
                    'pages': ['pages/cover.page', 'pages/closing.page'],
                }),
                encoding='utf-8',
            )
            for path, text in (
                ('cover.page', '模板封面'),
                ('closing.page', '模板结束页'),
            ):
                (template_dir / 'pages' / path).write_text(
                    yaml.safe_dump({
                        'elements': [{
                            'elementId': 'template-text',
                            'elementType': 'text',
                            'bounds': [0, 120, 960, 120],
                            'content': {'text': text},
                        }]
                    }),
                    encoding='utf-8',
                )
            content = {
                'meta': {'kind': 'content'},
                'elements': [{
                    'elementId': 'content-title',
                    'elementType': 'text',
                    'bounds': [40, 40, 800, 60],
                    'content': {'text': '研发进展'},
                }],
            }
            pages = [{'path': 'pages/content.page', 'data': content}]
            document = {'size': [960, 540], 'pages': ['pages/content.page']}
            _apply_template_cover_closing(
                template_dir,
                root / 'deck',
                document,
                pages,
            )
        self.assertEqual(len(pages), 1)
        self.assertEqual(
            pages[0]['data']['elements'][0]['elementId'],
            'content-title',
        )
        self.assertEqual(document['pages'], ['pages/content.page'])


class BenchmarkCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_review_cache_can_be_disabled_without_disabling_review(self):
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / 'slide-1.png').write_bytes(b'image')
            state = {
                'document': {
                    'version': 'v2',
                    'size': [960, 540],
                    'pages': ['pages/a.page'],
                },
                'pages': [
                    {
                        'path': 'pages/a.page',
                        'data': {'elements': []},
                    }
                ],
            }
            report = {
                'status': 'passed',
                'coveredPages': ['pages/a.page'],
                'observations': [],
                'issues': [],
                'model': 'vision',
            }
            config = SimpleNamespace(
                provider='openai',
                model='vision',
                base_url='http://vision',
            )
            with patch.dict(os.environ, {'PPT_REVIEW_CACHE': '0'}), patch.object(
                review_service.visual_review,
                'vision_configuration',
                return_value=config,
            ), patch.object(
                review_service.visual_review,
                'review_page',
                AsyncMock(return_value=report),
            ) as review:
                await review_service.review_page(
                    state,
                    folder,
                    'pages/a.page',
                    cache_dir=folder / 'cache',
                )
                await review_service.review_page(
                    state,
                    folder,
                    'pages/a.page',
                    cache_dir=folder / 'cache',
                )
            self.assertEqual(review.await_count, 2)
            self.assertFalse((folder / 'cache').exists())

    async def test_single_page_deck_review_uses_only_one_high_resolution_pass(self):
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            Image.new('RGB', (960, 540), 'white').save(
                folder / 'slide-1.png'
            )
            state = {
                'revision': 1,
                'document': {
                    'version': 'v2',
                    'size': [960, 540],
                    'pages': ['pages/closing.page'],
                },
                'pages': [{
                    'path': 'pages/closing.page',
                    'data': {'elements': []},
                }],
            }
            report = {
                'status': 'passed',
                'coveredPages': ['pages/closing.page'],
                'observations': [{
                    'pagePath': 'pages/closing.page',
                    'evidence': '结束页背景和文字实际可见',
                }],
                'issues': [],
                'deckFindingDecisions': [],
                'model': 'vision',
                'provider': 'openai',
                'capability': {'status': 'passed'},
                'images': [],
                'requests': 2,
            }
            config = SimpleNamespace(
                provider='openai',
                model='vision',
                base_url='http://vision',
            )
            with patch.object(
                visual_review,
                'vision_configuration',
                return_value=config,
            ), patch.object(
                visual_review,
                'review_page',
                AsyncMock(return_value=report),
            ) as review:
                result = await visual_review.review_deck(
                    state,
                    folder,
                )
            review.assert_awaited_once()
            self.assertEqual(result['status'], 'passed')
            self.assertEqual(result['requests'], 2)
