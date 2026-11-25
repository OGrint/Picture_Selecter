"""Tests for collecting Best folders into Year / Trip / Species."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reviewer_collect as rc  # noqa: E402


def photo(path, when="2026:03:14 10:00:00", colour=(120, 80, 40), size=(64, 48), sub=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ex = Image.Exif()
    ex.get_ifd(0x8769)[36867] = when
    if sub:
        ex.get_ifd(0x8769)[37521] = sub
    fmt = "TIFF" if path.lower().endswith((".tif", ".dng")) else "JPEG"
    Image.new("RGB", size, colour).save(path, fmt, exif=ex)
    return path


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "Pictures", "26")
        self.dest = os.path.join(self.tmp, "Collection")
        R = self.root
        # Spain: Best is empty, Final best beside it is the selection, with Topaz + exports
        os.makedirs(os.path.join(R, "Spain", "Best"))
        photo(os.path.join(R, "Spain", "DSC_0500.JPG"), "2026:03:13 09:00:00", (5, 5, 5))   # shoot only
        self.lynx = photo(os.path.join(R, "Spain", "Final best", "DSC_0601.JPG"), "2026:03:14 08:00:00", (200, 0, 0))
        photo(os.path.join(R, "Spain", "Final best", "DSC_0602.JPG"), "2026:03:14 08:00:05", (0, 200, 0))
        Path(os.path.join(R, "Spain", "Final best", "DSC_0601.xmp")).write_text("<x/>")
        photo(os.path.join(R, "Spain", "Final best", "tp", "tp-DSC_0601.dng"), "2026:03:14 08:00:00")
        Path(os.path.join(R, "Spain", "Final best", "tp", "tp-DSC_0601.dng.xmp")).write_text("<dt/>")
        photo(os.path.join(R, "Spain", "Final best", "tp", "edited", "tp-DSC_0601.jpg"), "2026:03:14 08:00:00")
        photo(os.path.join(R, "Spain", "Final best", "tp", "edited", "tp-DSC_0601-2.jpg"), "2026:03:14 08:00:00")
        photo(os.path.join(R, "Spain", "Final best", "tp", "edited", "Iberian lynx - 2026.jpeg"),
              "2026:03:14 08:00:05")                                                   # by time → 0602
        photo(os.path.join(R, "Spain", "Final best", "tp", "tp-DSC_0500.dng"), "2026:03:13 09:00:00")
        photo(os.path.join(R, "Spain", "Final best", "tp", "edited", "mystery.jpg"), "2026:01:01 01:01:01")
        # May: re-screened twice; the innermost non-empty Best is the decision
        photo(os.path.join(R, "May", "Best", "DSC_0700.JPG"), "2026:05:02 07:00:00", (1, 2, 3))
        photo(os.path.join(R, "May", "Best", "DSC_0701.JPG"), "2026:05:02 07:00:01", (4, 5, 6))
        photo(os.path.join(R, "May", "Best", "Best", "DSC_0701.JPG"), "2026:05:02 07:00:01", (4, 5, 6))
        os.makedirs(os.path.join(R, "May", "Best", "Best", "Best"))                    # empty: ignored
        # Scotland: named by the app, species in the session file, an exact duplicate of a May photo
        photo(os.path.join(R, "September", "Scotland", "Best", "Red-Deer_2026-09-13_Scotland_001.JPG"),
              "2026:09:13 06:00:00", (9, 9, 9))
        photo(os.path.join(R, "September", "Scotland", "Best", "DSC_3000.JPG"), "2026:09:13 06:10:00", (7, 7, 7))
        shutil.copy2(os.path.join(R, "May", "Best", "Best", "DSC_0701.JPG"),
                     os.path.join(R, "September", "Scotland", "Best", "DSC_0701.JPG"))
        Path(os.path.join(R, "September", "Scotland", "photo_reviewer_status.json")).write_text(json.dumps(
            {r"X:\old\September\Scotland\DSC_3000.JPG": {"best": True, "species": "Golden Eagle"}}))
        # November: camera clock never set (2000)
        photo(os.path.join(R, "November", "Best", "DSC_0176.JPG"), "2000:05:27 03:06:19", (50, 60, 70))
        # a hand-made collection: not a Best folder
        photo(os.path.join(R, "Best Combined", "DSC_0601.JPG"), "2026:03:14 08:00:00", (200, 0, 0))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def plan(self, **kw):
        items, notes, trips = rc.plan(self.root, self.dest, **kw)
        return items, notes, trips, {os.path.relpath(i.src, self.root): i for i in items}

    def test_rescreen_final_best_and_non_best_folders(self):
        items, notes, trips, by = self.plan()
        names = set(trips.values())
        self.assertIn("2026-03 Spain", names)
        self.assertIn("2026-05 May", names)
        self.assertIn(os.path.join("May", "Best", "Best", "DSC_0701.JPG"), by)       # innermost
        self.assertNotIn(os.path.join("May", "Best", "DSC_0700.JPG"), by)            # superseded
        self.assertTrue(any("Re-screened" in n and os.path.join("May", "Best") in n for n in notes))
        self.assertTrue(any("Best Combined" in n for n in notes))
        self.assertFalse(any("Best Combined" in k for k in by))
        self.assertIn(os.path.join("Spain", "Final best", "DSC_0601.JPG"), by)

    def test_versions_named_and_labelled(self):
        _i, _n, _t, by = self.plan()
        o = by[os.path.join("Spain", "Final best", "DSC_0601.JPG")]
        base = os.path.splitext(o.dest)[0]
        self.assertTrue(o.dest.startswith(os.path.join("2026", "2026-03 Spain", "Unidentified")))
        self.assertRegex(os.path.basename(o.dest), r"^Unidentified_2026-03-14_Spain_\d{3}\.JPG$")
        self.assertEqual(by[os.path.join("Spain", "Final best", "tp", "tp-DSC_0601.dng")].dest,
                         base + "_topaz.dng")
        finals = sorted(by[os.path.join("Spain", "Final best", "tp", "edited", f)].dest
                        for f in ("tp-DSC_0601.jpg", "tp-DSC_0601-2.jpg"))
        self.assertEqual(finals, sorted([base + "_final.jpg", base + "_final-2.jpg"]))
        lynx_export = by[os.path.join("Spain", "Final best", "tp", "edited", "Iberian lynx - 2026.jpeg")]
        self.assertTrue(lynx_export.of.endswith("DSC_0602.JPG") and lynx_export.dest.endswith("_final.jpeg"))
        # an edit of a frame that's only in the shoot folder: properly named, original not copied
        shoot_edit = by[os.path.join("Spain", "Final best", "tp", "tp-DSC_0500.dng")]
        self.assertRegex(os.path.basename(shoot_edit.dest), r"_2026-03-13_Spain_\d{3}_topaz\.dng$")
        self.assertNotIn(os.path.join("Spain", "DSC_0500.JPG"), by)
        mystery = by[os.path.join("Spain", "Final best", "tp", "edited", "mystery.jpg")]
        self.assertIn("Unmatched edits", mystery.dest)
        # darktable sidecar follows its file
        topaz = by[os.path.join("Spain", "Final best", "tp", "tp-DSC_0601.dng")]
        self.assertEqual([d for _s, d in topaz.sidecars], [base + "_topaz.dng.xmp"])

    def test_species_sources_duplicates_and_clock(self):
        _i, notes, trips, by = self.plan(identify=lambda p, iso=None: ("Iberian Lynx", 0.9) if "0601" in p
                                         else ("Rabbit", 0.2))
        sc = os.path.join("September", "Scotland", "Best")
        self.assertEqual(by[os.path.join(sc, "Red-Deer_2026-09-13_Scotland_001.JPG")].species, "Red Deer")
        self.assertEqual(by[os.path.join(sc, "DSC_3000.JPG")].species, "Golden Eagle")
        self.assertEqual(by[os.path.join(sc, "DSC_3000.JPG")].how, "confirmed")
        lynx = by[os.path.join("Spain", "Final best", "DSC_0601.JPG")]
        self.assertEqual((lynx.species, lynx.how), ("Iberian Lynx", "suggested"))
        self.assertIn(os.path.join("Iberian Lynx", "Iberian-Lynx_2026-03-14_Spain_"), lynx.dest)
        self.assertEqual(by[os.path.join("Spain", "Final best", "DSC_0602.JPG")].species, "Unidentified")
        dup = [by[os.path.join(sc, "DSC_0701.JPG")], by[os.path.join("May", "Best", "Best", "DSC_0701.JPG")]]
        self.assertEqual(sum(not d.include for d in dup), 1)
        self.assertTrue(any(d.note.startswith("duplicate of") for d in dup))
        nov = by[os.path.join("November", "Best", "DSC_0176.JPG")]
        self.assertEqual(nov.trip, "2026-11 November")
        self.assertRegex(os.path.basename(nov.dest), r"_2026-11_November_001\.JPG$")
        self.assertTrue(any("Camera clock looks wrong" in n for n in notes))

    def test_copy_manifest_rerun_and_new_numbers(self):
        items, _n, _t, by = self.plan()
        done = rc.run(items, self.dest)
        self.assertFalse(done["errors"])
        o = by[os.path.join("Spain", "Final best", "DSC_0601.JPG")]
        self.assertTrue(os.path.exists(os.path.join(self.dest, o.dest)))
        self.assertTrue(os.path.exists(os.path.join(self.dest, os.path.splitext(o.dest)[0] + ".xmp")))
        man = json.loads(Path(self.dest, rc.MANIFEST).read_text(encoding="utf-8"))["items"]
        self.assertEqual(len(man), done["copied"])
        # again: nothing new
        items2, _n, _t, by2 = self.plan()
        self.assertFalse([i for i in items2 if i.include])
        self.assertEqual(by2[os.path.join("Spain", "Final best", "DSC_0601.JPG")].dest, o.dest)
        # a new photo in the same trip / species / day: the next number, the old names untouched
        photo(os.path.join(self.root, "Spain", "Final best", "DSC_0603.JPG"), "2026:03:14 09:00:00", (0, 0, 200))
        items3, _n, _t, by3 = self.plan()
        new = [i for i in items3 if i.include]
        self.assertEqual([os.path.basename(i.src) for i in new], ["DSC_0603.JPG"])
        self.assertNotIn(new[0].dest, {i.dest for i in items3 if not i.include})
        self.assertEqual(by3[os.path.join("Spain", "Final best", "DSC_0601.JPG")].dest, o.dest)

    def test_renamed_source_folder_is_not_collected_again(self):
        items, _n, _t, by = self.plan()
        rc.run(items, self.dest)
        before = by[os.path.join("Spain", "Final best", "DSC_0601.JPG")].dest
        os.rename(os.path.join(self.root, "Spain"), os.path.join(self.root, "Spain Andalucia"))
        items2, _n, _t, by2 = self.plan()
        again = [i for i in items2 if i.include]
        self.assertEqual(again, [], [i.src for i in again])
        self.assertEqual(by2[os.path.join("Spain Andalucia", "Final best", "DSC_0601.JPG")].dest, before)

    def test_trip_rename_and_collection_inside_parent(self):
        _i, _n, trips, _b = self.plan()
        spain = next(f for f, n in trips.items() if n.endswith("Spain"))
        _i, _n, trips, by = self.plan(trip_names={spain: "2026-03 Andalucia"})
        self.assertIn(os.path.join("2026", "2026-03 Andalucia"),
                      by[os.path.join("Spain", "Final best", "DSC_0601.JPG")].dest)
        # a collection folder inside the searched folder is never collected again
        inner = os.path.join(self.root, "Collection")
        items, _n, _t = rc.plan(self.root, inner)[:3]
        rc.run(items, inner)
        items2 = rc.plan(self.root, inner)[0]
        self.assertFalse(any(rc._within(i.src, inner) for i in items2))

    def test_trip_labels(self):
        P = r"C:\Pictures\26"
        self.assertEqual(rc.trip_label(r"C:\Pictures\26\Spain", P), "Spain")
        self.assertEqual(rc.trip_label(r"C:\Pictures\26\May\2", P), "May")
        self.assertEqual(rc.trip_label(r"C:\Pictures\26\Jan\BOPH", P), "BOPH")
        self.assertEqual(rc.trip_label(r"C:\Pictures\2026\2026-06-01 to 06-07 Kenya", r"C:\Pictures"), "Kenya")
        self.assertEqual(rc.species_from_name("Grey-Heron_2026-05-12_Minsmere_001.NEF"), "Grey Heron")
        self.assertIsNone(rc.species_from_name("Unidentified_2026-05-12_Minsmere_001.NEF"))
        self.assertEqual(rc.dsc_number("tp-DSC_3052-2.jpg"), 3052)
        self.assertEqual(rc._path_date(r"C:\Pictures\26\November"), (2026, 11))


if __name__ == "__main__":
    unittest.main()
