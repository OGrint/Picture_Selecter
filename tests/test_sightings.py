"""Tests for grouping bursts into sightings."""

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reviewer_sightings import build_sightings  # noqa: E402

HERON = np.array([1.0, 0.1, 0.0])
FOX = np.array([0.0, 0.2, 1.0])


def run(spec, gap=180):
    """spec: list of (start seconds, n frames, species tuple or None, emb)."""
    groups, stamps, sp, em = [], {}, {}, {}
    for k, (t, n, s, e) in enumerate(spec):
        g = [f"b{k}_{i}" for i in range(n)]
        for i, p in enumerate(g):
            stamps[p] = t + i * 0.2
        groups.append(g)
        sp[g[0]], em[g[0]] = s, e
    return build_sightings(groups, stamps, lambda g: sp[g[0]], lambda g: em[g[0]], gap)


H = ("Ardea cinerea", "Grey Heron", False, 0.8)
H_CONF = ("Ardea cinerea", "Grey Heron", True, 0.3)
K = ("Alcedo atthis", "Common Kingfisher", False, 0.9)
WEAK_GBH = ("Ardea herodias", "Great Blue Heron", False, 0.3)


class SightingTests(unittest.TestCase):
    def test_time_gap_splits(self):
        s = run([(0, 5, H, HERON), (60, 5, H, HERON), (600, 5, H, HERON)])
        self.assertEqual([len(x["groups"]) for x in s], [2, 1])

    def test_species_change_splits_even_without_gap(self):
        s = run([(0, 5, H, HERON), (20, 3, K, FOX), (40, 4, H, HERON)])
        self.assertEqual([x["name"] for x in s], ["Grey Heron", "Common Kingfisher", "Grey Heron"])

    def test_weak_suggestion_does_not_split(self):
        # a doubtful "Great Blue Heron" burst of the same bird stays in the sighting
        s = run([(0, 5, H, HERON), (30, 5, WEAK_GBH, HERON * 1.1), (60, 5, H, HERON)])
        self.assertEqual(len(s), 1)
        self.assertEqual(s[0]["name"], "Grey Heron")
        self.assertEqual(len(s[0]["paths"]), 15)

    def test_unknown_species_split_by_look(self):
        s = run([(0, 5, None, HERON), (30, 5, None, FOX)])
        self.assertEqual(len(s), 2)
        s = run([(0, 5, None, HERON), (30, 5, None, HERON + 0.05)])
        self.assertEqual(len(s), 1)
        self.assertIsNone(s[0]["name"])

    def test_confirmed_species_wins(self):
        s = run([(0, 5, H_CONF, HERON), (30, 5, WEAK_GBH, HERON)])
        self.assertEqual(len(s), 1)
        self.assertTrue(s[0]["confirmed"])
        self.assertEqual(s[0]["species"], "Ardea cinerea")

    def test_adjustable_gap(self):
        spec = [(0, 3, H, HERON), (240, 3, H, HERON)]
        self.assertEqual(len(run(spec, gap=180)), 2)
        self.assertEqual(len(run(spec, gap=300)), 1)

    def test_time_order_and_bounds(self):
        s = run([(500, 2, K, FOX), (0, 2, H, HERON)])
        self.assertEqual([x["name"] for x in s], ["Grey Heron", "Common Kingfisher"])
        self.assertAlmostEqual(s[0]["start"], 0.0)
        self.assertAlmostEqual(s[0]["end"], 0.2)

    def test_frames_without_time_kept(self):
        groups = [["a"], ["b"], ["c"]]
        s = build_sightings(groups, {"a": 0, "c": 30}, lambda g: None, lambda g: None)
        self.assertEqual(sum(len(x["paths"]) for x in s), 3)


if __name__ == "__main__":
    unittest.main()
