"""
Eye check for Photo Reviewer: finds the animal's eye and measures how sharp it is.

The eye finder is OWLv2 (Google, Apache-2.0) with the questions "an eye",
"a bird's eye", "an animal's eye" and "a head" built into the model file
(models/owlv2_eye.onnx, weights stored at half precision). It runs on the
animal's crop taken from the full-resolution preview, so even small birds'
eyes are found.

Tested on the owner's own frames: where the finder is confident (≥ 0.3) it
is on the eye in nearly every case; below that there is usually no eye to see
(head turned, tucked or under water, a yawn, a distant bird). Eye sharpness
is the Crété-Roffet blur measure, which compares the eye with a re-blurred
copy of itself. On its own it is not a reliable sharp/soft verdict across
different photos (noise and feather texture fool it), but inside one burst
(same animal, light and ISO) it ranks frames the way the eye does. So the app
only ever says "sharpest in this burst" or "softer than another frame".
No Tkinter here.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

import reviewer_accel
from reviewer_imaging import _open_thumb_unoriented, auto_orient, background_read

log = logging.getLogger("photo_reviewer")

MODEL_FILE = "owlv2_eye.onnx"
MODEL_ID   = "owlv2-b16-eye-1"
CACHE_FILE = "photo_reviewer_eyes.json"
SIDE       = 960                 # the model's input size
SEEN       = 0.30                # finder confidence above which an eye is visible
SOFTER     = 0.035               # this much below the burst's best = visibly softer
MIN_ANIMAL = 0.004               # animals smaller than this share of the frame are skipped
_MEAN = np.array((0.48145466, 0.4578275, 0.40821073), np.float32)
_STD  = np.array((0.26862954, 0.26130258, 0.27577711), np.float32)


def _model_path() -> Optional[Path]:
    here = Path(__file__).resolve().parent
    exe = Path(sys.executable).parent if getattr(sys, "frozen", False) else here
    for d in (exe / "models", here / "models"):
        if (d / MODEL_FILE).exists():
            return d / MODEL_FILE
    return None


def eye_sharpness(gray: np.ndarray, k: int = 9) -> float:
    """
    Crété-Roffet sharpness of a grey patch: 1 − blur, where blur (0–1) is how
    little the patch changes when blurred again. Contrast does not matter,
    only how wide the edges are. Patches over 160 px are measured at 160 px.
    """
    g = gray.astype(np.float32)
    if g.shape[0] < k + 4 or g.shape[1] < k + 4:
        return 0.0
    kern = np.ones(k, np.float32) / k
    blur = []
    for ax in (0, 1):
        b = np.apply_along_axis(lambda v: np.convolve(v, kern, mode="same"), ax, g)
        df, db = np.abs(np.diff(g, axis=ax)), np.abs(np.diff(b, axis=ax))
        h = k // 2 + 1
        df, db = df[h:-h, h:-h], db[h:-h, h:-h]
        s = float(df.sum())
        blur.append((s - float(np.maximum(0, df - db).sum())) / s if s > 0 else 1.0)
    return round(1.0 - max(blur), 4)


class EyeFinder:
    def __init__(self, path: Path):
        # two CPU threads: it runs alongside culling; the graphics chip does the
        # heavy lifting when there is one
        self._run = reviewer_accel.Runner(path, 2, "eye check")

    def find(self, crop: Image.Image) -> tuple[float, tuple[float, float, float, float]]:
        """(confidence, eye box in crop pixels) for the most eye-like spot."""
        s = max(crop.size)
        pad = Image.new("RGB", (s, s), (128, 128, 128))        # padded bottom / right
        pad.paste(crop.convert("RGB"), (0, 0))
        a = np.asarray(pad.resize((SIDE, SIDE), Image.BICUBIC), np.float32) / 255
        x = ((a - _MEAN) / _STD).transpose(2, 0, 1)[None]
        out = self._run.run(None, {self._run.input_name: x})[0][0]   # (patches, 4 scores + box)
        eye = out[:, :3].max(axis=1)
        k = int(eye.argmax())
        cx, cy, w, h = (out[k, 4:8] * s).tolist()
        return float(eye[k]), (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


_finder: Optional[EyeFinder] = None
_finder_error = ""
_finder_lock = threading.Lock()


def get_eye_finder() -> Optional[EyeFinder]:
    global _finder, _finder_error
    with _finder_lock:
        if _finder is None and not _finder_error:
            p = _model_path()
            if p is None:
                _finder_error = f"model file {MODEL_FILE} not found in the models folder"
            else:
                try:
                    _finder = EyeFinder(p)
                except Exception as e:
                    _finder_error = f"could not load the eye model ({e})"
            if _finder_error:
                log.info("Eye check unavailable: %s", _finder_error)
        return _finder


def eye_status() -> str:
    """'' when the eye check works, else why not."""
    return _finder_error


def measure(path: str, animal_box, finder: EyeFinder) -> Optional[dict]:
    """
    Eye check for one frame:
      p — finder confidence; below SEEN there is no visible eye
      b — eye box (normalised x0, y0, x1, y1 of the whole frame) when visible
      s — eye sharpness (compare within a burst only) when visible
    None when the frame has no usable animal box.
    """
    if not animal_box or len(animal_box) < 4:
        return None
    x0, y0, x1, y1 = animal_box[:4]
    if (x1 - x0) * (y1 - y0) < MIN_ANIMAL:
        return None
    with background_read(path):
        full = auto_orient(_open_thumb_unoriented(path))
        full.load()
    W, H = full.size
    mx, my = 0.08 * (x1 - x0), 0.08 * (y1 - y0)
    cb = (int(max(0, x0 - mx) * W), int(max(0, y0 - my) * H),
          int(min(1, x1 + mx) * W), int(min(1, y1 + my) * H))
    if cb[2] - cb[0] < 16 or cb[3] - cb[1] < 16:
        return None
    crop = full.crop(cb).convert("RGB")
    p, (ex0, ey0, ex1, ey1) = finder.find(crop)
    out: dict = {"p": round(p, 3)}
    if p < SEEN:
        return out
    cx, cy = (ex0 + ex1) / 2, (ey0 + ey1) / 2
    half = max(20.0, 1.1 * max(ex1 - ex0, ey1 - ey0))
    patch = crop.crop((int(cx - half), int(cy - half), int(cx + half), int(cy + half))).convert("L")
    if max(patch.size) > 160:
        patch = patch.resize((160, 160), Image.LANCZOS)
    out["s"] = eye_sharpness(np.asarray(patch))
    out["b"] = [round((cb[0] + ex0) / W, 5), round((cb[1] + ey0) / H, 5),
                round((cb[0] + ex1) / W, 5), round((cb[1] + ey1) / H, 5)]
    return out


def burst_eye_note(path: str, group: list[str], eyes: dict) -> Optional[str]:
    """
    How this frame's eye compares within its burst:
      "best"   — sharpest eye in the burst (only said when ≥ 2 frames show an eye)
      "tie"    — as sharp as the best, within what the measure can tell apart
      "softer" — visibly softer than the best frame's eye
      "hidden" — the eye checker found no visible eye
      None     — not checked yet, no animal, or nothing to compare with
    """
    e = eyes.get(path)
    if not e:
        return None
    if e.get("s") is None:
        return "hidden"
    vals = [eyes[q]["s"] for q in group if (eyes.get(q) or {}).get("s") is not None]
    if len(vals) < 2:
        return None
    best = max(vals)
    if e["s"] >= best:
        return "best"
    return "softer" if best - e["s"] >= SOFTER else "tie"


def load_eyes(folder: str, valid: set[str]) -> dict[str, dict]:
    p = os.path.join(folder, CACHE_FILE)
    try:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if data.get("model") != MODEL_ID:
        return {}
    out = {}
    for name, e in (data.get("frames") or {}).items():
        path = os.path.join(folder, name)
        try:
            if path in valid and abs(os.path.getmtime(path) - e["_mtime"]) <= 1:
                out[path] = {k: v for k, v in e.items() if not k.startswith("_")}
        except (OSError, KeyError, TypeError):
            continue
    return out


def save_eyes(folder: str, eyes: dict[str, dict]) -> None:
    frames = {}
    for path, e in eyes.items():
        try:
            frames[os.path.basename(path)] = dict(e, _mtime=os.path.getmtime(path))
        except OSError:
            continue
    if not frames:
        return
    p = os.path.join(folder, CACHE_FILE)
    try:
        tmp = p + ".tmp"
        Path(tmp).write_text(json.dumps({"model": MODEL_ID, "frames": frames}), encoding="utf-8")
        os.replace(tmp, p)
    except OSError as e:
        log.warning("Could not save %s: %s", p, e)
