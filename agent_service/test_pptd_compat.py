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
