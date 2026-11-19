"""Tests for AI search helpers: CLIP tokenizer matches OpenCLIP; index files."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reviewer_search as rs  # noqa: E402

# Token ids produced by OpenCLIP's own tokenizer for ViT-B-32
EXPECTED = {
    "would make a good christmas card": [49406, 1311, 1078, 320, 886, 1677, 2601, 49407],
    "bird in flight against a blue sky": [49406, 3329, 530, 3795, 1601, 320, 1746, 2390, 49407],
    "kingfisher with a fish": [49406, 34330, 593, 320, 2759, 49407],
    "Robin in the SNOW!": [49406, 7988, 530, 518, 2583, 256, 49407],
    "a fox's den, 2026": [49406, 320, 3240, 568, 1181, 267, 273, 271, 273, 277, 49407],
    "red berries & frost": [49406, 736, 8319, 261, 11619, 49407],
    "close-up portrait of an eagle": [49406, 2660, 268, 705, 5352, 539, 550, 7517, 49407],
    "café crème": [49406, 15304, 1075, 12138, 614, 49407],
}


@unittest.skipUnless(rs._find(rs.VOCAB_FILE), "CLIP vocabulary not installed")
class TokenizerTests(unittest.TestCase):
    def test_matches_openclip(self):
        tok = rs.ClipTokenizer(rs._find(rs.VOCAB_FILE))
        out = tok(list(EXPECTED))
        for row, (text, ids) in zip(out, EXPECTED.items()):
            self.assertEqual([int(x) for x in row if x], ids, text)
        self.assertEqual(out.shape, (len(EXPECTED), rs.CONTEXT))

    def test_long_text_truncated_with_end_token(self):
        tok = rs.ClipTokenizer(rs._find(rs.VOCAB_FILE))
        row = tok(["bird " * 200])[0]
        self.assertEqual(row[-1], tok.eot)


class IndexTests(unittest.TestCase):
    def test_index_roundtrip_and_staleness(self):
        with tempfile.TemporaryDirectory() as d:
            for n in ("a.jpg", "b.jpg"):
                Path(d, n).write_bytes(b"x")
            mt = os.path.getmtime(os.path.join(d, "a.jpg"))
            rs.save_index(d, {"a.jpg": (mt, np.ones(512, np.float32))})
            idx = rs.load_index(d)
            self.assertEqual(list(idx), ["a.jpg"])
            self.assertEqual(rs.stale_files(d, {".jpg"}, idx), ["b.jpg"])

    def test_library_skips_process_copies(self):
        with tempfile.TemporaryDirectory() as d:
            for sub in ("2026-05-12 Minsmere", "2026-05-12 Minsmere/Best", "2026-05-12 Minsmere/ID"):
                os.makedirs(os.path.join(d, sub), exist_ok=True)
                Path(d, sub, "x.jpg").write_bytes(b"x")
            self.assertEqual([Path(f).name for f in rs.library_folders(d, {".jpg"})],
                             ["2026-05-12 Minsmere"])


if __name__ == "__main__":
    unittest.main()
