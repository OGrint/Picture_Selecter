"""Tests for the eye check: sharpness measure, burst comparison, cache, measure()."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reviewer_eyes as re_  # noqa: E402


def eye_img(blur=0.0, size=120):
    im = Image.new("L", (size, size), 200)
    d = ImageDraw.Draw(im)
    d.ellipse((30, 35, 90, 85), fill=20)
    d.ellipse((50, 45, 58, 53), fill=255)            # catchlight
    rng = np.random.default_rng(1)
    a = np.asarray(im, np.float32) + rng.normal(0, 3, (size, size))
    im = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
    return im.filter(ImageFilter.GaussianBlur(blur)) if blur else im


class SharpnessTests(unittest.TestCase):
    def test_blur_lowers_sharpness(self):
        s = [re_.eye_sharpness(np.asarray(eye_img(b))) for b in (0, 1.5, 3)]
        self.assertGreater(s[0], s[1])
        self.assertGreater(s[1], s[2])

    def test_contrast_does_not_matter(self):
        a = np.asarray(eye_img(1.0), np.float32)
        low = 100 + (a - 100) * 0.4                    # same edges, less contrast
        self.assertAlmostEqual(re_.eye_sharpness(a), re_.eye_sharpness(low), delta=0.03)

    def test_tiny_patch(self):
        self.assertEqual(re_.eye_sharpness(np.zeros((5, 5))), 0.0)


class BurstNoteTests(unittest.TestCase):
    def test_notes(self):
        g = ["a", "b", "c", "d", "e"]
        eyes = {"a": {"p": .6, "s": .47}, "b": {"p": .6, "s": .42}, "c": {"p": .6, "s": .46},
                "d": {"p": .1}, "e": {}}
        self.assertEqual(re_.burst_eye_note("a", g, eyes), "best")
        self.assertEqual(re_.burst_eye_note("b", g, eyes), "softer")
        self.assertEqual(re_.burst_eye_note("c", g, eyes), "tie")
        self.assertEqual(re_.burst_eye_note("d", g, eyes), "hidden")
        self.assertIsNone(re_.burst_eye_note("e", g, eyes))          # animal too small
        self.assertIsNone(re_.burst_eye_note("x", g, eyes))          # not checked
        self.assertIsNone(re_.burst_eye_note("a", ["a"], eyes))       # nothing to compare


class CacheTests(unittest.TestCase):
    def test_round_trip_and_stale(self):
        with tempfile.TemporaryDirectory() as d:
            f1, f2 = os.path.join(d, "1.JPG"), os.path.join(d, "2.JPG")
            for f in (f1, f2):
                Image.new("RGB", (8, 8)).save(f)
            re_.save_eyes(d, {f1: {"p": .7, "s": .5, "b": [0, 0, .1, .1]}, f2: {}})
            got = re_.load_eyes(d, {f1, f2})
            self.assertEqual(got[f1]["s"], .5)
            self.assertEqual(got[f2], {})
            os.utime(f1, (1, 1))                                    # file changed since
            self.assertNotIn(f1, re_.load_eyes(d, {f1, f2}))


class FakeFinder:
    def __init__(self, conf, box):
        self.conf, self.box, self.seen = conf, box, None

    def find(self, crop):
        self.seen = crop.size
        return self.conf, self.box


class MeasureTests(unittest.TestCase):
    def test_box_mapped_to_whole_frame(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "x.jpg")
            Image.new("RGB", (1000, 800), "grey").save(f)
            ff = FakeFinder(0.6, (40, 40, 60, 60))
            got = re_.measure(f, (0.5, 0.5, 0.9, 0.9), ff)
            # crop starts at the box minus an 8 % margin: x 0.468 → 468 px
            self.assertAlmostEqual(got["b"][0], (468 + 40) / 1000, places=2)
            self.assertIn("s", got)

    def test_hidden_eye_and_tiny_animal(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "x.jpg")
            Image.new("RGB", (1000, 800), "grey").save(f)
            got = re_.measure(f, (0.1, 0.1, 0.5, 0.5), FakeFinder(0.1, (0, 0, 5, 5)))
            self.assertEqual(set(got), {"p"})
            self.assertIsNone(re_.measure(f, (0.1, 0.1, 0.12, 0.12), FakeFinder(0.9, (0, 0, 5, 5))))


if __name__ == "__main__":
    unittest.main()
