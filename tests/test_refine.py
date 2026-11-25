"""Tests for learning from your species decisions (made-up fingerprints)."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reviewer_refine as rf  # noqa: E402

LABELS = [("Willow Warbler", "Phylloscopus trochilus"), ("Chiffchaff", "Phylloscopus collybita"),
          ("Hoopoe", "Upupa epops"), ("Lion", "Panthera leo")]
EMB = np.eye(4, 8, dtype=np.float32)          # each species matches one direction


def look(ww, cc, own, rng, other=0.0):
    """A fingerprint: how much it matches each species, plus a look of its own (dim 6/7)."""
    v = np.zeros(8, np.float32)
    v[0], v[1], v[6], v[7] = ww, cc, own, other
    v += rng.normal(0, 0.03, 8).astype(np.float32)
    return v / np.linalg.norm(v)


def example(v, sci, day):
    return {"f": rf.pack(v), "sci": sci, "group": f"place|2019-03-{day:02d}"}


class RefineTests(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(1)

    def test_learns_a_look_alike_the_model_gets_wrong(self):
        # Chiffchaffs that the model calls Willow Warblers (they share dim 6);
        # real Willow Warblers look different (dim 7)
        ex = [example(look(0.7, 0.5, 0.5, self.rng), "Phylloscopus collybita", d % 5) for d in range(12)]
        ex += [example(look(0.7, 0.5, 0.0, self.rng, other=0.5), "Phylloscopus trochilus", d % 4)
               for d in range(8)]
        r = rf.Refiner(LABELS, EMB, 20.0)
        rep = r.train(ex)
        self.assertTrue(rep["active"], rep)
        self.assertGreater(rep["after"], rep["before"])
        top = r.top(look(0.7, 0.5, 0.5, self.rng))
        self.assertEqual(top[0][0], "Chiffchaff")
        top = r.top(look(0.7, 0.5, 0.0, self.rng, other=0.5))
        self.assertEqual(top[0][0], "Willow Warbler")
        # a photo unlike any example is left as the model sees it
        far = np.zeros(8, np.float32)
        far[2] = 1.0
        self.assertEqual(r.top(far)[0][0], "Hoopoe")

    def test_one_odd_example_is_not_a_rule(self):
        # mostly real Willow Warblers, one photo you called a Chiffchaff
        ex = [example(look(0.8, 0.3, 0.4, self.rng), "Phylloscopus trochilus", d % 5) for d in range(12)]
        ex.append(example(look(0.8, 0.3, 0.4, self.rng), "Phylloscopus collybita", 9))
        r = rf.Refiner(LABELS, EMB, 20.0)
        r.train(ex)
        top = r.top(look(0.8, 0.3, 0.4, self.rng)) if r.active else None
        self.assertTrue(top is None or top[0][0] == "Willow Warbler")

    def test_too_few_or_unhelpful_examples_switch_it_off(self):
        r = rf.Refiner(LABELS, EMB, 20.0)
        rep = r.train([example(look(0.7, 0.5, 0.5, self.rng), "Phylloscopus collybita", 1)] * 3)
        self.assertFalse(rep["active"])
        self.assertIn("needs at least", rep["why"])
        self.assertIsNone(r.top(look(0.7, 0.5, 0.5, self.rng)))
        # the model is already right about these: nothing to gain
        ex = [example(look(0.2, 0.9, 0.3, self.rng), "Phylloscopus collybita", d % 5) for d in range(10)]
        rep = r.train(ex)
        self.assertFalse(rep["active"])

    def test_examples_file(self):
        tmp = tempfile.mkdtemp(prefix="pr_rf_")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, rf.EXAMPLES_FILE)
        ex = rf.Examples(path)
        ex.add("k1", "a.JPG", "Chiffchaff", "Phylloscopus collybita", "GB", "p|d", note="juvenile")
        ex.add("k2", "b.JPG", "Hoopoe", "Upupa epops", "ES", "p|d", feat=rf.pack(np.ones(8)))
        self.assertEqual([k for k, _p in ex.need_features()], ["k1"])
        ex.set_features("k1", rf.pack(np.ones(8)))
        ex.remove("k2")
        ex.save()
        again = rf.Examples(path)
        self.assertEqual(list(again.data), ["k1"])
        self.assertEqual(again.data["k1"]["note"], "juvenile")
        self.assertEqual(len(again.usable()), 1)
        np.testing.assert_allclose(rf.unpack(again.data["k1"]["f"]), np.ones(8))


if __name__ == "__main__":
    unittest.main()
