"""Tests for the region hint (no network needed)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reviewer_regions as rr  # noqa: E402


class WeightTests(unittest.TestCase):
    REGION = {"groups": ["birds", "mammals"],
              "species": {"Ardea cinerea": 5000, "Ardea alba": 300, "Vulpes vulpes": 900}}

    def test_recorded_species_boosted(self):
        self.assertGreater(rr.region_weight("Ardea cinerea", "birds", self.REGION), 3.0)

    def test_unrecorded_species_pushed_down(self):
        self.assertEqual(rr.region_weight("Ara macao", "birds", self.REGION), rr.NOT_IN_REGION)

    def test_same_genus_kept_neutral(self):          # taxonomy splits / lumps
        self.assertEqual(rr.region_weight("Ardea herodias", "birds", self.REGION), 1.0)

    def test_group_not_downloaded_or_no_region(self):
        self.assertEqual(rr.region_weight("Anax imperator", "dragonflies", self.REGION), 1.0)
        self.assertEqual(rr.region_weight("Ara macao", "birds", None), 1.0)

    def test_groups(self):
        self.assertEqual(rr.group_of(["Aves", "Pelecaniformes", "Ardeidae"]), "birds")
        self.assertEqual(rr.group_of(["Squamata", "", ""]), "reptiles")
        self.assertEqual(rr.group_of(["Insecta", "Odonata", "Aeshnidae"]), "dragonflies")
        self.assertEqual(rr.group_of(["Insecta", "Lepidoptera", "Nymphalidae"]), "butterflies")
        self.assertIsNone(rr.group_of(["Insecta", "Coleoptera", "Carabidae"]))


@unittest.skipUnless(rr.countries(), "country table not installed")
class CountryTests(unittest.TestCase):
    def test_guess_from_folder_label(self):
        self.assertEqual(rr.guess_country("2026-06-02 Kenya"), "KE")
        self.assertEqual(rr.guess_country("2026-05-12 Minsmere UK"), "GB")
        self.assertEqual(rr.guess_country("2026-03-01 Costa Rica day 2"), "CR")
        self.assertIsNone(rr.guess_country("2026-05-12 Minsmere"))

    def test_lookup(self):
        self.assertEqual(rr.by_name("kenya")["iso"], "KE")
        self.assertEqual(rr.by_iso("KE")["name"], "Kenya")


if __name__ == "__main__":
    unittest.main()
