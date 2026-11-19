"""Tests for raw highlight clipping (saturation detection, scoring, sidebar text)."""

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reviewer_imaging as ri  # noqa: E402


def plane(top_count, top=16188, seed=0):
    """A green plane like a D850's: a smooth tail plus top_count clipped photosites."""
    rng = np.random.default_rng(seed)
    v = np.clip(rng.normal(9000, 2500, 400_000), 400, top - 5).astype(np.uint16)
    v[:top_count] = top
    return v


class SaturationTests(unittest.TestCase):
    def test_clipped_plane_found_below_reported_white(self):
        # green clips at 16188 although the white level says 16383
        self.assertEqual(ri._saturation(plane(5000), 400, 16383), 16188)

    def test_unclipped_tail_is_not_clipping(self):
        v = plane(0)
        v[0] = 16203                      # one nearly-full photosite
        self.assertIsNone(ri._saturation(v, 400, 16383))

    def test_dark_frame_never_clipped(self):
        v = np.full(10_000, 3000, np.uint16)          # a big flat area, but dark
        self.assertIsNone(ri._saturation(v, 400, 16383))

    def test_orientation(self):
        m = np.zeros((2, 3), np.uint8)
        m[0, 2] = 1                                   # sensor top-right
        self.assertEqual(ri._oriented(m, 6).shape, (3, 2))
        self.assertEqual(ri._oriented(m, 6)[2, 1], 1)  # camera turned: bottom-right
        self.assertEqual(ri._oriented(m, 3)[1, 0], 1)


class BackgroundTests(unittest.TestCase):
    def test_sky_through_the_box_is_background_blob_inside_is_not(self):
        m = np.zeros((200, 300), bool)
        m[:60, :200] = True               # blown sky crossing into the box's top-left
        m[120:140, 200:220] = True        # blown patch on the bird, inside the box
        rs, cs = slice(40, 180), slice(150, 280)
        bg = ri._background(m, rs, cs)
        self.assertTrue(bg[50, 170])      # sky inside the box
        self.assertFalse(bg[130, 210].any())


class ScoreTests(unittest.TestCase):
    def test_exposure_uses_raw_and_keeps_jpeg_value(self):
        s = {"exposure": 60, "dark": 0.0, "sharpness": 80}
        ri.apply_raw_clipping(s, {"f": 0.0, "s": 0.0, "ch": "", "hr": 1.2})
        self.assertEqual((s["exposure"], s["exposure_jpeg"]), (100, 60))
        s2 = {"exposure": 100, "dark": 0.0}
        ri.apply_raw_clipping(s2, {"f": 0.02, "s": 0.10, "ch": "G", "hr": 0.0})
        self.assertLess(s2["exposure"], 60)            # blown animal costs more than sky

    def test_unreadable_raw_recorded(self):
        s = ri.apply_raw_clipping({"exposure": 70}, None)
        self.assertEqual((s["raw"], s["exposure"]), ({}, 70))

    def test_text(self):
        self.assertIn("2.5% of the animal blown (green)",
                      ri.raw_clip_text({"f": 0.1, "s": 0.025, "ch": "G", "hr": 0}))
        self.assertIn("1.3 stops headroom",
                      ri.raw_clip_text({"f": 0, "s": 0, "ch": "", "hr": 1.3}))
        self.assertIn("frame blown (all channels)",
                      ri.raw_clip_text({"f": 0.05, "s": None, "ch": "RGB", "hr": 0}))
        self.assertEqual(ri.raw_clip_text({}), "")

    def test_non_raw_file(self):
        self.assertIsNone(ri.raw_clipping(__file__))


if __name__ == "__main__":
    unittest.main()
