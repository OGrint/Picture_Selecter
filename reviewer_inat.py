"""
Reference material from iNaturalist (Photo Reviewer), to compare with your
photo when checking an ID: the species' photos, a few facts (size, how often
it's recorded, conservation status, family) and a range map.

Only the scientific name (and then iNaturalist's taxon number) is sent;
nothing about you or your photos. The world base map comes from
OpenStreetMap (the same 16 tiles for every species, downloaded once). All of
it is kept in %APPDATA%\\PhotoReviewer\\inat_photos, so each species is
downloaded once. Downloads are only ever opened as images or read as JSON.
The first photo is saved (and reported) as soon as it arrives; the rest come
in parallel. No Tkinter here.
"""

from __future__ import annotations

import html
import io
import json
import os
import re
import threading
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageEnhance

API = "https://api.inaturalist.org/v1/taxa"
GRID = "https://api.inaturalist.org/v1/grid/{z}/{x}/{y}.png?taxon_id={tid}&color=%23ff7a1a"
BASEMAP = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
WIKI = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "PhotoReviewer/1.0"
MAX_PHOTOS = 6
MAX_BYTES = 8 << 20
TIMEOUT = 20
MAP_ZOOM = 2               # 4 × 4 tiles: the world at 1024 px
SIZE_WORDS = ("long", "length", "wingspan", "weigh", "mass", "measur", "tall", "height", "body")


def _get(url: str, limit: int = MAX_BYTES) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError("too large")
    return data


def _safe(sci: str) -> str:
    return re.sub(r"[^A-Za-z0-9 _-]", "", sci).strip().replace(" ", "_") or "unknown"


def _text(s: str) -> str:
    """Plain text from iNaturalist's HTML summary."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s or ""))).strip()


def size_sentence(text: str) -> str:
    """The sentence of a description that gives a size (length, wingspan, weight)."""
    for sent in re.split(r"(?<=[.!?])\s+", text or ""):
        if re.search(r"\d\s*(?:–|-|to)?\s*\d*\.?\d*\s*(?:cm|mm|m|kg|g|in|inches|ft|lb)\b", sent) and \
                any(w in sent.lower() for w in SIZE_WORDS):
            return sent.strip()[:300]
    return ""


class ReferencePhotos:
    """fetch(sci) → {sci, taxon, name, url, photos: [{path, attribution}], info: {…}, map}."""

    def __init__(self, folder: str):
        self.folder = folder
        self._lock = threading.Lock()
        self._busy: set = set()

    def _dir(self, sci: str) -> str:
        return os.path.join(self.folder, _safe(sci))

    def _meta_path(self, sci: str) -> str:
        return os.path.join(self._dir(sci), "photos.json")

    def cached(self, sci: str) -> Optional[dict]:
        """What's been downloaded for the species (None: nothing yet)."""
        try:
            return json.loads(Path(self._meta_path(sci)).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _save_meta(self, sci: str, out: dict) -> None:
        os.makedirs(self._dir(sci), exist_ok=True)
        tmp = self._meta_path(sci) + ".tmp"
        Path(tmp).write_text(json.dumps(out), encoding="utf-8")
        os.replace(tmp, self._meta_path(sci))

    def _photo(self, sci: str, i: int, ph: dict) -> Optional[dict]:
        url = ph.get("medium_url") or ph.get("url") or ""
        url = url.replace("/square.", "/large.").replace("/medium.", "/large.")
        if not url.startswith("https://"):
            return None
        try:
            img = Image.open(io.BytesIO(_get(url)))          # (opened as an image only)
            img.load()
            path = os.path.join(self._dir(sci), f"{i + 1}.jpg")
            img.convert("RGB").save(path, "JPEG", quality=90)
        except Exception:
            return None
        return {"path": path, "attribution": ph.get("attribution", "")}

    def fetch(self, sci: str, partial: Optional[Callable[[dict], None]] = None) -> dict:
        """The species' reference material (see the class). partial(result) is called
        as soon as the first photo is in. Raises on a network problem."""
        got = self.cached(sci)
        if got is not None and got.get("complete", True):
            return got
        q = urllib.parse.urlencode({"q": sci, "rank": "species,subspecies", "per_page": 10})
        results = json.loads(_get(f"{API}?{q}", 2 << 20)).get("results", [])
        taxon = next((t for t in results if t.get("name", "").lower() == sci.lower()), None) \
            or (results[0] if results else None)
        out = {"sci": sci, "taxon": None, "name": "", "url": "", "photos": [], "info": {}, "map": "",
               "complete": False}
        if taxon is None:
            out["complete"] = True
            self._save_meta(sci, out)
            return out
        tid = int(taxon["id"])
        out.update(taxon=tid, name=taxon.get("preferred_common_name") or taxon.get("name", ""),
                   url=f"https://www.inaturalist.org/taxa/{tid}")
        os.makedirs(self._dir(sci), exist_ok=True)
        with ThreadPoolExecutor(max_workers=4) as ex:
            # the first photo now (from the search: no wait for the rest)
            first = ex.submit(self._photo, sci, 0, taxon.get("default_photo") or {})
            full_f = ex.submit(lambda: json.loads(_get(f"{API}/{tid}", 4 << 20)).get("results", [taxon])[0])
            map_f = ex.submit(self._range_map, sci, tid)
            p1 = first.result()
            if p1:
                out["photos"] = [p1]
                if partial:
                    partial(dict(out))
            full = full_f.result()
            out["name"] = full.get("preferred_common_name") or out["name"]
            first_id = (taxon.get("default_photo") or {}).get("id") if p1 else None
            more = [tp.get("photo", {}) for tp in full.get("taxon_photos", [])]
            more = [p for p in more if p and (first_id is None or p.get("id") != first_id)]
            more = more[:MAX_PHOTOS - len(out["photos"])]
            done = list(ex.map(lambda ip: self._photo(sci, ip[0] + 1, ip[1]), enumerate(more)))
            out["photos"] += [d for d in done if d]
            out["info"] = self._info(full)
            out["info"]["size"] = self._size(full)
            out["map"] = map_f.result() or ""
        out["complete"] = True
        self._save_meta(sci, out)
        return out

    @staticmethod
    def _info(full: dict) -> dict:
        ranks = {a.get("rank"): a.get("preferred_common_name") or a.get("name", "")
                 for a in full.get("ancestors", []) if isinstance(a, dict)}
        status = full.get("conservation_status") or {}
        statuses = full.get("conservation_statuses") or []
        iucn = status.get("status_name") or next(
            (s.get("status_name") for s in statuses if (s.get("authority") or "").startswith("IUCN")), "")
        return {"family": ranks.get("family", ""), "order": ranks.get("order", ""),
                "observations": int(full.get("observations_count") or 0),
                "status": (iucn or "").replace("_", " "),
                "summary": _text(full.get("wikipedia_summary", ""))[:700],
                "wikipedia": full.get("wikipedia_url") or ""}

    def _size(self, full: dict) -> str:
        """Its size, from the species' Wikipedia article (iNaturalist links it)."""
        s = size_sentence(_text(full.get("wikipedia_summary", "")))
        if s:
            return s
        title = (full.get("wikipedia_url") or "").rstrip("/").split("/")[-1]
        if not title:
            return ""
        try:
            q = urllib.parse.urlencode({"action": "query", "prop": "extracts", "explaintext": 1, "format": "json",
                                        "redirects": 1, "titles": urllib.parse.unquote(title)})
            pages = json.loads(_get(f"{WIKI}?{q}", 4 << 20)).get("query", {}).get("pages", {})
            text = " ".join(p.get("extract", "") for p in pages.values())
            return size_sentence(text)
        except Exception:
            return ""

    def _tile(self, url: str) -> Optional[Image.Image]:
        try:
            img = Image.open(io.BytesIO(_get(url, 2 << 20)))
            img.load()
            return img.convert("RGBA")
        except Exception:
            return None

    def _basemap(self) -> Optional[Image.Image]:
        """The world (OpenStreetMap, darkened), downloaded once for every species."""
        path = os.path.join(self.folder, "_world_map.png")
        if os.path.exists(path):
            try:
                img = Image.open(path)
                img.load()
                return img
            except OSError:
                pass
        n = 2 ** MAP_ZOOM
        world = Image.new("RGB", (256 * n, 256 * n), "#0b0f14")
        with ThreadPoolExecutor(max_workers=4) as ex:
            tiles = list(ex.map(lambda xy: (xy, self._tile(BASEMAP.format(z=MAP_ZOOM, x=xy[0], y=xy[1]))),
                                [(x, y) for x in range(n) for y in range(n)]))
        if any(t is None for _xy, t in tiles):
            return None
        for (x, y), t in tiles:
            world.paste(t.convert("RGB"), (256 * x, 256 * y))
        world = ImageEnhance.Brightness(ImageEnhance.Color(world).enhance(0.3)).enhance(0.45)
        os.makedirs(self.folder, exist_ok=True)
        world.save(path)
        return world

    def _range_map(self, sci: str, tid: int) -> Optional[str]:
        """Where it's been recorded: iNaturalist's observation grid over the world."""
        try:
            world = self._basemap()
            if world is None:
                return None
            n = 2 ** MAP_ZOOM
            with ThreadPoolExecutor(max_workers=4) as ex:
                tiles = list(ex.map(lambda xy: (xy, self._tile(GRID.format(z=MAP_ZOOM, x=xy[0], y=xy[1], tid=tid))),
                                    [(x, y) for x in range(n) for y in range(n)]))
            img = world.convert("RGBA")
            for (x, y), t in tiles:
                if t is not None:
                    img.alpha_composite(t, (256 * x, 256 * y))
            img = img.crop((0, int(256 * n * 0.12), 256 * n, int(256 * n * 0.82)))   # (no poles)
            path = os.path.join(self._dir(sci), "map.png")
            os.makedirs(self._dir(sci), exist_ok=True)
            img.convert("RGB").save(path)
            return path
        except Exception:
            return None

    def fetch_async(self, sci: str, done: Callable[[str, Optional[dict], str], None],
                    partial: Optional[Callable[[str, dict], None]] = None) -> None:
        """done(sci, result or None, error) on a background thread; partial(sci, result)
        once the first photo is in."""
        with self._lock:
            if sci in self._busy:
                return
            self._busy.add(sci)

        def work():
            try:
                done(sci, self.fetch(sci, (lambda r: partial(sci, r)) if partial else None), "")
            except Exception as e:
                done(sci, None, str(e)[:120])
            finally:
                with self._lock:
                    self._busy.discard(sci)
        threading.Thread(target=work, daemon=True).start()
