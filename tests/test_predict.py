"""Tests for the keeper predictor (synthetic data; no models needed)."""

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reviewer_predict as rp  # noqa: E402


def synthetic(n_bursts=120, size=6, seed=0):
    """Bursts where the kept frame has a 'pose' direction in its embedding,
    and sharpness is only weakly related (so sharpness alone is a poor guide)."""
    rng = np.random.default_rng(seed)
    pose = rng.normal(size=rp.EMBED_DIM).astype(np.float32)
    H, E, R, y, g, s = [], [], [], [], [], []
    for b in range(n_bursts):
        base = rng.normal(size=rp.EMBED_DIM).astype(np.float32)
        keep = rng.integers(size)
        frames = [base + rng.normal(scale=0.5, size=rp.EMBED_DIM).astype(np.float32)
                  + (1.5 * pose if i == keep else 0) for i in range(size)]
        mean = np.mean(frames, 0)
        for i, e in enumerate(frames):
            sharp = rng.uniform(40, 90)
            h = np.zeros(len(rp.HAND_NAMES), np.float32)
            h[0] = sharp / 100
            H.append(h); E.append(e); R.append(e - mean)
            y.append(1.0 if i == keep else 0.0); g.append(b); s.append(sharp)
    return tuple(np.array(v, np.float32 if k != 4 else np.int64) for k, v in enumerate((H, E, R, y, g, s)))


class PredictorTests(unittest.TestCase):
    def test_learns_preference_beyond_sharpness(self):
        H, E, R, y, g, s = synthetic()
        model, info = rp.train(H, E, R, y, g, s)
        self.assertIsNotNone(model)
        self.assertGreater(info["agree"], 0.6)
        self.assertGreater(info["agree"], info["agree_sharp"] + 0.2)

    def test_agreement_metric(self):
        y = np.array([1, 0, 0, 0, 1, 0], np.float32)
        g = np.array([1, 1, 1, 2, 2, 2])
        self.assertEqual(rp.agreement(np.array([.9, .1, .2, .3, .8, .1]), y, g), 1.0)
        self.assertEqual(rp.agreement(np.array([.1, .9, .2, .9, .1, .1]), y, g), 0.0)

    def test_model_and_history_roundtrip(self):
        H, E, R, y, g, s = synthetic(30)
        model, _ = rp.train(H, E, R, y, g, s)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.npz"
            model.save(p)
            m2 = rp.KeeperModel.load(p)
            np.testing.assert_allclose(model.predict(H[:5], E[:5], R[:5]),
                                       m2.predict(H[:5], E[:5], R[:5]), rtol=1e-5)
            hist = rp.History(Path(d) / "h.npz")
            hist.set_folder("A", H, E, R, y, g, s)
            hist.set_folder("B", H[:12], E[:12], R[:12], y[:12], g[:12], s[:12])
            hist.save()
            h2 = rp.History(Path(d) / "h.npz")
            self.assertEqual(h2.folders, 2)
            arr = h2.arrays(exclude="B")
            self.assertEqual(len(arr[3]), len(y))

    def test_labels_from_tags(self):
        bursts = [["a", "b", "c"], ["d"], ["e"], ["f", "g"]]
        status = {"b": {"best": True}, "d": {"best": True}, "e": {"delete": True},
                  "f": {"delete": True}}
        labels, _ = rp.labels_for(list("abcdefg"), status, bursts)
        self.assertEqual(labels, {"a": 0.0, "b": 1.0, "c": 0.0, "d": 1.0, "e": 0.0, "f": 0.0})

    def test_group_bursts(self):
        import imagehash
        h0 = imagehash.hex_to_hash("0000000000000000")
        h1 = imagehash.hex_to_hash("ffffffffffffffff")
        files = ["a", "b", "c", "d"]
        hashes = {"a": h0, "b": h0, "c": h0, "d": h1}
        stamps = {"a": 0, "b": 5, "c": 100, "d": 6}
        groups = rp.group_bursts(files, hashes, stamps)
        self.assertIn(["a", "b"], groups)       # c is >30 s later, d looks different
        self.assertIn(["c"], groups)


if __name__ == "__main__":
    unittest.main()
