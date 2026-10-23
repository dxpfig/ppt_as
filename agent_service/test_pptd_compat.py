from __future__ import annotations

import unittest

from agent_service.pptd_compat import (
    normalize_custom_fonts,
    normalize_pages,
    normalize_text_markup,
)


class PptdCompatTests(unittest.TestCase):
    def test_br_becomes_paragraph_with_same_style(self):
        markup = '<p style="text-align:center;line-height:1.4">上<br/>下</p>'
        self.assertEqual(
            normalize_text_markup(markup),
            '<p style="text-align:center;line-height:1.4">上</p>'
            '<p style="text-align:center;line-height:1.4">下</p>',
        )

    def test_opacity_is_removed_for_native_export(self):
        pages = [{"data": {"elements": [
            {"elementId": "rule", "elementType": "shape", "opacity": 0.3},
            {"elementId": "title", "elementType": "text", "content": {"text": "正常"}},
        ]}}]
        changed = normalize_pages(pages)
        self.assertEqual(changed["opacity"], 1)
        self.assertNotIn("opacity", pages[0]["data"]["elements"][0])

    def test_table_cell_br_is_normalized(self):
        pages = [{"data": {"elements": [{
            "elementId": "table", "elementType": "table",
            "rows": [[{"text": "甲<br>乙"}]],
        }]}}]
        changed = normalize_pages(pages)
        self.assertEqual(changed["table"], 1)
        self.assertIn("<p>甲</p><p>乙</p>", pages[0]["data"]["elements"][0]["rows"][0][0]["text"])

    def test_latin_only_font_gets_an_east_asian_route_for_cjk_text(self):
        pages = [{"data": {"elements": [{
            "elementId": "metric", "elementType": "text",
            "content": {"fontFamily": "Arial", "text": "<p><span>1.94 万亿元</span></p>"},
        }]}}]
        changed = normalize_pages(pages)
        self.assertEqual(changed["font"], 1)
        self.assertEqual(
            pages[0]["data"]["elements"][0]["content"]["fontFamily"],
            {"latin": "Arial", "ea": "微软雅黑"},
        )

    def test_inline_latin_font_on_cjk_span_uses_east_asian_fallback(self):
        pages = [{"data": {"elements": [{
            "elementId": "label", "elementType": "text",
            "content": {"text": '<p><span style="font-family:Arial">中文标签</span></p>'},
        }]}}]
        changed = normalize_pages(pages)
        self.assertEqual(changed["font"], 1)
        self.assertIn('font-family:微软雅黑', pages[0]["data"]["elements"][0]["content"]["text"])
        self.assertNotIn('font-family:Arial', pages[0]["data"]["elements"][0]["content"]["text"])

    def test_common_source_han_serif_aliases_resolve_to_installed_family(self):
        pages = [{"data": {"elements": [
            {
                "elementId": "title",
                "elementType": "text",
                "content": {
                    "fontFamily": "思源宋体 CN VF",
                    "text": "管理汇报",
                },
            },
            {
                "elementId": "label",
                "elementType": "text",
                "content": {
                    "fontFamily": "Noto Serif SC",
                    "text": "Management",
                },
            },
            {
                "elementId": "bad-model-font",
                "elementType": "text",
                "content": {
                    "fontFamily": "Liter",
                    "text": "中文与 English",
                },
            },
        ]}}]
        changed = normalize_pages(pages)
        self.assertEqual(changed["font"], 3)
        self.assertEqual(
            pages[0]["data"]["elements"][0]["content"]["fontFamily"],
            "Noto Serif CJK SC",
        )
        self.assertEqual(
            pages[0]["data"]["elements"][1]["content"]["fontFamily"],
            "Noto Serif CJK SC",
        )
        self.assertEqual(
            pages[0]["data"]["elements"][2]["content"]["fontFamily"],
            "MiSans",
        )

    def test_internal_pending_fields_are_removed_before_native_export(self):
        pages = [{
            'data': {
                '_pendingElements': [{'elementId': 'future'}],
                'elements': [{
                    'elementId': 'title',
                    'elementType': 'text',
                    '_pendingLayout': [1, 2, 3, 4],
                    'content': {'text': '标题'},
                }],
            }
        }]
        changed = normalize_pages(pages)
        self.assertEqual(changed['native'], 2)
        self.assertNotIn('_pendingElements', pages[0]['data'])
        self.assertNotIn(
            '_pendingLayout',
            pages[0]['data']['elements'][0],
        )

    def test_only_referenced_custom_fonts_are_kept(self):
        document = {
            'customFonts': [
                {'family': 'MiSans', 'src': 'https://example.test/misans.ttf'},
                {'family': '思源宋体', 'src': 'https://example.test/source-serif.ttf'},
            ]
        }
        pages = [
            {
                'data': {
                    'elements': [
                        {
                            'elementId': 'title',
                            'elementType': 'text',
                            'content': {'fontFamily': 'MiSans', 'text': '标题'},
                        }
                    ]
                }
            }
        ]
        self.assertEqual(normalize_custom_fonts(document, pages), 1)
        self.assertEqual([item['family'] for item in document['customFonts']], ['MiSans'])

    def test_theme_font_aliases_are_canonicalized_with_document_cleanup(self):
        document = {
            'theme': {
                'textStyles': {
                    'quote': {'fontFamily': '思源宋体'},
                }
            }
        }
        self.assertEqual(normalize_custom_fonts(document, []), 1)
        self.assertEqual(
            document['theme']['textStyles']['quote']['fontFamily'],
            'Noto Serif CJK SC',
        )

    def test_unsupported_line_arrow_is_removed_without_touching_geometry(self):
        pages = [{"data": {"elements": [{
            "elementId": "route",
            "elementType": "line",
            "bounds": [10, 20, 100, 2],
            "points": "0,1 100,1",
            "arrow": [None, "stealth"],
        }]}}]
        changed = normalize_pages(pages)
        element = pages[0]["data"]["elements"][0]
        self.assertEqual(changed["native"], 1)
        self.assertNotIn("arrow", element)
        self.assertEqual(element["bounds"], [10, 20, 100, 2])


if __name__ == "__main__":
    unittest.main()
