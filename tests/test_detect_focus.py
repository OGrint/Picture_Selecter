"""Tests for focus-point helpers and detection suggestions (no model needed)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reviewer_detect as rd  # noqa: E402
import reviewer_imaging as ri  # noqa: E402


class FocusTests(unittest.TestCase):
    def test_153_point_names(self):
        # ExifTool numbering: 1 = E9 (centre), runs E,D,C,B,A,F,G,H,I per column
        self.assertEqual(ri.af153_point(1)[0], "E9")
        self.assertEqual(ri.af153_point(5)[0], "A9")
        self.assertEqual(ri.af153_point(48)[0], "C12")
        self.assertEqual(ri.af153_point(149)[0], "A1")
        self.assertIsNone(ri.af153_point(0))

    def test_orientation(self):
        box = (0.1, 0.2, 0.3, 0.4)
        self.assertEqual(ri._orient_box(box, 1), box)
        self.assertEqual(ri._orient_box(box, 6), (0.6, 0.1, 0.8, 0.3))
        self.assertEqual(ri._orient_box(box, 8), (0.2, 0.7, 0.4, 0.9))

    def test_non_nikon_file_has_no_focus(self):
        import tempfile
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.jpg"
            Image.new("RGB", (64, 48)).save(p)
            self.assertIsNone(ri.read_focus_area(str(p)))


class SuggestionTests(unittest.TestCase):
    ANIMAL = [0, 0.9, 0.3, 0.3, 0.6, 0.7]

    def test_not_analysed_gives_nothing(self):
        self.assertEqual(rd.suggestions(None), [])

    def test_empty_frame(self):
        self.assertEqual(rd.suggestions([]), ["empty"])
        self.assertEqual(rd.suggestions([[0, 0.1, 0.3, 0.3, 0.6, 0.7]]), ["empty"])  # low confidence

    def test_focus_missed_animal(self):
        self.assertIn("missed", rd.suggestions([self.ANIMAL], (0.8, 0.1, 0.85, 0.15, "A15")))
        self.assertNotIn("missed", rd.suggestions([self.ANIMAL], (0.4, 0.4, 0.45, 0.45, "E9")))

    def test_edge_small_person(self):
        self.assertIn("edge", rd.suggestions([[0, 0.9, 0.0, 0.3, 0.4, 0.6]]))
        self.assertIn("small", rd.suggestions([[0, 0.9, 0.5, 0.5, 0.55, 0.55]]))
        self.assertIn("person", rd.suggestions([self.ANIMAL, [1, 0.8, 0.1, 0.1, 0.2, 0.4]]))

    def test_clean_animal_frame(self):
        self.assertEqual(rd.suggestions([self.ANIMAL], (0.4, 0.4, 0.45, 0.45, "E9")), [])


if __name__ == "__main__":
    unittest.main()
