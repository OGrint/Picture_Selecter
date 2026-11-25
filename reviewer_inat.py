"""
Reference photos from iNaturalist (Photo Reviewer), to compare with yours
when checking an ID.

Only the scientific name is sent (nothing about you or your photos). The
species' photos (the ones iNaturalist shows on its taxon page, with their
credits) are downloaded once and kept in %APPDATA%\\PhotoReviewer\\inat_photos.
Downloads are only ever opened as images. No Tkinter here.
"""

from __future__ import annotations

import io
import json
import os
import re
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from PIL import Image

API = "https://api.inaturalist.org/v1/taxa"
USER_AGENT = "PhotoReviewer/1.0"
MAX_PHOTOS = 6
MAX_BYTES = 8 << 20
TIMEOUT = 20


def _get(url: str, limit: int = MAX_BYTES) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError("too large")
    return data


def _safe(sci: str) -> str:
    return re.sub(r"[^A-Za-z0-9 _-]", "", sci).strip().replace(" ", "_") or "unknown"


class ReferencePhotos:
    """photos(sci) → [{path, attribution, url}] (downloaded on first use)."""

    def __init__(self, folder: str):
        self.folder = folder
        self._lock = threading.Lock()
        self._busy: set = set()

    def _meta_path(self, sci: str) -> str:
        return os.path.join(self.folder, _safe(sci), "photos.json")

    def cached(self, sci: str) -> Optional[dict]:
        try:
            return json.loads(Path(self._meta_path(sci)).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def fetch(self, sci: str) -> dict:
        """The species' reference photos: {taxon, name, url (taxon page), photos: [{path, attribution}]}.
        Raises on a network problem."""
        got = self.cached(sci)
        if got is not None:
            return got
        q = urllib.parse.urlencode({"q": sci, "rank": "species,subspecies", "per_page": 10})
        results = json.loads(_get(f"{API}?{q}", 2 << 20)).get("results", [])
        taxon = next((t for t in results if t.get("name", "").lower() == sci.lower()), None) \
            or (results[0] if results else None)
        out = {"sci": sci, "taxon": None, "name": "", "url": "", "photos": []}
        if taxon is not None:
            tid = int(taxon["id"])
            full = json.loads(_get(f"{API}/{tid}", 4 << 20)).get("results", [taxon])[0]
            out.update(taxon=tid, name=full.get("preferred_common_name") or full.get("name", ""),
                       url=f"https://www.inaturalist.org/taxa/{tid}")
            photos = [tp.get("photo", {}) for tp in full.get("taxon_photos", [])] or \
                [full.get("default_photo") or {}]
            folder = os.path.dirname(self._meta_path(sci))
            os.makedirs(folder, exist_ok=True)
            for i, ph in enumerate([p for p in photos if p][:MAX_PHOTOS]):
                url = ph.get("medium_url") or ph.get("url") or ""
                url = url.replace("/square.", "/large.").replace("/medium.", "/large.")
                if not url.startswith("https://"):
                    continue
                try:
                    data = _get(url)
                    img = Image.open(io.BytesIO(data))          # (opened as an image only)
                    img.load()
                    path = os.path.join(folder, f"{i + 1}.jpg")
                    img.convert("RGB").save(path, "JPEG", quality=90)
                except Exception:
                    continue
                out["photos"].append({"path": path, "attribution": ph.get("attribution", "")})
        os.makedirs(os.path.dirname(self._meta_path(sci)), exist_ok=True)
        Path(self._meta_path(sci)).write_text(json.dumps(out), encoding="utf-8")
        return out

    def fetch_async(self, sci: str, done: Callable[[str, Optional[dict], str], None]) -> None:
        """done(sci, result or None, error) — called on a background thread."""
        with self._lock:
            if sci in self._busy:
                return
            self._busy.add(sci)

        def work():
            try:
                done(sci, self.fetch(sci), "")
            except Exception as e:
                done(sci, None, str(e)[:120])
            finally:
                with self._lock:
                    self._busy.discard(sci)
        threading.Thread(target=work, daemon=True).start()
