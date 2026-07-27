import unittest

from region_geometry import (
    normalize_selection_rect,
    rect_to_capture_bbox,
    rect_to_capture_region,
)


class SelectionGeometryTests(unittest.TestCase):
    def test_normalizes_drag_in_any_direction(self):
        self.assertEqual(normalize_selection_rect(100, 100, 20, 40), (20, 40, 80, 60))
        self.assertEqual(normalize_selection_rect(20, 40, 100, 100), (20, 40, 80, 60))
        self.assertEqual(normalize_selection_rect(100, 20, 40, 100), (40, 20, 60, 80))

    def test_supports_negative_global_coordinates(self):
        self.assertEqual(normalize_selection_rect(-300, 50, -101, 251), (-300, 50, 198, 200))

    def test_supports_virtual_screen_left_edge_coordinates(self):
        self.assertEqual(
            normalize_selection_rect(-1920, 0, -1819, 1081),
            (-1920, 0, 100, 1080),
        )

    def test_reduces_odd_dimensions_and_rejects_zero(self):
        self.assertEqual(normalize_selection_rect(0, 0, 101, 51), (0, 0, 100, 50))
        self.assertIsNone(normalize_selection_rect(0, 0, 1, 2))
        self.assertIsNone(normalize_selection_rect(0, 0, 2, 1))

    def test_builds_backend_specific_capture_boxes(self):
        rect = (-300, 50, 198, 200)
        self.assertEqual(rect_to_capture_bbox(rect), (-300, 50, -102, 250))
        self.assertEqual(rect_to_capture_region(rect), rect)


if __name__ == "__main__":
    unittest.main()
