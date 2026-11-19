"""Tests for library folders on several drives, the offline index and folder renames."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reviewer_predict as rp  # noqa: E402
import reviewer_search as rs  # noqa: E402
from reviewer_storage import remap_path  # noqa: E402


class RootTests(unittest.TestCase):
    def test_found_again_after_drive_letter_change(self):
        with tempfile.TemporaryDirectory() as d:
            real = os.path.join(d, "Holidays")
            os.makedirs(real)
            drive = os.path.splitdrive(real)[0]
            other = "Q:" if drive.upper() != "Q:" else "R:"
            entry = {"path": other + os.path.splitdrive(real)[1], "volume": "ABCD1234", "label": "HDD"}

            def vol(p):
                return ("ABCD1234", "HDD") if os.path.splitdrive(p)[0].upper() == drive.upper() \
                    else ("", "")
            with mock.patch.object(rs, "volume_of", vol), mock.patch.object(rs.sys, "platform", "win32"):
                self.assertEqual(os.path.normcase(rs.resolve_root(entry)), os.path.normcase(real))
                entry["volume"] = "FFFF0000"          # a different drive with the same path
                self.assertIsNone(rs.resolve_root(entry))

    def test_root_name(self):
        self.assertEqual(rs.root_name({"path": r"E:\Photos\Holidays", "label": "HDD-2TB"}),
                         "Holidays on HDD-2TB")
        self.assertEqual(rs.root_name({"path": r"C:\Photos\2026", "label": ""}), "2026")


class OfflineIndexTests(unittest.TestCase):
    def test_round_trip_and_thumbs(self):
        with tempfile.TemporaryDirectory() as d:
            off = rs.OfflineIndex(Path(d), {"path": r"E:\Photos\Holidays", "volume": "ABCD1234"})
            e = np.random.default_rng(0).standard_normal(512).astype(np.float32)
            off.save({r"2025-10 Spain\DSC_1.NEF": {"mtime": 1.5, "emb": e, "best": True,
                                                    "delete": False, "star": 3,
                                                    "species": "Hoopoe"}})
            back = off.load()[r"2025-10 Spain\DSC_1.NEF"]
            self.assertTrue(back["best"])
            self.assertEqual((back["star"], back["species"]), (3, "Hoopoe"))
            self.assertGreater(float(back["emb"] @ e / np.linalg.norm(e) ** 2), 0.999)
            off.save_thumb(r"2025-10 Spain\DSC_1.NEF", Image.new("RGB", (800, 600)))
            with Image.open(off.thumb_path(r"2025-10 Spain\DSC_1.NEF")) as t:
                self.assertEqual(max(t.size), rs.THUMB_SIDE)

    def test_same_folder_on_other_drive_kept_apart(self):
        a = rs.OfflineIndex(Path("x"), {"path": r"E:\Photos", "volume": "AAAA0000"})
        b = rs.OfflineIndex(Path("x"), {"path": r"F:\Photos", "volume": "BBBB0000"})
        self.assertNotEqual(a.file, b.file)


class RenameTests(unittest.TestCase):
    def test_remap_path(self):
        old, new = r"C:\P\2026-06-02", r"C:\P\2026-06-02 Minsmere"
        self.assertEqual(remap_path(old, old, new), new)
        self.assertEqual(remap_path(old + r"\Best", old, new), new + r"\Best")
        self.assertEqual(remap_path(r"C:\P\2026-06-02b", old, new), r"C:\P\2026-06-02b")
        self.assertEqual(remap_path(old.lower(), old, new), new)     # Windows: any case

    def test_history_rename(self):
        with tempfile.TemporaryDirectory() as d:
            h = rp.History(Path(d) / "h.npz")
            z = np.zeros((2, 3), np.float32)
            h.set_folder(r"C:\P\A", z, z, z, np.array([1, 0], np.float32), np.array([0, 0]),
                         np.array([1.0, 2.0], np.float32))
            self.assertTrue(h.rename_folder(r"C:\P\A", r"C:\P\A Minsmere"))
            self.assertEqual(list(h.blocks), [r"C:\P\A Minsmere"])
            self.assertFalse(h.rename_folder(r"C:\P\B", r"C:\P\C"))


if __name__ == "__main__":
    unittest.main()
