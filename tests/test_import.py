"""Tests for import grouping, folder names, the import ledger and folder merging."""

import datetime
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reviewer_import as ri  # noqa: E402


def ts(y, m, d, h=8):
    return datetime.datetime(y, m, d, h).timestamp()


ITEMS = [("c/DSC_1.NEF", 10, ts(2026, 6, 1)), ("c/DSC_2.NEF", 10, ts(2026, 6, 2)),
         ("c/DSC_3.NEF", 10, ts(2026, 6, 2, 9)), ("c/DSC_4.NEF", 10, ts(2026, 6, 8)),
         ("c/DSC_5.NEF", 10, ts(2026, 7, 1))]


class GroupingTests(unittest.TestCase):
    def names(self, mode):
        return [g["base"] for g in ri.group_imports(ITEMS, mode)]

    def test_day(self):
        self.assertEqual(self.names("day"), ["2026-06-01", "2026-06-02", "2026-06-08", "2026-07-01"])

    def test_week_uses_dates_covered(self):
        # Mon 1 Jun – Sun 7 Jun; 8 Jun starts a new week; 1 Jul is on its own
        self.assertEqual(self.names("week"), ["2026-06-01 to 06-02", "2026-06-08", "2026-07-01"])

    def test_month(self):
        self.assertEqual(self.names("month"), ["2026-06", "2026-07"])

    def test_one_folder(self):
        self.assertEqual(self.names("one"), ["2026-06-01 to 07-01"])
        g = ri.group_imports(ITEMS, "one")[0]
        self.assertEqual(len(g["files"]), 5)

    def test_across_new_year(self):
        items = [("a", 1, ts(2025, 12, 30)), ("b", 1, ts(2026, 1, 2))]
        self.assertEqual([g["base"] for g in ri.group_imports(items, "one")],
                         ["2025-12-30 to 2026-01-02"])

    def test_files_in_time_order(self):
        g = ri.group_imports(list(reversed(ITEMS)), "month")[0]
        self.assertEqual([f[0] for f in g["files"]], ["c/DSC_1.NEF", "c/DSC_2.NEF",
                                                     "c/DSC_3.NEF", "c/DSC_4.NEF"])

    def test_folder_name_and_label_cleaning(self):
        self.assertEqual(ri.folder_name("2026-06-02", " Masai  Mara "), "2026-06-02 Masai Mara")
        self.assertEqual(ri.folder_name("2026-06-02", "Lake: N/S?"), "2026-06-02 Lake- N-S-")
        self.assertEqual(ri.folder_name("2026-06-02", ""), "2026-06-02")

    def test_year_folder(self):
        d = [datetime.date(2026, 6, 2)]
        self.assertEqual(ri.import_root("P", True, d), os.path.join("P", "2026"))
        self.assertEqual(ri.import_root("P", False, d), "P")


class FolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_existing_label_found(self):
        os.makedirs(os.path.join(self.root, "2026-06-02 Minsmere"))
        os.makedirs(os.path.join(self.root, "2026-06-03"))
        self.assertEqual(ri.existing_label(self.root, "2026-06-02"), "Minsmere")
        self.assertEqual(ri.existing_label(self.root, "2026-06-03"), "")
        self.assertEqual(ri.existing_label(self.root, "2026-06-04"), "")

    def test_move_folder_rename_and_merge(self):
        a = os.path.join(self.root, "2026-06-02")
        os.makedirs(a)
        Path(a, "DSC_1.NEF").write_bytes(b"x" * 5)
        final, ren = ri.move_folder(a, os.path.join(self.root, "2026-06-02 Minsmere"))
        self.assertTrue(os.path.isfile(os.path.join(final, "DSC_1.NEF")))
        self.assertFalse(os.path.exists(a))
        # second import into a dated folder, then the same label → merged
        os.makedirs(a)
        Path(a, "DSC_1.NEF").write_bytes(b"x" * 5)           # identical: dropped
        Path(a, "DSC_2.NEF").write_bytes(b"y" * 3)
        final2, ren2 = ri.move_folder(a, final)
        self.assertEqual(final2, final)
        self.assertEqual(sorted(os.listdir(final)), ["DSC_1.NEF", "DSC_2.NEF"])
        self.assertFalse(os.path.exists(a))

    def test_move_folder_name_clash_renamed(self):
        dst = os.path.join(self.root, "2026-06-02 Minsmere")
        os.makedirs(dst)
        Path(dst, "DSC_1.NEF").write_bytes(b"old")
        src = os.path.join(self.root, "2026-06-02")
        os.makedirs(src)
        Path(src, "DSC_1.NEF").write_bytes(b"different")       # camera counter wrapped
        _final, ren = ri.move_folder(src, dst)
        self.assertEqual(ren, {"DSC_1.NEF": "DSC_1_1.NEF"})
        self.assertEqual(Path(dst, "DSC_1_1.NEF").read_bytes(), b"different")


class LedgerTests(unittest.TestCase):
    def test_find_only_under_root_and_same_size(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(ri, "_load_user_json", return_value={}):
            p = os.path.join(d, "Photos", "2026-06-01 to 06-07 Kenya", "DSC_1.NEF")
            os.makedirs(os.path.dirname(p))
            Path(p).write_bytes(b"x" * 10)
            led = ri.Ledger()
            k = ri.Ledger.key("E:/DCIM/100NIKON/DSC_1.NEF", 10, 1000.4)
            led.add(k, p)
            self.assertEqual(led.find(k, os.path.join(d, "Photos"), 10), p)   # any grouping
            self.assertIsNone(led.find(k, os.path.join(d, "Backup"), 10))     # other root
            self.assertIsNone(led.find(k, os.path.join(d, "Photos"), 11))     # other size
            os.remove(p)
            self.assertIsNone(led.find(k, os.path.join(d, "Photos"), 10))     # deleted


class SettingsTests(unittest.TestCase):
    def test_migrates_old_single_destination(self):
        with mock.patch.object(ri, "_load_user_json", return_value=None):
            s = ri.load_import_settings({"import_dest": "D:/Photos", "import_backup": "E:/B"})
        ev = s["presets"]["Everyday"]
        self.assertEqual((ev["dest"], ev["backup"], ev["group"]), ("D:/Photos", "E:/B", "day"))
        self.assertFalse(ev["year_folder"])            # keeps the layout you already have
        self.assertEqual(s["presets"]["Holiday"]["group"], "week")
        self.assertEqual(s["preset"], "Everyday")


if __name__ == "__main__":
    unittest.main()
