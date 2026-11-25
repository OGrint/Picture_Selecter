"""Tests for the species list from an archive (the AI model is faked)."""

import csv
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reviewer_species_list as sl  # noqa: E402


def photo(path, when, colour=(120, 80, 40)):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ex = Image.Exif()
    ex.get_ifd(0x8769)[36867] = when
    Image.new("RGB", (64, 48), colour).save(path, "JPEG", exif=ex)
    return path


class FakeModel:
    labels = [("Iberian Lynx", "Lynx pardinus"), ("Hoopoe", "Upupa epops"),
              ("Willow Warbler", "Phylloscopus trochilus"), ("Chiffchaff", "Phylloscopus collybita"),
              ("Lion", "Panthera leo")]
    group = {"Lynx pardinus": "mammals", "Upupa epops": "birds", "Phylloscopus trochilus": "birds",
             "Phylloscopus collybita": "birds", "Panthera leo": "mammals"}


def result(name, p, box=(0.2, 0.2, 0.6, 0.6)):
    sci = dict(FakeModel.labels)[name]
    rest = [[n, s, round((1 - p) / 4, 4)] for n, s in FakeModel.labels if n != name]
    return {"top": [[name, sci, p]] + rest, "box": list(box), "conf": 0.9 if box else 0.0, "other": False}


NONE = {"top": [["Hoopoe", "Upupa epops", 0.05]], "box": None, "conf": 0.0, "other": False}


class SpeciesListTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pr_sl_")
        # registered first, so it runs last: after the scan caches are closed
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.user = os.path.join(self.tmp, "user")
        self.root = os.path.join(self.tmp, "Pictures")
        self.dest = os.path.join(self.tmp, "Species list")
        R = self.root
        # Spain: a burst of lynx (Best copy of one frame), a hoopoe, a warbler nobody is sure of
        self.lynx = [photo(os.path.join(R, "2019", "Spain", "Day 1", f"DSC_00{i}.JPG"),
                           f"2019:03:14 08:00:0{i}") for i in range(1, 6)]
        os.makedirs(os.path.join(R, "2019", "Spain", "Day 1", "Best"))
        shutil.copy2(self.lynx[3], os.path.join(R, "2019", "Spain", "Day 1", "Best", "DSC_004.JPG"))
        self.hoopoe = photo(os.path.join(R, "2019", "Spain", "Day 2", "DSC_0100.JPG"), "2019:03:15 09:00:00")
        self.warbler = photo(os.path.join(R, "2019", "Spain", "Day 2", "DSC_0200.JPG"), "2019:03:15 10:00:00")
        self.empty = photo(os.path.join(R, "2019", "Spain", "Day 2", "DSC_0300.JPG"), "2019:03:15 11:00:00")
        # edits and a RAW+JPEG pair's JPEG are not read
        photo(os.path.join(R, "2019", "Spain", "Day 1", "Best", "tp-DSC_004.JPG"), "2019:03:14 08:00:04")
        photo(os.path.join(R, "2019", "Spain", "Day 1", "Best", "edited", "lynx.JPG"), "2019:03:14 08:00:04")
        Path(os.path.join(R, "2019", "Spain", "Day 2", "DSC_0100.NEF")).write_bytes(b"x")
        # a folder with no country in its name, but darktable wrote one into the XMP
        self.lion = photo(os.path.join(R, "2018", "Holiday", "DSC_0500.JPG"), "2018:07:01 07:00:00")
        Path(os.path.join(R, "2018", "Holiday", "DSC_0500.JPG.xmp")).write_text(
            '<x photoshop:Country="Kenya"/>')
        # a folder with nothing to go on, and a lynx you named in the app
        self.named = photo(os.path.join(R, "2017", "May", "Best", "DSC_0900.JPG"), "2017:05:01 07:00:00")
        Path(os.path.join(R, "2017", "May", "Best", "photo_reviewer_status.json")).write_text(
            json.dumps({self.named: {"species": "Hoopoe"}}))
        self.results = {"DSC_004.JPG": result("Iberian Lynx", 0.95), "DSC_003.JPG": result("Iberian Lynx", 0.9),
                        "DSC_0100.JPG": result("Hoopoe", 0.97), "DSC_0200.JPG": result("Willow Warbler", 0.45),
                        "DSC_0300.JPG": NONE, "DSC_0500.JPG": result("Lion", 0.99)}
        self.calls = []
        patches = [mock.patch("reviewer_species.get_species_model", return_value=FakeModel()),
                   mock.patch.object(sl, "analyse", side_effect=self._analyse)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _analyse(self, path):
        self.calls.append(os.path.basename(path))
        return self.results.get(os.path.basename(path), NONE)

    def _identifier(self, sure=0.8):
        ident = sl.Identifier(self.user, sure)
        ident.ranker._regions = None                    # (no downloads in tests)
        self.addCleanup(ident.close)
        return ident

    def _run(self, countries=None, decisions=None):
        photos = sl.find_photos(self.root, exclude=self.dest)
        places = sl.guess_places(photos, self.root)
        cs = {p: i["iso"] for p, i in places.items()}
        cs.update(countries or {})
        ident = self._identifier()
        return photos, places, ident.run(photos, cs, self.root, progress=lambda d: None,
                                         decisions=decisions)

    def _copied(self):
        self.results["DSC_0100.NEF"] = self.results["DSC_0100.JPG"]
        _p, _pl, res = self._run()
        entries, clarify = sl.build(res["sightings"])
        sl.copy_list(entries, clarify, self.dest)
        return entries, clarify, sl.ListEditor(entries, clarify, self.dest, trash=os.remove)

    def _again(self):
        """Identify again with the review's decisions, as the window does."""
        _p, _pl, res = self._run(decisions=sl.load_list(self.dest).get("decisions"))
        entries, clarify = sl.build(res["sightings"])
        sl.plan_copies(entries, clarify, self.dest)
        return entries, clarify

    def _files(self):
        return sorted(os.path.relpath(os.path.join(d, f), self.dest)
                      for d, _s, fs in os.walk(self.dest) for f in fs
                      if os.path.splitext(f)[1] in (".JPG", ".NEF") and sl.HOLD not in d)

    def test_review_pick_another_species_moves_the_copy_and_is_remembered(self):
        entries, clarify, ed = self._copied()
        warbler = clarify[0]
        msg = ed.assign(warbler, "Chiffchaff", "Phylloscopus collybita", "birds")
        self.assertIn("added to Spain", msg)
        self.assertEqual(clarify, [])
        chiff = os.path.join("Spain", "Birds", "Chiffchaff (Phylloscopus collybita)_2019-03-15.JPG")
        self.assertIn(chiff, self._files())
        self.assertFalse(os.path.exists(os.path.join(self.dest, "Spain", "To clarify")))  # tidied
        self.assertTrue(os.path.exists(self.warbler))                                    # original kept
        with open(os.path.join(self.dest, "All species.csv"), encoding="utf-8-sig") as f:
            self.assertIn("Chiffchaff", [r["Common name"] for r in csv.DictReader(f)])
        # Identify again: it's yours now, certain, and the copy already there is kept
        self.calls.clear()
        entries2, clarify2 = self._again()
        self.assertEqual(clarify2, [])
        e = [e for e in entries2 if e.scientific == "Phylloscopus collybita"][0]
        self.assertEqual((e.how, e.kept, e.dest), ("yours", True, chiff))

    def test_review_duplicate_keeps_one_photo(self):
        entries, clarify, ed = self._copied()
        hoopoe = [e for e in entries if e.species == "Hoopoe" and e.iso == "ES"][0]
        hoopoe_file = os.path.join(self.dest, hoopoe.dest)
        self.assertIs(ed.entry_for("ES", "Upupa epops", "Hoopoe"), hoopoe)
        msg = ed.assign(clarify[0], "Hoopoe", "Upupa epops", "birds")
        self.assertIn("already in Spain", msg)
        self.assertTrue(os.path.exists(hoopoe_file))                     # the one there is kept
        self.assertEqual(len([f for f in self._files() if "Hoopoe" in f and "Spain" in f]), 1)
        self.assertEqual(len(hoopoe.sightings), 2)
        self.assertNotIn("To clarify", " ".join(self._files()))

    def test_review_duplicate_use_this_photo(self):
        entries, clarify, ed = self._copied()
        hoopoe = [e for e in entries if e.species == "Hoopoe" and e.iso == "ES"][0]
        old = os.path.join(self.dest, hoopoe.dest)
        warbler = clarify[0]
        ed.assign(warbler, "Hoopoe", "Upupa epops", "birds", use_this=True)
        self.assertIs(hoopoe.pick, warbler.sighting)
        spain = [f for f in self._files() if "Hoopoe" in f and f.startswith("Spain")]
        self.assertEqual(len(spain), 1)
        with open(os.path.join(self.dest, spain[0]), "rb") as a, open(self.warbler, "rb") as b:
            self.assertEqual(a.read(), b.read())                          # this photo now
        self.assertFalse(os.path.exists(old))                             # the old copy removed
        self.assertTrue(os.path.exists(self.hoopoe))                      # (its original isn't)
        self.assertEqual(sl.load_list(self.dest)["species"]["ES|Upupa epops"], hoopoe.dest)

    def test_review_remove_and_rename_a_species(self):
        entries, clarify, ed = self._copied()
        lion = [e for e in entries if e.species == "Lion"][0]
        ed.reject(lion)
        self.assertNotIn(lion, entries)
        self.assertFalse(any("Lion" in f for f in self._files()))
        lynx = [e for e in entries if e.species == "Iberian Lynx"][0]
        ed.assign(lynx, "my lynx")                         # a name the model doesn't know
        self.assertIn(os.path.join("Spain", "Other animals", "my lynx_2019-03-14.JPG"), self._files())
        entries2, _c = self._again()
        names = {(e.iso, e.species) for e in entries2}
        self.assertNotIn(("KE", "Lion"), names)            # removed stays removed
        self.assertIn(("ES", "my lynx"), names)
        self.assertNotIn(("ES", "Iberian Lynx"), names)
        self.assertTrue(os.path.exists(self.lion))         # originals never touched

    def test_review_decisions_teach_the_next_identify(self):
        import numpy as np
        import reviewer_refine as rf

        class LearnModel(FakeModel):                     # fingerprints: one direction per species
            _emb = np.eye(5, 8, dtype=np.float32)
            _scale = 20.0

        def look(rng):        # the model sees a Willow Warbler (dim 2) more than a Chiffchaff (dim 3)
            v = np.array([0, 0, 0.7, 0.5, 0, 0, 0.5, 0], np.float32) + rng.normal(0, 0.03, 8).astype(np.float32)
            return v / np.linalg.norm(v)
        rng = np.random.default_rng(3)
        with mock.patch("reviewer_species.get_species_model", return_value=LearnModel()):
            # (read before fingerprints were kept, as in an existing cache)
            entries, clarify, ed = self._copied()
            self.assertEqual([c.sighting.species for c in clarify], ["Willow Warbler"])
            self.results["DSC_0200.JPG"] = dict(result("Willow Warbler", 0.45), f=rf.pack(look(rng)))
            # your earlier decisions: photos like this one were Chiffchaffs
            ed.examples = sl.Identifier(self.user).examples
            for d in range(10):
                ed.examples.add(f"k{d}", f"x{d}.JPG", "Chiffchaff", "Phylloscopus collybita", "ES",
                                f"elsewhere|2018-04-{d % 5 + 1:02d}", rf.pack(look(rng)))
            ed.examples.save()
            self.calls.clear()
            _p, _pl, res = self._run()
            learn = res["learning"]
            self.assertTrue(learn["active"], learn)
            self.assertEqual(learn["reread"], 1)                 # only the uncertain one
            self.assertEqual(self.calls, ["DSC_0200.JPG"])
            # the photo you named in the main window became an example on the first run
            # (read once: it gives no fingerprint here, so it isn't read again)
            mine = [e for e in sl.Identifier(self.user).examples.data.values() if e["path"] == self.named]
            self.assertEqual([(e["sci"], e.get("read")) for e in mine], [("Upupa epops", True)])
            self.assertGreaterEqual(learn["changed"], 1)
            s = [s for s in res["sightings"] if s.pick.path == self.warbler][0]
            self.assertEqual(s.species, "Chiffchaff")
            self.assertTrue(s.certain)
            self.assertIn("AI said Willow Warbler 45%", s.learned)
            # a decision in Review becomes an example, with your note and its fingerprint
            entries, clarify = sl.build(res["sightings"])
            ed2 = sl.ListEditor(entries, clarify, self.dest, trash=os.remove, examples=ed.examples)
            ident = sl.Identifier(self.user)
            self.addCleanup(ident.close)
            ed2.feature_of = lambda k: (ident.cache.get(k).get("a") or {}).get("f")
            lion = [e for e in entries if e.species == "Lion"][0]
            ed2.assign(lion, "Lion", "Panthera leo", "mammals", note="male")
            got = sl.Identifier(self.user).examples.data
            lion_ex = [e for e in got.values() if e["sci"] == "Panthera leo"]
            self.assertEqual(len(lion_ex), 1)
            self.assertEqual(lion_ex[0]["note"], "male")
            self.assertEqual(lion.note, "male")
            with open(os.path.join(self.dest, "All species.csv"), encoding="utf-8-sig") as f:
                rows = {r["Common name"]: r for r in csv.DictReader(f)}
            self.assertEqual(rows["Lion"]["Your note"], "male")

    def test_review_undo_brings_everything_back(self):
        entries, clarify, ed = self._copied()
        before = self._files()
        lion = [e for e in entries if e.species == "Lion"][0]
        ed.reject(lion)
        ed.assign(clarify[0], "Chiffchaff", "Phylloscopus collybita", "birds")
        self.assertNotEqual(self._files(), before)
        self.assertEqual(ed.undo(), "Undone")
        self.assertEqual(ed.undo(), "Undone")
        self.assertEqual(ed.undo(), "Nothing to undo")
        self.assertEqual(self._files(), before)
        self.assertEqual(sorted(e.species for e in entries), ["Hoopoe", "Hoopoe", "Iberian Lynx", "Lion"])
        self.assertEqual([c.sighting.species for c in clarify], ["Willow Warbler"])
        self.assertEqual(sl.load_list(self.dest)["decisions"], {})
        # removed copies wait in a hidden folder until Review closes, then go
        ed.reject([e for e in entries if e.species == "Lion"][0])
        self.assertTrue(os.listdir(os.path.join(self.dest, sl.HOLD)))
        ed.close()
        self.assertFalse(os.path.exists(os.path.join(self.dest, sl.HOLD)))
        self.assertEqual(ed.undo(), "Nothing to undo")

    def test_rejecting_a_species_photo_keeps_the_species_if_seen_elsewhere(self):
        # a second, later lynx burst in Spain
        later = photo(os.path.join(self.root, "2019", "Spain", "Day 3", "DSC_0700.JPG"), "2019:03:16 09:00:00")
        self.results["DSC_0700.JPG"] = result("Iberian Lynx", 0.92, box=(0.1, 0.1, 0.2, 0.2))
        self.results["DSC_0100.NEF"] = self.results["DSC_0100.JPG"]
        _p, _pl, res = self._run()
        entries, clarify = sl.build(res["sightings"])
        sl.copy_list(entries, clarify, self.dest)
        ed = sl.ListEditor(entries, clarify, self.dest, trash=os.remove, sightings=res["sightings"])
        lynx = [e for e in entries if e.species == "Iberian Lynx"][0]
        self.assertEqual(len(lynx.sightings), 2)
        self.assertTrue(lynx.pick.pick.best)                       # the Best-folder burst first
        msg = ed.reject(lynx)
        self.assertIn("seen 1 more time", msg)
        self.assertIn(lynx, entries)                               # the species stays…
        self.assertEqual(lynx.pick.pick.path, later)               # …with the other photo
        self.assertIn(os.path.join("Spain", "Mammals", "Iberian Lynx (Lynx pardinus)_2019-03-16.JPG"),
                      self._files())
        self.assertTrue(os.path.exists(later))
        # that one's wrong too: nothing confident left, so the uncertain photos with
        # a lynx among their guesses go to To clarify
        msg = ed.reject(lynx)
        self.assertNotIn(lynx, entries)
        self.assertIn("isn't anywhere else", msg)                  # (the warbler was already there)
        # Identify again: only the photos you removed are left out
        _p, _pl, res2 = self._run(decisions=sl.load_list(self.dest)["decisions"])
        self.assertFalse(any(s.species == "Iberian Lynx" for s in res2["sightings"]))
        self.assertTrue(any(s.species == "Hoopoe" and s.iso == "ES" for s in res2["sightings"]))

    def test_recheck_finds_uncertain_photos_with_the_species_as_a_guess(self):
        self.results["DSC_0100.NEF"] = self.results["DSC_0100.JPG"]
        _p, _pl, res = self._run()
        entries, clarify = sl.build(res["sightings"], per_guess=0)        # none shown yet
        ed = sl.ListEditor(entries, clarify, self.dest, trash=os.remove, sightings=res["sightings"])
        lynx = [e for e in entries if e.species == "Iberian Lynx"][0]
        msg = ed.reject(lynx)
        # the uncertain warbler had Iberian Lynx as its second guess
        self.assertIn("1 uncertain photo", msg)
        self.assertEqual([c.sighting.pick.path for c in clarify], [self.warbler])

    def test_species_listed_twice_are_merged(self):
        from reviewer_species import merge_synonyms, synonyms
        labels = [("Great Spotted Woodpecker", "Dendrocopos major"),
                  ("Great Spotted Woodpecker", "Dendrocopus major"),
                  ("Middle Spotted Woodpecker", "Dendrocopos medius"),
                  ("Lesser Gray Shrike", "Lanius minor"), ("Black-backed Barbet", "Lybius minor"),
                  ("Northern Broken-dash", "Polites egeremet"), ("Northern Broken-dash", "Wallengrenia egeremet"),
                  ("Wallengrenia otho", "Wallengrenia otho"),
                  ("Paintbrush Swift", "Baoris farri"), ("Paintbrush Swift", "Baoris penicillata")]
        same = synonyms(labels)
        self.assertEqual(same, {"Dendrocopus major": "Dendrocopos major",
                                "Polites egeremet": "Wallengrenia egeremet"})
        top = merge_synonyms([["Great Spotted Woodpecker", "Dendrocopos major", 0.57],
                              ["Great Spotted Woodpecker", "Dendrocopus major", 0.24],
                              ["Middle Spotted Woodpecker", "Dendrocopos medius", 0.1]], same)
        self.assertEqual([(s, round(p, 2)) for _n, s, p in top],
                         [("Dendrocopos major", 0.81), ("Dendrocopos medius", 0.1)])

    def test_copy_carries_on_after_the_drive_was_unplugged(self):
        self.results["DSC_0100.NEF"] = self.results["DSC_0100.JPG"]
        _p, _pl, res = self._run()
        entries, clarify = sl.build(res["sightings"])
        sl.copy_list(entries, clarify, self.dest)
        # as if it stopped part-way: the last copies weren't recorded, one under an
        # earlier pick's date, and a half-copied .part file was left
        data = sl.load_list(self.dest)
        lion = [e for e in entries if e.species == "Lion"][0]
        lynx = [e for e in entries if e.species == "Iberian Lynx"][0]
        del data["species"]["KE|Panthera leo"]
        del data["species"]["ES|Lynx pardinus"]
        Path(self.dest, sl.LIST_FILE).write_text(json.dumps(data), encoding="utf-8")
        old = os.path.join(self.dest, lynx.dest)
        earlier = old.replace("_2019-03-14", "_2019-03-01")
        os.replace(old, earlier)
        part = os.path.join(self.dest, "Kenya", "Mammals", "Zebra (Equus quagga)_2018-07-02.JPG.part")
        Path(part).write_bytes(b"half")
        before = self._files()
        _p, _pl, res = self._run()
        entries, clarify = sl.build(res["sightings"])
        out = sl.copy_list(entries, clarify, self.dest)
        self.assertEqual(out["copied"], 1)                     # the lion: already there, recorded
        self.assertEqual(self._files(), before)                # nothing copied twice
        self.assertFalse(os.path.exists(part))                 # the .part tidied
        have = sl.load_list(self.dest)["species"]
        self.assertEqual(have["KE|Panthera leo"], lion.dest)
        self.assertEqual(have["ES|Lynx pardinus"], os.path.relpath(earlier, self.dest))

    def test_organised_by_type_species_and_year(self):
        self.results["DSC_0100.NEF"] = self.results["DSC_0100.JPG"]
        _p, _pl, res = self._run()
        entries, clarify = sl.build(res["sightings"])
        sl.write_lists(entries, clarify, self.dest)
        with open(os.path.join(self.dest, "All species.csv"), encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        # birds first (A–Z, then country), then mammals
        self.assertEqual([(r["Type"], r["Common name"], r["Country"]) for r in rows],
                         [("Birds", "Hoopoe", "Spain"), ("Birds", "Hoopoe", "Unknown country"),
                          ("Mammals", "Iberian Lynx", "Spain"), ("Mammals", "Lion", "Kenya")])
        self.assertEqual(rows[2]["Years"], "2019")
        with open(os.path.join(self.dest, "Species by year.csv"), encoding="utf-8-sig") as f:
            got = {r["Common name"]: r for r in csv.DictReader(f)}
        # (the Spanish hoopoe's NEF here has no capture date: it's dated by the file)
        self.assertEqual((got["Hoopoe"]["2017"], got["Hoopoe"]["Years"]), ("1", "2"))
        self.assertEqual((got["Lion"]["2018"], got["Lion"]["2019"]), ("1", ""))

    def test_inaturalist_reference_photos(self):
        import io
        import reviewer_inat as ri
        jpg = io.BytesIO()
        Image.new("RGB", (40, 30), (10, 200, 10)).save(jpg, "JPEG")
        sent = []

        def fake_get(url, limit=ri.MAX_BYTES):
            sent.append(url)
            if "/taxa?" in url:
                return json.dumps({"results": [{"id": 7, "name": "Upupa epops"}]}).encode()
            if url.endswith("/taxa/7"):
                return json.dumps({"results": [{"id": 7, "name": "Upupa epops", "preferred_common_name":
                                   "Eurasian Hoopoe", "taxon_photos": [
                                       {"photo": {"medium_url": "https://x/photos/1/medium.jpg",
                                                  "attribution": "(c) someone, CC BY"}},
                                       {"photo": {"medium_url": "http://insecure/2/medium.jpg"}}]}]}).encode()
            return jpg.getvalue()
        refs = ri.ReferencePhotos(os.path.join(self.user, "inat_photos"))
        with mock.patch.object(ri, "_get", side_effect=fake_get):
            got = refs.fetch("Upupa epops")
            self.assertEqual(got["name"], "Eurasian Hoopoe")
            self.assertEqual(got["url"], "https://www.inaturalist.org/taxa/7")
            self.assertEqual(len(got["photos"]), 1)                    # (https only)
            self.assertEqual(got["photos"][0]["attribution"], "(c) someone, CC BY")
            self.assertTrue(os.path.exists(got["photos"][0]["path"]))
            self.assertIn("https://x/photos/1/large.jpg", sent)       # (the larger size)
            # only the scientific name is sent
            self.assertIn("q=Upupa+epops", sent[0])
            n = len(sent)
            self.assertEqual(refs.fetch("Upupa epops")["photos"], got["photos"])
            self.assertEqual(len(sent), n)                             # (kept: not asked again)

    def test_country_lists_answer_to_both_spellings(self):
        import reviewer_regions
        import reviewer_species

        class Model:
            same_as = {"Dendrocopus major": "Dendrocopos major"}
        os.makedirs(os.path.join(self.user, "regions"))
        Path(self.user, "regions", "GB.json").write_text(json.dumps(
            {"species": {"Dendrocopus major": 900}, "names": {"Dendrocopus major": "Great Spotted Woodpecker"},
             "groups": ["birds"]}))
        with mock.patch.object(reviewer_species, "_model", Model()):
            region = reviewer_regions.RegionLists(Path(self.user, "regions")).get("GB")
        self.assertEqual(region["species"]["Dendrocopos major"], 900)
        self.assertEqual(region["names"]["Dendrocopos major"], "Great Spotted Woodpecker")
        self.assertGreater(reviewer_regions.region_weight("Dendrocopos major", "birds", region), 1.0)
        # (only in memory: the file is as downloaded)
        self.assertNotIn("Dendrocopos major", Path(self.user, "regions", "GB.json").read_text())

    def test_review_writes_the_workbook_when_it_closes(self):
        entries, clarify, ed = self._copied()
        book = os.path.join(self.dest, sl.WORKBOOK)
        before = os.path.getmtime(book)
        ed.assign(clarify[0], "Chiffchaff", "Phylloscopus collybita", "birds")
        self.assertEqual(os.path.getmtime(book), before)          # (not after every decision)
        with open(os.path.join(self.dest, "All species.csv"), encoding="utf-8-sig") as f:
            self.assertIn("Chiffchaff", f.read())                  # (the CSVs are)
        os.utime(book, (before - 100, before - 100))
        ed.close()
        self.assertGreater(os.path.getmtime(book), before - 100)

    def test_last_results_are_kept_for_review(self):
        _p, _pl, res = self._run()
        entries, clarify = sl.build(res["sightings"])
        self.assertIsNone(sl.load_results(self.user, self.root))
        sl.save_results(self.user, self.root, res, entries, clarify)
        got = sl.load_results(self.user, self.root)
        self.assertEqual(len(got["entries"]), len(entries))
        self.assertEqual(got["entries"][0].species, entries[0].species)
        # (the same photo objects in the sightings and the entries)
        self.assertTrue(any(s is got["entries"][0].pick for s in got["res"]["sightings"]))
        self.assertIsNone(sl.load_results(self.user, os.path.join(self.root, "2019")))

    def test_review_only_touches_the_list_folder(self):
        entries, clarify, ed = self._copied()
        outside = os.path.join(self.tmp, "elsewhere.JPG")
        Path(outside).write_bytes(b"x")
        ed._remove(outside)
        ed._remove(self.dest)
        self.assertTrue(os.path.exists(outside))
        self.assertTrue(os.path.isdir(self.dest))

    def test_finds_originals_only_and_places(self):
        photos = sl.find_photos(self.root, exclude=self.dest)
        names = sorted(os.path.basename(p.path) for p in photos)
        self.assertNotIn("tp-DSC_004.JPG", names)
        self.assertNotIn("lynx.JPG", names)
        self.assertNotIn("DSC_0100.JPG", names)          # the NEF of the pair is read instead
        self.assertIn("DSC_0100.NEF", names)
        places = {os.path.relpath(p.place, self.root) for p in photos}
        self.assertEqual(places, {os.path.join("2019", "Spain"), os.path.join("2018", "Holiday"),
                                  os.path.join("2017", "May")})

    def test_country_from_folder_name_and_xmp(self):
        photos = sl.find_photos(self.root)
        places = sl.guess_places(photos, self.root)
        got = {os.path.relpath(p, self.root): (i["iso"], i["how"]) for p, i in places.items()}
        self.assertEqual(got[os.path.join("2019", "Spain")], ("ES", "folder name"))
        self.assertEqual(got[os.path.join("2018", "Holiday")], ("KE", "XMP"))
        self.assertEqual(got[os.path.join("2017", "May")], (None, ""))

    def test_identify_bursts_duplicates_and_certainty(self):
        # (the hoopoe is the NEF of the pair here: give it the JPEG's answer)
        self.results["DSC_0100.NEF"] = self.results["DSC_0100.JPG"]
        _photos, _places, res = self._run()
        # the lynx burst is read once, from the Best copy; the named photo not at all
        self.assertEqual(self.calls.count("DSC_004.JPG"), 1)
        self.assertEqual(sum(c.startswith("DSC_00") and len(c) == 11 for c in self.calls), 1)
        self.assertNotIn("DSC_0900.JPG", self.calls)
        by = {(s.iso, s.species): s for s in res["sightings"]}
        lynx = by[("ES", "Iberian Lynx")]
        self.assertTrue(lynx.certain)
        self.assertTrue(lynx.pick.best)
        self.assertEqual(len(lynx.photos), 5)            # the Best copy counted once
        self.assertFalse(by[("ES", "Willow Warbler")].certain)
        self.assertTrue(by[("KE", "Lion")].certain)
        self.assertEqual(by[(None, "Hoopoe")].how, "yours")
        self.assertEqual(by[(None, "Hoopoe")].scientific, "Upupa epops")
        self.assertEqual(res["no_animal"], 1)

        entries, clarify = sl.build(res["sightings"])
        self.assertEqual(sorted((sl.country_name(e.iso), e.species) for e in entries),
                         [("Kenya", "Lion"), ("Spain", "Hoopoe"), ("Spain", "Iberian Lynx"),
                          (sl.UNKNOWN, "Hoopoe")])               # one per species per country
        self.assertEqual([c.sighting.species for c in clarify], ["Willow Warbler"])
        # an unsure hoopoe in Spain is still sent to clarify, though hoopoe is certain there
        import dataclasses
        hoopoe = by[("ES", "Hoopoe")]
        unsure = dataclasses.replace(hoopoe, certain=False, p=0.5)
        _e, clarify = sl.build(res["sightings"] + [unsure])
        self.assertEqual(sorted(c.sighting.species for c in clarify), ["Hoopoe", "Willow Warbler"])

    def test_copy_lists_and_rerun_keeps_choice(self):
        self.results["DSC_0100.NEF"] = self.results["DSC_0100.JPG"]
        _p, _pl, res = self._run()
        entries, clarify = sl.build(res["sightings"])
        before = {p: os.path.getsize(p) for p in [self.lynx[0], self.hoopoe, self.warbler]}
        out = sl.copy_list(entries, clarify, self.dest)
        self.assertEqual(out["copied"], 5)
        self.assertEqual(out["errors"], [])
        lynx = [e for e in entries if e.species == "Iberian Lynx"][0]
        self.assertEqual(lynx.dest, os.path.join("Spain", "Mammals",
                                                 "Iberian Lynx (Lynx pardinus)_2019-03-14.JPG"))
        self.assertTrue(os.path.exists(os.path.join(self.dest, lynx.dest)))
        c = clarify[0]
        self.assertTrue(c.dest.startswith(os.path.join(
            "Spain", "To clarify", "Willow Warbler (Phylloscopus trochilus) - possible")))
        self.assertIn("Willow Warbler 45%", c.dest)
        with open(os.path.join(self.dest, "All species.csv"), encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 4)
        self.assertTrue(os.path.exists(os.path.join(self.dest, "Spain", "Spain species.csv")))
        self.assertTrue(os.path.exists(os.path.join(self.dest, "To clarify.csv")))
        with open(os.path.join(self.dest, "Countries.csv"), encoding="utf-8-sig") as f:
            got = {r["Country"]: r for r in csv.DictReader(f)}
        self.assertEqual((got["Spain"]["Species"], got["Spain"]["Birds"], got["Spain"]["Mammals"],
                          got["Spain"]["To clarify"]), ("2", "1", "1", "1"))
        self.assertEqual(got["All countries"]["Species"], "3")         # Hoopoe in two countries
        with open(os.path.join(self.dest, "Species by country.csv"), encoding="utf-8-sig") as f:
            got = {r["Common name"]: r for r in csv.DictReader(f)}
        self.assertEqual(got["Hoopoe"]["Countries"], "2")
        self.assertEqual(got["Iberian Lynx"]["Spain"], "2019-03-14")
        self.assertEqual(got["Iberian Lynx"]["Kenya"], "")
        from openpyxl import load_workbook
        wb = load_workbook(os.path.join(self.dest, sl.WORKBOOK), read_only=True)
        self.addCleanup(wb.close)              # (before the folder goes, even if a check fails)
        self.assertEqual(wb.sheetnames[:5], ["Countries", "Species by country", "Species by year",
                                             "All species", "To clarify"])
        self.assertIn("Spain species", wb.sheetnames)
        rows = list(wb["Countries"].values)
        self.assertEqual(rows[0][:2], ("Country", "Species"))
        wb.close()
        self.assertEqual(before, {p: os.path.getsize(p) for p in before})       # originals untouched
        self.assertTrue(os.path.exists(self.lynx[0]))

        # again, everything is remembered: nothing read, nothing copied, same choice
        self.calls.clear()
        _p, _pl, res2 = self._run()
        self.assertEqual(self.calls, [])
        entries2, clarify2 = sl.build(res2["sightings"])
        out2 = sl.copy_list(entries2, clarify2, self.dest)
        self.assertEqual(out2["copied"], 0)
        self.assertEqual(out2["kept"], 4)

    def test_country_you_set_is_used(self):
        may = os.path.join(self.root, "2017", "May")
        _p, _pl, res = self._run({may: "PT"})
        named = [s for s in res["sightings"] if s.how == "yours"][0]
        self.assertEqual(named.iso, "PT")

    def test_not_recorded_in_country_needs_checking(self):
        ident = self._identifier()

        class Regions:
            def get(self, iso):
                return {"groups": ["birds", "mammals"],
                        "species": {"Upupa epops": 500, "Lynx pardinus": 9, "Phylloscopus collybita": 80}}

            def fetching(self, iso):
                return False
        ident.ranker._regions = Regions()
        ranked = ident.ranker.rank(result("Lion", 0.99)["top"], "ES")
        self.assertFalse(ident.certain(ranked, "ES"))           # no lions recorded in Spain
        ranked = ident.ranker.rank(result("Hoopoe", 0.9)["top"], "ES")
        self.assertTrue(ident.certain(ranked, "ES"))
        ranked = ident.ranker.rank(result("Iberian Lynx", 0.99)["top"], "ES")
        self.assertFalse(ident.certain(ranked, "ES"))           # 9 records: a rarity there

    def test_country_names_and_odd_ai_names(self):
        self.assertTrue(sl.odd_name("Elk", "Red Deer"))
        self.assertFalse(sl.odd_name("Spanish Lynx", "Iberian Lynx"))
        self.assertFalse(sl.odd_name("Brant Goose", "Brent Goose"))
        self.assertEqual(sl._name("Blue Rock Thrush", "Monticola solitarius", "ES", "AI",
                                  lambda sc, iso: "Blue Rock-Thrush"), ("Blue Rock-Thrush", "", ""))
        self.results["DSC_0100.NEF"] = self.results["DSC_0100.JPG"]
        _p, _pl, res = self._run()
        local = {("Lynx pardinus", "ES"): "Iberian Lynx", ("Upupa epops", "ES"): "Eurasian Hoopoe",
                 ("Panthera leo", "KE"): "Simba", ("Phylloscopus trochilus", "ES"): "Willow Warbler"}
        entries, clarify = sl.build(res["sightings"], name_of=lambda sc, iso: local.get((sc, iso)))
        by = {(sl.country_name(e.iso), e.scientific): e for e in entries}
        lynx = by[("Spain", "Lynx pardinus")]
        self.assertEqual((lynx.species, lynx.name_note), ("Iberian Lynx", ""))       # same name
        hoopoe = by[("Spain", "Upupa epops")]
        self.assertEqual((hoopoe.species, hoopoe.name_note), ("Eurasian Hoopoe", "AI: Hoopoe"))
        lion = by[("Kenya", "Panthera leo")]
        self.assertEqual((lion.species, lion.ai_name), ("Simba", "Lion"))
        self.assertTrue(lion.name_note.startswith("⚠"))
        self.assertEqual(sl.name_entry(lion), os.path.join("Kenya", "Mammals", "Simba (Panthera leo)_2018-07-01.JPG"))
        named = by[(sl.UNKNOWN, "Upupa epops")]
        self.assertEqual((named.species, named.name_note), ("Hoopoe", ""))            # your name: kept
        self.assertEqual(clarify[0].name, "Willow Warbler")

    def test_photos_per_guess(self):
        self.results["DSC_0300.JPG"] = result("Willow Warbler", 0.4)
        self.results["DSC_0500.JPG"] = result("Willow Warbler", 0.4)
        _p, _pl, res = self._run({os.path.join(self.root, "2018", "Holiday"): "ES"})
        for per_guess, n in ((1, 1), (3, 3), (None, 3)):
            _e, clarify = sl.build(res["sightings"], per_guess)
            self.assertEqual(len([c for c in clarify if c.name == "Willow Warbler"]), n)
        _e, clarify = sl.build(res["sightings"], 2)
        self.assertEqual(len({c.sighting.date for c in clarify}), 2)        # different days first

    def test_list_is_saved_inside_the_chosen_folder_and_not_read(self):
        dest = sl.list_folder(self.root)
        self.assertEqual(dest, os.path.join(self.root, "Species list"))
        photo(os.path.join(dest, "Spain", "Birds", "Hoopoe (Upupa epops)_2019-03-15.JPG"), "2019:03:15 09:00:00")
        names = [os.path.basename(p.path) for p in sl.find_photos(self.root, exclude=dest)]
        self.assertNotIn("Hoopoe (Upupa epops)_2019-03-15.JPG", names)

    def test_country_set_on_a_parent_folder_is_inherited(self):
        R = self.root
        spain, holiday, may = (os.path.join(R, "2019", "Spain"), os.path.join(R, "2018", "Holiday"),
                               os.path.join(R, "2017", "May"))
        photos = sl.find_photos(R)
        guess = sl.guess_places(photos, R)

        def country(place, saved):
            return sl.effective_country(place, R, guess[place], saved)
        # nothing set: Spain from its name, Holiday from XMP, May unknown
        self.assertEqual(country(spain, {}), ("ES", "folder name"))
        self.assertEqual(country(holiday, {}), ("KE", "XMP"))
        self.assertEqual(country(may, {}), (None, ""))
        # GB on the top folder: fills only folders with no country of their own
        top = {R: "GB"}
        self.assertEqual(country(may, top), ("GB", "you, on Pictures"))
        self.assertEqual(country(spain, top), ("ES", "folder name"))       # named: kept
        self.assertEqual(country(holiday, top), ("KE", "XMP"))             # XMP: kept
        # the nearest folder you set wins, and your setting on a folder itself beats everything
        self.assertEqual(country(may, {R: "GB", os.path.join(R, "2017"): "IE"})[0], "IE")
        self.assertEqual(country(spain, {R: "GB", spain: "PT"}), ("PT", "you"))
        self.assertEqual(country(may, {R: "GB", may: ""}), (None, "you: not known"))

    def test_stop_part_way_then_carry_on(self):
        import threading
        photos = sl.find_photos(self.root)
        cs = {p: i["iso"] for p, i in sl.guess_places(photos, self.root).items()}
        stop = threading.Event()
        stop.set()
        res = self._identifier().run(photos, cs, self.root, progress=lambda d: None, stop=stop)
        self.assertLess(res["done"], res["bursts"])
        res = self._identifier().run(photos, cs, self.root, progress=lambda d: None)
        self.assertEqual(res["done"], res["bursts"])


if __name__ == "__main__":
    unittest.main()
