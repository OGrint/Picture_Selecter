"""
Offline species suggestions for Photo Reviewer using BioCLIP 2.

BioCLIP 2 (Imageomics, MIT licence) compares an image with the names of
~48,000 species worldwide — birds, mammals, reptiles, amphibians,
dragonflies and butterflies — and ranks the best matches. The image encoder
runs with onnxruntime (int8, ~300 MB); the species names' embeddings are
precomputed (models/bioclip2_species_*).

Predictions are suggestions: the naming panel shows them in a dropdown and
you confirm or correct them. Species you confirm are remembered and given
extra weight next time, which separates look-alikes you see often (e.g.
Grey Heron vs Great Blue Heron).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

log = logging.getLogger("photo_reviewer")

MODEL_FILE  = "bioclip2_visual_int8.onnx"
EMB_FILE    = "bioclip2_species_emb.npy"
NAMES_FILE  = "bioclip2_species.json"
MODEL_ID    = "BioCLIP2-wildlife-48k"
CACHE_FILE  = "photo_reviewer_species.json"       # per folder
HISTORY_FILE = "species_history.json"             # per user
TOP_K       = 5
_MEAN = np.array((0.48145466, 0.4578275, 0.40821073), np.float32)
_STD  = np.array((0.26862954, 0.26130258, 0.27577711), np.float32)


def nice_name(common: str, scientific: str) -> str:
    """'grey heron' → 'Grey Heron'; scientific name if there is no common name."""
    if not common:
        return scientific
    return " ".join(w[:1].upper() + w[1:] for w in common.split())


def _model_dirs() -> list[Path]:
    here = Path(__file__).resolve().parent
    exe = Path(sys.executable).parent if getattr(sys, "frozen", False) else here
    return [exe / "models", here / "models"]


class SpeciesModel:
    def __init__(self, folder: Path):
        from reviewer_accel import Runner
        # int8 on the CPU (leave room for the UI); a float copy on the graphics chip
        self._s = Runner(folder / MODEL_FILE, max(1, (os.cpu_count() or 4) // 4), "species",
                         dequantize=True)
        self._emb = np.load(folder / EMB_FILE).astype(np.float32)        # (n, 768)
        meta = json.loads((folder / NAMES_FILE).read_text(encoding="utf-8"))
        self._scale = float(meta["scale"])
        self.labels = [(nice_name(c, s), s) for c, s, *_ in meta["labels"]]
        self._index = {s: i for i, (_n, s) in enumerate(self.labels)}
        from reviewer_regions import group_of
        # group (birds, mammals, …) of each species, for the region hint
        self.group = {s: group_of(list(tax)) for _c, s, *tax in meta["labels"]}

    def predict(self, img: Image.Image, box: Optional[list] = None,
                prior: Optional[dict] = None, k: int = TOP_K) -> list[list]:
        """
        Top species for the animal in img (box = normalised animal box).
        prior: {scientific name: weight ≥ 1} for species you've confirmed.
        Returns [[display name, scientific name, probability], …].
        """
        iw, ih = img.size
        if box is not None:
            x0, y0, x1, y1 = box
            mx, my = (x1 - x0) * 0.12, (y1 - y0) * 0.12               # a little context
            crop = (int(max(0, x0 - mx) * iw), int(max(0, y0 - my) * ih),
                    int(min(1, x1 + mx) * iw), int(min(1, y1 + my) * ih))
            if crop[2] - crop[0] >= 32 and crop[3] - crop[1] >= 32:
                img = img.crop(crop)
        x = np.asarray(img.convert("RGB").resize((224, 224), Image.BICUBIC), np.float32) / 255
        x = ((x - _MEAN) / _STD).transpose(2, 0, 1)[None]
        feat = self._s.run(None, {"image": x})[0][0]
        logits = self._scale * (self._emb @ feat)
        p = np.exp(logits - logits.max())
        if prior:
            for sci, w in prior.items():
                i = self._index.get(sci)
                if i is not None:
                    p[i] *= w
        p /= p.sum()
        top = np.argsort(-p)[:k]
        return [[self.labels[i][0], self.labels[i][1], round(float(p[i]), 4)] for i in top]


_model: Optional[SpeciesModel] = None
_error: Optional[str] = None
_lock = threading.Lock()


def get_species_model() -> Optional[SpeciesModel]:
    global _model, _error
    with _lock:
        if _model is None and _error is None:
            folder = next((d for d in _model_dirs() if (d / MODEL_FILE).exists()
                           and (d / EMB_FILE).exists() and (d / NAMES_FILE).exists()), None)
            if folder is None:
                _error = "BioCLIP 2 model files not found in 'models' folder"
            else:
                try:
                    _model = SpeciesModel(folder)
                    log.info("Species suggestions: %s (%d species)", MODEL_ID, len(_model.labels))
                except Exception as e:
                    _error = str(e)
            if _error:
                log.warning("Species suggestions unavailable: %s", _error)
        return _model


def species_status() -> str:
    get_species_model()
    return _error or ""


# ── Per-folder cache of predictions ──────────────────────────────────────────

def load_predictions(folder: str, valid: set[str]) -> dict[str, list]:
    p = os.path.join(folder, CACHE_FILE)
    try:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for name, e in data.items():
        path = os.path.join(folder, name)
        try:
            if path in valid and e.get("model") == MODEL_ID and \
                    abs(os.path.getmtime(path) - e["_mtime"]) <= 1:
                out[path] = e["top"]
        except (OSError, KeyError, TypeError):
            continue
    return out


def save_predictions(folder: str, preds: dict[str, list]) -> None:
    out = {}
    for path, top in preds.items():
        try:
            out[os.path.basename(path)] = {"_mtime": os.path.getmtime(path),
                                           "model": MODEL_ID, "top": top}
        except OSError:
            continue
    if not out:
        return
    p = os.path.join(folder, CACHE_FILE)
    try:
        tmp = p + ".tmp"
        Path(tmp).write_text(json.dumps(out), encoding="utf-8")
        os.replace(tmp, p)
    except OSError as e:
        log.warning("Could not save %s: %s", p, e)


# ── Species you have confirmed (personal prior) ──────────────────────────────

class SpeciesHistory:
    """Counts of species you've confirmed, across all folders."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data: dict = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def confirm(self, name: str, scientific: str = "") -> None:
        key = scientific or name
        e = self.data.setdefault(key, {"name": name, "count": 0})
        e["name"], e["count"], e["last"] = name, e["count"] + 1, time.time()

    def scientific_for(self, name: str) -> str:
        """Scientific name previously confirmed for a display name ('' if none)."""
        low = name.strip().lower()
        for key, e in self.data.items():
            if e.get("name", "").lower() == low and " " in key:
                return key
        return ""

    def prior(self) -> dict[str, float]:
        """Weight for species you've confirmed: more often → stronger, max ×6."""
        return {k: 1 + min(5.0, e["count"] ** 0.5) for k, e in self.data.items() if " " in k}

    def recent(self, n: int = 15) -> list[str]:
        items = sorted(self.data.values(), key=lambda e: -e.get("last", 0))
        return [e["name"] for e in items[:n]]

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = str(self.path) + ".tmp"
            Path(tmp).write_text(json.dumps(self.data, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("Could not save species history: %s", e)
