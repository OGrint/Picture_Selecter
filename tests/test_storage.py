"""
Tests for reviewer_storage — session files, caches and XMP sidecars.

These guard backwards compatibility with session folders written by earlier
versions. Run from the project folder:

    python -m unittest discover -s tests
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import imagehash  # noqa: E402

import reviewer_storage as rs  # noqa: E402


class TempFolder(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pr_test_")
        self.files = []
        for name in ("DSC_0001.NEF", "DSC_0002.NEF", "IMG_0003.jpg"):
            p = os.path.join(self.dir, name)
            Path(p).write_bytes(b"x")
            self.files.append(p)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.dir, name)


class StatusFileTests(TempFolder):
    def test_old_format_loads_all_fields(self):
        old = {self.files[0]: {"best": True, "delete": False, "id": False, "star": 4,
                               "keywords": ["Heron"], "caption": "A & B"}}
        Path(self.path(rs.STATUS_FILENAME)).write_text(json.dumps(old), encoding="utf-8")
        loaded = rs.load_status(self.path(rs.STATUS_FILENAME), set(self.files))
        self.assertEqual(loaded, old)

    def test_save_roundtrip_and_backup(self):
        sf = self.path(rs.STATUS_FILENAME)
        rs.save_status(sf, {self.files[0]: {"best": True, "delete": False, "id": False}})
        rs.save_status(sf, {self.files[0]: {"best": False, "delete": True, "id": False}})
        self.assertTrue(json.loads(Path(sf).read_text())[self.files[0]]["delete"])
        self.assertTrue(json.loads(Path(sf + ".bak").read_text())[self.files[0]]["best"])

    def test_untagged_entries_not_written(self):
        sf = self.path(rs.STATUS_FILENAME)
        rs.save_status(sf, {self.files[0]: {"best": True},
                            self.files[1]: {"best": False, "delete": False, "id": False}})
        self.assertEqual(list(json.loads(Path(sf).read_text())), [self.files[0]])

    def test_corrupt_file_is_moved_aside_and_backup_used(self):
        sf = self.path(rs.STATUS_FILENAME)
        Path(sf + ".bak").write_text(json.dumps({self.files[1]: {"best": True}}))
        Path(sf).write_text('{"truncated": ')
        loaded = rs.load_status(sf, set(self.files))
        self.assertEqual(loaded, {self.files[1]: {"best": True}})
        self.assertFalse(os.path.exists(sf))
        self.assertTrue(any(".corrupt-" in f for f in os.listdir(self.dir)))

    def test_moved_folder_matches_by_filename(self):
        old_key = os.path.join("D:\\old\\place", "DSC_0002.NEF")
        Path(self.path(rs.STATUS_FILENAME)).write_text(json.dumps({old_key: {"star": 3}}))
        loaded = rs.load_status(self.path(rs.STATUS_FILENAME), set(self.files))
        self.assertEqual(loaded, {self.files[1]: {"star": 3}})

    def test_exact_path_beats_filename_match(self):
        data = {os.path.join("D:\\x", "DSC_0001.NEF"): {"star": 1},
                self.files[0]: {"star": 5}}
        self.assertEqual(rs._remap_to_files(data, set(self.files)),
                         {self.files[0]: {"star": 5}})


class CacheTests(TempFolder):
    def test_old_hash_cache_without_timestamps(self):
        h = imagehash.hex_to_hash("ffff0000ffff0000")
        cf = self.path(rs.HASH_CACHE_FILE)
        Path(cf).write_text(json.dumps(
            {self.files[0]: {"hash": str(h), "_mtime": os.path.getmtime(self.files[0])}}))
        hashes, stamps, focus = rs.load_hash_cache(cf, set(self.files))
        self.assertEqual(hashes[self.files[0]], h)
        self.assertNotIn(self.files[0], stamps)     # capture time still to be read
        self.assertNotIn(self.files[0], focus)      # focus point still to be read

    def test_hash_cache_roundtrip_with_timestamps(self):
        h = imagehash.hex_to_hash("0123456789abcdef")
        cf = self.path(rs.HASH_CACHE_FILE)
        rs.save_hash_cache(cf, {self.files[0]: h, self.files[1]: h},
                           {self.files[0]: 1700000000.0, self.files[1]: None},
                           {self.files[0]: (0.4, 0.4, 0.45, 0.46, "E9"), self.files[1]: None})
        hashes, stamps, focus = rs.load_hash_cache(cf, set(self.files))
        self.assertEqual(stamps, {self.files[0]: 1700000000.0, self.files[1]: None})
        self.assertEqual(focus, {self.files[0]: (0.4, 0.4, 0.45, 0.46, "E9"), self.files[1]: None})
        # older versions read only "hash" and "_mtime" — both still present
        entry = json.loads(Path(cf).read_text())[self.files[0]]
        self.assertIn("hash", entry)
        self.assertIn("_mtime", entry)

    def test_stale_cache_entry_ignored(self):
        cf = self.path(rs.SCORE_CACHE_FILE)
        Path(cf).write_text(json.dumps({self.files[0]: {"sharpness": 50, "_mtime": 1}}))
        self.assertEqual(rs.load_score_cache(cf, set(self.files)), {})

    def test_old_score_cache_with_motion_blur_loads(self):
        cf = self.path(rs.SCORE_CACHE_FILE)
        Path(cf).write_text(json.dumps({self.files[0]: {
            "sharpness": 70, "motion_blur": 20, "exposure": 90,
            "_mtime": os.path.getmtime(self.files[0])}}))
        scores = rs.load_score_cache(cf, set(self.files))
        self.assertEqual(scores[self.files[0]]["sharpness"], 70)


class XmpTests(TempFolder):
    TAGS = {"best": True, "delete": False, "id": False, "star": 4,
            "keywords": ["Kingfisher", "R&D <test>"], "caption": "Dive & catch",
            "copyright": "© Me"}

    def xmp_text(self, name="DSC_0001.xmp"):
        return Path(self.path(name)).read_text(encoding="utf-8")

    def test_own_xmp_is_valid_and_escaped(self):
        rs.write_xmp_sidecar(self.files[0], self.TAGS)
        text = self.xmp_text()
        ET.fromstring(text.split("?>", 1)[1].rsplit("<?xpacket", 1)[0])
        self.assertIn("R&amp;D &lt;test&gt;", text)
        self.assertIn("<xmp:Rating>4</xmp:Rating>", text)
        self.assertIn("<xmp:Label>Pick</xmp:Label>", text)

    def test_lightroom_xmp_is_merged_not_replaced(self):
        Path(self.path("DSC_0001.xmp")).write_text(
            '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
            'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            '<rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/" '
            'xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmp:Rating="2" xmp:Label="Red" '
            'crs:Exposure2012="+0.35"><dc:subject><rdf:Bag><rdf:li>Wetland</rdf:li>'
            '</rdf:Bag></dc:subject></rdf:Description></rdf:RDF></x:xmpmeta>',
            encoding="utf-8")
        rs.write_xmp_sidecar(self.files[0], {"best": False, "delete": False, "id": False,
                                             "star": 5, "keywords": ["Bird"]})
        text = self.xmp_text()
        self.assertIn('Exposure2012="+0.35"', text)   # develop settings kept
        self.assertIn("Red", text)                     # colour label kept
        self.assertIn("<xmp:Rating>5</xmp:Rating>", text)
        self.assertIn("Wetland", text)
        self.assertIn("Bird", text)

    def test_unparseable_foreign_xmp_left_alone(self):
        Path(self.path("DSC_0001.xmp")).write_text("<not xml", encoding="utf-8")
        rs.write_xmp_sidecar(self.files[0], self.TAGS)
        self.assertEqual(self.xmp_text(), "<not xml")

    def test_jpeg_next_to_raw_does_not_take_its_sidecar(self):
        jpg = self.path("DSC_0001.jpg")
        Path(jpg).write_bytes(b"x")
        self.assertIsNone(rs._xmp_path_for(jpg))
        self.assertEqual(rs._xmp_path_for(self.files[2]), Path(self.path("IMG_0003.xmp")))


class DetectionCacheTests(TempFolder):
    def test_roundtrip_and_model_check(self):
        cf = self.path(rs.DETECT_CACHE_FILE)
        dets = {self.files[0]: [[0, 0.9, 0.1, 0.1, 0.5, 0.5]], self.files[1]: []}
        rs.save_detections(cf, dets, "MDv1000-spruce")
        self.assertEqual(rs.load_detections(cf, set(self.files), "MDv1000-spruce"), dets)
        # results from a different model are not reused
        self.assertEqual(rs.load_detections(cf, set(self.files), "other-model"), {})


class ReviewedTests(unittest.TestCase):
    def test_is_reviewed(self):
        self.assertFalse(rs.is_reviewed({"best": False, "caption": "x"}))
        self.assertTrue(rs.is_reviewed({"star": 2}))
        self.assertTrue(rs.is_reviewed({"delete": True}))


if __name__ == "__main__":
    unittest.main()
