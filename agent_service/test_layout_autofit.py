from __future__ import annotations

import unittest

from agent_service.layout_autofit import autofit_state, scale_element


class LayoutAutofitTests(unittest.TestCase):
    def test_text_scaling_keeps_content_and_reduces_typography(self):
        element = {
            "elementId": "body",
            "elementType": "text",
            "content": {
                "lineHeight": 1.5,
                "text": '<p style="text-align:left;line-height:1.5;margin-top:8px">'
                        '<strong><span style="font-size:12.5px;color:#333333">保留原文</span></strong></p>',
            },
        }
        self.assertTrue(scale_element(element, 0.8))
        markup = element["content"]["text"]
        self.assertIn("保留原文", markup)
        self.assertIn("font-size:10px", markup)
        self.assertIn("line-height:1.2", markup)
        self.assertIn("margin-top:4px", markup)
        self.assertEqual(element["content"]["lineHeight"], 1.2)

    def test_table_scaling_updates_style_and_every_cell(self):
        element = {
            "elementId": "table",
            "elementType": "table",
            "style": {
                "cellStyle": {"fontSize": 12, "lineHeight": 1.3},
                "bodyStyles": [{"fontSize": 12}],
            },
            "rows": [[{"text": '<p><span style="font-size:12.5px">甲</span></p>'},
                      {"text": '<p><span style="font-size:12.5px">乙</span></p>'}]],
        }
        self.assertTrue(scale_element(element, 0.8))
        self.assertEqual(element["style"]["cellStyle"]["fontSize"], 9.6)
        self.assertEqual(element["style"]["bodyStyles"][0]["fontSize"], 9.6)
        self.assertIn("font-size:10px", element["rows"][0][0]["text"])
        self.assertIn("乙", element["rows"][0][1]["text"])

    def test_autofit_uses_measured_height_ratio(self):
        state = {"pages": [{"path": "pages/a.page", "data": {"elements": [
            {"elementId": "body", "elementType": "text", "content": {
                "text": '<p><span style="font-size:12px">正文</span></p>',
            }},
            {"elementId": "other", "elementType": "text", "content": {
                "text": '<p><span style="font-size:12px">不应变化</span></p>',
            }},
        ]}}]}
        layouts = {"pages/a.page": {"elements": [
            {"elementId": "body", "availableSize": [100, 50], "measuredTextSize": [100, 100]},
        ]}}
        issues = [{"page": "pages/a.page", "elementId": "body", "code": "text-overflow"}]
        changed = autofit_state(state, layouts, issues, min_size=9)
        self.assertEqual(changed["text"], 1)
        elements = state["pages"][0]["data"]["elements"]
        self.assertIn("font-size:9", elements[0]["content"]["text"])
        self.assertIn("font-size:12px", elements[1]["content"]["text"])


if __name__ == "__main__":
    unittest.main()
