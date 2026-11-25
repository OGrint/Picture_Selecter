"""
Species list from a photo archive (Photo Reviewer).

Goes through every photo under a parent folder, works out which species are in
them, and copies one photo of each species per country into

    <list>/<Country>/<Group>/<Common name> (<Scientific>)_<date>.<ext>
    <list>/<Country>/To clarify/<best guess> - possible/<date>_<file> (guesses).<ext>
    <list>/<Country>/<Country> species.csv
    <list>/All species.csv, <list>/To clarify.csv

Rules
- Species you named in the app (the folders' session files, or the app's
  Species_Date_… file names) are certain.
- Everything else: BioCLIP on the animal MegaDetector finds, re-ranked by the
  country's species list and the species you've confirmed before (as in the
  app). Certain = at least `sure` probable AND recorded in that country (when
  the country's list is downloaded and covers the group). Anything less goes to
  To clarify, filed under its best guess with the top three in the file name.
- A burst (frames in one place at most 2 s apart) is one sighting: one frame is
  identified (a frame in a Best folder first) and more only while it stays
  uncertain. A mixed flock in one burst gives only its clearest species.
- The same photo in two places (a shoot folder and its Best copy) is read once.
- Frames with no animal (nothing found and the species model unsure) are skipped.
- Country: named in the folders (Spain, Kenya…), else an IPTC country in the
  XMP sidecars (darktable / Lightroom), else you choose it. The D850 records no
  location, so most come from folder names.
- Copies only. Re-running keeps the photo already chosen for a species, and
  everything read is remembered (species_scan.jsonl), so a stopped run carries on.
- Review (ListEditor): pick a guess, type your own name or remove a photo. The
  copies in the list folder are moved / merged / removed to match, and the
  decision is remembered (species_list.json "decisions"), so Identify again
  keeps it: a named photo counts as yours, a removed one is left out.
- Learning (reviewer_refine): every photo you name or accept in Review is
  also an example for the species model. Identify fits a soft pull towards
  your examples (tested on photos held out; off if it doesn't help), applies
  it wherever a photo's fingerprint is known, and re-reads the uncertain
  photos read before fingerprints were kept.
No Tkinter here.
"""

from __future__ import annotations

import csv
import datetime
import json
import os
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from reviewer_collect import (FINAL_DIRS, MONTHS, SpeciesLookup, _is_generic, _key, _times,
                              _within, clean, is_best_dir, norm_name)
from reviewer_imaging import BITMAP_EXTS, RAW_EXTS
from reviewer_import import _copy_verified
from reviewer_regions import by_iso, by_name, guess_country

PHOTO_EXTS = set(RAW_EXTS) | set(BITMAP_EXTS)
SKIP_DIRS = FINAL_DIRS | {"tp", "topaz"}          # edits: their originals are read instead
BURST_GAP = 2.0          # seconds between frames of one burst
SURE = 0.80              # default: this probable (after re-ranking) = certain
MAX_TRIES = 3            # frames of a burst identified while it stays uncertain
CLARIFY_PER_GUESS = 7    # To clarify: examples per best guess per country
RARE_RECORDS = 25        # fewer iNaturalist records than this in a country = a rarity there
MY_EXAMPLES = 6          # your own IDs (main window, file names) used as examples per species
                         # (Beluga in Scotland: 9) — checked by you, not listed as certain;
                         # lowered to half the country's median for sparsely recorded countries
LIST_FILE = "species_list.json"
CACHE_FILE = "species_scan.jsonl"
GROUP_FOLDERS = {"birds": "Birds", "mammals": "Mammals", "reptiles": "Reptiles",
                 "amphibians": "Amphibians", "dragonflies": "Dragonflies",
                 "butterflies": "Butterflies and moths", None: "Other animals"}
UNKNOWN = "Unknown country"
OUT_FOLDER = "Species list"          # made inside the folder you choose
_PLAIN = {"common", "eurasian", "european", "western", "eastern", "northern", "southern", "great",
          "greater", "lesser", "little", "the", "of", "and"}


def list_folder(parent: str) -> str:
    return os.path.join(parent, OUT_FOLDER)


def odd_name(ai: str, local: str) -> bool:
    """The model's common name has nothing in common with the country's
    ('Elk' / 'Red Deer'), as opposed to a variant ('Spanish' / 'Iberian Lynx')."""
    words = lambda s: set(re.findall(r"[a-z]+", s.lower())) - _PLAIN      # noqa: E731
    a, b = words(ai), words(local)
    return bool(a and b) and not (a & b)


def country_name(iso: Optional[str]) -> str:
    c = by_iso(iso) if iso else None
    return c["name"] if c else UNKNOWN


# ── finding the photos ───────────────────────────────────────────────────────

@dataclass(eq=False)                   # (compared and hashed as itself)
class Photo:
    path: str
    size: int
    mtime: float
    place: str                    # the folder whose country applies
    best: bool                    # in a Best folder (your pick: preferred)
    ts: float = 0.0
    also: list[str] = field(default_factory=list)       # the same file elsewhere

    @property
    def key(self) -> str:
        return f"{_key(self.path)}|{self.size}|{int(self.mtime)}"


def _skip_dir(name: str) -> bool:
    n = norm_name(name)
    return n in SKIP_DIRS or n.startswith((".", "$")) or n == "system volume information"


def place_of(folder: str, parent: str) -> str:
    """
    The folder a country is set for: the first folder on the way down whose
    name is a country ('Holidays/Kenya 2019/Day 3/Best' → '…/Kenya 2019'), else
    the trip folder (Best and day-number folders dropped: '26/May/2/Best' → '26/May').
    """
    rel = [p for p in Path(os.path.relpath(folder, parent)).parts if p not in (".", "")]
    for i, part in enumerate(rel):
        if guess_country(part):
            return os.path.join(parent, *rel[:i + 1])
    while rel and (is_best_dir(rel[-1]) or (_is_generic(rel[-1]) and norm_name(rel[-1]) not in MONTHS)):
        rel.pop()
    return os.path.join(parent, *rel) if rel else parent


def find_photos(parent: str, exclude: Optional[str] = None, stop: Optional[threading.Event] = None,
                progress: Optional[Callable[[str], None]] = None) -> list[Photo]:
    """Every original photo under parent (edits, tp-… files and the JPEG of a
    RAW+JPEG pair left out)."""
    out: list[Photo] = []
    places: dict[str, str] = {}
    for root, dirs, files in os.walk(parent):
        if stop is not None and stop.is_set():
            break
        if exclude and _within(root, exclude):
            dirs[:] = []
            continue
        dirs[:] = sorted(d for d in dirs if not _skip_dir(d)
                         and not (exclude and _within(os.path.join(root, d), exclude)))
        raws = {os.path.splitext(f)[0].lower() for f in files
                if os.path.splitext(f)[1].lower() in RAW_EXTS}
        place = places.setdefault(root, place_of(root, parent))
        best = any(is_best_dir(p) for p in Path(os.path.relpath(root, parent)).parts)
        for f in sorted(files):
            stem, ext = os.path.splitext(f)
            ext = ext.lower()
            if ext not in PHOTO_EXTS or f.lower().startswith("tp-"):
                continue
            if ext not in RAW_EXTS and stem.lower() in raws:
                continue
            p = os.path.join(root, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out.append(Photo(p, st.st_size, st.st_mtime, place, best))
        if progress:
            progress(f"Looking for photos… {len(out):,} so far ({os.path.relpath(root, parent)})")
    return out


_XMP_COUNTRY = re.compile(r'(?:photoshop:Country|Iptc4xmpCore:CountryCode)(?:="|>)([^"<]+)')


def _xmp_country(paths: list[str]) -> Optional[str]:
    """A country written into XMP sidecars (darktable / Lightroom location fields)."""
    for p in paths:
        for s in (os.path.splitext(p)[0] + ".xmp", p + ".xmp"):
            try:
                text = Path(s).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for m in _XMP_COUNTRY.finditer(text):
                v = m.group(1).strip()
                c = by_iso(v.upper()) if len(v) == 2 else by_name(v)
                if c:
                    return c["iso"]
    return None


def guess_places(photos: list[Photo], parent: str) -> dict[str, dict]:
    """place → {photos, iso (or None), how: 'folder name' | 'XMP' | ''}."""
    by_place: dict[str, list[Photo]] = defaultdict(list)
    for ph in photos:
        by_place[ph.place].append(ph)
    out = {}
    for place, phs in sorted(by_place.items()):
        rel = os.path.relpath(place, parent)
        iso, how = guess_country(rel if rel != "." else ""), "folder name"
        if not iso:
            step = max(1, len(phs) // 40)
            iso, how = _xmp_country([p.path for p in phs[::step]]), "XMP"
        out[place] = {"photos": len(phs), "iso": iso, "how": how if iso else ""}
    return out


def effective_country(place: str, parent: str, guess: dict, saved: dict) -> tuple[Optional[str], str]:
    """
    The country used for a place: (iso or None, where it came from).
    saved = {folder: iso ('' = not known)} set by you, on the place itself or
    on any folder above it (up to parent), which then applies to everything
    under it. Most specific first: your setting on the place, then its own
    folder name / XMP (guess), then the nearest folder above it you set.
    """
    norm = {_key(k): v for k, v in saved.items()}
    k = _key(place)
    if k in norm:
        return (norm[k] or None), ("you" if norm[k] else "you: not known")
    if guess.get("iso"):
        return guess["iso"], guess.get("how") or ""
    folder = place
    top = _key(parent)
    while _key(folder) != top:
        up = os.path.dirname(folder)
        if up == folder:
            break
        folder = up
        if _key(folder) in norm:
            v = norm[_key(folder)]
            return (v or None), (f"you, on {os.path.basename(folder) or folder}" if v else "you: not known")
    return None, ""


# ── remembering what was read ────────────────────────────────────────────────

class ScanCache:
    """Capture times and analyses, one JSON line each (appended, so a stopped
    or crashed run keeps everything read so far)."""

    def __init__(self, path: str):
        self.path = path
        self.data: dict[str, dict] = {}
        self.lock = threading.Lock()
        self._f = None
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                        self.data.setdefault(rec.pop("k"), {}).update(rec)
                    except (ValueError, KeyError, AttributeError):
                        continue                      # (a half-written last line)
        except OSError:
            pass

    def get(self, key: str) -> dict:
        return self.data.get(key, {})

    def put(self, key: str, **rec) -> None:
        with self.lock:
            self.data.setdefault(key, {}).update(rec)
            try:
                if self._f is None:
                    os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                    self._f = open(self.path, "a", encoding="utf-8")
                self._f.write(json.dumps({"k": key, **rec}) + "\n")
                self._f.flush()
            except OSError:
                pass

    def close(self) -> None:
        with self.lock:
            if self._f is not None:
                self._f.close()
                self._f = None


# ── identifying ──────────────────────────────────────────────────────────────

def analyse(path: str) -> dict:
    """Animal box and the species model's raw top 10 for one photo."""
    from reviewer_detect import OTHER_CONF, get_detector, main_animal, refine
    from reviewer_imaging import load_preview
    from reviewer_species import get_species_model
    model = get_species_model()
    if model is None:
        raise RuntimeError("the species model isn't installed")
    img = load_preview(path, 1600)
    img.load()
    det = get_detector()
    dets = refine(det.detect(img), img, model) if det is not None else []
    a = main_animal(dets)
    box = [round(float(v), 4) for v in a[2:6]] if a and len(a) >= 6 else None
    from reviewer_refine import pack
    feat = model.features(img, box)
    top = [[n, s, round(float(p), 4)] for n, s, p in model.top(feat, k=10)]
    return {"top": top, "box": box, "conf": round(float(a[1]), 3) if a else 0.0,
            "other": any(d[0] in (1, 2) and d[1] >= OTHER_CONF for d in dets),
            "f": pack(feat)}                  # (the fingerprint, for learning from you)


def has_animal(a: dict) -> bool:
    from reviewer_detect import SPECIES_PRESENT
    top = a.get("top") or []
    if a.get("conf", 0) > 0:
        return True
    return bool(top) and top[0][2] >= SPECIES_PRESENT and not top[0][1].startswith("Homo ")


@dataclass
class Sighting:
    place: str
    iso: Optional[str]
    photos: list[Photo]                     # the burst
    pick: Photo                             # the frame the species comes from
    species: str
    scientific: str = ""
    group: Optional[str] = None
    p: float = 1.0
    how: str = "AI"                         # yours | file name | AI
    certain: bool = False
    guesses: list = field(default_factory=list)      # [(name, scientific, p)] top 3
    box: Optional[list] = None
    learned: str = ""                       # how your examples changed it, e.g. "52% → 81%"
    note: str = ""                          # your reason / note from Review

    @property
    def date(self) -> str:
        t = self.pick.ts
        if not t or datetime.datetime.fromtimestamp(t).year < 2005:
            return "date unknown"
        return f"{datetime.datetime.fromtimestamp(t):%Y-%m-%d}"

    @property
    def quality(self) -> float:
        """How good a photo of the species this is (choosing the one to keep)."""
        q = self.p
        if self.box:
            q += min((self.box[2] - self.box[0]) * (self.box[3] - self.box[1]), 0.25) / 0.25
        if self.pick.best:
            q += 1.5
        if self.how != "AI":
            q += 1.0
        return q


def bursts(photos: list[Photo]) -> list[list[Photo]]:
    by_place: dict[str, list[Photo]] = defaultdict(list)
    for ph in photos:
        by_place[ph.place].append(ph)
    out = []
    for phs in by_place.values():
        phs.sort(key=lambda p: (p.ts, p.path))
        cur = [phs[0]]
        for ph in phs[1:]:
            if ph.ts - cur[-1].ts <= BURST_GAP:
                cur.append(ph)
            else:
                out.append(cur)
                cur = [ph]
        out.append(cur)
    return out


def dedupe(photos: list[Photo]) -> list[Photo]:
    """The same file in two places (size + capture time + type) is read once;
    the copy in a Best folder represents it."""
    groups: dict[tuple, list[Photo]] = defaultdict(list)
    for ph in photos:
        groups[(ph.size, round(ph.ts, 2), os.path.splitext(ph.path)[1].lower())].append(ph)
    out = []
    for g in groups.values():
        g.sort(key=lambda p: (not p.best, p.path))
        g[0].also = [p.path for p in g[1:]]
        out.append(g[0])
    return out


def _order(burst: list[Photo]) -> list[Photo]:
    """Frames to try: your Best picks first, then the middle, first and last."""
    picks = [p for p in burst if p.best]
    picks += [burst[len(burst) // 2], burst[0], burst[-1]]
    return list(dict.fromkeys(picks))[:MAX_TRIES]


class Identifier:
    """Works out the sightings in a list of photos (see the module notes)."""

    def __init__(self, user_dir: str, sure: float = SURE):
        from reviewer_collect import SpeciesIdentifier
        self.sure = sure
        self.cache = ScanCache(os.path.join(user_dir, CACHE_FILE))
        self.ranker = SpeciesIdentifier(os.path.join(user_dir, "species_list_unused.json"), user_dir)
        self._names: Optional[dict] = None
        self._rare_cache: dict = {}
        from reviewer_refine import EXAMPLES_FILE, Examples
        self.examples = Examples(os.path.join(user_dir, EXAMPLES_FILE))
        self.refiner = None
        self._history = None
        try:
            from reviewer_species import SpeciesHistory
            self._history = SpeciesHistory(Path(user_dir) / "species_history.json")
        except Exception:
            pass

    @staticmethod
    def _group(sci: str) -> Optional[str]:
        from reviewer_species import get_species_model
        model = get_species_model()
        return model.group.get(sci) if (model is not None and sci) else None

    @staticmethod
    def canon(sci: str) -> str:
        """The spelling used for a species listed twice in the model (Dendrocopus → Dendrocopos major)."""
        from reviewer_species import get_species_model
        model = get_species_model()
        return (getattr(model, "same_as", None) or {}).get(sci, sci) if sci else sci

    def resolve(self, text: str, iso: Optional[str]) -> tuple[str, str, Optional[str]]:
        """(name, scientific, group) for a name you typed (see _resolve)."""
        name, sci, group = self._resolve(text, iso)
        c = self.canon(sci)
        if c != sci:
            return (self.local_name(c, iso) or name), c, self._group(c)
        return name, sci, group

    def _resolve(self, text: str, iso: Optional[str]) -> tuple[str, str, Optional[str]]:
        """(name, scientific, group) for a name you typed: 'Name (Scientific)', the
        country's own common name, a scientific name or the model's name."""
        text = text.strip()
        m = re.fullmatch(r"(.*?)\s*\(([A-Z][a-z]+(?: [a-z-]+){1,2})\)", text)
        if m:
            sci = m.group(2)
            return (self.local_name(sci, iso) or m.group(1).strip() or sci), sci, self._group(sci)
        low = text.lower()
        region = self.ranker.region(iso) if iso else None
        for sci, n in (region or {}).get("names", {}).items():
            if n.lower() == low:
                return n, sci, self._group(sci)
        from reviewer_species import get_species_model
        model = get_species_model()
        if model is not None:
            for n, sci in model.labels:
                if sci.lower() == low:
                    return (self.local_name(sci, iso) or n), sci, self._group(sci)
        sci, group = self._lookup_name(text)
        return (self.local_name(sci, iso) or text) if sci else text, sci, group

    def names(self, iso: Optional[str]) -> list[str]:
        """'Name (Scientific)' for every species: the country's own names first."""
        region = self.ranker.region(iso) if iso else None
        local = (region or {}).get("names", {})
        out = [f"{n} ({sci})" for sci, n in sorted(local.items(), key=lambda kv: kv[1].lower())]
        from reviewer_species import get_species_model
        model = get_species_model()
        if model is not None:
            out += [f"{n} ({sci})" for n, sci in model.labels if sci not in local]
        return out

    # names you gave → scientific name and group
    def _lookup_name(self, name: str) -> tuple[str, Optional[str]]:
        from reviewer_species import get_species_model
        model = get_species_model()
        if self._names is None:
            self._names = {n.lower(): s for n, s in model.labels} if model is not None else {}
        sci = self._names.get(name.strip().lower(), "")
        if not sci and self._history is not None:
            sci = self._history.scientific_for(name)
        group = model.group.get(sci) if (model is not None and sci) else None
        return sci, group

    def fetch_regions(self, isos: set, wait: float = 240.0,
                      say: Optional[Callable[[str], None]] = None) -> list[str]:
        """Download the countries' species lists (needed for 'recorded there').
        Returns the countries that couldn't be downloaded."""
        regions = self.ranker._regions
        if regions is None:
            return sorted(isos)
        from reviewer_regions import NAMES_ISO
        todo = [i for i in isos if i and (regions.get(i) is None
                                          or regions.get(i).get("names_from") != NAMES_ISO)]
        events = {}
        for iso in todo:
            ev = threading.Event()
            events[iso] = ev
            regions.fetch(iso, lambda _i, _e, ev=ev: ev.set())
        end = time.time() + wait
        for iso, ev in events.items():
            if say:
                say(f"Downloading the species list for {country_name(iso)} (once)…")
            ev.wait(max(0.0, end - time.time()))
        return [i for i in todo if regions.get(i) is None]

    def local_name(self, scientific: str, iso: Optional[str]) -> Optional[str]:
        """The species' British English common name (iNaturalist), if known."""
        region = self.ranker.region(iso) if (iso and scientific) else None
        return (region or {}).get("names", {}).get(scientific)

    def learn(self, say: Callable[[str], None], stop: Optional[threading.Event] = None) -> dict:
        """Fit the pull towards your examples (reviewer_refine). Examples made
        before their fingerprint was known are read first (where reachable)."""
        from reviewer_refine import Refiner
        from reviewer_species import get_species_model
        model = get_species_model()
        if model is None or not hasattr(model, "_emb"):
            self.refiner = None
            return {"examples": 0, "active": False, "why": "the species model isn't installed"}
        self.examples.refresh()
        todo = [(k, path) for k, path in self.examples.need_features() if os.path.exists(path)]

        def read(item):
            k, path = item
            if stop is not None and stop.is_set():
                return
            a = self._analysis(Photo(path, 0, 0.0, "", False), key=k, need_f=True)
            self.examples.set_features(k, (a or {}).get("f"))     # (once, even if no animal)
        with ThreadPoolExecutor(max_workers=3) as ex:
            for n, _r in enumerate(ex.map(read, todo), 1):
                if n % 5 == 1 or n == len(todo):
                    say(f"Reading your examples (once) {n:,} / {len(todo):,}…")
        self.examples.save()
        say("Learning from your decisions…")
        self.refiner = Refiner(model.labels, model._emb, model._scale, getattr(model, "top_of", None),
                               getattr(model, "same_as", None))
        return self.refiner.train(self.examples.usable())

    def add_my_examples(self, unique: list[Photo], lookup: dict, countries: dict) -> int:
        """Your own IDs (named in the main window, or in an app file name) as
        examples: up to MY_EXAMPLES per species, from different days."""
        mine: dict[str, list] = defaultdict(list)
        for ph in unique:
            got = lookup.get(ph.path)
            if not got or got[1] not in ("yours", "file name") or self.examples.has(ph.key):
                continue
            sci = self.canon(got[2] if len(got) > 2 and got[2] else self._lookup_name(got[0])[0])
            if sci:
                mine[sci].append((ph, got[0]))
        added = 0
        for sci, xs in mine.items():
            room = MY_EXAMPLES - self.examples.count(sci)
            days: set = set()
            for ph, name in sorted(xs, key=lambda x: (not x[0].best, x[0].ts)):
                if room <= 0:
                    break
                day = datetime.date.fromtimestamp(ph.ts).isoformat() if ph.ts else ""
                if day in days:
                    continue
                days.add(day)
                self.examples.add(ph.key, ph.path, name, sci, countries.get(ph.place), f"{ph.place}|{day}")
                room -= 1
                added += 1
        if added:
            self.examples.save()
        return added

    def times(self, photos: list[Photo], say: Callable[[str], None],
              stop: Optional[threading.Event] = None) -> None:
        need = []
        for ph in photos:
            t = self.cache.get(ph.key).get("ts")
            if t is None:
                need.append(ph)
            else:
                ph.ts = t
        for i in range(0, len(need), 400):
            if stop is not None and stop.is_set():
                return
            say(f"Reading capture times {i:,} / {len(need):,}")
            chunk = need[i:i + 400]
            got = _times([p.path for p in chunk])
            for ph in chunk:
                ph.ts = got[ph.path]
                self.cache.put(ph.key, ts=ph.ts)

    def certain(self, ranked: list, iso: Optional[str]) -> bool:
        if not ranked or ranked[0][2] < self.sure:
            return False
        region = self.ranker.region(iso)
        if region:
            from reviewer_species import get_species_model
            model = get_species_model()
            sci = ranked[0][1]
            group = model.group.get(sci) if model is not None else None
            if group in region.get("groups", []):
                species = region.get("species", {})
                if sci not in species:
                    return False               # never recorded in that country: check it
                if species[sci] < self._rare(region):
                    return False               # a rarity there: check it
        return True

    def _rare(self, region: dict) -> float:
        key = (region.get("iso"), len(region.get("species", {})))
        if key not in self._rare_cache:
            counts = sorted(region.get("species", {}).values())
            median = counts[len(counts) // 2] if counts else 0
            self._rare_cache[key] = min(RARE_RECORDS, median / 2)
        return self._rare_cache[key]

    def _analysis(self, ph: Photo, key: Optional[str] = None, need_f: bool = False) -> Optional[dict]:
        key = key or ph.key
        a = self.cache.get(key).get("a")
        if a is None or (need_f and "error" not in a and not a.get("f")):
            try:
                a = analyse(ph.path)
            except Exception as e:                    # unreadable file: remembered, skipped
                a = {"error": str(e)[:200]}
            self.cache.put(key, a=a)
            self.read += 1
        return a

    def _ranked(self, a: dict, iso: Optional[str]) -> tuple[list, str]:
        """The guesses for an analysis, with your examples' pull where it applies;
        and how that changed them ('' if it didn't)."""
        from reviewer_species import get_species_model, merge_synonyms
        same_as = getattr(get_species_model(), "same_as", None) or {}
        base = self.ranker.rank(merge_synonyms(a["top"], same_as), iso)
        if self.refiner is None or not self.refiner.active or not a.get("f") or not base:
            return base, ""
        from reviewer_refine import unpack
        top = self.refiner.top(unpack(a["f"]))
        ranked = self.ranker.rank(top, iso) if top else base
        if not ranked:
            return base, ""
        was = next((p for _n, sc, p in base if sc == ranked[0][1]), 0.0)
        if ranked[0][1] == base[0][1] and abs(ranked[0][2] - base[0][2]) < 0.05:
            return ranked, ""
        if ranked[0][1] != base[0][1]:
            return ranked, f"AI said {base[0][0]} {base[0][2]:.0%}; {ranked[0][0]} {was:.0%} → {ranked[0][2]:.0%}"
        return ranked, f"{was:.0%} → {ranked[0][2]:.0%}"

    def burst(self, b: list[Photo], countries: dict, lookup: dict) -> list[Sighting]:
        place = b[0].place
        iso = countries.get(place) or None
        # 1. what you named
        named: dict[str, tuple[Photo, str, str, str]] = {}
        for ph in b:
            got = lookup.get(ph.path)
            if got and got[1] == "skip":
                return []                              # removed by you in a review
            if got:
                named.setdefault(got[0].lower(), (ph, got[0], got[1], got[2] if len(got) > 2 else "",
                                                  got[3] if len(got) > 3 else ""))
        if named:
            out = []
            for _k, (ph, name, how, sci, note) in named.items():
                if not sci:
                    sci, _g = self._lookup_name(name)
                sci = self.canon(sci)
                group = self._group(sci)
                out.append(Sighting(place, iso, b, ph, name, sci, group, 1.0, how, True,
                                    [(name, sci, 1.0)], note=note))
            return out
        # 2. the species model
        best = None
        for ph in _order(b):
            a = self._analysis(ph)
            if not a or "error" in a or not has_animal(a):
                continue
            ranked, learned = self._ranked(a, iso)
            if not ranked:
                continue
            s = Sighting(place, iso, b, ph, ranked[0][0], ranked[0][1], None, ranked[0][2], "AI",
                         self.certain(ranked, iso), [tuple(r) for r in ranked[:3]], a.get("box"),
                         learned)
            if best is None or (s.certain, s.p) > (best.certain, best.p):
                best = s
            if s.certain:
                break
        if best is not None:
            from reviewer_species import get_species_model
            model = get_species_model()
            best.group = model.group.get(best.scientific) if model is not None else None
        return [best] if best is not None else []

    def run(self, photos: list[Photo], countries: dict, parent: str,
            progress: Callable[[dict], None], stop: Optional[threading.Event] = None,
            decisions: Optional[dict] = None) -> dict:
        """Identify everything. Returns {sightings, photos, unique, bursts, no_animal, read}.
        decisions: what you decided in a review ({photo key: {name, sci} or {skip}})."""
        say = lambda m: progress({"status": m})     # noqa: E731
        self.read = 0
        failed = self.fetch_regions({i for i in countries.values() if i}, say=say)
        self.times(photos, say, stop)
        unique = dedupe(photos)
        bs = bursts(unique) if unique else []
        say("Reading the species you've named in the app…")
        sl, lookup = SpeciesLookup(), {}
        decided = {k: v for k, v in (decisions or {}).items() if isinstance(v, dict)}
        for ph in unique:
            d = next((decided[_key(p)] for p in [ph.path] + ph.also if _key(p) in decided), None)
            if d is not None:
                lookup[ph.path] = (("", "skip", "") if d.get("skip")
                                   else (d.get("name", ""), "yours", d.get("sci", ""), d.get("note", "")))
                continue
            for path in [ph.path] + ph.also:
                sp, how, _o = sl.lookup(path, os.path.dirname(path))
                if how in ("confirmed", "name"):
                    lookup[ph.path] = (sp, "yours" if how == "confirmed" else "file name")
                    break
        mine = self.add_my_examples(unique, lookup, countries)
        learning = self.learn(say, stop)
        learning["yours_added"] = mine
        sightings: list[Sighting] = []
        done = empty = 0
        errors: list[str] = []
        t0, read0 = time.time(), 0
        with ThreadPoolExecutor(max_workers=3) as ex:
            futures = []
            for b in bs:
                futures.append(ex.submit(lambda b=b: [] if (stop is not None and stop.is_set())
                                         else self.burst(b, countries, lookup)))
            for f in futures:
                try:
                    got = f.result()
                except Exception as e:                # (one bad burst doesn't stop the run)
                    errors.append(f"{e.__class__.__name__}: {e}")
                    got = []
                done += 1
                sightings += got
                empty += 0 if got else 1
                if done % 10 == 0 or done == len(bs):
                    rate = (self.read - read0) / max(1e-6, time.time() - t0)
                    left = (len(bs) - done) / max(1e-6, done / max(1e-6, time.time() - t0))
                    progress({"done": done, "total": len(bs), "rate": rate, "left": left,
                              "species": len({(s.iso, s.scientific or s.species.lower())
                                              for s in sightings if s.certain})})
                if stop is not None and stop.is_set():
                    for g in futures:
                        g.cancel()
                    break
        # your examples: re-read the uncertain photos read before fingerprints were kept
        if self.refiner is not None and self.refiner.active and not (stop is not None and stop.is_set()):
            redo = [s for s in sightings if s.how == "AI" and not s.certain
                    and not (self.cache.get(s.pick.key).get("a") or {}).get("f")]

            def again(s):
                if stop is not None and stop.is_set():
                    return s, None
                self._analysis(s.pick, need_f=True)
                return s, self.burst(s.photos, countries, lookup)
            with ThreadPoolExecutor(max_workers=3) as ex:         # (3 at a time, as the first pass)
                futures = [ex.submit(again, s) for s in redo]
                for n, f in enumerate(futures, 1):
                    if n % 5 == 1 or n == len(redo):
                        say(f"Using your examples: re-reading uncertain photos {n:,} / {len(redo):,}…")
                    try:
                        s, new = f.result()
                    except Exception as e:
                        errors.append(f"{e.__class__.__name__}: {e}")
                        continue
                    if new is not None:
                        i = next(j for j, x in enumerate(sightings) if x is s)
                        sightings[i:i + 1] = new
            learning["reread"] = len(redo)
        learning["changed"] = sum(1 for s in sightings if s.learned)
        return {"sightings": sightings, "photos": len(photos), "unique": len(unique),
                "bursts": len(bs), "no_animal": empty, "read": self.read, "done": done,
                "regions_missing": failed, "errors": errors, "learning": learning}

    def close(self) -> None:
        self.cache.close()


# ── the list ─────────────────────────────────────────────────────────────────

@dataclass
class Entry:
    """A species seen in a country, with the photo to keep."""
    iso: Optional[str]
    species: str
    scientific: str
    group: Optional[str]
    sightings: list[Sighting]
    pick: Sighting
    dest: str = ""
    kept: bool = False                       # already in the list folder
    ai_name: str = ""                        # the model's name, if not the one shown
    name_note: str = ""                      # e.g. "⚠ the AI called it Elk"

    @property
    def note(self) -> str:
        return "; ".join(dict.fromkeys(s.note for s in self.sightings if s.note))

    @property
    def learned(self) -> str:
        return self.pick.learned

    @property
    def frames(self) -> int:
        return sum(len(s.photos) for s in self.sightings)

    @property
    def first(self) -> str:
        return min(s.date for s in self.sightings)

    @property
    def last(self) -> str:
        return max((s.date for s in self.sightings if s.date != "date unknown"), default="date unknown")

    @property
    def how(self) -> str:
        hows = {s.how for s in self.sightings}
        for h in ("yours", "file name"):
            if h in hows:
                return h
        return f"AI {max(s.p for s in self.sightings):.0%}"


@dataclass
class Clarify:
    sighting: Sighting
    dest: str = ""
    kept: bool = False
    guesses: list = field(default_factory=list)     # [(name shown, scientific, p)] top 3

    @property
    def name(self) -> str:
        return self.guesses[0][0] if self.guesses else self.sighting.species


def species_key(s: Sighting) -> str:
    return s.scientific or s.species.strip().lower()


def _name(ai: str, sci: str, iso: Optional[str], how: str,
          name_of: Optional[Callable]) -> tuple[str, str, str]:
    """(name to show, the AI's name if different, note). The country's own
    common name is used when known; a name you gave is kept as it is."""
    if how != "AI":
        return ai, "", ""
    local = name_of(sci, iso) if (name_of and sci) else None
    if not local:
        return ai, "", "" if iso else "name not checked (no country)"
    if re.sub(r"[^a-z]", "", local.lower()) == re.sub(r"[^a-z]", "", ai.lower()):
        return local, "", ""                   # (only a hyphen apart: Rock-Thrush / Rock Thrush)
    if odd_name(ai, local):
        return local, ai, f"⚠ the AI called it {ai}"
    return local, ai, f"AI: {ai}"


def _entry_order(e: Entry) -> tuple:
    return country_name(e.iso), GROUP_FOLDERS.get(e.group, "Other animals"), e.species.lower()


def entry_key(e: Entry) -> str:
    return f"{e.iso or ''}|{e.scientific or e.species.lower()}"


def build(sightings: list[Sighting], per_guess: Optional[int] = CLARIFY_PER_GUESS,
          name_of: Optional[Callable] = None) -> tuple[list[Entry], list[Clarify]]:
    """One entry per species per country; up to per_guess uncertain examples per
    best guess per country (from different days where possible; None = all).
    Every uncertain photo counts, even when its best guess is certain there too.
    name_of(scientific, iso) gives the country's own common name (optional)."""
    sure: dict[tuple, list[Sighting]] = defaultdict(list)
    unsure: dict[tuple, list[Sighting]] = defaultdict(list)
    for s in sightings:
        (sure if s.certain else unsure)[(s.iso, species_key(s))].append(s)
    entries = []
    for (iso, _k), ss in sure.items():
        pick = max(ss, key=lambda s: (s.quality, -s.pick.ts))
        how = "AI" if all(s.how == "AI" for s in ss) else "yours"
        shown, ai, note = _name(pick.species, pick.scientific, iso, how, name_of)
        entries.append(Entry(iso, shown, pick.scientific, pick.group, ss, pick, ai_name=ai, name_note=note))
    entries.sort(key=_entry_order)
    clarify = []
    for (iso, _k), ss in sorted(unsure.items(), key=lambda kv: (country_name(kv[0][0]), kv[0][1])):
        ss = sorted(ss, key=lambda s: -s.quality)
        limit = len(ss) if per_guess is None else per_guess
        chosen, days = [], set()
        for s in ss:                                  # different days first
            if s.date not in days and len(chosen) < limit:
                chosen.append(s)
                days.add(s.date)
        for s in ss:
            if len(chosen) >= limit:
                break
            if s not in chosen:
                chosen.append(s)
        for s in chosen:
            gs = [(_name(n, sc, iso, "AI", name_of)[0], sc, p) for n, sc, p in s.guesses]
            clarify.append(Clarify(s, guesses=gs))
    return entries, clarify


def _short(s: str, n: int) -> str:
    s = clean(s)
    return s if len(s) <= n else s[:n].rstrip(" -_.")


def name_entry(e: Entry) -> str:
    ext = os.path.splitext(e.pick.pick.path)[1]
    label = f"{e.species} ({e.scientific})" if e.scientific and e.scientific != e.species else e.species
    return os.path.join(_short(country_name(e.iso), 60), GROUP_FOLDERS.get(e.group, "Other animals"),
                        f"{_short(label, 120)}_{e.pick.date}{ext}")


def name_clarify(c: Clarify) -> str:
    s = c.sighting
    stem, ext = os.path.splitext(os.path.basename(s.pick.path))
    guesses = ", ".join(f"{n} {p:.0%}" for n, _sc, p in (c.guesses or s.guesses)[:3])
    sci = (c.guesses or s.guesses)[0][1] if (c.guesses or s.guesses) else ""
    folder = f"{c.name} ({sci}) - possible" if sci and sci != c.name else f"{c.name} - possible"
    return os.path.join(_short(country_name(s.iso), 60), "To clarify", _short(folder, 100),
                        f"{s.date}_{_short(stem, 40)} ({_short(guesses, 110)}){ext}")


def load_list(dest: str) -> dict:
    try:
        data = json.loads(Path(dest, LIST_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _unrecorded(dest: str, rel: str, recorded: set) -> Optional[str]:
    """A copy of the same species (same name, another date) in its folder that
    the list doesn't record, if there is one."""
    folder = os.path.join(dest, os.path.dirname(rel))
    stem = os.path.basename(rel).rsplit("_", 1)[0] + "_"
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return None
    for n in names:
        r = os.path.join(os.path.dirname(rel), n)
        if n.startswith(stem) and not n.endswith(".part") and r not in recorded:
            return r
    return None


def _tidy_parts(dest: str) -> None:
    """Remove half-copied '.part' files left by a copy that stopped part-way
    (the app's own temporary files, inside the list folder only)."""
    for d, _s, fs in os.walk(dest):
        for f in fs:
            if f.endswith(".part"):
                try:
                    os.remove(os.path.join(d, f))
                except OSError:
                    pass


def plan_copies(entries: list[Entry], clarify: list[Clarify], dest: str) -> None:
    """Where each file goes. A species already in the list keeps its photo."""
    done = load_list(dest)
    have = dict(done.get("species", {}))
    try:                                    # (a list made before species listed twice were
        import reviewer_species             #  merged may hold the other spelling)
        same_as = getattr(reviewer_species._model, "same_as", None) or {}
    except Exception:
        same_as = {}
    for k in list(have):
        iso, _s, sci = k.partition("|")
        if sci in same_as:
            have.setdefault(f"{iso}|{same_as[sci]}", have[k])
    for e in entries:
        rel = have.get(f"{e.iso or ''}|{e.scientific or e.species.lower()}")
        if rel and os.path.exists(os.path.join(dest, rel)):
            e.dest, e.kept = rel, True
        else:
            e.dest = name_entry(e)
            # copied but not recorded (a copy that stopped part-way: the record is
            # saved every 20 files): the copy of this species already there is kept
            if not os.path.exists(os.path.join(dest, e.dest)):
                found = _unrecorded(dest, e.dest, set(have.values()))
                if found:
                    e.dest, e.kept = found, True
    seen = done.get("clarify", {})
    for c in clarify:
        rel = seen.get(_key(c.sighting.pick.path))
        if rel and os.path.exists(os.path.join(dest, rel)):
            c.dest, c.kept = rel, True
        else:
            c.dest = name_clarify(c)


def _write_csv(path: str, header: list[str], rows: list[list]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:       # (opens in Excel)
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    os.replace(tmp, path)


SPECIES_HEADER = ["Country", "Type", "Common name", "Scientific name", "Name check", "Sightings",
                  "Photos", "First seen", "Last seen", "Years", "Identified by", "Photo kept", "Original",
                  "Your note", "Your examples"]


def _years(sightings: list[Sighting]) -> list[str]:
    return sorted({s.date[:4] for s in sightings if s.date[:4].isdigit()})


def _type_order(e: Entry) -> tuple:
    groups = list(dict.fromkeys(GROUP_FOLDERS.values()))
    return groups.index(GROUP_FOLDERS.get(e.group, "Other animals")), e.species.lower()


def write_overview(entries: list[Entry], clarify: list[Clarify], dest: str) -> list[str]:
    """Countries.csv (species per country and group) and Species by country.csv
    (one row per species, a column per country with the date first seen there)."""
    groups = list(dict.fromkeys(GROUP_FOLDERS.values()))
    countries = sorted({country_name(e.iso) for e in entries} | {country_name(c.sighting.iso) for c in clarify})
    rows = []
    for country in countries:
        es = [e for e in entries if country_name(e.iso) == country]
        dated = [d for e in es for d in (e.first, e.last) if d != "date unknown"]
        rows.append([country, len(es)]
                    + [sum(GROUP_FOLDERS.get(e.group, "Other animals") == g for e in es) for g in groups]
                    + [min(dated, default=""), max(dated, default=""),
                       sum(country_name(c.sighting.iso) == country for c in clarify)])
    rows.append(["All countries", len({e.scientific or e.species.lower() for e in entries})]
                + [len({e.scientific or e.species.lower() for e in entries
                        if GROUP_FOLDERS.get(e.group, "Other animals") == g}) for g in groups]
                + ["", "", len(clarify)])
    p1 = os.path.join(dest, "Countries.csv")
    _write_csv(p1, ["Country", "Species"] + groups + ["First seen", "Last seen", "To clarify"], rows)

    species: dict[str, dict] = {}
    for e in entries:
        s = species.setdefault(e.scientific or e.species.lower(), {
            "group": GROUP_FOLDERS.get(e.group, "Other animals"), "name": e.species,
            "sci": e.scientific, "seen": {}})
        s["seen"][country_name(e.iso)] = e.first if e.first != "date unknown" else "✓"
    in_list = [c for c in countries if any(c in s["seen"] for s in species.values())]
    srows = [[s["group"], s["name"], s["sci"], len(s["seen"])] + [s["seen"].get(c, "") for c in in_list]
             for s in sorted(species.values(), key=lambda s: (groups.index(s["group"]), s["name"].lower()))]
    p2 = os.path.join(dest, "Species by country.csv")
    _write_csv(p2, ["Type", "Common name", "Scientific name", "Countries"] + in_list, srows)

    # one row per species, a column per year: how many sightings that year
    by_year: dict[str, dict] = {}
    for e in sorted(entries, key=_type_order):
        s = by_year.setdefault(e.scientific or e.species.lower(), {
            "type": GROUP_FOLDERS.get(e.group, "Other animals"), "name": e.species,
            "sci": e.scientific, "years": defaultdict(int)})
        for x in e.sightings:
            if x.date[:4].isdigit():
                s["years"][x.date[:4]] += 1
    years = sorted({y for s in by_year.values() for y in s["years"]})
    yrows = [[s["type"], s["name"], s["sci"], len(s["years"])] + [s["years"].get(y) or "" for y in years]
             for s in by_year.values()]
    p3 = os.path.join(dest, "Species by year.csv")
    _write_csv(p3, ["Type", "Common name", "Scientific name", "Years"] + years, yrows)
    return [p1, p2, p3]


def write_lists(entries: list[Entry], clarify: list[Clarify], dest: str, book: bool = True) -> list[str]:
    """The CSV lists (and, with book, the Excel workbook: slower, so Review
    writes it when it closes rather than after every decision)."""
    # (by type, then species, then country: Birds A–Z, Mammals A–Z…)
    ordered = sorted(entries, key=lambda e: _type_order(e) + (country_name(e.iso),))
    rows = [[country_name(e.iso), GROUP_FOLDERS.get(e.group, "Other animals"), e.species, e.scientific,
             e.name_note, len(e.sightings), e.frames, e.first, e.last, ", ".join(_years(e.sightings)),
             e.how, e.dest, e.pick.pick.path, e.note, e.learned] for e in ordered]
    written = [os.path.join(dest, "All species.csv")]
    _write_csv(written[0], SPECIES_HEADER, rows)
    by_country = defaultdict(list)
    for r in rows:
        by_country[r[0]].append(r)
    for country, rs in by_country.items():
        p = os.path.join(dest, _short(country, 60), f"{_short(country, 60)} species.csv")
        _write_csv(p, SPECIES_HEADER, rs)
        written.append(p)
    written += write_overview(entries, clarify, dest)
    crow = []
    for c in clarify:
        s = c.sighting
        g = list(c.guesses or s.guesses) + [("", "", 0.0)] * 3
        crow.append([country_name(s.iso), s.date, g[0][0], g[0][1], f"{g[0][2]:.0%}",
                     g[1][0], f"{g[1][2]:.0%}" if g[1][0] else "", g[2][0], f"{g[2][2]:.0%}" if g[2][0] else "",
                     len(s.photos), c.dest, s.pick.path, s.learned])
    p = os.path.join(dest, "To clarify.csv")
    _write_csv(p, ["Country", "Date", "Best guess", "Scientific name", "Probability", "2nd guess", "p",
                   "3rd guess", "p", "Photos in burst", "Copied to", "Original", "Your examples"], crow)
    # (names are the country's own where known, as in the species list)
    written.append(p)
    out = write_workbook(written, dest) if book else None
    return written + ([out] if out else [])


WORKBOOK = "Species list.xlsx"


def write_workbook(csvs: list[str], dest: str) -> Optional[str]:
    """Every list in one Excel workbook: Countries, Species by country, All
    species, To clarify, then a tab per country. (Skipped without openpyxl.)"""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        return None
    by_name = {os.path.basename(c): c for c in csvs}
    first = ["Countries.csv", "Species by country.csv", "Species by year.csv", "All species.csv",
             "To clarify.csv"]
    order = [by_name[n] for n in first if n in by_name]
    order += sorted((c for c in csvs if c not in order), key=lambda c: os.path.basename(c).lower())
    wb = Workbook()
    wb.remove(wb.active)
    used: set = set()
    for path in order:
        try:
            with open(path, encoding="utf-8-sig", newline="") as f:
                rows = list(csv.reader(f))
        except OSError:
            continue
        title = re.sub(r"[\[\]:*?/\\]", "", os.path.splitext(os.path.basename(path))[0])
        title = title[:31] or "Sheet"
        while title.lower() in used:
            title = title[:28] + f"~{len(used)}"
        used.add(title.lower())
        ws = wb.create_sheet(title)
        for r in rows:
            ws.append([int(v) if v.isdigit() and len(v) < 10 else v for v in r])
        if rows:
            for cell in ws[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="2D7A4F")
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            for i, _h in enumerate(rows[0], 1):
                width = max(len(str(r[i - 1])) if i - 1 < len(r) else 0 for r in rows[:500])
                ws.column_dimensions[get_column_letter(i)].width = min(60, max(8, width + 2))
    out = os.path.join(dest, WORKBOOK)
    try:
        tmp = out + ".tmp.xlsx"
        wb.save(tmp)
        os.replace(tmp, out)
    except OSError:                       # (open in Excel: the CSVs are still written)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None
    return out


def copy_list(entries: list[Entry], clarify: list[Clarify], dest: str,
              progress: Optional[Callable[[int, int, str], None]] = None,
              stop: Optional[threading.Event] = None) -> dict:
    """Copy the chosen photos (verified) and write the CSV lists."""
    plan_copies(entries, clarify, dest)
    data = load_list(dest)
    have, seen = data.setdefault("species", {}), data.setdefault("clarify", {})
    for e in entries:                       # (copies found in place, not yet recorded)
        if e.kept and e.dest:
            have.setdefault(entry_key(e), e.dest)
    todo = [(e.pick.pick.path, e.dest, "species", f"{e.iso or ''}|{e.scientific or e.species.lower()}")
            for e in entries if not e.kept]
    todo += [(c.sighting.pick.path, c.dest, "clarify", _key(c.sighting.pick.path))
             for c in clarify if not c.kept]
    out = {"copied": 0, "bytes": 0, "errors": [], "kept": sum(e.kept for e in entries)}
    os.makedirs(dest, exist_ok=True)
    _tidy_parts(dest)

    def save():
        tmp = os.path.join(dest, LIST_FILE + ".tmp")
        Path(tmp).write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, os.path.join(dest, LIST_FILE))
    for n, (src, rel, kind, key) in enumerate(todo, 1):
        if stop is not None and stop.is_set():
            break
        target = os.path.join(dest, rel)
        try:
            if not (os.path.exists(target) and os.path.getsize(target) == os.path.getsize(src)):
                _copy_verified(src, [target], True)
            (have if kind == "species" else seen)[key] = rel
            out["copied"] += 1
            out["bytes"] += os.path.getsize(src)
        except Exception as e:
            out["errors"].append(f"{src}: {e}")
        if n % 20 == 0:
            save()
        if progress:
            progress(n, len(todo), rel)
    save()
    out["lists"] = write_lists(entries, clarify, dest)
    return out


# ── review: your corrections ─────────────────────────────────────────────────

def _trash(path: str) -> None:
    try:
        from send2trash import send2trash
        send2trash(path)                       # (the Recycle Bin, where the drive has one)
    except Exception:
        os.remove(path)


HOLD = ".removed"            # removed copies wait here until Review closes (so Undo works)
UNDO_STEPS = 50


class ListEditor:
    """Your corrections to a species list (the lists are changed in place).
    The copies in the list folder follow: moved to the species' place, merged
    with a species already there (one photo kept, the other copy removed), or
    removed. Only copies inside the list folder are ever moved or removed, and
    removed ones wait in a hidden folder until close(), so undo() can bring
    them back."""

    def __init__(self, entries: list[Entry], clarify: list[Clarify], dest: str,
                 trash: Callable[[str], None] = _trash, examples=None,
                 sightings: Optional[list[Sighting]] = None, name_of: Optional[Callable] = None):
        self.entries, self.clarify, self.dest, self._trash = entries, clarify, dest, trash
        self.examples = examples                 # (reviewer_refine.Examples: what the AI learns from)
        self.all = sightings if sightings is not None else []    # every sighting (for a re-check)
        self.name_of = name_of                   # (scientific, iso) → the country's common name
        self.data = load_list(dest)
        for k in ("species", "clarify", "decisions"):
            self.data.setdefault(k, {})
        self._undo: list = []
        self._ops: Optional[list] = None
        self._held = 0
        self._changed = False
        self._purge()                            # (left from a Review that didn't close)

    # what's where
    def on_disk(self, item) -> Optional[str]:
        if item is not None and item.dest:
            path = os.path.join(self.dest, item.dest)
            if os.path.exists(path):
                return path
        return None

    @staticmethod
    def iso(item) -> Optional[str]:
        return item.iso if isinstance(item, Entry) else item.sighting.iso

    @staticmethod
    def sightings(item) -> list[Sighting]:
        return item.sightings if isinstance(item, Entry) else [item.sighting]

    @staticmethod
    def pick(item) -> Sighting:
        return item.pick if isinstance(item, Entry) else item.sighting

    def entry_for(self, iso: Optional[str], sci: str, name: str, besides=None) -> Optional[Entry]:
        """The species already in that country's list, if it is."""
        k = sci or name.strip().lower()
        for e in self.entries:
            if e is not besides and e.iso == iso and (
                    (e.scientific or e.species.lower()) == k or (not sci and e.species.lower() == k)):
                return e
        return None

    def decided(self, s: Sighting) -> bool:
        return any(_key(ph.path) in self.data["decisions"] for ph in [s.pick])

    # changes
    def assign(self, item, name: str, sci: str = "", group: Optional[str] = None,
               use_this: bool = False, note: str = "") -> str:
        """It's this species. Already in the list: the photo there is kept and
        this copy removed (use_this: the other way round). Each photo is also
        an example the AI learns from. (Renaming a species renames every
        sighting of it in that country.)"""
        self._begin()
        try:
            return self._assign(item, name, sci, group, use_this, note)
        finally:
            self._end()

    def _assign(self, item, name, sci, group, use_this, note) -> str:
        name, note = name.strip(), note.strip()
        iso, ss, pick = self.iso(item), self.sightings(item), self.pick(item)
        old = self.on_disk(item)
        self._forget(item)
        for s in ss:
            if self.examples is not None and sci:
                a = s.pick
                self.examples.add(a.key, a.path, name, sci, iso, f"{s.place}|{s.date}",
                                  self._feature(s), note)
            s.species, s.scientific, s.group = name, sci, group
            s.p, s.how, s.certain, s.learned = 1.0, "yours", True, ""
            s.note = note or s.note
            self.data["decisions"][_key(s.pick.path)] = {"name": name, "sci": sci, "note": s.note}
        target = self.entry_for(iso, sci, name)
        where = country_name(iso)
        if target is None:
            e = Entry(iso, name, sci, group, list(ss), pick)
            e.dest = name_entry(e)
            self._place(e, old)
            self.entries.append(e)
            self.entries.sort(key=_entry_order)
            msg = f"{name}: added to {where}"
        else:
            target.sightings += [s for s in ss if s not in target.sightings]
            if use_this:
                self._remove(self.on_disk(target))
                target.pick = pick
                target.dest = name_entry(target)
                self._place(target, old)
                msg = f"{target.species}: already in {where}; now this photo is the one kept"
            else:
                self._remove(old)
                msg = f"{target.species}: already in {where}; the photo there is kept, this copy removed"
        self._save()
        return msg

    def reject(self, item, note: str = "") -> str:
        """Remove this photo (a duplicate, no animal, a wrong ID…); left out when
        you Identify again. Not an example: there's no answer to learn.
        A species in the list keeps going if it was seen elsewhere in that
        country: its next best sighting takes its place (shown for you to
        check); with none, uncertain photos there that had it as a guess go to
        To clarify."""
        self._begin()
        try:
            msg = self._reject_entry(item, note) if isinstance(item, Entry) else self._reject_clarify(item, note)
            self._save()
            return msg
        finally:
            self._end()

    def _skip(self, s: Sighting, note: str) -> None:
        self.data["decisions"][_key(s.pick.path)] = {"skip": True, "note": note.strip()}
        if self.examples is not None:
            self.examples.remove(s.pick.key)

    def _reject_clarify(self, c: Clarify, note: str) -> str:
        self._skip(c.sighting, note)
        self._remove(self.on_disk(c))
        self._forget(c)
        return "Removed"

    def _reject_entry(self, e: Entry, note: str) -> str:
        where, name = country_name(e.iso), e.species
        gone = e.pick
        self._skip(gone, note)
        self._remove(self.on_disk(e))
        self.data["species"].pop(entry_key(e), None)
        e.sightings = [s for s in e.sightings if s is not gone]
        if self.all:
            self.all[:] = [s for s in self.all if s is not gone]
        if e.sightings:                          # seen elsewhere: the next best photo
            e.pick = max(e.sightings, key=lambda s: (s.quality, -s.pick.ts))
            e.dest = name_entry(e)
            self._copy_in(e)
            n = len(e.sightings)
            return (f"Removed this photo. {name} was seen {n} more time{'s' if n > 1 else ''} in {where}: "
                    "the next best photo is shown, check it")
        self._forget(e)
        found = self._recheck(e)
        if found:
            return (f"Removed: no other confident {name} in {where}. {found} uncertain photo"
                    f"{'s' if found > 1 else ''} there had it as a guess: added to To clarify")
        return f"Removed: {name} isn't anywhere else in {where}"

    def _recheck(self, e: Entry) -> int:
        """Uncertain photos in e's country that had e's species as a guess (best
        first), into To clarify."""
        k = e.scientific or e.species.lower()
        shown = {id(c.sighting) for c in self.clarify}

        def p_of(s: Sighting) -> float:
            return max([p for _n, sc, p in s.guesses if sc and sc == e.scientific]
                       + ([s.p] if species_key(s) == k else []) + [0.0])
        found = [s for s in self.all if s.iso == e.iso and id(s) not in shown and not self.decided(s)
                 and not s.certain and p_of(s) > 0]
        found.sort(key=lambda s: -p_of(s))
        for s in found[:CLARIFY_PER_GUESS]:
            gs = [(_name(n, sc, s.iso, "AI", self.name_of)[0], sc, p) for n, sc, p in s.guesses]
            c = Clarify(s, guesses=gs)
            c.dest = name_clarify(c)
            self.clarify.append(c)
        return min(len(found), CLARIFY_PER_GUESS)

    def undo(self) -> str:
        """Take back the last change (files, lists and what the AI learns from)."""
        if not self._undo:
            return "Nothing to undo"
        snap, ops = self._undo.pop()
        for op in reversed(ops):
            try:
                if op[0] == "move":
                    os.makedirs(os.path.dirname(op[1]), exist_ok=True)
                    os.replace(op[2], op[1])
                    self._tidy(os.path.dirname(op[2]))
                elif op[0] == "new":
                    os.remove(op[1])
                    self._tidy(os.path.dirname(op[1]))
            except OSError:
                pass
        entries, clarify, everything, data, examples = snap
        self.entries[:] = entries
        self.clarify[:] = clarify
        self.all[:] = everything
        self.data = data
        if self.examples is not None and examples is not None:
            self.examples.restore(examples)
        self._save()
        return "Undone"

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    def close(self) -> None:
        """Review closed: removed copies go to the Recycle Bin (no more undo), and
        the Excel workbook is brought up to date."""
        self._undo.clear()
        self._purge()
        if self._changed:
            write_lists(self.entries, self.clarify, self.dest)
            self._changed = False

    # (helpers)
    feature_of: Optional[Callable] = None        # photo key → fingerprint (packed), if read

    def _feature(self, s: Sighting) -> Optional[str]:
        return self.feature_of(s.pick.key) if self.feature_of else None

    def _begin(self) -> None:
        import copy
        # (photos never change: shared, not copied, which keeps this quick)
        memo = {}
        for s in self.all + [x for e in self.entries for x in e.sightings] + [c.sighting for c in self.clarify]:
            memo[id(s.pick)] = s.pick
            for ph in s.photos:
                memo[id(ph)] = ph
        snap = copy.deepcopy((list(self.entries), list(self.clarify), list(self.all), self.data), memo)
        self._pending = snap + (self.examples.snapshot() if self.examples is not None else None,)
        self._ops = []

    def _end(self) -> None:
        if self._ops is not None:
            self._undo.append((self._pending, self._ops))
            del self._undo[:-UNDO_STEPS]
        self._ops = None

    def _forget(self, item) -> None:
        if isinstance(item, Entry):
            self.entries.remove(item)
            self.data["species"].pop(entry_key(item), None)
        else:
            self.clarify.remove(item)
            self.data["clarify"].pop(_key(item.sighting.pick.path), None)

    def _move(self, src: str, dst: str) -> None:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.replace(src, dst)
        if self._ops is not None:
            self._ops.append(("move", src, dst))

    def _place(self, e: Entry, old: Optional[str]) -> None:
        """Move the copy (if there is one) to the entry's place in the list;
        without one, Copy list copies it."""
        if old:
            target = os.path.join(self.dest, e.dest)
            if _key(old) != _key(target):
                if os.path.exists(target):
                    self._remove(target)
                self._move(old, target)
                self._tidy(os.path.dirname(old))
            e.kept = True
            self.data["species"][entry_key(e)] = e.dest
        else:
            e.kept = False
            self.data["species"].pop(entry_key(e), None)

    def _copy_in(self, e: Entry) -> None:
        """Copy a species' (new) photo from the original into the list (verified)."""
        target = os.path.join(self.dest, e.dest)
        try:
            if os.path.exists(target):
                self._remove(target)
            _copy_verified(e.pick.pick.path, [target], True)
            if self._ops is not None:
                self._ops.append(("new", target))
            e.kept = True
            self.data["species"][entry_key(e)] = e.dest
        except Exception:
            e.kept = False                       # (Copy list will copy it)

    def _remove(self, path: Optional[str]) -> None:
        """Into the hidden holding folder (Recycle Bin when Review closes)."""
        if not path or not _within(path, self.dest) or _key(path) == _key(self.dest):
            return                               # (only the list's own copies)
        if _within(path, os.path.join(self.dest, HOLD)):
            return
        self._held += 1
        held = os.path.join(self.dest, HOLD, f"{self._held:05d}_{os.path.basename(path)}")
        self._move(path, held)
        self._tidy(os.path.dirname(path))

    def _purge(self) -> None:
        hold = os.path.join(self.dest, HOLD)
        if not os.path.isdir(hold):
            return
        for name in os.listdir(hold):
            try:
                self._trash(os.path.join(hold, name))
            except OSError:
                pass
        try:
            os.rmdir(hold)
        except OSError:
            pass

    def _tidy(self, folder: str) -> None:
        """Remove folders left empty (a '… - possible' folder that's been sorted)."""
        while _within(folder, self.dest) and _key(folder) != _key(self.dest):
            try:
                os.rmdir(folder)
            except OSError:
                return
            folder = os.path.dirname(folder)

    def _save(self) -> None:
        os.makedirs(self.dest, exist_ok=True)
        tmp = os.path.join(self.dest, LIST_FILE + ".tmp")
        Path(tmp).write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        os.replace(tmp, os.path.join(self.dest, LIST_FILE))
        write_lists(self.entries, self.clarify, self.dest, book=False)
        self._changed = True
        if self.examples is not None:
            self.examples.save()


# ── the last results, so Review opens without identifying again ──────────────

RESULTS_DIR = "species_list_results"
RESULTS_VERSION = 1


def results_file(user_dir: str, parent: str) -> str:
    import hashlib
    h = hashlib.sha1(_key(parent).encode("utf-8")).hexdigest()[:16]
    return os.path.join(user_dir, RESULTS_DIR, f"{h}.pickle")


def save_results(user_dir: str, parent: str, res: dict, entries: list[Entry],
                 clarify: list[Clarify]) -> None:
    import pickle
    path = results_file(user_dir, parent)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "wb") as f:
            pickle.dump({"v": RESULTS_VERSION, "parent": _key(parent), "t": time.time(),
                         "res": res, "entries": entries, "clarify": clarify}, f)
        os.replace(path + ".tmp", path)
    except (OSError, pickle.PickleError, TypeError, AttributeError):
        pass


def load_results(user_dir: str, parent: str) -> Optional[dict]:
    """The last Identify for this folder (with your Review changes), or None.
    (The app's own file in its data folder.)"""
    import pickle
    try:
        with open(results_file(user_dir, parent), "rb") as f:
            got = pickle.load(f)
    except Exception:
        return None
    if not isinstance(got, dict) or got.get("v") != RESULTS_VERSION or got.get("parent") != _key(parent):
        return None
    return got
