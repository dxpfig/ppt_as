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


if __name__ == "__main__":
    unittest.main()
