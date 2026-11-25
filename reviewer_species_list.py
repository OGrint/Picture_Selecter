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
  uncertain. In a mixed flock, the other clearly seen animals in that frame are
  identified too: one that's a different species becomes a sighting of its own
  (part 1, 2… of the photo, decided separately in Review).
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
from reviewer_imaging import BITMAP_EXTS, RAW_EXTS, raw_to_jpeg
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


_COUNTRY_NAMES: dict = {}


def country_name(iso: Optional[str]) -> str:
    if iso not in _COUNTRY_NAMES:
        c = by_iso(iso) if iso else None
        _COUNTRY_NAMES[iso] = c["name"] if c else UNKNOWN
    return _COUNTRY_NAMES[iso]


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

ANALYSIS_V = 3          # 2: people stay people; weak frames looked at closer (tiles); 3: other animals too
OTHER_ANIMALS = 4       # other animals in a frame identified (a mixed flock)
OTHER_ANIMAL_CONF = 0.3  # …each seen at least this clearly
OTHER_SPECIES_P = 0.5   # an other animal is a sighting of its own if the AI is this sure of its species
TILE_BELOW = 0.5        # the detector less sure than this: look closer, in tiles
TILE_ANIMAL = 0.3       # an animal found in the tiles at least this sure counts
SURE_ANIMAL = 0.5       # below this (whole frame and tiles), it's marked "no animal?"


def analyse(path: str) -> dict:
    """Animal box and the species model's raw top 10 for one photo. A frame the
    detector isn't sure about is looked at again in tiles at a higher
    resolution, so a distant animal gets a box of its own (and a proper crop
    for the species model)."""
    from reviewer_detect import (ANIMAL_CONF, OTHER_CONF, PERSON_SURE, detect_tiled, get_detector, main_animal,
                                 refine, tiled_animals)
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
    conf = round(float(a[1]), 3) if a else 0.0
    tile, src = 0.0, img
    if det is not None and conf < TILE_BELOW:
        big = load_preview(path, 3600)
        big.load()
        people = [d for d in dets if d[0] == 1 and d[1] >= ANIMAL_CONF]
        found = tiled_animals(detect_tiled(det, big), people)      # (people first: then the animals that aren't)
        if found:
            tile = round(float(found[0][1]), 3)
            if tile >= TILE_ANIMAL and tile > conf:
                box, src = found[0][2:6], big            # (the distant animal, cropped closer)
    from reviewer_refine import pack
    feat = model.features(src, box)
    top = [[n, s, round(float(p), 4)] for n, s, p in model.top(feat, k=10)]
    others = []                                  # (a mixed flock: the other animals, each identified)
    if box is not None and src is img:
        from reviewer_detect import _inside
        found = [d for d in dets if d[0] == 0 and d[1] >= OTHER_ANIMAL_CONF and len(d) == 6
                 and _inside(d[2:6], box) < 0.5 and _inside(box, d[2:6]) < 0.5]
        found.sort(key=lambda d: -(d[1] * (d[4] - d[2]) * (d[5] - d[3])))
        for d in found[:OTHER_ANIMALS]:
            ob = [round(float(v), 4) for v in d[2:6]]
            otop = model.top(model.features(img, ob), k=5)
            others.append({"box": ob, "conf": round(float(d[1]), 3),
                           "top": [[n, sc, round(float(p), 4)] for n, sc, p in otop]})
    return {"top": top, "box": box, "conf": conf, "tile": tile, "others": others,
            "other": any(d[0] in (1, 2) and d[1] >= OTHER_CONF for d in dets),
            "person": any(d[0] == 1 and d[1] >= PERSON_SURE for d in dets),
            "f": pack(feat), "v": ANALYSIS_V}   # (the fingerprint, for learning from you)


def has_animal(a: dict) -> bool:
    """An animal box (whole frame, or found in the tiles); for analyses from
    before tiles, also the species model being sure about the whole frame."""
    from reviewer_detect import SPECIES_PRESENT
    top = a.get("top") or []
    if a.get("conf", 0) > 0 or a.get("tile", 0) >= TILE_ANIMAL:
        return True
    if a.get("v", 1) >= ANALYSIS_V:
        return False            # (looked at closer and nothing found: a guess from the whole frame isn't enough)
    return bool(top) and top[0][2] >= SPECIES_PRESENT and not top[0][1].startswith("Homo ")


def unsure_animal(a: dict) -> bool:
    """Counted as an animal, but neither the whole frame nor the tiles were sure, or
    it's only in the tiles of a photo of people: "no animal?"."""
    if a.get("person") and a.get("conf", 0) < SURE_ANIMAL:
        return True
    return max(a.get("conf", 0), a.get("tile", 0)) < SURE_ANIMAL


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
    weak: bool = False                      # an animal only faintly seen: "no animal?"
    unknown: Optional[str] = None           # you don't know what it is: your hint ("terrapin"), to research
    best: Optional[Photo] = None            # the burst's best frame (sharpest animal), when chosen
    part: int = 0                           # 0: the photo's main animal; 1, 2…: others in it (a mixed flock)
    score: Optional[float] = None           # how good that frame is (0–100+)
    note: str = ""                          # your reason / note from Review

    @property
    def date(self) -> str:
        t = self.pick.ts
        if not t or datetime.datetime.fromtimestamp(t).year < 2005:
            return "date unknown"
        return f"{datetime.datetime.fromtimestamp(t):%Y-%m-%d}"

    @property
    def shot(self) -> "Photo":
        """The frame to keep: the burst's best (predicted) if it's been chosen, else
        the one the species came from."""
        return getattr(self, "best", None) or self.pick

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
        self.read = 0                               # (photos read: by Identify, or a re-check)
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

    _GROUP_WORDS = {"bird": "birds", "birds": "birds", "mammal": "mammals", "mammals": "mammals",
                    "reptile": "reptiles", "reptiles": "reptiles", "amphibian": "amphibians",
                    "amphibians": "amphibians", "dragonfly": "dragonflies", "dragonflies": "dragonflies",
                    "damselfly": "dragonflies", "butterfly": "butterflies", "butterflies": "butterflies",
                    "moth": "butterflies", "moths": "butterflies"}

    # everyday words for a kind of animal → the words species names use
    _LOOSE = {
        "bird of prey": ["eagle", "hawk", "kite", "buzzard", "falcon", "kestrel", "hobby", "harrier", "osprey",
                         "vulture", "goshawk", "sparrowhawk", "merlin"],
        "raptor": ["eagle", "hawk", "kite", "buzzard", "falcon", "kestrel", "harrier", "osprey", "vulture"],
        "terrapin": ["terrapin", "turtle", "slider", "cooter"],
        "wader": ["sandpiper", "plover", "godwit", "curlew", "redshank", "greenshank", "dunlin", "stint",
                  "snipe", "lapwing", "avocet", "oystercatcher", "knot", "turnstone", "sanderling",
                  "whimbrel", "ruff", "phalarope", "stilt"],
        "shorebird": ["sandpiper", "plover", "godwit", "curlew", "dunlin", "stint", "turnstone", "sanderling"],
        "duck": ["duck", "teal", "wigeon", "mallard", "pochard", "scaup", "eider", "scoter", "goldeneye",
                 "merganser", "goosander", "smew", "shoveler", "gadwall", "pintail", "garganey"],
        "dragonfly": ["dragonfly", "darter", "chaser", "skimmer", "hawker", "emperor", "clubtail"],
        "damselfly": ["damselfly", "damsel", "demoiselle", "bluet", "dancer", "forktail", "spreadwing"],
        "seabird": ["gull", "tern", "auk", "puffin", "guillemot", "razorbill", "gannet", "shearwater",
                    "petrel", "fulmar", "kittiwake", "cormorant", "shag"],
        "lizard": ["lizard", "gecko", "skink", "agama", "anole", "iguana"],
    }

    def hint_species(self, hint: str, like: str = "") -> list[int]:
        """The model's species that fit your hint: a type ("reptile"), a word in their
        names ("terrapin", "warbler", "Mauremys"), with the rest of the genera found
        (relatives named otherwise). No hint: the same type as `like` (its best guess)."""
        from reviewer_species import get_species_model
        model = get_species_model()
        if model is None:
            return []
        labels = model.labels
        text = hint.lower()
        for loose, names in self._LOOSE.items():          # ("bird of prey" → eagle, hawk, kite…)
            if loose in text:
                text = text.replace(loose, " ") + " " + " ".join(names)
        words = [w for w in re.split(r"[^a-z-]+", text) if len(w) >= 3]
        groups = {self._GROUP_WORDS[w] for w in words if w in self._GROUP_WORDS}
        words = [w for w in words if w not in self._GROUP_WORDS]
        if not words and not groups:
            g = model.group.get(like) if like else None
            return [i for i, (_n, sc) in enumerate(labels) if g and model.group.get(sc) == g]
        hits = set()
        if words:
            for i, (n, sc) in enumerate(labels):
                name, sci = n.lower(), sc.lower()
                if any(w in name.split() or w.rstrip("s") in name.split() or (len(w) >= 5 and w in name)
                       or sci.split()[0] == w or w in sci.split() for w in words):
                    hits.add(i)
            genera = {labels[i][1].split()[0] for i in hits}
            hits |= {i for i, (_n, sc) in enumerate(labels) if sc.split()[0] in genera}
        else:
            hits = set(range(len(labels)))
        if groups:
            hits = {i for i in hits if model.group.get(labels[i][1]) in groups}
        return sorted(hits)

    def rescreen(self, s: Sighting, hint: str, k: int = 5) -> list[tuple]:
        """The model asked again about one photo, choosing only among the species
        that fit your hint (re-ranked by its country, as always)."""
        import numpy as np
        from reviewer_refine import unpack
        from reviewer_regions import region_weight
        from reviewer_species import get_species_model
        model = get_species_model()
        if model is None or not hasattr(model, "_emb"):
            return []
        a = self._analysis(s.pick, need_f=True) or {}
        if not a.get("f"):
            return []
        idx = self.hint_species(hint, s.scientific)
        if not idx:
            return []
        idx = np.array(idx)
        logits = model._scale * (model._emb[idx] @ unpack(a["f"]))
        p = np.exp(logits - logits.max())
        region = self.ranker.region(s.iso)
        w = np.array([region_weight(model.labels[i][1], model.group.get(model.labels[i][1]), region)
                      for i in idx])
        p = p * w
        p /= p.sum()
        order = np.argsort(-p)[:k]
        out = []
        for j in order:
            n, sc = model.labels[idx[j]]
            out.append((self.local_name(sc, s.iso) or n, sc, round(float(p[j]), 4)))
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

    def records(self, scientific: str, iso: Optional[str]) -> Optional[int]:
        """iNaturalist records of the species in the country (its downloaded list;
        None: no list for that country)."""
        region = self.ranker.region(iso) if iso else None
        if not region:
            return None
        return int(region.get("species", {}).get(self.canon(scientific), 0))

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

    # the best photo of a species (predicted): the sharpest, well exposed animal
    BEST_SIGHTINGS = 3          # sightings of a species looked at
    BEST_FRAMES = 5             # frames of each one's burst

    FRAME_KEY = "q2"            # (the frame score kept in the scan cache; 2: the animal itself counts)

    def frame_score(self, ph: Photo, box: Optional[list]) -> float:
        """How good a photo of the animal a frame is (0–100, kept): the animal seen
        clearly (the detector's confidence: it drops when it's half hidden), its
        size in the frame, then sharpness and exposure on it; a frame from a
        Best folder (your pick) ahead. A hint for choosing: the Best photo view
        in Review is where you decide."""
        rec = self.cache.get(ph.key)
        q = rec.get(self.FRAME_KEY)
        if q is None:
            from reviewer_detect import get_detector, main_animal
            from reviewer_imaging import load_preview, score_image
            try:
                img = load_preview(ph.path, 1600)
                img.load()
                det = get_detector()
                a = main_animal(det.detect(img)) if det is not None else None
                region = list(a[2:6]) if a else ((rec.get("a") or {}).get("box") or box)
                conf = float(a[1]) if a else 0.0
                area = (region[2] - region[0]) * (region[3] - region[1]) if region else 0.0
                sc = score_image(img, tuple(region) if region else None, "animal" if region else "centre") or {}
                q = round(35 * conf + 20 * min(1.0, (area ** 0.5) / 0.5)
                          + 0.35 * sc.get("sharpness", 0) + 0.1 * sc.get("exposure", 0), 1)
            except Exception:
                q = 0.0
            self.cache.put(ph.key, **{self.FRAME_KEY: q})
        return q + (15.0 if ph.best else 0.0)

    def best_frame(self, s: Sighting) -> tuple[float, Photo]:
        """The sighting's best frame (the identified one and those nearest it in time)."""
        frames = sorted(s.photos, key=lambda p: (p is not s.pick, abs(p.ts - s.pick.ts)))[:self.BEST_FRAMES]
        scored = [(self.frame_score(p, s.box), p) for p in frames]
        return max(scored, key=lambda x: x[0])

    def choose_best(self, entries: list, progress: Optional[Callable[[str], None]] = None,
                    stop: Optional[threading.Event] = None, chosen: Optional[dict] = None) -> int:
        """The photos you chose in Review's Best photo view become their species'
        photos (for the list and iNaturalist). Returns how many."""
        chosen = chosen or {}
        for e in entries:
            path = chosen.get(entry_key(e))
            hit = next(((s, ph) for s in e.sightings for ph in s.photos if path and _key(ph.path) == _key(path)),
                       None)
            if hit:
                hit[0].best, e.pick = hit[1], hit[0]
        # (the scores only order the photos in Review's Best photo view: tried on real
        #  photos, they didn't pick better ones on their own, e.g. a bird half hidden by
        #  sharp brambles; so the list keeps its own choice unless you choose)
        return sum(1 for e in entries if entry_key(e) in chosen)

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
        # (read again: no fingerprint when one's needed; or an "animal" from before
        #  people stayed people and weak frames were looked at closer)
        stale = a is not None and "error" not in a and a.get("v", 1) < ANALYSIS_V and has_animal(a)
        if a is None or stale or (need_f and "error" not in a and not a.get("f")):
            try:
                a = analyse(ph.path)
            except Exception as e:                    # unreadable file: remembered, skipped
                a = {"error": str(e)[:200]}
            a.setdefault("v", ANALYSIS_V)
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
        research = None
        for ph in b:
            got = lookup.get(ph.path)
            if got and got[1] == "skip":
                return []                              # removed by you in a review
            if got and got[1] == "unknown":
                research = (ph, got[2])                # (you don't know what it is: the AI's guesses kept)
                continue
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
                         learned, weak=unsure_animal(a))
            if best is None or (s.certain, s.p) > (best.certain, best.p):
                best = s
            if s.certain:
                break
        if best is not None and best.how == "AI" and best.box is not None:
            out = [best] + self._others(best, b, iso)
        else:
            out = None
        if research is not None:
            ph, hint = research
            if best is None:                          # (even with no animal found: you kept it)
                a = self._analysis(ph) or {}
                ranked = self.ranker.rank(a["top"], iso) if a.get("top") else []
                best = Sighting(place, iso, b, ph, ranked[0][0] if ranked else "Unknown",
                                ranked[0][1] if ranked else "", None, ranked[0][2] if ranked else 0.0, "AI",
                                False, [tuple(r) for r in ranked[:3]], a.get("box"))
            best.unknown, best.certain = hint, False
        if best is not None:
            from reviewer_species import get_species_model
            model = get_species_model()
            best.group = model.group.get(best.scientific) if model is not None else None
        if out:
            return out
        return [best] if best is not None else []

    def _others(self, best: Sighting, b: list, iso: Optional[str]) -> list[Sighting]:
        """The other species in the same frame (a mixed flock): each animal the AI is
        sure enough is a different species becomes a sighting of its own (with your
        decision about it, if you made one)."""
        from reviewer_species import get_species_model, merge_synonyms
        model = get_species_model()
        same_as = getattr(model, "same_as", None) or {}
        a = self._analysis(best.pick) or {}
        out, seen = [], {best.scientific}
        for n, o in enumerate(a.get("others") or [], 1):
            ranked = self.ranker.rank(merge_synonyms(o.get("top") or [], same_as), iso)
            if not ranked or ranked[0][2] < OTHER_SPECIES_P or ranked[0][1] in seen:
                continue
            seen.add(ranked[0][1])
            s = Sighting(best.place, iso, b, best.pick, ranked[0][0], ranked[0][1],
                         model.group.get(ranked[0][1]) if model is not None else None, ranked[0][2], "AI",
                         self.certain(ranked, iso), [tuple(r) for r in ranked[:3]], o.get("box"), part=n)
            d = getattr(self, "_decided", {}).get(sighting_key(s))
            if d is not None and d.get("skip"):
                continue
            if d is not None and d.get("unknown"):
                s.unknown, s.certain = d.get("hint", ""), False
            elif d is not None:
                sci = d.get("sci", "")
                s.species, s.scientific = d.get("name", s.species), sci
                s.group = model.group.get(sci) if (model is not None and sci) else None
                s.p, s.how, s.certain, s.note = 1.0, "yours", True, d.get("note", "")
            out.append(s)
        return out

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
        self._decided = decided                   # (the other animals in a frame: their own decisions)
        for ph in unique:
            d = next((decided[_key(p)] for p in [ph.path] + ph.also if _key(p) in decided), None)
            if d is not None:
                if d.get("unknown"):
                    lookup[ph.path] = ("", "unknown", d.get("hint", ""))
                else:
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
    rechecked: Optional[str] = None                  # re-checked within this hint ("": within its type)

    @property
    def name(self) -> str:
        return self.guesses[0][0] if self.guesses else self.sighting.species

    @property
    def unknown(self) -> bool:
        """You don't know what it is (yet): kept to research, in To identify."""
        return self.sighting.unknown is not None


def sighting_key(s: Sighting) -> str:
    """What a sighting is known by (decisions, copies): its photo, and which animal
    in it when there are several (a mixed flock)."""
    part = getattr(s, "part", 0)
    return _key(s.pick.path) + (f"#{part}" if part else "")


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
    research = []
    for s in sightings:
        if s.unknown is not None:                     # (you don't know: always kept, to research)
            research.append(s)
            continue
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
    for s in research:
        gs = [(_name(n, sc, s.iso, "AI", name_of)[0], sc, p) for n, sc, p in s.guesses]
        clarify.append(Clarify(s, guesses=gs))
    return entries, clarify


def _short(s: str, n: int) -> str:
    s = clean(s)
    return s if len(s) <= n else s[:n].rstrip(" -_.")


def out_ext(path: str, jpeg: bool) -> str:
    """The extension of a photo's copy in the list (.jpg for a RAW, with jpeg)."""
    ext = os.path.splitext(path)[1]
    return ".jpg" if jpeg and ext.lower() in RAW_EXTS else ext


def name_entry(e: Entry, jpeg: bool = False) -> str:
    ext = out_ext(e.pick.shot.path, jpeg)
    label = f"{e.species} ({e.scientific})" if e.scientific and e.scientific != e.species else e.species
    return os.path.join(_short(country_name(e.iso), 60), GROUP_FOLDERS.get(e.group, "Other animals"),
                        f"{_short(label, 120)}_{e.pick.date}{ext}")


def name_clarify(c: Clarify, jpeg: bool = False) -> str:
    s = c.sighting
    stem = os.path.splitext(os.path.basename(s.pick.path))[0]
    ext = out_ext(s.pick.path, jpeg)
    guesses = ", ".join(f"{n} {p:.0%}" for n, _sc, p in (c.guesses or s.guesses)[:3])
    if c.unknown:                               # (you don't know: kept to research)
        return os.path.join(_short(country_name(s.iso), 60), "To identify",
                            _short(s.unknown.strip() or "Unknown", 60),
                            f"{s.date}_{_short(stem, 40)} ({_short(guesses, 110)}){ext}")
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


def plan_copies(entries: list[Entry], clarify: list[Clarify], dest: str,
                jpeg: Optional[bool] = None) -> None:
    """Where each file goes. A species already in the list keeps its photo.
    jpeg: RAW photos become JPEGs in the list (None: as the list was made)."""
    done = load_list(dest)
    if jpeg is None:
        jpeg = bool(done.get("jpeg", False))
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
            e.dest = name_entry(e, jpeg)
            # copied but not recorded (a copy that stopped part-way: the record is
            # saved every 20 files): the copy of this species already there is kept
            if not os.path.exists(os.path.join(dest, e.dest)):
                found = _unrecorded(dest, e.dest, set(have.values()))
                if found:
                    e.dest, e.kept = found, True
    seen = done.get("clarify", {})
    for c in clarify:
        rel = seen.get(sighting_key(c.sighting))
        if rel and os.path.exists(os.path.join(dest, rel)):
            c.dest, c.kept = rel, True
        else:
            c.dest = name_clarify(c, jpeg)


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
                  "Original folder", "Your note", "Your examples"]


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
             e.how, e.dest, e.pick.pick.path, os.path.dirname(e.pick.pick.path), e.note, e.learned]
            for e in ordered]
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
                     len(s.photos), c.dest, s.pick.path, os.path.dirname(s.pick.path), s.learned,
                     s.unknown if s.unknown is not None else ""])
    p = os.path.join(dest, "To clarify.csv")
    _write_csv(p, ["Country", "Date", "Best guess", "Scientific name", "Probability", "2nd guess", "p",
                   "3rd guess", "p", "Photos in burst", "Copied to", "Original", "Original folder",
                   "Your examples", "To research (your hint)"], crow)
    # (names are the country's own where known, as in the species list)
    written.append(p)
    out = write_workbook(written, dest) if book else None
    return written + ([out] if out else [])


WORKBOOK = "Species list.xlsx"
LINK_COLUMNS = {"Original", "Original folder", "Photo kept", "Copied to"}


def find_original(path: str) -> Optional[str]:
    """The photo, or the same path on another drive letter (a USB drive plugged
    back in as E: instead of D:), or None."""
    if not path:
        return None
    if os.path.exists(path):
        return path
    import string
    drive, tail = os.path.splitdrive(path)
    if len(drive) == 2 and drive[1] == ":":
        for letter in string.ascii_uppercase:
            p = f"{letter}:{tail}"
            if letter != drive[0].upper() and os.path.exists(p):
                return p
    return None


def show_in_folder(path: str) -> bool:
    """Open the photo's folder in Explorer with the photo selected (Windows)."""
    found = find_original(path)
    if not found:
        return False
    import subprocess
    try:
        subprocess.Popen(["explorer", "/select,", os.path.normpath(found)])
        return True
    except OSError:
        return False


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
            # links: the original and its folder; the list's own copy (relative to
            # the workbook, so it still works if the list folder moves)
            for i, h in enumerate(rows[0], 1):
                if h not in LINK_COLUMNS:
                    continue
                for row in ws.iter_rows(min_row=2, min_col=i, max_col=i):
                    cell = row[0]
                    if isinstance(cell.value, str) and cell.value:
                        cell.hyperlink = cell.value
                        cell.style = "Hyperlink"
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
              stop: Optional[threading.Event] = None, jpeg: Optional[bool] = None) -> dict:
    """Copy the chosen photos (verified; RAW ones as JPEGs with jpeg) and write
    the CSV lists."""
    plan_copies(entries, clarify, dest, jpeg)
    data = load_list(dest)
    if jpeg is not None:
        data["jpeg"] = jpeg                 # (Review and later runs make the same)
    have, seen = data.setdefault("species", {}), data.setdefault("clarify", {})
    for e in entries:                       # (copies found in place, not yet recorded)
        if e.kept and e.dest:
            have.setdefault(entry_key(e), e.dest)
    todo = [(e.pick.shot.path, e.dest, "species", f"{e.iso or ''}|{e.scientific or e.species.lower()}",
             e.pick.shot.ts, f"{e.species} ({e.scientific})" if e.scientific else e.species)
            for e in entries if not e.kept]
    todo += [(c.sighting.pick.path, c.dest, "clarify", sighting_key(c.sighting),
              c.sighting.pick.ts, f"To clarify: {c.name}?") for c in clarify if not c.kept]
    out = {"copied": 0, "bytes": 0, "errors": [], "kept": sum(e.kept for e in entries)}
    os.makedirs(dest, exist_ok=True)
    _tidy_parts(dest)

    def save():
        tmp = os.path.join(dest, LIST_FILE + ".tmp")
        Path(tmp).write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, os.path.join(dest, LIST_FILE))
    for n, (src, rel, kind, key, when, what) in enumerate(todo, 1):
        if stop is not None and stop.is_set():
            break
        target = os.path.join(dest, rel)
        try:
            # (a photo you named in a Review that didn't get to resolve: its To clarify copy is moved)
            was = seen.get(_key(src)) if kind == "species" else None
            was = os.path.join(dest, was) if was else None
            if (was and os.path.exists(was) and not os.path.exists(target)
                    and os.path.splitext(was)[1].lower() == os.path.splitext(target)[1].lower()):
                os.makedirs(os.path.dirname(target), exist_ok=True)
                os.replace(was, target)
                seen.pop(_key(src), None)
            put_in(src, target, when, what)
            (have if kind == "species" else seen)[key] = rel
            if kind == "species":
                data.setdefault("shot", {})[key] = src             # (which frame the copy is)
            out["copied"] += 1
            out["bytes"] += os.path.getsize(target)
        except Exception as e:
            out["errors"].append(f"{src}: {e}")
        if n % 20 == 0:
            save()
        if progress:
            progress(n, len(todo), rel)
    save()
    if jpeg and not (stop is not None and stop.is_set()):
        out["converted"] = _convert_raw_copies(entries, clarify, dest, data, save, progress, stop, out)
    out["lists"] = write_lists(entries, clarify, dest)
    return out


def _convert_raw_copies(entries, clarify, dest, data, save, progress, stop, out) -> int:
    """RAW as JPEG: RAW copies already in the list (made before) become JPEGs too,
    made from the copy itself. The RAW copy goes once its JPEG is made."""
    have, seen = data.setdefault("species", {}), data.setdefault("clarify", {})
    todo = [("species", e, entry_key(e)) for e in entries if e.kept] + \
        [("clarify", c, sighting_key(c.sighting)) for c in clarify if c.kept]
    todo = [(kind, it, key) for kind, it, key in todo
            if os.path.splitext(it.dest)[1].lower() in RAW_EXTS and os.path.exists(os.path.join(dest, it.dest))]
    done = 0
    for n, (kind, it, key) in enumerate(todo, 1):
        if stop is not None and stop.is_set():
            break
        old = os.path.join(dest, it.dest)
        rel = os.path.splitext(it.dest)[0] + ".jpg"
        s = it.pick if kind == "species" else it.sighting
        what = (f"{it.species} ({it.scientific})" if it.scientific else it.species) if kind == "species" \
            else f"To clarify: {it.name}?"
        try:
            raw_to_jpeg(old, os.path.join(dest, rel), s.pick.ts or None, what, f"Original: {s.pick.path}")
            os.remove(old)                        # (the list's own RAW copy: the original stays)
            it.dest = rel
            (have if kind == "species" else seen)[key] = rel
            done += 1
        except Exception as e:
            out["errors"].append(f"{old}: {e}")
        if n % 10 == 0:
            save()
        if progress:
            progress(n, len(todo), f"RAW copy → JPEG: {rel}")
    save()
    return done


def put_in(src: str, target: str, when: Optional[float] = None, what: str = "") -> None:
    """A photo into the list: a JPEG made from a RAW (when the target is .jpg and
    the source a RAW), else a verified copy. Already there whole: left as it is."""
    convert = (os.path.splitext(target)[1].lower() == ".jpg"
               and os.path.splitext(src)[1].lower() in RAW_EXTS)
    if convert:
        if not os.path.exists(target):          # (made whole or not at all: .part, then renamed)
            raw_to_jpeg(src, target, when or None, what, f"Original: {src}")
    elif not (os.path.exists(target) and os.path.getsize(target) == os.path.getsize(src)):
        _copy_verified(src, [target], True)


# ── iNaturalist: photos ready to upload ──────────────────────────────────────

INAT_FOLDER = "iNaturalist"
INAT_SIDE = 2048            # long side of the photos for upload (what iNaturalist keeps)


def place_name(s: Sighting) -> str:
    """Where a sighting was, in words: the trip folder and the country."""
    trip = os.path.basename((s.place or "").rstrip("\\/"))
    country = country_name(s.iso)
    if not trip or trip.lower() == country.lower():
        return country
    if country == UNKNOWN or country.lower() in trip.lower():
        return trip
    return f"{trip}, {country}"


def inat_observations(entries: list[Entry], unit: str = "country", only_yours: bool = False) -> list[dict]:
    """One observation per species per country ("country": the historic backlog)
    or per species per trip ("trip": the trip folder). only_yours: just the
    sightings you named (not the AI's own)."""
    out = []
    for e in entries:
        ss = [s for s in e.sightings if not only_yours or s.how != "AI"]
        if not ss:
            continue
        if unit == "country":
            groups = [ss]
        else:
            by_trip: dict[str, list] = defaultdict(list)
            for s in ss:
                by_trip[_key(s.place or "")].append(s)
            groups = list(by_trip.values())
        out += [{"entry": e, "sightings": g} for g in groups]
    out.sort(key=lambda o: (country_name(o["entry"].iso), _type_order(o["entry"]),
                            min(x.date for x in o["sightings"])))
    return out


def inat_export(entries: list[Entry], dest: str, ident, unit: str = "country", only_yours: bool = False,
                progress: Optional[Callable[[int, int, str], None]] = None,
                stop: Optional[threading.Event] = None, chosen: Optional[dict] = None) -> dict:
    """Photos ready for iNaturalist's uploader, in <list>\\iNaturalist\\by country (or
    by trip): one per observation, its best frame (predicted), 2048 px JPEG with
    the date, and the species and place in its description and name; plus
    iNaturalist upload.csv to work from. The D850 records no location: drop
    the pin in the uploader (the place is in the name). Made again: what's
    there is kept."""
    obs = inat_observations(entries, unit, only_yours)
    root = os.path.join(dest, INAT_FOLDER, "by country" if unit == "country" else "by trip")
    out = {"made": 0, "kept": 0, "errors": [], "folder": root, "observations": len(obs)}
    rows = []
    for n, o in enumerate(obs, 1):
        if stop is not None and stop.is_set():
            break
        e = o["entry"]
        best = None
        mine = (chosen or {}).get(entry_key(e))           # (the photo you chose, if it's in this one)
        for s in o["sightings"]:
            for ph in s.photos:
                if mine and _key(ph.path) == _key(mine):
                    best = (1e9, s, ph)
        if best is None:                                   # (else the list's own choice of photo)
            s = e.pick if e.pick in o["sightings"] else max(o["sightings"], key=lambda x: x.quality)
            best = (0.0, s, s.shot)
        _sc, s, ph = best
        place = place_name(s)
        label = f"{e.species} ({e.scientific})" if e.scientific and e.scientific != e.species else e.species
        when = datetime.datetime.fromtimestamp(ph.ts) if ph.ts else None
        date = f"{when:%Y-%m-%d}" if when and when.year >= 2005 else "date unknown"
        folder = _short(country_name(e.iso), 60) if unit == "country" else \
            os.path.join(_short(country_name(e.iso), 60), _short(os.path.basename((s.place or "").rstrip("\\/"))
                                                                  or "Unknown trip", 60))
        rel = os.path.join(folder, f"{_short(label, 100)} - {date} - {_short(place, 60)}.jpg")
        target = os.path.join(root, rel)
        try:
            if os.path.exists(target):
                out["kept"] += 1
            else:
                raw_to_jpeg(ph.path, target, ph.ts or None, f"{label} - {place}", f"Original: {ph.path}",
                            max_side=INAT_SIDE)
                out["made"] += 1
        except Exception as ex:
            out["errors"].append(f"{ph.path}: {ex}")
            continue
        rows.append([rel, e.scientific, e.species, date, f"{when:%H:%M}" if when else "", place,
                     country_name(e.iso), len(o["sightings"]), sum(len(x.photos) for x in o["sightings"]),
                     "you" if any(x.how != "AI" for x in o["sightings"]) else e.how, ph.path])
        if progress:
            progress(n, len(obs), rel)
    _write_csv(os.path.join(root, "iNaturalist upload.csv"),
               ["File", "Scientific name", "Common name", "Date", "Time", "Place", "Country", "Sightings",
                "Photos", "Identified by", "Original"], rows)
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

    Decisions change only what the list should be: which photo is which
    species, and what's removed. apply() then makes the list folder match in
    one go: copies moved to their new places, copies no longer wanted held in
    a hidden folder (the Recycle Bin when Review closes), and a species whose
    photo you took away gets its next one copied in. Only copies inside the
    list folder are ever moved or removed.

    deferred: decisions are only recorded while you review (each one instant)
    and the files are changed when you resolve (Review). Otherwise each
    decision is applied straight away."""

    def __init__(self, entries: list[Entry], clarify: list[Clarify], dest: str,
                 trash: Callable[[str], None] = _trash, examples=None,
                 sightings: Optional[list[Sighting]] = None, name_of: Optional[Callable] = None,
                 background_save: bool = False, deferred: bool = False):
        self.entries, self.clarify, self.dest, self._trash = entries, clarify, dest, trash
        self.deferred = deferred
        # (Review saves in the background: the decisions and the examples are
        #  written after each decision without holding up the next photo)
        self._saver = _Saver() if background_save else None
        self._where: Optional[dict] = None       # (where each photo is: worked out once per change)
        self.examples = examples                 # (reviewer_refine.Examples: what the AI learns from)
        self.all = sightings if sightings is not None else []    # every sighting (for a re-check)
        self.name_of = name_of                   # (scientific, iso) → the country's common name
        self.data = load_list(dest)
        for k in ("species", "clarify", "decisions"):
            self.data.setdefault(k, {})
        self.jpeg = bool(self.data.get("jpeg", False))      # (RAW photos as JPEGs, as the list was made)
        self._undo: list = []
        self._changing = False                   # (in the middle of a change)
        self._batch = 0
        self._held = 0
        self._changed = False
        self._purge()                            # (left from a Review that didn't close)
        # where each photo's copy is now (its key → the file), and what was there to begin with
        self._files: dict[str, str] = {}
        for e in entries:
            p = os.path.join(dest, e.dest) if e.dest else ""
            if p and os.path.exists(p):
                self._files[sighting_key(e.pick)] = p
        for c in clarify:
            p = os.path.join(dest, c.dest) if c.dest else ""
            if p and os.path.exists(p):
                self._files[sighting_key(c.sighting)] = p
        self._had_photo = {entry_key(e) for e in entries if sighting_key(e.pick) in self._files}
        self._decided_before = json.loads(json.dumps(self.data["decisions"]))
        self._species_at_start = {entry_key(e) for e in entries}

    # what's where
    @staticmethod
    def _pk(s: Sighting) -> str:
        return sighting_key(s)

    def copy_of(self, s: Sighting) -> Optional[str]:
        """The photo's copy in the list folder, wherever it is now (None: no copy)."""
        p = self._files.get(self._pk(s))
        return p if p and os.path.exists(p) and not _within(p, os.path.join(self.dest, HOLD)) else None

    def on_disk(self, item) -> Optional[str]:
        return self.copy_of(self.pick(item)) if item is not None else None

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
        return self._pk(s) in self.data["decisions"]

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
        old = self.copy_of(pick)
        self._forget(item)
        for s in ss:
            if self.examples is not None and sci and not s.part:     # (the photo's main animal)
                a = s.pick
                self.examples.add(a.key, a.path, name, sci, iso, f"{s.place}|{s.date}",
                                  self._feature(s), note)
            s.species, s.scientific, s.group = name, sci, group
            s.p, s.how, s.certain, s.learned = 1.0, "yours", True, ""
            s.unknown = None
            s.note = note or s.note
            self.data["decisions"][self._pk(s)] = {"name": name, "sci": sci, "note": s.note}
        target = self.entry_for(iso, sci, name)
        where = country_name(iso)
        if target is None:
            e = Entry(iso, name, sci, group, list(ss), pick)
            e.dest = self._name(e, old)
            self.entries.append(e)
            self.entries.sort(key=_entry_order)
            msg = f"{name}: added to {where}"
        else:
            target.sightings += [s for s in ss if s not in target.sightings]
            if use_this:
                target.pick = pick
                target.dest = self._name(target, old)
                msg = f"{target.species}: already in {where}; now this photo is the one kept"
            else:
                msg = f"{target.species}: already in {where}; the photo there is kept, this one isn't"
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

    def choose_photo(self, e: Entry, s: Sighting, ph: Photo) -> str:
        """This frame is the species' best photo (for the list and for iNaturalist):
        its copy is replaced when you resolve, and Identify keeps your choice."""
        self._begin()
        try:
            e.pick, s.best = s, ph
            e.dest = self._name(e, None)
            self.data.setdefault("best", {})[entry_key(e)] = ph.path
            self._save()
            return f"{e.species}: this photo is the one kept (and for iNaturalist)"
        finally:
            self._end()

    def mark_unknown(self, s: Sighting, hint: str = "", note: str = "") -> str:
        """You don't know what it is: kept (not removed), to research. Re-check it
        within your hint (Review), or it goes to <Country>\\To identify\\<hint>."""
        self._begin()
        try:
            hint = hint.strip()
            kind, it = self.locate(s)
            if kind == "entry" and len(it.sightings) == 1:
                self._forget(it)                     # (its only photo: the species goes, the photo stays)
                kind = "removed"
            item = it if kind == "clarify" else (self._detach(it, s) if kind == "entry" else None)
            s.unknown, s.certain = hint, False
            if item is None:
                if kind == "removed" and self._pk(s) in self.data["decisions"]:
                    self.data["decisions"].pop(self._pk(s), None)
                if not any(x is s for x in self.all):
                    self.all.append(s)
                item = self._new_clarify(s)
            item.dest = self._clarify_name(item)
            self.data["decisions"][self._pk(s)] = {"unknown": True, "hint": hint, "note": note.strip()}
            if self.examples is not None and not s.part:    # (examples: the photo's main animal)
                self.examples.remove(s.pick.key)
            self._save()
            return "Kept to research" + (f": {hint}" if hint else "") + " (Re-check, or To identify when you resolve)"
        finally:
            self._end()

    def _clarify_name(self, c: Clarify) -> str:
        rel = name_clarify(c, self.jpeg)
        copy = self.copy_of(c.sighting) or self._files.get(self._pk(c.sighting))
        return os.path.splitext(rel)[0] + os.path.splitext(copy)[1] if copy else rel

    def set_guesses(self, c: Clarify, guesses: list, hint: str) -> None:
        """New guesses for a photo to research (a re-check within its hint)."""
        c.guesses, c.rechecked = list(guesses), hint
        c.dest = self._clarify_name(c)
        self._where = None

    def _skip(self, s: Sighting, note: str) -> None:
        s.unknown = None
        self.data["decisions"][self._pk(s)] = {"skip": True, "note": note.strip()}
        if self.examples is not None and not s.part:        # (examples: the photo's main animal)
            self.examples.remove(s.pick.key)

    def _reject_clarify(self, c: Clarify, note: str) -> str:
        self._skip(c.sighting, note)
        self._forget(c)
        return "Removed" + (f": {note.strip()}" if note.strip() else "")

    def _reject_entry(self, e: Entry, note: str) -> str:
        where, name = country_name(e.iso), e.species
        gone = e.pick
        self._skip(gone, note)
        e.sightings = [s for s in e.sightings if s is not gone]
        if self.all:
            self.all[:] = [s for s in self.all if s is not gone]
        if e.sightings:                          # seen elsewhere: the next best photo
            e.pick = max(e.sightings, key=lambda s: (s.quality, -s.pick.ts))
            e.dest = self._name(e, self.copy_of(e.pick))
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
            self._new_clarify(s)
        return min(len(found), CLARIFY_PER_GUESS)

    # one photo at a time, as often as you like (Review's filmstrip)
    def locate(self, s: Sighting) -> tuple[str, object]:
        """Where a photo is now: ('clarify', its Clarify), ('entry', the species it's
        part of) or ('removed', None)."""
        if not self._changing:                   # (between changes: looked up, not searched)
            if self._where is None:
                where = {id(c.sighting): ("clarify", c) for c in self.clarify}
                for e in self.entries:
                    for x in e.sightings:
                        where[id(x)] = ("entry", e)
                self._where = where
            return self._where.get(id(s), ("removed", None))
        for c in self.clarify:
            if c.sighting is s:
                return "clarify", c
        for e in self.entries:
            if any(x is s for x in e.sightings):
                return "entry", e
        return "removed", None

    def guesses_for(self, s: Sighting) -> list:
        """The model's top guesses for a photo, named as in the country's list."""
        return [(_name(n, sc, s.iso, "AI", self.name_of)[0], sc, p) for n, sc, p in (s.guesses or [])]

    def assign_photo(self, s: Sighting, name: str, sci: str = "", group: Optional[str] = None,
                     use_this: bool = False, note: str = "") -> str:
        """Name this one photo, whatever was decided about it before (it's taken
        out of the species it was part of, or brought back if it was removed)."""
        self._begin()
        try:
            return self._assign(self._item_for(s), name, sci, group, use_this, note)
        finally:
            self._end()

    def reject_photo(self, s: Sighting, note: str = "") -> str:
        """Remove this one photo, whatever was decided about it before."""
        self._begin()
        try:
            item = self._item_for(s)
            msg = self._reject_entry(item, note) if isinstance(item, Entry) else self._reject_clarify(item, note)
            self._save()
            return msg
        finally:
            self._end()

    def _item_for(self, s: Sighting):
        kind, it = self.locate(s)
        if kind == "clarify":
            return it
        if kind == "entry":
            return it if len(it.sightings) == 1 else self._detach(it, s)
        return self._restore(s)

    def _new_clarify(self, s: Sighting) -> Clarify:
        c = Clarify(s, guesses=self.guesses_for(s))
        c.dest = self._clarify_name(c)               # (its copy keeps its type; held ones can come back)
        self.clarify.append(c)
        return c

    def _detach(self, e: Entry, s: Sighting) -> Clarify:
        """Take one photo out of a species (the species keeps its other sightings;
        if this was its photo, the next best one takes its place)."""
        was_pick = e.pick is s
        e.sightings = [x for x in e.sightings if x is not s]
        c = self._new_clarify(s)
        if was_pick:
            e.pick = max(e.sightings, key=lambda x: (x.quality, -x.pick.ts))
            e.dest = self._name(e, self.copy_of(e.pick))
        return c

    def _restore(self, s: Sighting) -> Clarify:
        """Bring back a photo you removed (its copy too, until Review closes)."""
        self.data["decisions"].pop(self._pk(s), None)
        if not any(x is s for x in self.all):
            self.all.append(s)
        return self._new_clarify(s)

    def undo(self) -> str:
        """Take back the last change (and what the AI learns from)."""
        if not self._undo:
            return "Nothing to undo"
        snap = self._undo.pop()
        sightings, entries, entry_state, clarify, clarify_state, everything, data, examples = snap
        for x, values in sightings:              # (the same objects, as they were)
            for f, v in zip(self._S_FIELDS, values):
                setattr(x, f, v)
        for e, state, its in entry_state:
            e.__dict__.update(state)
            e.sightings = list(its)
        for c, state in clarify_state:
            c.__dict__.update(state)
        self.entries[:] = entries
        self.clarify[:] = clarify
        self.all[:] = everything
        decisions = data.get("decisions", {})
        self.data["decisions"] = decisions       # (what's on disk stays as apply() left it)
        self._where = None
        if self.examples is not None and examples is not None:
            self.examples.restore(examples)
        self._save()
        if not self.deferred:
            self.apply()
        return "Undone"

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    # resolving: the list folder made to match
    def decided_count(self) -> int:
        """How many photos you've decided since the last resolve (quick: no files looked at)."""
        before = self._decided_before
        return sum(1 for k, v in self.data["decisions"].items() if before.get(k) != v)

    def summary(self) -> dict:
        """What you decided this time and what resolving will do to the files."""
        before, now = self._decided_before, self.data["decisions"]
        changed = {k: v for k, v in now.items() if before.get(k) != v}
        named = [v for v in changed.values() if not v.get("skip") and not v.get("unknown")]
        removed = [v for v in changed.values() if v.get("skip")]
        research = [v for v in changed.values() if v.get("unknown")]
        reasons: dict[str, int] = defaultdict(int)
        for v in removed:
            reasons[v.get("note") or "no reason given"] += 1
        plan = self._plan()
        return {"decided": len(changed), "named": len(named), "removed": len(removed), "research": len(research),
                "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
                "new_species": sum(1 for e in self.entries if entry_key(e) not in self._species_at_start),
                "moves": len(plan["move"]), "drops": len(plan["drop"]), "copy_in": len(plan["copy_in"]),
                "to_copy": plan["to_copy"], "pending": bool(plan["move"] or plan["drop"] or plan["copy_in"])}

    def _new_frame(self, e: Entry) -> bool:
        """You chose a frame other than the one its copy was made from."""
        chosen = self.data.get("best", {}).get(entry_key(e))
        if not chosen or _key(chosen) != _key(e.pick.shot.path):
            return False
        made = self.data.get("shot", {}).get(entry_key(e)) or e.pick.pick.path
        return _key(made) != _key(chosen)

    def _want(self) -> dict[str, tuple]:
        want = {}
        for e in self.entries:
            want[self._pk(e.pick)] = ("species", e)
        for c in self.clarify:
            want[self._pk(c.sighting)] = ("clarify", c)
        return want

    def _plan(self) -> dict:
        hold = os.path.join(self.dest, HOLD)
        want = self._want()
        drop = [k for k, p in self._files.items() if k not in want and not _within(p, hold)]
        move, copy_in, to_copy = [], [], 0
        for k, (kind, it) in want.items():
            target = os.path.join(self.dest, it.dest)
            have = self._files.get(k)
            if have and os.path.exists(have) and kind == "species" and self._new_frame(it):
                copy_in.append(it)               # (you chose another frame of it: that one copied in)
            elif have and os.path.exists(have):
                if _key(have) != _key(target):
                    move.append((k, have, target))
            elif kind == "species" and entry_key(it) in self._had_photo:
                copy_in.append(it)               # (its photo was taken away: the next one copied in)
            else:
                to_copy += 1                     # (Copy list copies it)
        return {"drop": drop, "move": move, "copy_in": copy_in, "to_copy": to_copy}

    def apply(self, progress: Optional[Callable[[str], None]] = None, book: bool = False) -> dict:
        """Make the list folder match the decisions. Returns the counts."""
        say = progress or (lambda _m: None)
        plan = self._plan()
        hold = os.path.join(self.dest, HOLD)
        out = {"moved": 0, "held": 0, "copied": 0, "to_copy": plan["to_copy"], "errors": []}
        # 1. copies no longer wanted: held (the Recycle Bin when Review closes)
        for n, k in enumerate(plan["drop"], 1):
            if n % 20 == 1:
                say(f"Removing copies {n} / {len(plan['drop'])}…")
            try:
                self._files[k] = self._hold(self._files[k])
                out["held"] += 1
            except OSError as e:
                out["errors"].append(str(e))
        # 2. moves, through a staging folder (two photos can swap places)
        stage = os.path.join(hold, "moving")
        staged = []
        for n, (k, src, dst) in enumerate(plan["move"]):
            try:
                tmp = os.path.join(stage, f"{n:05d}_{os.path.basename(src)}")
                os.makedirs(stage, exist_ok=True)
                os.replace(src, tmp)
                if not _within(src, hold):
                    self._tidy(os.path.dirname(src))
                staged.append((k, tmp, dst))
            except OSError as e:
                out["errors"].append(str(e))
        for n, (k, tmp, dst) in enumerate(staged, 1):
            if n % 20 == 1:
                say(f"Moving copies {n} / {len(staged)}…")
            try:
                if os.path.exists(dst):              # (something stray in the way)
                    self._hold(dst)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                os.replace(tmp, dst)
                self._files[k] = dst
                out["moved"] += 1
            except OSError as e:
                out["errors"].append(str(e))
        try:
            os.rmdir(stage)
        except OSError:
            pass
        # 3. a species whose photo you took away: its next one copied in
        for n, e in enumerate(plan["copy_in"], 1):
            say(f"Copying in {n} / {len(plan['copy_in'])}: {e.species}…")
            target = os.path.join(self.dest, e.dest)
            try:
                old = self._files.get(self._pk(e.pick))
                if old and os.path.exists(old) and not _within(old, hold):
                    self._hold(old)                  # (the frame copied before)
                if os.path.exists(target):
                    self._hold(target)
                put_in(e.pick.shot.path, target, e.pick.shot.ts,
                       f"{e.species} ({e.scientific})" if e.scientific else e.species)
                self._files[self._pk(e.pick)] = target
                self.data.setdefault("shot", {})[entry_key(e)] = e.pick.shot.path
                out["copied"] += 1
            except Exception as ex:                    # (unplugged…: Copy list copies it)
                out["errors"].append(f"{e.pick.pick.path}: {ex}")
        # 4. what the list records
        species, clarify = {}, {}
        managed = set(self._files) | set(self._want())
        for k, rel in self.data.get("clarify", {}).items():
            if k not in managed:
                clarify[k] = rel                         # (photos Review doesn't show: left as they are)
        for e in self.entries:
            p = self._files.get(self._pk(e.pick))
            e.kept = bool(p) and _key(p) == _key(os.path.join(self.dest, e.dest))
            if e.kept:
                species[entry_key(e)] = e.dest
        for c in self.clarify:
            p = self._files.get(self._pk(c.sighting))
            c.kept = bool(p) and _key(p) == _key(os.path.join(self.dest, c.dest))
            if c.kept:
                clarify[self._pk(c.sighting)] = c.dest
        self.data["species"], self.data["clarify"] = species, clarify
        self._had_photo |= set(species)
        self._save()
        self.flush()
        say("Writing the lists…")
        write_lists(self.entries, self.clarify, self.dest, book=book)
        self._changed = not book
        return out

    def flush(self) -> None:
        """Wait until everything decided is written."""
        if self._saver is not None:
            self._saver.flush()

    def close(self) -> None:
        """Review closed: resolved if it wasn't; removed copies go to the Recycle
        Bin (no more undo); the Excel workbook brought up to date."""
        if self._plan()["move"] or self._plan()["drop"] or self._plan()["copy_in"]:
            self.apply()
        self.flush()
        self._undo.clear()
        self._purge()
        if self._changed:
            write_lists(self.entries, self.clarify, self.dest)
            self._changed = False

    # (helpers)
    feature_of: Optional[Callable] = None        # photo key → fingerprint (packed), if read

    def _feature(self, s: Sighting) -> Optional[str]:
        return self.feature_of(s.pick.key) if self.feature_of else None

    _S_FIELDS = ("species", "scientific", "group", "p", "how", "certain", "learned", "note", "unknown", "best")

    def batch(self):
        """Many decisions as one (a gallery selection): one undo step, one save.

            with editor.batch():
                for s in chosen:
                    editor.reject_photo(s, "duplicate")
        """
        from contextlib import contextmanager

        @contextmanager
        def run():
            self._begin()
            self._batch += 1
            try:
                yield self
            finally:
                self._batch -= 1
                self._save()
                self._end()
        return run()

    def _begin(self) -> None:
        """Remember what a change can alter, for undo: only the parts that change
        (the photos themselves never do), so it's quick on a big list."""
        if self._batch:
            return                               # (inside a batch: begun once)
        self._where = None
        seen: dict = {}
        for x in self.all + [y for e in self.entries for y in e.sightings] + [c.sighting for c in self.clarify]:
            seen[id(x)] = x
        sightings = [(x, tuple(getattr(x, f) for f in self._S_FIELDS)) for x in seen.values()]
        entries = [(e, dict(e.__dict__), list(e.sightings)) for e in self.entries]
        clarify = [(c, dict(c.__dict__)) for c in self.clarify]
        self._pending = (sightings, list(self.entries), entries, list(self.clarify), clarify, list(self.all),
                         {"decisions": json.loads(json.dumps(self.data["decisions"]))},
                         self.examples.snapshot() if self.examples is not None else None)
        self._changing = True

    def _end(self) -> None:
        if self._batch:
            return
        if self._changing:
            self._undo.append(self._pending)
            del self._undo[:-UNDO_STEPS]
        self._changing = False
        self._where = None
        if not self.deferred:
            self.apply()

    def _name(self, e: Entry, old: Optional[str]) -> str:
        """Where a species' photo goes; a copy being moved keeps its own type
        (a RAW copied before JPEGs were chosen stays a RAW)."""
        rel = name_entry(e, self.jpeg)
        return os.path.splitext(rel)[0] + os.path.splitext(old)[1] if old else rel

    def _forget(self, item) -> None:
        if isinstance(item, Entry):
            self.entries.remove(item)
        else:
            self.clarify.remove(item)

    def _hold(self, path: str) -> str:
        """Into the hidden holding folder (Recycle Bin when Review closes)."""
        hold = os.path.join(self.dest, HOLD)
        if not _within(path, self.dest) or _key(path) == _key(self.dest) or _within(path, hold):
            return path                          # (only the list's own copies)
        self._held += 1
        held = os.path.join(hold, f"{self._held:05d}_{os.path.basename(path)}")
        os.makedirs(hold, exist_ok=True)
        os.replace(path, held)
        self._tidy(os.path.dirname(path))
        return held

    def _purge(self) -> None:
        hold = os.path.join(self.dest, HOLD)
        if not os.path.isdir(hold):
            return
        for name in os.listdir(hold):
            p = os.path.join(hold, name)
            if os.path.isdir(p):
                continue
            try:
                self._trash(p)
            except OSError:
                pass
        try:
            os.rmdir(hold)
        except OSError:
            pass
        files = getattr(self, "_files", {})
        for k in [k for k, p in files.items() if _within(p, hold)]:
            del files[k]

    def _tidy(self, folder: str) -> None:
        """Remove folders left empty (a '… - possible' folder that's been sorted)."""
        while _within(folder, self.dest) and _key(folder) != _key(self.dest):
            try:
                os.rmdir(folder)
            except OSError:
                return
            folder = os.path.dirname(folder)

    def _save(self) -> None:
        """The record (with your decisions) and the examples; in the background for Review."""
        self._changed = True
        self._where = None
        if self._batch:
            return                               # (once, at the end of the batch)
        record = json.dumps(self.data, indent=1)
        if self._saver is None:
            self._write(record)
        else:
            self._saver.put(lambda: self._write(record))

    def _write(self, record: str) -> None:
        os.makedirs(self.dest, exist_ok=True)
        tmp = os.path.join(self.dest, LIST_FILE + ".tmp")
        Path(tmp).write_text(record, encoding="utf-8")
        os.replace(tmp, os.path.join(self.dest, LIST_FILE))
        if self.examples is not None:
            self.examples.save()


class _Saver:
    """Writes in the background, one job at a time; a newer job replaces one
    still waiting (deciding quickly writes once, with the latest)."""

    def __init__(self):
        self._job = None
        self._cv = threading.Condition()
        self._busy = False
        self.error: Optional[str] = None
        threading.Thread(target=self._run, daemon=True).start()

    def put(self, job: Callable[[], None]) -> None:
        with self._cv:
            self._job = job
            self._cv.notify_all()

    def _run(self) -> None:
        while True:
            with self._cv:
                while self._job is None:
                    self._cv.wait()
                job, self._job = self._job, None
                self._busy = True
            try:
                job()
                self.error = None
            except Exception as e:                # (shown by Review; tried again next time)
                self.error = str(e)[:200]
            finally:
                with self._cv:
                    self._busy = False
                    self._cv.notify_all()

    def flush(self, timeout: float = 120.0) -> None:
        end = time.time() + timeout
        with self._cv:
            while (self._job is not None or self._busy) and time.time() < end:
                self._cv.wait(0.2)


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
    _fill_new_fields(got)
    return got


def _fill_new_fields(got: dict) -> None:
    """Results saved by an earlier version lack fields added since: their defaults."""
    import dataclasses
    seen: set = set()

    def fill(obj) -> None:
        if id(obj) in seen or not dataclasses.is_dataclass(obj):
            return
        seen.add(id(obj))
        for f in dataclasses.fields(obj):
            if f.name not in obj.__dict__:
                obj.__dict__[f.name] = (f.default_factory() if f.default_factory is not dataclasses.MISSING
                                        else f.default if f.default is not dataclasses.MISSING else None)
    for s in got.get("res", {}).get("sightings", []):
        fill(s)
    for e in got.get("entries", []):
        fill(e)
        for s in e.sightings:
            fill(s)
    for c in got.get("clarify", []):
        fill(c)
        fill(c.sighting)
