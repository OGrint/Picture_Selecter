"""Tests for species-suggestion helpers (no model needed)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reviewer_detect as rd  # noqa: E402
import reviewer_species as rs  # noqa: E402


class SpeciesHelperTests(unittest.TestCase):
    def test_nice_name(self):
        self.assertEqual(rs.nice_name("grey heron", "Ardea cinerea"), "Grey Heron")
        self.assertEqual(rs.nice_name("Lilac-breasted roller", "Coracias caudatus"),
                         "Lilac-breasted Roller")
        self.assertEqual(rs.nice_name("", "Acinonyx pardinensis"), "Acinonyx pardinensis")

    def test_history_prior_and_lookup(self):
        with tempfile.TemporaryDirectory() as d:
            h = rs.SpeciesHistory(Path(d) / "h.json")
            for _ in range(4):
                h.confirm("Grey Heron", "Ardea cinerea")
            h.confirm("Kingfisher", "")
            h.save()
            h2 = rs.SpeciesHistory(Path(d) / "h.json")
            self.assertEqual(h2.scientific_for("grey heron"), "Ardea cinerea")
            self.assertEqual(h2.prior(), {"Ardea cinerea": 3.0})     # 1 + sqrt(4)
            self.assertEqual(set(h2.recent()), {"Grey Heron", "Kingfisher"})

    def test_prediction_cache_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.jpg")
            Path(p).write_bytes(b"x")
            preds = {p: [["Grey Heron", "Ardea cinerea", 0.5]]}
            rs.save_predictions(d, preds)
            self.assertEqual(rs.load_predictions(d, {p}), preds)


class RefineTests(unittest.TestCase):
    class FakeSpecies:
        def __init__(self, answer):
            self.answer = answer

        def predict(self, img, box=None, prior=None, k=5):
            return [self.answer]

    def setUp(self):
        from PIL import Image
        self.img = Image.new("RGB", (100, 80))

    def test_closeup_bird_labelled_person_becomes_animal(self):
        dets = [[1, 0.6, 0.2, 0.1, 0.8, 0.9]]
        out = rd.refine(dets, self.img, self.FakeSpecies(["Blue Tit", "Cyanistes caeruleus", 0.9]))
        self.assertEqual(out[0][0], 0)
        self.assertEqual(rd.suggestions(out), [])

    def test_real_person_stays_person(self):
        dets = [[1, 0.6, 0.2, 0.1, 0.8, 0.9]]
        out = rd.refine(dets, self.img, self.FakeSpecies(["Human", "Homo sapiens", 0.8]))
        self.assertEqual(out[0][0], 1)

    def test_person_stays_person_when_the_species_model_is_unsure(self):
        # (the species model has no human: a person gets some animal's name, unsure)
        dets = [[1, 0.9, 0.2, 0.1, 0.8, 0.9]]
        out = rd.refine(dets, self.img, self.FakeSpecies(["Yellow-legged Gull", "Larus michahellis", 0.08]))
        self.assertEqual(out[0][0], 1)
        self.assertFalse(rd.animals(out))       # (and no animal guessed from the whole of a selfie)

    def test_tiles_find_a_distant_animal(self):
        from PIL import Image

        class FakeDetector:                     # sees the bird only in the top-right tile
            def detect(self, tile):
                return [[0, 0.8, 0.4, 0.4, 0.6, 0.6]] if tile.getpixel((0, 0)) == (255, 0, 0) else []
        img = Image.new("RGB", (300, 200))
        img.paste((255, 0, 0), (int(2 * 300 / (3 - 2 * 0.15) * 0.85), 0, 300, 200))
        found = rd.tiled_animals(rd.detect_tiled(FakeDetector(), img), [])
        self.assertTrue(found)
        d = found[0]
        self.assertEqual((d[0], d[1]), (0, 0.8))
        self.assertTrue(0.6 < d[2] < d[4] <= 1.0 and 0.0 <= d[3] < d[5] <= 1.0)
        # people first: an "animal" on a person is left out
        self.assertEqual(rd.tiled_animals(found, [[1, 0.9, 0.5, 0.0, 1.0, 1.0]]), [])

    def test_hidden_animal_found_by_species_model(self):
        out = rd.refine([], self.img, self.FakeSpecies(["Grey Heron", "Ardea cinerea", 0.55]))
        self.assertTrue(rd.animals(out))
        self.assertEqual(rd.suggestions(out), [])          # no 'empty', no 'edge'
        self.assertIsNone(rd.main_animal(out))              # no box to measure sharpness on

    def test_empty_frame_stays_empty(self):
        out = rd.refine([], self.img, self.FakeSpecies(["Some Moth", "Azanus x", 0.04]))
        self.assertEqual(rd.suggestions(out), ["empty"])


if __name__ == "__main__":
    unittest.main()
