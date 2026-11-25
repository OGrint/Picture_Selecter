"""
Region hint for species suggestions.

Each folder can have a country. The species recorded in that country
(from iNaturalist's research-grade observations, by group: birds, mammals,
reptiles, amphibians, dragonflies, butterflies) are downloaded once and
cached; species suggestions then favour species known from that country and
push down ones that have never been recorded there.

Only the country and the taxon group are sent to iNaturalist — nothing
about you or your photos. Without a connection (or a region) suggestions are
worldwide, as before.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("photo_reviewer")

COUNTRIES_FILE = "regions_countries.json"
API = "https://api.inaturalist.org/v1/observations/species_counts"
USER_AGENT = "PhotoReviewer/1.0"
MAX_AGE_DAYS = 365
# iNaturalist taxon ids for the groups the species model covers
GROUPS = {"birds": 3, "mammals": 40151, "reptiles": 26036, "amphibians": 20978,
          "dragonflies": 47792, "butterflies": 47224}
IN_REGION = 3.0          # weight for species recorded in the country
NOT_IN_REGION = 0.15     # covered group, never recorded there (genus unknown too)
# Out of range, by type: birds turn up as vagrants, insects migrate; mammals,
# reptiles and amphibians almost never do (and a "same genus" relative recorded
# there is far likelier: Red Deer, not Elk, in the UK).
NOT_IN_REGION_BY_GROUP = {"birds": 0.3, "mammals": 0.02, "reptiles": 0.04, "amphibians": 0.04}
SAME_GENUS_BY_GROUP = {"mammals": 0.25, "reptiles": 0.5, "amphibians": 0.5}
VAGRANT_GROUPS = {"birds", "dragonflies", "butterflies"}

NAMES_ISO = "GB"         # common names: British English, whichever country (asking for the
                         # country's own gives Spanish names in Spain: "Buitre leonado")
ALIASES = {"uk": "GB", "england": "GB", "scotland": "GB", "wales": "GB", "britain": "GB",
           "usa": "US", "america": "US", "uae": "AE", "holland": "NL"}


def _data_dirs() -> list[Path]:
    here = Path(__file__).resolve().parent
    exe = Path(sys.executable).parent if getattr(sys, "frozen", False) else here
    return [exe / "models", here / "models"]


_countries: Optional[list[dict]] = None


def countries() -> list[dict]:
    """[{iso, name, place_id}, …] sorted by name ([] if the table is missing)."""
    global _countries
    if _countries is None:
        p = next((d / COUNTRIES_FILE for d in _data_dirs() if (d / COUNTRIES_FILE).exists()), None)
        try:
            rows = json.loads(p.read_text(encoding="utf-8"))["countries"] if p else []
        except (OSError, ValueError, KeyError):
            rows = []
        _countries = sorted(rows, key=lambda c: c["name"])
    return _countries


def by_iso(iso: Optional[str]) -> Optional[dict]:
    return next((c for c in countries() if c["iso"] == iso), None) if iso else None


def by_name(name: str) -> Optional[dict]:
    low = name.strip().lower()
    return next((c for c in countries() if c["name"].lower() == low), None) or \
        next((c for c in countries() if c["name"].lower().startswith(low)), None) if low else None


def guess_country(label: str) -> Optional[str]:
    """Country ISO code named in a folder label ('2026-06-02 Kenya' → 'KE')."""
    words = re.findall(r"[a-z]+", label.lower())
    text = " " + " ".join(words) + " "
    for w in words:
        if w in ALIASES:
            return ALIASES[w]
    for c in sorted(countries(), key=lambda c: -len(c["name"])):
        name = " " + " ".join(re.findall(r"[a-z]+", c["name"].lower())) + " "
        if len(name) > 3 and name in text:
            return c["iso"]
    return None


class RegionLists:
    """Downloaded species lists per country, cached under the user folder."""

    def __init__(self, cache_dir: Path):
        self.dir = cache_dir
        self._mem: dict[str, dict] = {}
        self._fetching: set[str] = set()
        self._lock = threading.Lock()

    def _path(self, iso: str) -> Path:
        return self.dir / f"{iso}.json"

    def get(self, iso: str) -> Optional[dict]:
        """{'species': {scientific: count}, 'groups': [...]} or None if not downloaded."""
        if iso in self._mem:
            return _both_spellings(self._mem[iso])
        try:
            data = json.loads(self._path(iso).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        self._mem[iso] = data
        return _both_spellings(data)

    def stale(self, iso: str) -> bool:
        d = self.get(iso)
        return d is None or time.time() - d.get("fetched", 0) > MAX_AGE_DAYS * 86400

    def fetching(self, iso: str) -> bool:
        return iso in self._fetching

    def fetch(self, iso: str, done: Callable[[str, Optional[str]], None]) -> None:
        """Download in the background; done(iso, error or None) is called from the thread."""
        c = by_iso(iso)
        if c is None:
            done(iso, "unknown country")
            return
        with self._lock:
            if iso in self._fetching:
                return
            self._fetching.add(iso)
        threading.Thread(target=self._fetch, args=(c, done), daemon=True).start()

    def _fetch(self, c: dict, done) -> None:
        iso, err = c["iso"], None
        species: dict[str, int] = {}
        names: dict[str, str] = {}               # British English common names
        name_place = (by_iso(NAMES_ISO) or c)["place_id"]
        groups: list[str] = []
        try:
            for group, taxon in GROUPS.items():
                page = 1
                while page <= 20:
                    q = urllib.parse.urlencode({"place_id": c["place_id"], "taxon_id": taxon,
                                                "verifiable": "true", "rank": "species",
                                                "per_page": 500, "page": page,
                                                "locale": "en", "preferred_place_id": name_place})
                    req = urllib.request.Request(f"{API}?{q}", headers={"User-Agent": USER_AGENT})
                    with urllib.request.urlopen(req, timeout=30) as r:
                        data = json.loads(r.read())
                    for row in data.get("results", []):
                        species[row["taxon"]["name"]] = int(row.get("count", 1))
                        if row["taxon"].get("preferred_common_name"):
                            names[row["taxon"]["name"]] = row["taxon"]["preferred_common_name"]
                    if page * 500 >= data.get("total_results", 0):
                        break
                    page += 1
                    time.sleep(1.0)                 # be gentle with the public API
                groups.append(group)
                time.sleep(1.0)
            out = {"iso": iso, "name": c["name"], "fetched": time.time(), "names": names,
                   "names_from": NAMES_ISO,
                   "groups": groups, "species": species}
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self._path(iso).with_suffix(".tmp")
            tmp.write_text(json.dumps(out), encoding="utf-8")
            os.replace(tmp, self._path(iso))
            self._mem[iso] = out
            log.info("Region list for %s: %d species", c["name"], len(species))
        except Exception as e:
            err = str(e)
            log.warning("Could not download species list for %s: %s", c["name"], e)
        finally:
            self._fetching.discard(iso)
        done(iso, err)


def group_of(taxonomy: list[str]) -> Optional[str]:
    """Group name for a BioCLIP label's taxonomy [class, order, family]."""
    cls, order, family = (taxonomy + ["", "", ""])[:3]
    if cls == "Aves":
        return "birds"
    if cls == "Mammalia":
        return "mammals"
    if cls in ("Squamata", "Testudines", "Crocodylia", "Sphenodontia", "Reptilia"):
        return "reptiles"
    if cls == "Amphibia":
        return "amphibians"
    if order == "Odonata":
        return "dragonflies"
    if order == "Lepidoptera":
        return "butterflies"
    return None


def _both_spellings(data: dict) -> dict:
    """A few species are in the AI's list twice (Dendrocopos / Dendrocopus major)
    and only the kept spelling is used; iNaturalist may use the other. The
    country's list answers to both (in memory only)."""
    if not isinstance(data, dict) or data.get("_both"):
        return data
    try:
        import reviewer_species
        model = reviewer_species._model          # (only once loaded: never loads it)
    except Exception:
        model = None
    same_as = getattr(model, "same_as", None)
    if not same_as:
        return data
    for k in ("species", "names"):
        d = data.get(k)
        if isinstance(d, dict):
            for other, kept in same_as.items():
                if other in d and kept not in d:
                    d[kept] = d[other]
    data["_both"] = True
    return data


def region_weight(scientific: str, group: Optional[str], region: Optional[dict]) -> float:
    """How much a region changes a species suggestion's weight."""
    if not region or group not in region.get("groups", []):
        return 1.0
    species = region["species"]
    if scientific in species:
        # well-recorded species get a little more
        return IN_REGION * (1 + min(1.0, math.log10(species[scientific] + 1) / 4))
    genus = scientific.split(" ")[0]
    if any(s.startswith(genus + " ") for s in species):
        # taxonomies differ (splits/lumps): don't hide it (unless it's a mammal & co.)
        return SAME_GENUS_BY_GROUP.get(group, 1.0)
    return NOT_IN_REGION_BY_GROUP.get(group, NOT_IN_REGION)


def out_of_range(group: Optional[str]) -> str:
    """How to read 'not recorded in X' for a type of animal."""
    return "a vagrant?" if group in VAGRANT_GROUPS else "very unlikely there"
