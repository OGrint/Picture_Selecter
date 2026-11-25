"""
Collect Best folders into one library (Photo Reviewer).

Finds every Best folder under a parent folder (any depth), keeps only the final
screening of each trip, and plans copies into

    <collection>/<year>/<YYYY-MM Trip>/<Species>/<Species>_<date>_<Trip>_<###>.<ext>

with each photo's edits beside it:

    Iberian-Lynx_2026-03-14_Spain_001.NEF        original
    Iberian-Lynx_2026-03-14_Spain_001_topaz.dng  Topaz
    Iberian-Lynx_2026-03-14_Spain_001_final.jpg  darktable export

Rules
- A Best folder is one named "Best" or "Final best" (any case). Other folders
  with "best" in the name (e.g. "Best Combined", "24 Best") are left alone and
  listed in the preview.
- Re-screening: inside a Best folder, a deeper non-empty Best is the final
  decision, so only the deepest is collected; a non-empty "Final best" beside
  "Best" replaces it. Empty inner Best folders are ignored.
- Edits are files in sub-folders of a trip's Best folders (any level), plus
  "tp-…" files in a Best folder itself: in a folder called edited / edit /
  export / final(s) → "final" (darktable exports); otherwise "tp-…" or in a "tp"
  folder → "topaz"; anything else → "edit". Every version is kept and labelled;
  an edit is matched to its original by the DSC number in its name and the
  capture time.
- Byte-identical copies of the same photo in two trips are collected once.
- Copies only (originals are never moved). Re-running adds only what's new;
  collection.json in the collection folder records what was copied from where.
No Tkinter here.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from PIL import Image

from reviewer_imaging import BITMAP_EXTS, RAW_EXTS, _capture_timestamp
from reviewer_import import _copy_verified

MANIFEST = "collection.json"
BEST_NAMES = {"best", "final best"}
FINAL_DIRS = {"edited", "edit", "edits", "export", "exports", "final", "finals"}
EDIT_EXTS = {".jpg", ".jpeg", ".tif", ".tiff", ".dng", ".png", ".heic", ".webp"}
IMAGE_EXTS = set(RAW_EXTS) | set(BITMAP_EXTS) | EDIT_EXTS
MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], 1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
_DSC = re.compile(r"(?:^|[^a-z0-9])(?:tp-)?(?:dsc|img|_dsc)[_-]?(\d{4,5})(?!\d)")
_TEMPLATE = re.compile(r"^(?P<species>[A-Za-z][A-Za-z' .-]*?)_(?P<date>\d{4}-\d{2}-\d{2})_")
_BAD = re.compile(r'[\\/:*?"<>|]+')


def norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def is_best_dir(name: str) -> bool:
    return norm_name(name) in BEST_NAMES


def _images(d: str) -> list[str]:
    try:
        return sorted(os.path.join(d, f) for f in os.listdir(d)
                      if os.path.splitext(f)[1].lower() in IMAGE_EXTS
                      and os.path.isfile(os.path.join(d, f)))
    except OSError:
        return []


def _subdirs(d: str) -> list[str]:
    try:
        return sorted(os.path.join(d, f) for f in os.listdir(d)
                      if os.path.isdir(os.path.join(d, f)))
    except OSError:
        return []


def _within(path: str, folder: str) -> bool:
    p, f = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(folder))
    return p == f or p.startswith(f.rstrip("\\/") + os.sep)


@dataclass
class Trip:
    folder: str                       # the shoot folder holding the Best folder(s)
    final: str                        # the Best folder whose photos are collected
    best_dirs: list[str]              # every Best folder of this trip (all levels)
    originals: list[str] = field(default_factory=list)       # from `final`
    all_originals: list[str] = field(default_factory=list)   # every Best level (to match edits)
    edits: list[tuple[str, str]] = field(default_factory=list)   # (path, kind)
    superseded: list[str] = field(default_factory=list)      # Best levels not collected
    shoot: list[str] = field(default_factory=list)           # photos in the shoot folder itself


def _deepest(best: str) -> str:
    """The final screening inside a Best folder: its deepest non-empty Best."""
    kids = [c for c in _subdirs(best) if is_best_dir(os.path.basename(c))]
    deeper = [_deepest(c) for c in kids]
    deeper = [d for d in deeper if _images(d)]
    if deeper:
        return max(deeper, key=lambda d: (len(Path(d).parts), len(_images(d))))
    return best


def _edit_kind(rel_parts: list[str], name: str) -> str:
    parts = [norm_name(p) for p in rel_parts]
    if any(p in FINAL_DIRS for p in parts):
        return "final"
    if name.lower().startswith("tp-") or "tp" in parts or "topaz" in parts:
        return "topaz"
    return "edit"


def find_trips(parent: str, exclude: Optional[str] = None) -> tuple[list[Trip], list[str]]:
    """
    The trips under parent and what was left out: ([Trip], [notes]). exclude
    (the collection folder) is never searched.
    """
    trips: dict[str, list[str]] = defaultdict(list)       # shoot folder → its top Best folders
    notes: list[str] = []
    for root, dirs, _files in os.walk(parent):
        if exclude and _within(root, exclude):
            dirs[:] = []
            continue
        keep = []
        for d in dirs:
            full = os.path.join(root, d)
            if exclude and _within(full, exclude):
                continue
            if is_best_dir(d):
                if not is_best_dir(os.path.basename(root)):
                    trips[root].append(full)              # a top-level Best of this shoot
                continue                                  # (its insides are handled below)
            if "best" in d.lower():
                n = len(_images(full))
                notes.append(f"Not a Best folder, left alone: {full} ({n} photos)")
            keep.append(d)
        dirs[:] = keep
    out = []
    for folder, tops in sorted(trips.items()):
        finals = [(_deepest(b), b) for b in tops]
        nonempty = [(f, b) for f, b in finals if _images(f)]
        if not nonempty:
            notes.append(f"Empty Best folder(s), nothing to collect: {folder}")
            continue
        # "Final best" beats "Best" at the same level; then the deeper screening
        nonempty.sort(key=lambda fb: (norm_name(os.path.basename(fb[1])) == "final best",
                                      len(Path(fb[0]).parts)), reverse=True)
        final = nonempty[0][0]
        best_dirs = []
        for b in tops:
            for r, ds, _f in os.walk(b):
                if is_best_dir(os.path.basename(r)):
                    best_dirs.append(r)
        t = Trip(folder=folder, final=final, best_dirs=best_dirs,
                 shoot=[p for p in _images(folder)
                        if os.path.splitext(p)[1].lower() in set(RAW_EXTS) | set(BITMAP_EXTS)])
        for b in best_dirs:
            imgs = _images(b)
            raws = [p for p in imgs if not os.path.basename(p).lower().startswith("tp-")
                    and os.path.splitext(p)[1].lower() in set(RAW_EXTS) | set(BITMAP_EXTS)]
            t.all_originals += raws
            if os.path.normcase(b) == os.path.normcase(final):
                t.originals = raws
            elif raws:
                t.superseded.append(b)
            # tp-… files saved next to the originals
            t.edits += [(p, "topaz") for p in imgs if os.path.basename(p).lower().startswith("tp-")]
        # edits: files in the non-Best sub-folders of any Best level
        for b in best_dirs:
            for c in _subdirs(b):
                if is_best_dir(os.path.basename(c)):
                    continue
                for r, ds, fs in os.walk(c):
                    ds[:] = [x for x in ds if not is_best_dir(x)]
                    rel = os.path.relpath(r, b).split(os.sep)
                    for f in sorted(fs):
                        if os.path.splitext(f)[1].lower() in EDIT_EXTS | set(RAW_EXTS):
                            t.edits.append((os.path.join(r, f), _edit_kind(rel, f)))
        for s in t.superseded:
            notes.append(f"Re-screened, using the deeper selection instead: {s}")
        out.append(t)
    return out, notes


# ── trip and species names ───────────────────────────────────────────────────

def _is_generic(part: str) -> bool:
    p = norm_name(part)
    return (p.isdigit() or p in MONTHS or re.fullmatch(r"(19|20)?\d{2}", p) is not None
            or is_best_dir(p) or p in ("photos", "pictures", "dcim"))


def trip_label(folder: str, parent: str) -> str:
    """The most descriptive folder name on the way from parent to the trip
    ('26/Spain' → 'Spain', '26/May/2' → 'May')."""
    rel = Path(os.path.relpath(folder, parent)).parts if _within(folder, parent) else Path(folder).parts
    rel = [p for p in rel if p not in (".", "")]
    for p in reversed(rel):
        if not _is_generic(p):
            return clean(re.sub(r"^\d{4}-\d{2}(-\d{2})?\s*(to\s+\d{2}-\d{2}\s*)?", "", p)) or p
    for p in reversed(rel):
        if norm_name(p) in MONTHS:
            return p.strip().title()
    return clean(os.path.basename(folder)) or "Trip"


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", _BAD.sub("-", s or "")).strip().rstrip(". ")


def species_folder(name: str) -> str:
    n = clean(name) or "Unidentified"
    return n[:1].upper() + n[1:]


def _hyphen(s: str) -> str:
    return re.sub(r"[\s_]+", "-", clean(s)).strip("-")


def species_from_name(filename: str) -> Optional[str]:
    """'Grey-Heron_2026-05-12_Minsmere_001.NEF' → 'Grey Heron' (named by the app)."""
    m = _TEMPLATE.match(os.path.basename(filename))
    if not m:
        return None
    s = m.group("species").replace("-", " ").strip()
    return None if s.lower() == "unidentified" else s


def dsc_number(filename: str) -> Optional[int]:
    m = _DSC.search(os.path.basename(filename).lower())
    return int(m.group(1)) if m else None


def _read_json(p: str) -> dict:
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class SpeciesLookup:
    """What the app knows about a photo's species, from the folders' session files."""

    def __init__(self):
        self._by_folder: dict[str, tuple[dict, dict, dict]] = {}

    def _load(self, folder: str):
        if folder not in self._by_folder:
            status = _read_json(os.path.join(folder, "photo_reviewer_status.json"))
            by_name, by_best = {}, {}
            for path, e in status.items():
                if not isinstance(e, dict):
                    continue
                by_name[os.path.basename(path).lower()] = e
                if e.get("best_name"):
                    by_best[str(e["best_name"]).lower()] = (os.path.basename(path), e)
            preds = {}
            raw = _read_json(os.path.join(folder, "photo_reviewer_species.json"))
            for name, e in raw.items():
                if isinstance(e, dict) and e.get("top"):
                    preds[name.lower()] = e["top"]
            self._by_folder[folder] = (by_name, by_best, preds)
        return self._by_folder[folder]

    def lookup(self, path: str, trip_folder: str) -> tuple[str, str, Optional[str]]:
        """(species, how, original file name) — how: confirmed | name | suggested | unknown."""
        name = os.path.basename(path).lower()
        stem = os.path.splitext(name)[0]
        original = None
        for folder in (os.path.dirname(path), trip_folder):
            by_name, by_best, _p = self._load(folder)
            e = by_name.get(name)
            if e is None and stem in by_best:
                original, e = by_best[stem]
            if e and e.get("species"):
                return e["species"], "confirmed", original
        s = species_from_name(os.path.basename(path))        # (original case)
        if s:
            return s, "name", original
        for folder in (os.path.dirname(path), trip_folder):
            top = self._load(folder)[2].get(name) or (
                self._load(folder)[2].get(original.lower()) if original else None)
            if top and float(top[0][2]) >= SUGGEST_MIN:
                return top[0][0], "suggested", original
        return "Unidentified", "unknown", original


SUGGEST_MIN = 0.5            # species suggestions at least this sure name a folder


# ── the plan ─────────────────────────────────────────────────────────────────

@dataclass
class Item:
    src: str
    kind: str                     # original | topaz | final | edit
    trip: str                     # "2026-03 Spain"
    year: str
    species: str
    how: str                      # confirmed | name | suggested | unknown | (edits: of original)
    ts: float
    size: int
    of: Optional[str] = None      # an edit's original (source path)
    dest: str = ""                # relative to the collection folder
    include: bool = True
    note: str = ""
    sidecars: list[tuple[str, str]] = field(default_factory=list)   # (src, dest rel)


def _ts(p: str) -> float:
    """Capture time from the file's EXIF header (fast: NEF, DNG, TIFF and JPEG
    keep it in a TIFF/EXIF header; nothing is decoded), else the file time."""
    try:
        with Image.open(p) as im:
            exif = im.getexif()
            sub = exif.get_ifd(0x8769)
            dt = sub.get(36867) or exif.get(306)
            if dt:
                t = datetime.datetime.strptime(str(dt).strip("\x00 ")[:19], "%Y:%m:%d %H:%M:%S")
                frac = str(sub.get(37521) or "").strip("\x00 ")
                return t.timestamp() + (float("0." + frac) if frac.isdigit() else 0.0)
    except Exception:
        pass
    try:
        if os.path.splitext(p)[1].lower() in (".nef", ".cr2", ".arw"):
            return _capture_timestamp(p) or os.path.getmtime(p)
    except Exception:
        pass
    return os.path.getmtime(p)


def _times(paths: list[str]) -> dict[str, float]:
    """Capture times for many files, read in parallel (I/O bound)."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as ex:
        return dict(zip(paths, ex.map(_ts, paths)))


def _fingerprint(p: str) -> str:
    h = hashlib.blake2b(digest_size=16)
    size = os.path.getsize(p)
    with open(p, "rb") as f:
        h.update(f.read(1 << 20))
        if size > 2 << 20:
            f.seek(-(1 << 20), os.SEEK_END)
            h.update(f.read(1 << 20))
    return f"{size}:{h.hexdigest()}"


def load_manifest(dest: str) -> dict:
    data = _read_json(os.path.join(dest, MANIFEST))
    return data.get("items", {}) if isinstance(data, dict) else {}


def save_manifest(dest: str, items: dict) -> None:
    p = os.path.join(dest, MANIFEST)
    tmp = p + ".tmp"
    Path(tmp).write_text(json.dumps({"version": 1, "items": items}, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _key(p: str) -> str:
    return os.path.normcase(os.path.abspath(p))


class _Collected:
    """
    What's already in the collection, found by source path — or, if the source
    folder was renamed or moved since, by the same file (size + capture time to a
    fraction of a second + extension), so a renamed trip isn't copied again.
    """

    def __init__(self, manifest: dict, dest: str):
        self.manifest, self.dest = manifest, dest
        self.by_content = {}
        for e in manifest.values():
            if "ts" in e:
                self.by_content[(e.get("size"), round(float(e["ts"]), 1),
                                 os.path.splitext(e["dest"])[1].lower(), e.get("kind"))] = e

    def find(self, src: str, size: int, ts: float, kind: str) -> Optional[dict]:
        e = self.manifest.get(_key(src))
        if e is None:
            e = self.by_content.get((size, round(ts, 1), os.path.splitext(src)[1].lower(), kind))
        if e and e.get("size") == size and os.path.exists(os.path.join(self.dest, e["dest"])):
            return e
        return None


def _path_date(folder: str) -> tuple[Optional[int], Optional[int]]:
    """(year, month) named by the folders on a path ('…/26/November' → 2026, 11)."""
    year = month = None
    this = datetime.date.today().year
    for part in Path(folder).parts:
        p = norm_name(part)
        m = re.match(r"^((?:19|20)\d{2})(?:\b|-)", p) or re.match(r"^(\d{2})(?:\s|$)", p)
        if m:
            y = int(m.group(1))
            y = y + 2000 if y < 100 else y
            if 1990 <= y <= this + 1:
                year = y
        word = p.split(" ")[0]
        if word in MONTHS:
            month = MONTHS[word]
    return year, month


def _clock_off(ts: float, path_year: Optional[int]) -> bool:
    """A capture date that can't be right (camera clock never set, etc.)."""
    y = datetime.datetime.fromtimestamp(ts).year
    return y < 2005 or y > datetime.date.today().year + 1 or (path_year is not None and y != path_year)


@dataclass
class Frame:
    src: str                      # the original (collected or not)
    species: str
    how: str
    ts: float
    date: str                     # YYYY-MM-DD, or YYYY-MM when the camera clock was wrong
    trip: str
    year: str


def plan(parent: str, dest: str, trip_names: Optional[dict] = None,
         species_override: Optional[dict] = None, identify: Optional[Callable] = None,
         progress: Optional[Callable[[str], None]] = None, stop: Optional[threading.Event] = None,
         home_region: Optional[str] = None) -> tuple[list[Item], list[str], dict]:
    """
    Plan the collection: (items, notes, trips) where trips maps each shoot folder
    to its "YYYY-MM Label" name. trip_names / species_override (original src →
    species) apply your edits from the preview. identify(path) → (species, prob)
    names photos the app knows nothing about (optional, slower); identify(path, iso)
    is called with the trip's country (named in its path, else home_region) so
    suggestions are re-ranked by what's recorded there.
    """
    from reviewer_regions import guess_country
    say = progress or (lambda s: None)
    trips, notes = find_trips(parent, exclude=dest)
    manifest = load_manifest(dest)
    lookup = SpeciesLookup()
    items: list[Item] = []
    frames: dict[str, Frame] = {}
    trip_of: dict[str, str] = {}
    n_ident = sum(len(t.originals) for t in trips) if identify else 0
    done_ident = 0                                  # (a rough total: edits' frames add a few)
    for n, t in enumerate(trips, 1):
        if stop is not None and stop.is_set():
            break
        say(f"Reading {n}/{len(trips)}: {os.path.relpath(t.folder, parent)}")
        times = _times(t.all_originals)
        etimes = _times([e for e, _k in t.edits])
        # edits whose frame isn't in a Best folder: look in the shoot folder (by DSC number)
        need = {dsc_number(e) for e, _k in t.edits} - {None}
        have = {dsc_number(p) for p in t.all_originals}
        shoot = [p for p in t.shoot if dsc_number(p) in need - have]
        times.update(_times(shoot))
        py, pm = _path_date(t.folder)
        good = [times[p] for p in t.originals if not _clock_off(times[p], py)]
        if good:                  # the month most of the photos were taken in (a stray
            months = Counter(f"{datetime.datetime.fromtimestamp(x):%Y-%m}" for x in good)
            ym = max(sorted(months), key=lambda k: months[k])    # copy can't re-date a trip)
        else:                                    # camera clock wrong: date from the folders
            any_ts = min((times[p] for p in t.originals), default=os.path.getmtime(t.final))
            ym = f"{py or datetime.datetime.fromtimestamp(any_ts).year}-" \
                 f"{pm or datetime.datetime.fromtimestamp(any_ts).month:02d}"
            if t.originals:
                notes.append(f"Camera clock looks wrong in {os.path.relpath(t.folder, parent)} "
                             f"({datetime.datetime.fromtimestamp(any_ts):%Y-%m-%d}); dated {ym} from the folders")
        name = (trip_names or {}).get(t.folder) or f"{ym} {trip_label(t.folder, parent)}"
        iso = guess_country(" ".join(Path(os.path.relpath(t.folder, parent)).parts + (name,))) \
            or home_region
        trip_of[t.folder] = name
        year = name[:4] if re.match(r"\d{4}", name) else ym[:4]

        # match edits to their frames first, so every frame to name is known
        by_num = defaultdict(list)
        for p in t.all_originals + shoot:
            _sp, _how, orig_name = lookup.lookup(p, t.folder)
            num = dsc_number(orig_name or p)
            if num is not None:
                by_num[num].append(p)
        matches = {}
        for e, _kind in t.edits:
            ets = etimes[e]
            num = dsc_number(e)
            cands = by_num.get(num, []) if num is not None else []
            if not cands:
                cands = [p for p in t.all_originals if abs(times[p] - ets) <= 1]
            matches[e] = min(cands, key=lambda p: (p not in t.all_originals, abs(times[p] - ets))) \
                if cands else None
        srcs = list(dict.fromkeys(t.originals + [m for m in matches.values() if m]))
        known = {p: lookup.lookup(p, t.folder) for p in srcs}
        guesses = {}
        unknown = [p for p in srcs if known[p][1] == "unknown" and p not in (species_override or {})]
        if identify is not None and unknown:
            from concurrent.futures import ThreadPoolExecutor

            def one(p):
                try:
                    return p, identify(p, iso)
                except Exception:
                    return p, None
            with ThreadPoolExecutor(max_workers=3) as ex:
                for k, (p, got) in enumerate(ex.map(one, unknown), 1):
                    guesses[p] = got
                    done_ident += 1
                    if k % 5 == 0 or k == len(unknown):
                        say(f"Identifying species {done_ident}/{max(n_ident, done_ident)} "
                            f"({os.path.relpath(t.folder, parent)})")
                    if stop is not None and stop.is_set():
                        break

        def frame(p: str) -> Frame:
            if p in frames:
                return frames[p]
            sp, how, _orig = known.get(p) or lookup.lookup(p, t.folder)
            if p in (species_override or {}):
                sp, how = species_override[p], "confirmed"
            elif how == "unknown" and guesses.get(p) and guesses[p][1] >= SUGGEST_MIN:
                sp, how = guesses[p][0], "suggested"
            ts = times.get(p) or _ts(p)
            date = ym if _clock_off(ts, py) else f"{datetime.datetime.fromtimestamp(ts):%Y-%m-%d}"
            frames[p] = Frame(src=p, species=sp, how=how, ts=ts, date=date, trip=name, year=year)
            return frames[p]

        for p in t.originals:
            f = frame(p)
            items.append(Item(src=p, kind="original", trip=name, year=year, species=f.species,
                              how=f.how, ts=f.ts, size=os.path.getsize(p)))
        for e, kind in t.edits:
            match = matches[e]
            if match:
                f = frame(match)
                items.append(Item(src=e, kind=kind, trip=name, year=year, species=f.species,
                                  how=f.how, ts=f.ts, size=os.path.getsize(e), of=match))
            else:
                items.append(Item(src=e, kind=kind, trip=name, year=year, species="Unmatched edits",
                                  how="unknown", ts=etimes[e], size=os.path.getsize(e),
                                  note="original not found"))
    _dedupe(items)
    collected = _Collected(manifest, dest)
    _name(items, frames, dest, collected)
    for it in items:
        it.sidecars = _sidecars(it)
        m = collected.find(it.src, it.size, it.ts, it.kind)
        if m:
            it.include, it.note = False, "already collected"
            it.dest = m["dest"]
    return items, notes, trip_of


def _dedupe(items: list[Item]) -> None:
    """Byte-identical originals (the same photo in two trips): collect once."""
    by_size = defaultdict(list)
    for it in items:
        if it.kind == "original":
            by_size[it.size].append(it)
    for group in by_size.values():
        if len(group) < 2:
            continue
        seen = {}
        for it in sorted(group, key=lambda i: i.src):
            try:
                fp = _fingerprint(it.src)
            except OSError:
                continue
            if fp in seen:
                it.include, it.note = False, f"duplicate of {seen[fp]}"
            else:
                seen[fp] = it.src


def _name(items: list[Item], frames: dict, dest: str, collected: "_Collected") -> None:
    """
    Every frame with something to collect (its original and/or edits) gets
    Species_Date_Trip_###; its edits get that name + _topaz / _final / _edit.
    """
    taken_by_dir: dict[str, set] = {}

    def taken(rel_dir: str) -> set:
        if rel_dir not in taken_by_dir:
            try:
                taken_by_dir[rel_dir] = {f.lower() for f in os.listdir(os.path.join(dest, rel_dir))}
            except OSError:
                taken_by_dir[rel_dir] = set()
        return taken_by_dir[rel_dir]

    wanted = {it.src for it in items if it.kind == "original" and it.include}
    wanted |= {it.of for it in items if it.kind != "original" and it.of}
    groups = defaultdict(list)
    for src in wanted:
        f = frames[src]
        groups[(f.year, f.trip, f.species, f.date)].append(f)
    base_of: dict[str, tuple[str, str]] = {}
    for (year, trip, species, date), fs in groups.items():
        rel_dir = os.path.join(year, clean(trip), species_folder(species))
        label = _hyphen(re.sub(r"^\d{4}-\d{2}\s*", "", trip)) or "Trip"
        prefix = f"{_hyphen(species_folder(species))}_{date}_{label}_"
        used = {int(m.group(1)) for f_ in taken(rel_dir)
                for m in [re.match(re.escape(prefix.lower()) + r"(\d{3,})", f_)] if m}
        n = max(used, default=0)
        for f in sorted(fs, key=lambda f: (f.ts, os.path.basename(f.src))):
            try:
                size = os.path.getsize(f.src)
            except OSError:
                size = -1
            prev = collected.find(f.src, size, f.ts, "original")
            if prev:
                base = os.path.splitext(os.path.basename(prev["dest"]))[0]
            else:
                n += 1
                base = f"{prefix}{n:03d}"
            base_of[f.src] = (rel_dir, base)
    for it in items:
        if it.kind == "original" and it.src in base_of:
            rel_dir, base = base_of[it.src]
            it.dest = os.path.join(rel_dir, base + Path(it.src).suffix)
        elif it.kind == "original":              # duplicate: not copied
            it.dest = ""
    count: dict = defaultdict(int)
    for it in sorted((i for i in items if i.kind != "original"), key=lambda i: i.src.lower()):
        if it.of:
            rel_dir, base = base_of[it.of]
            key = (it.of, it.kind, Path(it.src).suffix.lower())
            count[key] += 1
            suffix = it.kind if count[key] == 1 else f"{it.kind}-{count[key]}"
            it.dest = os.path.join(rel_dir, f"{base}_{suffix}{Path(it.src).suffix.lower()}")
        else:
            it.dest = os.path.join(it.year, clean(it.trip), "Unmatched edits", os.path.basename(it.src))


def _sidecars(it: Item) -> list[tuple[str, str]]:
    """XMP sidecars to copy with a file: name.xmp (Lightroom / this app) and
    name.ext.xmp (darktable), renamed to match."""
    out = []
    src_stem = os.path.splitext(it.src)[0]
    dest_stem = os.path.splitext(it.dest)[0]
    for s, d in ((src_stem + ".xmp", dest_stem + ".xmp"),
                 (it.src + ".xmp", it.dest + ".xmp")):
        if os.path.exists(s):
            out.append((s, d))
    return out


def run(items: list[Item], dest: str, verify: bool = True,
        progress: Optional[Callable[[int, int, str], None]] = None,
        stop: Optional[threading.Event] = None) -> dict:
    """Copy the included items. Returns counts; the manifest is updated as it goes."""
    manifest = load_manifest(dest)
    todo = [i for i in items if i.include]
    done = {"copied": 0, "errors": [], "bytes": 0}
    os.makedirs(dest, exist_ok=True)
    for n, it in enumerate(todo, 1):
        if stop is not None and stop.is_set():
            break
        target = os.path.join(dest, it.dest)
        try:
            if not (os.path.exists(target) and os.path.getsize(target) == it.size):
                _copy_verified(it.src, [target], verify)
            for s, d in it.sidecars:
                _copy_verified(s, [os.path.join(dest, d)], False)
            manifest[_key(it.src)] = {"dest": it.dest, "size": it.size, "kind": it.kind, "ts": it.ts,
                                      "species": it.species, "trip": it.trip}
            done["copied"] += 1
            done["bytes"] += it.size
        except Exception as e:
            done["errors"].append(f"{it.src}: {e}")
        if n % 20 == 0 or n == len(todo):
            save_manifest(dest, manifest)
        if progress:
            progress(n, len(todo), it.dest)
    save_manifest(dest, manifest)
    return done


class SpeciesIdentifier:
    """
    identify(path, iso) → (species, probability) or None, for photos the app knows
    nothing about: BioCLIP on the animal MegaDetector finds, re-ranked like the
    app does — by the region's species list (iso country code) and by species you
    have confirmed before. The model's raw top 25 is cached (by file, size and
    date), so scanning again, even for another region, is instant.
    """

    def __init__(self, cache_file: str, user_dir: Optional[str] = None):
        self.file = cache_file
        self.cache = _read_json(cache_file)
        self.lock = threading.RLock()        # save() also takes it, from inside __call__
        self._dirty = 0
        self._regions = self._prior = None
        if user_dir:
            from reviewer_regions import RegionLists
            from reviewer_species import SpeciesHistory
            self._regions = RegionLists(Path(user_dir) / "regions")
            try:
                self._prior = SpeciesHistory(Path(user_dir) / "species_history.json").prior()
            except Exception:
                self._prior = None

    def raw(self, path: str):
        """The model's top 25 [[name, scientific, p], …] for a photo (cached)."""
        try:
            st = os.stat(path)
        except OSError:
            return None
        key = f"v2|{_key(path)}|{st.st_size}|{int(st.st_mtime)}"
        with self.lock:
            if key in self.cache:
                return self.cache[key] or None
        from reviewer_detect import get_detector, main_animal, refine
        from reviewer_imaging import load_preview
        from reviewer_species import get_species_model
        model = get_species_model()
        if model is None:
            return None
        img = load_preview(path, 1600)
        img.load()
        box = None
        det = get_detector()
        if det is not None:
            a = main_animal(refine(det.detect(img), img, model))
            box = a[2:6] if a and len(a) >= 6 else None
        top = [[n, sci, float(pr)] for n, sci, pr in model.predict(img, box, k=25)]
        with self.lock:
            self.cache[key] = top
            self._dirty += 1
            if self._dirty >= 25:
                self.save()
        return top or None

    def __call__(self, path: str, iso: Optional[str] = None):
        top = self.raw(path)
        if not top:
            return None
        ranked = self.rank(top, iso)
        return ranked[0][0], ranked[0][2]

    def region(self, iso: Optional[str]) -> Optional[dict]:
        """The country's species list (None until downloaded; a download is started)."""
        if not iso or self._regions is None:
            return None
        region = self._regions.get(iso)
        if region is None and not self._regions.fetching(iso):
            self._regions.fetch(iso, lambda *_a: None)          # next scan will use it
        return region

    def rank(self, top: list, iso: Optional[str] = None) -> list[tuple[str, str, float]]:
        """The model's raw top re-ranked by the region and your history:
        [(name, scientific, probability)], most likely first."""
        region = self.region(iso)
        from reviewer_regions import region_weight
        from reviewer_species import get_species_model
        model = get_species_model()
        groups = model.group if model is not None else {}
        prior = self._prior or {}
        weighted = [(n, sc, pr * prior.get(sc, 1.0) * region_weight(sc, groups.get(sc), region))
                    for n, sc, pr in top]
        if region:
            # filing needs one answer: a species not recorded in the region loses to a
            # relative (same genus) among the suggestions that is (Barn Owl, not Eastern)
            here = {sc for _n, sc, _p in top if sc in region.get("species", {})}
            genera = {sc.split(" ")[0] for sc in here}
            weighted = [(n, sc, w * (0.15 if sc not in here and sc.split(" ")[0] in genera else 1.0))
                        for n, sc, w in weighted]
        rest = max(0.0, 1.0 - sum(pr for _n, _s, pr in top))
        total = (sum(w for _n, _s, w in weighted) + rest) or 1.0
        return sorted(((n, sc, w / total) for n, sc, w in weighted), key=lambda r: -r[2])

    def save(self) -> None:
        with self.lock:
            try:
                os.makedirs(os.path.dirname(self.file), exist_ok=True)
                tmp = self.file + ".tmp"
                Path(tmp).write_text(json.dumps(self.cache), encoding="utf-8")
                os.replace(tmp, self.file)
                self._dirty = 0
            except OSError:
                pass
