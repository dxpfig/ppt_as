import unittest

from agent_service.pptd_validation import normalize_table_tracks


class TableTrackNormalizationTests(unittest.TestCase):
    def test_row_count_mismatch_rebuilds_even_tracks(self):
        page = {
            "elements": [{
                "elementType": "table",
                "rows": [[{"text": str(index)}] for index in range(7)],
                "rowHeights": [0.125] * 8,
                "columnWidths": [0.5, 0.5],
            }]
        }
        self.assertTrue(normalize_table_tracks(page))
        table = page["elements"][0]
        self.assertEqual(len(table["rowHeights"]), 7)
        self.assertAlmostEqual(sum(table["rowHeights"]), 1)
        self.assertTrue(all(abs(value - 1 / 7) < 1e-9 for value in table["rowHeights"]))

    def test_partial_sum_preserves_relative_row_proportions(self):
        page = {
            "elements": [{
                "elementType": "table",
                "rows": [[{"text": str(index)}] for index in range(4)],
                "rowHeights": [0.1, 0.2, 0.1, 0.2],
                "columnWidths": [2, 1, 1],
            }]
        }
        normalize_table_tracks(page)
        table = page["elements"][0]
        self.assertEqual(table["rowHeights"], [1 / 6, 2 / 6, 1 / 6, 2 / 6])
        self.assertEqual(table["columnWidths"], [0.5, 0.25, 0.25])
        self.assertEqual(sum(table["rowHeights"]), 1)
        self.assertEqual(sum(table["columnWidths"]), 1)

    def test_valid_tracks_are_left_unchanged(self):
        heights = [0.25, 0.25, 0.25, 0.25]
        widths = [0.2, 0.3, 0.5]
        page = {
            "elements": [{
                "elementType": "table",
                "rows": [[{"text": str(index)}] for index in range(4)],
                "rowHeights": heights,
                "columnWidths": widths,
            }]
        }
        self.assertFalse(normalize_table_tracks(page))
        self.assertEqual(page["elements"][0]["rowHeights"], heights)
        self.assertEqual(page["elements"][0]["columnWidths"], widths)

    def test_scalar_cells_are_wrapped_without_changing_grid(self):
        page = {
            "elements": [{
                "elementType": "table",
                "rows": [["产品谱系", "核心型号", "对应芯片"]],
                "rowHeights": [1],
                "columnWidths": [0.5, 0.25, 0.25],
            }]
        }
        self.assertTrue(normalize_table_tracks(page))
        self.assertEqual(
            page["elements"][0]["rows"],
            [[{"text": "产品谱系"}, {"text": "核心型号"}, {"text": "对应芯片"}]],
        )

    def test_cells_wrapper_is_unwrapped_without_changing_content(self):
        page = {
            "elements": [{
                "elementType": "table",
                "rows": [
                    {"cells": ["驱动因素", "测算依据", "需求拉动"]},
                    {"cells": ["预算", "1.94 万亿元", "采购增加"]},
                ],
                "rowHeights": [0.4, 0.6],
                "columnWidths": [0.3, 0.4, 0.3],
            }]
        }
        self.assertTrue(normalize_table_tracks(page))
        self.assertEqual(
            page["elements"][0]["rows"],
            [
                [{"text": "驱动因素"}, {"text": "测算依据"}, {"text": "需求拉动"}],
                [{"text": "预算"}, {"text": "1.94 万亿元"}, {"text": "采购增加"}],
            ],
        )

    def test_split_horizontal_header_is_collapsed(self):
        page = {
            "elements": [{
                "elementType": "table",
                "rows": [
                    [{"text": "主体"}],
                    [{"text": "定位"}],
                    [{"text": "业务"}],
                    [{"text": "协同"}],
                    [{"text": "中国电子"}, {"text": "统筹"}, {"text": "规划"}, {"text": "考核"}],
                ],
                "rowHeights": [1],
                "columnWidths": [0.25, 0.25, 0.25, 0.25],
            }]
        }
        self.assertTrue(normalize_table_tracks(page))
        table = page["elements"][0]
        self.assertEqual(len(table["rows"]), 2)
        self.assertEqual(
            [cell["text"] for cell in table["rows"][0]],
            ["主体", "定位", "业务", "协同"],
        )
        self.assertEqual(table["rowHeights"], [0.5, 0.5])


if __name__ == "__main__":
    unittest.main()
