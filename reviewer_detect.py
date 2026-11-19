"""
Animal detection for Photo Reviewer using MegaDetector (ONNX, run with
onnxruntime — no PyTorch needed at run time).

Detections only ever produce *suggestions* (e.g. "no animal detected");
nothing here tags or deletes images.

Model: MegaDetector MDv1000-spruce (YOLOv5s, 640 px input), exported to
ONNX. Classes: 0 animal, 1 person, 2 vehicle. The model file is looked for
in a "models" folder next to the program.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from typing import Optional

from PIL import Image

log = logging.getLogger("photo_reviewer")

MODEL_FILE = "md_v1000.0.0-spruce_640.onnx"
MODEL_ID   = "MDv1000-spruce-v3"       # stored with cached detections (checked labels)
CLASSES    = ("animal", "person", "vehicle")

STORE_CONF  = 0.10     # detections kept in the cache
ANIMAL_CONF = 0.15     # an animal counts as present at this confidence
OTHER_CONF  = 0.40     # person / vehicle flagged at this confidence
NMS_IOU     = 0.45
SMALL_AREA  = 0.01     # animal box below 1 % of the frame = "very small"
EDGE_MARGIN = 0.004    # box this close to the border = "cut off"
SPECIES_PRESENT = 0.3  # species model this sure about the frame = animal present

# Suggestion codes → text shown to the user
SUGGESTION_TEXT = {
    "empty":   "No animal detected",
    "missed":  "Focus point is not on the animal",
    "edge":    "Animal cut off by the frame edge",
    "small":   "Animal very small in the frame",
    "person":  "Person in the frame",
    "vehicle": "Vehicle in the frame",
    "blown":   "Highlights blown on the animal (raw data)",
    "eye_soft": "Eye softer than in another frame of this burst",
}
SUGGESTION_SHORT = {
    "empty": "No animal", "missed": "Focus missed", "edge": "Cut off",
    "small": "Tiny subject", "person": "Person", "vehicle": "Vehicle",
    "blown": "Blown", "eye_soft": "Soft eye",
}


def _model_path() -> Optional[Path]:
    here = Path(__file__).resolve().parent
    exe = Path(sys.executable).parent if getattr(sys, "frozen", False) else here
    for d in (exe / "models", here / "models"):
        if (d / MODEL_FILE).exists():
            return d / MODEL_FILE
    return None


class Detector:
    """MegaDetector session. detect() is safe to call from several threads."""

    def __init__(self, model_path: Path):
        import numpy as np
        from reviewer_accel import Runner
        self._np = np
        # CPU threads: leave room for decoding and the UI. Uses the graphics
        # chip instead once a checked copy is ready (reviewer_accel).
        self._session = Runner(model_path, max(1, (os.cpu_count() or 4) // 4), "animal detection")
        self._input = self._session.input_name
        _, _, h, w = self._session.input_shape
        self.size = (int(w), int(h))

    def detect(self, img: Image.Image) -> list[list]:
        """
        Detect animals / people / vehicles.
        Returns [[class_index, confidence, x0, y0, x1, y1], …] with the box
        normalised to the image (0–1), best first.
        """
        np = self._np
        W, H = self.size
        iw, ih = img.size
        scale = min(W / iw, H / ih)
        nw, nh = max(1, round(iw * scale)), max(1, round(ih * scale))
        px, py = (W - nw) // 2, (H - nh) // 2
        canvas = Image.new("RGB", (W, H), (114, 114, 114))   # YOLOv5 letterbox
        canvas.paste(img.convert("RGB").resize((nw, nh), Image.BILINEAR), (px, py))
        x = np.asarray(canvas, dtype=np.float32).transpose(2, 0, 1)[None] / 255.0

        pred = self._session.run(None, {self._input: x})[0][0]     # (N, 5 + classes)
        cls_conf = pred[:, 5:] * pred[:, 4:5]
        cls = cls_conf.argmax(1)
        conf = cls_conf[np.arange(len(cls)), cls]
        keep = conf >= STORE_CONF
        if not keep.any():
            return []
        pred, cls, conf = pred[keep], cls[keep], conf[keep]
        cx, cy, bw, bh = pred[:, 0], pred[:, 1], pred[:, 2], pred[:, 3]
        boxes = np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], 1)
        order = _nms(boxes + cls[:, None] * 4096.0, conf, NMS_IOU, np)   # per class
        out = []
        def norm(v: float, offset: int, size: int) -> float:
            # plain Python floats, so results can be saved as JSON
            return round(min(1.0, max(0.0, (float(v) - offset) / scale / size)), 4)

        for i in order[:50]:
            x0, y0, x1, y1 = boxes[i]
            out.append([int(cls[i]), round(float(conf[i]), 3),
                        norm(x0, px, iw), norm(y0, py, ih),
                        norm(x1, px, iw), norm(y1, py, ih)])
        return out


def _nms(boxes, scores, iou, np) -> list[int]:
    """Plain non-maximum suppression; returns kept indices, best first."""
    order = scores.argsort()[::-1]
    area = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    keep = []
    while order.size:
        i = order[0]
        keep.append(int(i))
        xx0 = np.maximum(boxes[i, 0], boxes[order[1:], 0])
        yy0 = np.maximum(boxes[i, 1], boxes[order[1:], 1])
        xx1 = np.minimum(boxes[i, 2], boxes[order[1:], 2])
        yy1 = np.minimum(boxes[i, 3], boxes[order[1:], 3])
        inter = np.clip(xx1 - xx0, 0, None) * np.clip(yy1 - yy0, 0, None)
        overlap = inter / (area[i] + area[order[1:]] - inter + 1e-9)
        order = order[1:][overlap <= iou]
    return keep


_detector: Optional[Detector] = None
_detector_error: Optional[str] = None
_lock = threading.Lock()


def get_detector() -> Optional[Detector]:
    """The shared detector, loaded on first use (None if unavailable)."""
    global _detector, _detector_error
    with _lock:
        if _detector is not None or _detector_error is not None:
            return _detector
        path = _model_path()
        if path is None:
            _detector_error = f"model file {MODEL_FILE} not found in 'models' folder"
        else:
            try:
                _detector = Detector(path)
                log.info("Animal detection: %s (%dx%d)", MODEL_ID, *_detector.size)
            except Exception as e:                      # onnxruntime missing etc.
                _detector_error = str(e)
        if _detector_error:
            log.warning("Animal detection unavailable: %s", _detector_error)
        return _detector


def detector_status() -> str:
    """Short explanation when detection is unavailable ('' if available)."""
    get_detector()
    return _detector_error or ""


def refine(dets: list, img: Image.Image, species_model=None) -> list:
    """
    MegaDetector is trained on camera-trap photos and often labels a
    frame-filling bird or mammal (a typical telephoto shot) as "person".
    Check each person box: with the species model, keep it as a person only
    if the crop really looks human; without it, treat large person boxes
    as animals.
    """
    out = []
    for d in dets:
        d = list(d)
        if d[0] == 1 and d[1] >= ANIMAL_CONF:
            area = (d[4] - d[2]) * (d[5] - d[3])
            if species_model is not None:
                try:
                    top = species_model.predict(img, d[2:6], k=1)[0]
                    if not top[1].startswith("Homo "):
                        d[0] = 0
                except Exception as e:
                    log.debug("Person check failed: %s", e)
            elif area > 0.2:
                d[0] = 0
        out.append(d)
    # Busy scenes (reeds, branches) can hide an animal from the detector; if
    # the species model is confident about the whole frame, count it as
    # present. Marked "species": no box, so not used for edge / size checks.
    if species_model is not None and not animals(out):
        try:
            top = species_model.predict(img, None, k=1)[0]
            if top[2] >= SPECIES_PRESENT and not top[1].startswith("Homo "):
                out.append([0, round(top[2] * 0.5, 3), 0.0, 0.0, 1.0, 1.0, "species"])
        except Exception as e:
            log.debug("Species presence check failed: %s", e)
    return out


# ── Interpreting detections ──────────────────────────────────────────────────

def animals(dets: Optional[list]) -> list[list]:
    return [d for d in (dets or []) if d[0] == 0 and d[1] >= ANIMAL_CONF]


def main_animal(dets: Optional[list]) -> Optional[list]:
    """The most prominent animal with a real box (largest confident box)."""
    found = [d for d in animals(dets) if len(d) <= 6]
    if not found:
        return None
    return max(found, key=lambda d: (d[4] - d[2]) * (d[5] - d[3]) * d[1])


def _overlaps(a, b, grow: float = 0.02) -> bool:
    return not (a[2] + grow < b[0] or b[2] + grow < a[0] or
                a[3] + grow < b[1] or b[3] + grow < a[1])


def suggestions(dets: Optional[list], focus: Optional[tuple] = None) -> list[str]:
    """
    Suggestion codes for one image (see SUGGESTION_TEXT). Empty list = no
    concerns; None detections (not analysed yet) also give an empty list.
    """
    if dets is None:
        return []
    out: list[str] = []
    found = animals(dets)
    if not found:
        out.append("empty")
    else:
        boxed = [d for d in found if len(d) <= 6]
        if focus is not None and boxed and not any(_overlaps(focus[:4], d[2:6]) for d in boxed):
            out.append("missed")
        best = main_animal(dets)
        if best is not None:                    # (no box when only the species model saw it)
            x0, y0, x1, y1 = best[2:6]
            if x0 < EDGE_MARGIN or y0 < EDGE_MARGIN or x1 > 1 - EDGE_MARGIN or y1 > 1 - EDGE_MARGIN:
                out.append("edge")
            if (x1 - x0) * (y1 - y0) < SMALL_AREA:
                out.append("small")
    if any(d[0] == 1 and d[1] >= OTHER_CONF for d in dets):
        out.append("person")
    if any(d[0] == 2 and d[1] >= OTHER_CONF for d in dets):
        out.append("vehicle")
    return out
