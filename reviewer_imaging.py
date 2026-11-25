"""
Image access for Photo Reviewer: EXIF, orientation, fast preview / thumbnail
loading (embedded RAW previews, reduced-scale JPEG decoding), capture times,
JPEG conversion and quality scoring. No Tkinter here.
"""

from __future__ import annotations

import datetime
import io
import math
import logging
import os
import struct
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from PIL import Image, ExifTags, ImageFilter

log = logging.getLogger("photo_reviewer")

try:
    import rawpy
    _RAWPY_AVAILABLE = True
except ImportError:
    rawpy = None
    _RAWPY_AVAILABLE = False
    logging.warning("rawpy not installed — RAW files will not load.")

try:
    import numpy as np
    _NUMPY_AVAILABLE = True
except ImportError:
    np = None
    _NUMPY_AVAILABLE = False
    logging.warning("numpy not installed — quality scoring disabled.")

JPEG_QUALITY     = 95
THUMB_MAX        = 320  # px — small thumbnails (gallery views, filmstrip)

RAW_EXTS: frozenset[str] = frozenset({
    ".nef", ".cr2", ".cr3", ".arw", ".raf",
    ".rw2", ".orf", ".dng", ".pef", ".srw", ".nrw",
})
BITMAP_EXTS: frozenset[str] = frozenset({".jpg", ".jpeg", ".tif", ".tiff"})
SUPPORTED_EXTS: frozenset[str] = RAW_EXTS | BITMAP_EXTS

_EXIF_ORIENTATION: dict[int, object] = {
    2: Image.FLIP_LEFT_RIGHT,
    3: 180,
    4: Image.FLIP_TOP_BOTTOM,
    5: (Image.FLIP_LEFT_RIGHT, 90),
    6: 270,
    7: (Image.FLIP_LEFT_RIGHT, 270),
    8: 90,
}


# ── Disk priority ─────────────────────────────────────────────────────────────
# On a USB hard drive, several threads reading at once make the heads jump
# between files and every read waits behind the others, including the photo
# you're trying to look at. So, on drives other than the system drive:
#   - background passes read one file at a time (per drive);
#   - they wait while the viewer is reading, and for a moment after it.
# Reads from the system drive (usually an SSD) are never held back.

_SYSTEM_DRIVE = (os.environ.get("SystemDrive") or "C:").upper()
_FG_GRACE = 0.4                 # s of quiet after a viewer read before background resumes
_fg_cv = threading.Condition()
_fg_count = 0
_fg_last = 0.0
_drive_locks: dict[str, threading.RLock] = {}
_drive_guard = threading.Lock()
_hold = 0                       # > 0 while Process runs: background reads on slow drives wait


def slow_drive(path: str) -> bool:
    """True for paths not on the system drive (external disks, network shares)."""
    drive = os.path.splitdrive(os.path.abspath(path))[0].upper()
    if drive.startswith("\\\\"):
        return True
    return bool(drive) and drive != _SYSTEM_DRIVE


@contextmanager
def foreground_read():
    """Wrap reads the user is waiting for (viewer, preload, zoom, thumbnails)."""
    global _fg_count, _fg_last
    with _fg_cv:
        _fg_count += 1
    try:
        yield
    finally:
        with _fg_cv:
            _fg_count -= 1
            _fg_last = time.monotonic()
            _fg_cv.notify_all()


@contextmanager
def background_read(path: str, max_wait: float = 30.0):
    """Wrap reads by background passes: one at a time per slow drive, after the viewer."""
    if not slow_drive(path):
        yield
        return
    drive = os.path.splitdrive(os.path.abspath(path))[0].upper()
    with _drive_guard:
        lock = _drive_locks.setdefault(drive, threading.RLock())
    with lock:
        deadline = time.monotonic() + max_wait          # never starve the background for good
        with _fg_cv:
            while _hold > 0 or (time.monotonic() < deadline and (
                    _fg_count > 0 or time.monotonic() - _fg_last < _FG_GRACE)):
                _fg_cv.wait(0.1)
        yield


@contextmanager
def hold_background():
    """Pause background reads on slow drives completely (e.g. while Process copies)."""
    global _hold
    with _fg_cv:
        _hold += 1
    try:
        yield
    finally:
        with _fg_cv:
            _hold -= 1
            _fg_cv.notify_all()


def read_file_background(path: str, chunk: int = 4 << 20) -> bytes:
    """A whole file read by a background pass, in chunks so the viewer can get in between."""
    parts = []
    with open(path, "rb") as f:
        while True:
            with background_read(path):
                b = f.read(chunk)
            if not b:
                break
            parts.append(b)
    return b"".join(parts)


# ── EXIF ──────────────────────────────────────────────────────────────────────

def _find_orient_key() -> Optional[int]:
    for k, v in ExifTags.TAGS.items():
        if v == "Orientation":
            return k
    return None

_ORIENT_KEY: Optional[int] = _find_orient_key()


def auto_orient(img: Image.Image) -> Image.Image:
    if _ORIENT_KEY is None:
        return img
    try:
        exif = img._getexif() or {}
        op = _EXIF_ORIENTATION.get(exif.get(_ORIENT_KEY))
        if isinstance(op, int):
            return img.rotate(op, expand=True)
        if isinstance(op, tuple):
            img = img.transpose(op[0])
            return img.rotate(op[1], expand=True)
        if op is not None:
            return img.transpose(op)
    except Exception:
        pass
    return img


def read_exif(path: str) -> dict[str, str]:
    ext = Path(path).suffix.lower()
    raw_exif: dict = {}
    try:
        if ext in RAW_EXTS and _RAWPY_AVAILABLE:
            with rawpy.imread(path) as raw:
                try:
                    tb = raw.extract_thumb()
                    if tb.format == rawpy.ThumbFormat.JPEG:
                        raw_exif = Image.open(io.BytesIO(tb.data))._getexif() or {}
                except rawpy.LibRawNoThumbnailError:
                    pass
        else:
            raw_exif = Image.open(path)._getexif() or {}
    except Exception as e:
        return {"Error": str(e)}
    result: dict[str, str] = {}
    for tag_id, value in raw_exif.items():
        name = ExifTags.TAGS.get(tag_id, str(tag_id))
        if isinstance(value, bytes) and len(value) > 64:
            continue
        result[name] = str(value)
    return result


def format_exif_display(exif: dict[str, str]) -> list[tuple[str, str]]:
    """
    Convert raw EXIF dict into a clean list of (label, value) pairs
    using photographer-friendly formatting. Mimics how Lightroom / Photo
    Mechanic present shooting data.
    """
    rows: list[tuple[str, str]] = []

    def get(*keys: str) -> Optional[str]:
        for k in keys:
            if k in exif:
                return exif[k]
        return None

    # ── Camera ───────────────────────────────────────────────────────────────
    make  = get("Make", "make") or ""
    model = get("Model", "model") or ""
    # Strip redundant make prefix from model (e.g. "NIKON CORPORATION" + "NIKON D850")
    make_short = make.split()[0] if make else ""
    if make_short and model.upper().startswith(make_short.upper()):
        camera = model.strip()
    elif make_short:
        camera = f"{make_short} {model}".strip()
    else:
        camera = model.strip()
    if camera:
        rows.append(("Camera", camera))

    lens = get("LensModel", "Lens")
    if lens:
        rows.append(("Lens", lens.strip()))

    # ── Exposure ─────────────────────────────────────────────────────────────
    shutter = get("ExposureTime")
    if shutter:
        try:
            # Convert decimal (0.00125) to fraction (1/800)
            val = float(shutter)
            if val > 0 and val < 1:
                denom = round(1 / val)
                shutter_str = f"1/{denom}s"
            elif val >= 1:
                shutter_str = f"{val:.1f}s"
            else:
                shutter_str = shutter
        except Exception:
            shutter_str = shutter
        rows.append(("Shutter", shutter_str))

    fnum = get("FNumber")
    if fnum:
        try:
            rows.append(("Aperture", f"f/{float(fnum):.1f}"))
        except Exception:
            rows.append(("Aperture", f"f/{fnum}"))

    iso = get("ISOSpeedRatings", "ISO", "PhotographicSensitivity")
    if iso:
        rows.append(("ISO", f"ISO {iso}"))

    ev = get("ExposureBiasValue", "ExposureCompensation")
    if ev:
        try:
            evf = float(ev)
            rows.append(("Exp. comp.", f"{evf:+.1f} EV"))
        except Exception:
            rows.append(("Exp. comp.", ev))

    focal = get("FocalLength")
    if focal:
        try:
            rows.append(("Focal len.", f"{float(focal):.0f} mm"))
        except Exception:
            rows.append(("Focal len.", focal))

    # ── Date / time ───────────────────────────────────────────────────────────
    dt = get("DateTimeOriginal", "DateTime")
    if dt:
        # Raw format: "2026:05:12 14:23:01" → "2026-05-12  14:23"
        try:
            parts = dt.split()
            date  = parts[0].replace(":", "-")
            time  = parts[1][:5] if len(parts) > 1 else ""
            rows.append(("Date", f"{date}"))
            if time:
                rows.append(("Time", time))
        except Exception:
            rows.append(("Date/Time", dt))

    # ── Image dimensions ──────────────────────────────────────────────────────
    w = get("PixelXDimension", "ExifImageWidth")
    h = get("PixelYDimension", "ExifImageLength")
    if w and h:
        try:
            mp = int(w) * int(h) / 1_000_000
            rows.append(("Resolution", f"{w} × {h}  ({mp:.1f} MP)"))
        except Exception:
            rows.append(("Resolution", f"{w} × {h}"))

    # ── Flash / WB ────────────────────────────────────────────────────────────
    flash = get("Flash")
    if flash:
        try:
            # Flash EXIF is a bitmask — bit 0 = fired
            fired = int(flash) & 0x1
            rows.append(("Flash", "Fired" if fired else "No flash"))
        except Exception:
            rows.append(("Flash", flash))

    wb = get("WhiteBalance")
    if wb:
        rows.append(("White bal.", "Auto" if wb == "0" else "Manual"))

    # GPS shown in Location map widget — not duplicated in EXIF text
    alt = get("GPSAltitude")
    if alt:
        try:
            rows.append(("Altitude", f"{float(alt):.0f} m"))
        except Exception:
            pass

    return rows


# ── Image loading / conversion ────────────────────────────────────────────────

def convert_to_jpeg(src: str, out_dir: str,
                    out_name: Optional[str] = None) -> Optional[str]:
    """Full-quality JPEG conversion — full raw decode, not thumbnail."""
    out_path = os.path.join(out_dir, out_name or (Path(src).stem + ".jpg"))
    try:
        ext = Path(src).suffix.lower()
        if ext in RAW_EXTS and _RAWPY_AVAILABLE:
            with rawpy.imread(src) as raw:
                img = Image.fromarray(raw.postprocess())
        else:
            img = Image.open(src)
        auto_orient(img).convert("RGB").save(
            out_path, "JPEG", quality=JPEG_QUALITY, optimize=True
        )
        log.info("Converted %s → %s", src, out_path)
        return out_path
    except Exception as e:
        log.error("Conversion failed for %s: %s", src, e)
        return None


# ── Camera focus point (Nikon AFInfo2) ───────────────────────────────────────
#
# Nikon stores the AF result unencrypted in MakerNote tag 0x00B7 (AFInfo2).
# Layout follows ExifTool's Nikon.pm (AFInfo2V0101 — D5, D500, D850, D7500…):
#   byte 4  AFDetectionMethod (0 phase detect / viewfinder, 1 contrast / live view)
#   byte 5  AFAreaMode         byte 6  FocusPointSchema (7 = 153-point)
#   0x30    AFPointsInFocus (20-byte bitmask, point n = bit n-1)
#   0x44    PrimaryAFPoint (153-point index, see _AF153_COLS / _AF153_ROWS)
#   0x46..  AFImageWidth/Height, AFAreaX/YPosition, AFAreaWidth/Height
#           (int16u, live view only — exact AF rectangle in AFImage coordinates)

# 153-point grid: 9 rows A–I × 17 columns 1–17, centre E9. Point numbers run
# down each column in the order E,D,C,B,A,F,G,H,I; columns in this order:
_AF153_COLS = (9, 10, 11, 8, 7, 12, 13, 14, 15, 16, 17, 6, 5, 4, 3, 2, 1)
_AF153_ROWS = "EDCBAFGHI"

# Part of the FX frame spanned by the outermost 153-point columns/rows.
# Approximate — check with the focus overlay (P) and adjust in settings
# ("af_grid_width" / "af_grid_height") if boxes sit off the focused subject.
AF_GRID_WIDTH  = 0.56
AF_GRID_HEIGHT = 0.42
_AF_POINT_SIZE = 0.035      # box drawn / scored around one viewfinder point
_FX_WIDTH_D850 = 8256       # DX-crop images have the grid spread wider


def af153_point(index: int) -> Optional[tuple[str, int, int]]:
    """153-point index → (name, row 0–8 top→bottom, column 0–16 left→right)."""
    if not 1 <= index <= 153:
        return None
    col = _AF153_COLS[(index - 1) // 9]
    row_letter = _AF153_ROWS[(index - 1) % 9]
    return f"{row_letter}{col}", "ABCDEFGHI".index(row_letter), col - 1


def _nikon_makernote_entries(path: str) -> Optional[tuple[str, dict]]:
    """Parse the Nikon MakerNote IFD → (byte order, {tag: raw bytes})."""
    with Image.open(path) as im:
        exif = im.getexif()
        orientation = exif.get(0x0112, 1)
        width = exif.get_ifd(0x8769).get(0xA002)        # PixelXDimension
        mn = exif.get_ifd(0x8769).get(0x927C)
    if not mn or not isinstance(mn, (bytes, bytearray)) or mn[:6] != b"Nikon\x00":
        return None
    t = bytes(mn[10:])                              # embedded TIFF header
    bo = "<" if t[:2] == b"II" else ">"
    off = struct.unpack(bo + "I", t[4:8])[0]
    n = struct.unpack(bo + "H", t[off:off + 2])[0]
    sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 8: 2, 9: 4, 10: 8}
    out: dict = {"_orientation": orientation, "_width": width}
    for i in range(n):
        e = t[off + 2 + 12 * i: off + 14 + 12 * i]
        if len(e) < 12:
            break
        tag, typ, cnt = struct.unpack(bo + "HHI", e[:8])
        length = sizes.get(typ, 1) * cnt
        if length <= 4:
            out[tag] = e[8:8 + length]
        else:
            start = struct.unpack(bo + "I", e[8:12])[0]
            out[tag] = t[start:start + length]
    return bo, out


def _orient_box(box: tuple[float, float, float, float],
                orientation: int) -> tuple[float, float, float, float]:
    """Map a normalised sensor-orientation box into the displayed orientation."""
    x0, y0, x1, y1 = box
    if orientation == 3:
        return 1 - x1, 1 - y1, 1 - x0, 1 - y0
    if orientation == 6:            # displayed rotated 90° clockwise
        return 1 - y1, x0, 1 - y0, x1
    if orientation == 8:            # displayed rotated 90° anticlockwise
        return y0, 1 - x1, y1, 1 - x0
    return box


def read_focus_area(path: str, grid_w: Optional[float] = None,
                    grid_h: Optional[float] = None
                    ) -> Optional[tuple[float, float, float, float, str]]:
    """
    Where the camera focused, as a normalised box (x0, y0, x1, y1) in the
    displayed orientation plus a short label — or None if unknown (manual
    focus, no point recorded, non-Nikon camera).
    """
    try:
        parsed = _nikon_makernote_entries(path)
    except Exception:
        return None
    if not parsed:
        return None
    bo, ents = parsed
    b = ents.get(0x00B7)
    if not b or len(b) < 0x52 or b[:4] != b"0101":
        return None
    method, schema = b[4], b[6]
    orientation = ents.get("_orientation", 1) or 1

    if method == 1:                                   # live view: exact rectangle
        iw, ih, cx, cy, aw, ah = (struct.unpack(bo + "H", b[o:o + 2])[0]
                                  for o in (0x46, 0x48, 0x4A, 0x4C, 0x4E, 0x50))
        if not (iw and ih and aw and ah):
            return None
        box = ((cx - aw / 2) / iw, (cy - ah / 2) / ih,
               (cx + aw / 2) / iw, (cy + ah / 2) / ih)
        return (*_orient_box(box, orientation), "Live view")

    if schema != 7:
        return None
    points = []
    primary = af153_point(b[0x44])
    if primary:
        points = [primary]
    else:                                             # auto-area / 3D: points in focus
        for i, byte in enumerate(b[0x30:0x44]):
            for j in range(8):
                if byte & (1 << j):
                    p = af153_point(i * 8 + j + 1)
                    if p:
                        points.append(p)
    if not points:
        return None

    gw = grid_w or AF_GRID_WIDTH
    gh = grid_h or AF_GRID_HEIGHT
    width = ents.get("_width")
    if width and width < _FX_WIDTH_D850 * 0.8:       # DX crop: grid covers more
        gw, gh = min(0.95, gw * 1.5), min(0.9, gh * 1.5)
    half = _AF_POINT_SIZE / 2
    xs = [0.5 + (c - 8) / 16 * gw for _n, _r, c in points]
    ys = [0.5 + (r - 4) / 8 * gh for _n, r, _c in points]
    box = (max(0.0, min(xs) - half), max(0.0, min(ys) - half * 1.5),
           min(1.0, max(xs) + half), min(1.0, max(ys) + half * 1.5))
    label = points[0][0] if len(points) == 1 else f"{len(points)} points"
    return (*_orient_box(box, orientation), label)


# ── Quality scoring ───────────────────────────────────────────────────────────

SCORE_VERSION = 2           # scores without this "v" are recomputed
_MIN_REGION   = 0.12        # sharpness region is at least this share of the long side


def score_image(img: Image.Image,
                region: Optional[tuple[float, float, float, float]] = None,
                region_kind: str = "centre") -> Optional[dict]:
    """
    Quality metrics, each 0-100 (higher = better):
      sharpness — edge strength where it matters: the camera's focus point
                  if known, else the detected animal, else the centre 60 %
      exposure  — highlight / shadow clipping penalty (whole frame)
    region is a normalised (x0, y0, x1, y1) box; region_kind records which
    source it came from ("af", "animal" or "centre").
    """
    if not _NUMPY_AVAILABLE:
        return None

    thumb = img.copy()
    thumb.thumbnail((800, 800), Image.LANCZOS)
    arr      = np.asarray(thumb, dtype=np.float32)
    gray_arr = (arr @ np.array([0.299, 0.587, 0.114], np.float32)
                if arr.ndim == 3 else arr.copy())

    lap = np.asarray(thumb.convert("L").filter(ImageFilter.FIND_EDGES()), np.float32)
    h_lap, w_lap = lap.shape
    if region is None:
        region, region_kind = (0.2, 0.2, 0.8, 0.8), "centre"
    x0, y0, x1, y1 = region[:4]
    # Small boxes (one AF point) are widened so there is enough detail to measure
    min_w = _MIN_REGION * max(w_lap, h_lap) / w_lap
    min_h = _MIN_REGION * max(w_lap, h_lap) / h_lap
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    hw, hh = max(x1 - x0, min_w) / 2, max(y1 - y0, min_h) / 2
    rx0, rx1 = int(max(0, cx - hw) * w_lap), int(min(1, cx + hw) * w_lap)
    ry0, ry1 = int(max(0, cy - hh) * h_lap), int(min(1, cy + hh) * h_lap)
    patch = lap[ry0:max(ry1, ry0 + 1), rx0:max(rx1, rx0 + 1)]
    # 95th percentile — robust to noise spikes but captures sharp edges
    p95 = float(np.percentile(patch, 95)) if patch.size > 0 else 0
    # Calibrate: p95 ~5 = very blurry, ~25 = acceptable, ~60+ = sharp
    sharpness = int(min(100, max(0, (p95 - 3) / 60 * 100)))

    total    = gray_arr.size
    exposure = int(max(0, 100 - (float(np.sum(gray_arr > 250)) / total
                                + float(np.sum(gray_arr < 5))  / total) * 200))

    return {"sharpness": sharpness, "exposure": exposure,
            "dark": round(float(np.sum(gray_arr < 5)) / total, 5),
            "sharp_at": region_kind, "v": SCORE_VERSION}


def apply_raw_clipping(score: dict, rc: Optional[dict]) -> dict:
    """
    Add raw_clipping()'s result to a score. With raw data, "exposure" judges
    highlights from the sensor instead of the JPEG preview (clipping on the
    animal counts three times as much as elsewhere); the preview-based value
    stays in "exposure_jpeg" for the keeper model, which learnt from it.
    rc = {} records that the raw data couldn't be read (not retried).
    """
    score["raw"] = rc or {}
    if rc:
        hi = rc.get("f", 0) + 2 * (rc.get("s") or 0)
        score.setdefault("exposure_jpeg", score.get("exposure", 0))
        score["exposure"] = int(max(0, 100 - (hi + score.get("dark", 0)) * 200))
    return score


# ── Raw highlight clipping ────────────────────────────────────────────────────
# The embedded JPEG shows highlights as blown that the raw file still holds
# (and can miss one channel clipping), so clipping is read from the sensor
# data itself. Bits in a clip mask: 1 = red, 2 = green, 4 = blue clipped.

CLIP_R, CLIP_G, CLIP_B = 1, 2, 4
RAW_CLIP_STEP = 2            # use every 2nd Bayer quad each way (¼ of the data)
SUBJECT_BLOWN = 0.01         # ≥ 1 % of the animal clipped → suggestion


def _saturation(v, black: float, white: float) -> Optional[int]:
    """
    The value where one colour plane of this file clipped, or None if it
    didn't. Reported white levels are not the real ones and differ per
    channel and per frame (D850: 15311 reported; green clips at 16188–16276,
    red and blue at 16380), so it is read from the data: clipped photosites
    all hold the plane's maximum, a spike far above the values just below it.
    Unclipped planes tail off with only a handful of pixels at their maximum.
    """
    top = int(v.max())
    if white <= black or top < black + 0.6 * (white - black):
        return None
    at_top = int(np.count_nonzero(v == top))
    below = v[(v < top) & (v >= top - 0.01 * (white - black))]
    nxt = int(np.bincount(below - below.min()).max()) if below.size else 0
    if at_top < max(8, v.size // 4_000_000) or at_top < 3 * nxt:
        return None
    return top


def _oriented(mask, flip: int):
    """Turn a sensor-oriented array the way the camera held it."""
    k = {3: 2, 5: 1, 6: -1}.get(flip, 0)
    return np.rot90(mask, k) if k else mask


def raw_clip_mask(path: str, step: int = RAW_CLIP_STEP, background: bool = False):
    """
    (mask, values) for a RAW file, oriented like the preview: mask holds the
    clipped-channel bits per sampled Bayer quad, values the brightest channel
    of each quad as a share of saturation (0–1). None for non-RAW files,
    unusual sensors or unreadable files.
    """
    if not (_RAWPY_AVAILABLE and _NUMPY_AVAILABLE) or Path(path).suffix.lower() not in RAW_EXTS:
        return None
    try:
        # background passes on a slow drive read the file in polite chunks first
        src = io.BytesIO(read_file_background(path)) if background and slow_drive(path) else path
        with rawpy.imread(src) as raw:
            a = raw.raw_image_visible
            pattern = raw.raw_pattern
            desc = raw.color_desc.decode("ascii", "replace")
            if a.ndim != 2 or pattern is None or pattern.shape != (2, 2):
                return None               # X-Trans / linear DNG: not handled
            s = raw.sizes
            ct, cl = getattr(s, "crop_top_margin", 0), getattr(s, "crop_left_margin", 0)
            cw, ch = getattr(s, "crop_width", 0), getattr(s, "crop_height", 0)
            if cw and ch and ct + ch <= a.shape[0] and cl + cw <= a.shape[1]:
                a = a[ct:ct + ch, cl:cl + cw]          # the area the JPEG shows
                pattern = np.roll(pattern, (-(ct % 2), -(cl % 2)), axis=(0, 1))
            blacks = list(raw.black_level_per_channel)
            white = float(raw.white_level)
            flip = s.flip
            h, w = (a.shape[0] // 2) * 2, (a.shape[1] // 2) * 2
            quads = a[:h, :w].reshape(h // 2, 2, w // 2, 2)[::step, :, ::step, :]
            quads = np.ascontiguousarray(quads)
    except Exception as e:
        log.debug("Raw clipping unavailable for %s: %s", path, e)
        return None
    bits = {"R": CLIP_R, "G": CLIP_G, "B": CLIP_B}
    mask = np.zeros((quads.shape[0], quads.shape[2]), np.uint8)
    top = np.zeros(mask.shape, np.float32)
    for i in (0, 1):
        for j in (0, 1):
            v = quads[:, i, :, j]
            c = int(pattern[i, j])
            black = float(blacks[c]) if c < len(blacks) else float(np.mean(blacks))
            sat = _saturation(v, black, white)
            top = np.maximum(top, (v.astype(np.float32) - black)
                             / ((sat if sat is not None else white) - black))
            if sat is not None:
                mask[v >= sat] |= bits.get(desc[c] if c < len(desc) else "", 0)
    return _oriented(mask, flip), _oriented(np.clip(top, 0, 1), flip)


def _box_slice(shape, box):
    h, w = shape
    x0, y0, x1, y1 = box[:4]
    r0, r1 = int(max(0, y0) * h), int(math.ceil(min(1, y1) * h))
    c0, c1 = int(max(0, x0) * w), int(math.ceil(min(1, x1) * w))
    return slice(r0, max(r1, r0 + 1)), slice(c0, max(c1, c0 + 1))


def _background(clipped, rs: slice, cs: slice, k: int = 4, max_steps: int = 800):
    """
    Clipped areas that continue outside the subject box (a blown sky behind
    the bird) — a box is a rectangle, so it always takes in some background.
    Grown from the clipped pixels outside the box through connected clipped
    pixels, on a k× smaller grid. Returns a full-size boolean array.
    """
    h, w = clipped.shape
    hs, ws = -(-h // k), -(-w // k)
    pad = np.zeros((hs * k, ws * k), bool)
    pad[:h, :w] = clipped
    small = pad.reshape(hs, k, ws, k).any(axis=(1, 3))
    grow = small.copy()
    grow[rs.start // k:-(-rs.stop // k), cs.start // k:-(-cs.stop // k)] = False
    for _ in range(max_steps):
        g = grow.copy()
        g[1:, :] |= grow[:-1, :]; g[:-1, :] |= grow[1:, :]
        g[:, 1:] |= grow[:, :-1]; g[:, :-1] |= grow[:, 1:]
        g[1:, 1:] |= grow[:-1, :-1]; g[:-1, :-1] |= grow[1:, 1:]
        g[1:, :-1] |= grow[:-1, 1:]; g[:-1, 1:] |= grow[1:, :-1]
        g &= small
        if np.array_equal(g, grow):
            break
        grow = g
    return np.repeat(np.repeat(grow, k, axis=0), k, axis=1)[:h, :w]


def raw_clipping(path: str, subject=None) -> Optional[dict]:
    """
    Highlight clipping from the raw data:
      f  — share of the frame with any channel clipped
      s  — the same on the subject (normalised box x0, y0, x1, y1), or None;
           clipping that continues outside the box is background and not counted
      ch — which channels clip where it matters (subject if given), e.g. "R" or "RGB"
      hr — headroom in stops: how much brighter that area could have been
           exposed before its brightest 0.1 % clipped (0 when clipped)
    None for files this can't read.
    """
    got = raw_clip_mask(path, background=True)
    if got is None:
        return None
    mask, top = got
    out = {"f": round(float(np.count_nonzero(mask)) / mask.size, 5), "s": None}
    m, t = mask, top
    if subject is not None:
        rs, cs = _box_slice(mask.shape, subject)
        inside = mask[rs, cs] * ~_background(mask > 0, rs, cs)[rs, cs]
        m = inside
        out["s"] = round(float(np.count_nonzero(m)) / max(1, m.size), 5)
        x0, y0, x1, y1 = subject[:4]
        cx, cy, hw, hh = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 4, (y1 - y0) / 4
        t = top[_box_slice(top.shape, (cx - hw, cy - hh, cx + hw, cy + hh))]   # the animal's core
    seen = int(np.bitwise_or.reduce(m, axis=None)) if m.size else 0
    out["ch"] = "".join(c for c, b in (("R", CLIP_R), ("G", CLIP_G), ("B", CLIP_B)) if seen & b)
    p = float(np.percentile(t, 99.9)) if t.size else 1.0
    out["hr"] = 0.0 if seen else round(min(6.0, -math.log2(max(p, 1 / 64))), 1)
    return out


def raw_clip_text(rc: Optional[dict]) -> str:
    """One sidebar line for raw_clipping()'s result ('' when unknown)."""
    if not rc:
        return ""
    names = {"R": "red", "G": "green", "B": "blue"}
    chans = "all channels" if rc.get("ch") == "RGB" else \
        " + ".join(names[c] for c in rc.get("ch", ""))
    where, share = ("animal", rc["s"]) if rc.get("s") is not None else ("frame", rc.get("f", 0))
    if share and share >= 0.0005:
        return f"Raw: {share:.1%} of the {where} blown ({chans})"
    if rc.get("ch"):
        return f"Raw: a few {where} pixels blown ({chans})"
    hr = rc.get("hr", 0)
    room = f" · {hr:g} stop{'s' if hr != 1 else ''} headroom" if hr >= 0.1 else ""
    return f"Raw: no highlights blown on the {where}{room}"


def composite_score(
    scores: Optional[dict[str, int]],
    use_sharpness: bool = True,
    use_exposure: bool = True,
) -> float:
    """
    Weighted composite from active metrics (equal weight among active ones).
    Returns 0.0 if no scores or no metrics selected.
    """
    if not scores:
        return 0.0
    active = []
    if use_sharpness: active.append(scores.get("sharpness", 0))
    if use_exposure:  active.append(scores.get("exposure", 0))
    return sum(active) / len(active) if active else 0.0


def build_histogram(img: Image.Image) -> list[list[int]]:
    hist = img.convert("RGB").histogram()
    return [hist[0:256], hist[256:512], hist[512:768]]


# ── Hashing / burst detection ─────────────────────────────────────────────────

def _open_thumb_unoriented(path: str) -> Image.Image:
    """
    Open a small image for hashing / scoring / display without rotating it.
    Uses the embedded JPEG thumbnail from RAW files — fast, no full decode.
    The returned image still carries its EXIF (for capture time).
    """
    ext = Path(path).suffix.lower()
    if ext in RAW_EXTS and _RAWPY_AVAILABLE:
        with rawpy.imread(path) as raw:
            try:
                tb  = raw.extract_thumb()
                img = (Image.open(io.BytesIO(tb.data))
                       if tb.format == rawpy.ThumbFormat.JPEG
                       else Image.fromarray(tb.data))
            except rawpy.LibRawNoThumbnailError:
                img = None
        if img is None:
            img = _developed_preview(path)
        return img
    return Image.open(path)


# RAW files without an embedded preview (e.g. Topaz DNGs) have to be developed to
# be seen: 3–4 s for a 45 MP file, and preparation, analysis, thumbnails and the
# viewer each look at a photo. So each is developed once into a JPEG kept in
# %APPDATA%\PhotoReviewer\previews (by file, size and date).
_dev_locks: dict[str, threading.Lock] = {}
_dev_guard = threading.Lock()


def _preview_cache_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "PhotoReviewer" / "previews"


def _developed_preview(path: str) -> Image.Image:
    import hashlib
    st = os.stat(path)
    key = hashlib.sha1(f"{os.path.normcase(os.path.abspath(path))}|{st.st_size}|{int(st.st_mtime)}"
                       .encode()).hexdigest()
    cached = _preview_cache_dir() / f"{key}.jpg"
    with _dev_guard:
        lock = _dev_locks.setdefault(key, threading.Lock())
    with lock:                                 # two threads, one development
        if not cached.exists():
            with rawpy.imread(path) as raw:
                img = Image.fromarray(raw.postprocess(half_size=True))
            img.thumbnail((4096, 4096), Image.LANCZOS)      # (linear DNGs ignore half_size)
            try:
                cached.parent.mkdir(parents=True, exist_ok=True)
                tmp = cached.with_suffix(".tmp")
                img.save(tmp, "JPEG", quality=90)
                os.replace(tmp, cached)
            except OSError as e:
                log.debug("Could not cache the developed preview of %s: %s", path, e)
                return img
    # opened, not loaded: callers can still ask for a reduced-size JPEG decode
    return Image.open(cached)


def _extract_thumb_img(path: str) -> Image.Image:
    """Small, correctly oriented image (embedded JPEG for RAW files)."""
    return auto_orient(_open_thumb_unoriented(path))


def load_preview(path: str, min_side: int, box: Optional[tuple[int, int]] = None) -> Image.Image:
    """
    Oriented image whose sides are at least min_side (or, with box=(long, short),
    at least that long side by short side) where the source allows. JPEG data
    (including RAW-embedded previews) is decoded at a reduced scale — several
    times faster than a full-size decode. A box lets a 1900×1000 window get a
    quarter-size decode of a D850 preview instead of a half-size one.
    """
    img = _open_thumb_unoriented(path)
    if getattr(img, "format", None) == "JPEG":
        img.draft("RGB", box or (min_side, min_side))
    return auto_orient(img)


RAW_JPEG_QUALITY = 92
RAW_JPEG_MIN_SIDE = 2500      # an embedded preview smaller than this: develop the RAW instead


def raw_to_jpeg(src: str, target: str, when: Optional[float] = None, description: str = "",
                comment: str = "") -> None:
    """
    A JPEG of a RAW file for a list (the species list): the camera's own
    full-size preview embedded in the RAW (its colours, about a second for a
    D850 NEF), or the RAW developed when that preview is missing or small.
    Carries the capture time, a description and a comment (Windows shows it
    in Properties → Details → Comments). Written whole: to
    target + '.part', then renamed. Raises if it can't be made.
    """
    import datetime
    img = load_preview(src, 100000)                 # (full size: no reduced decode)
    img.load()
    if max(img.size) < RAW_JPEG_MIN_SIDE and _RAWPY_AVAILABLE and Path(src).suffix.lower() in RAW_EXTS:
        with rawpy.imread(src) as raw:
            img = Image.fromarray(raw.postprocess(use_camera_wb=True))
    exif = Image.Exif()
    exif[0x0112] = 1                                # (already the right way up)
    if when:
        stamp = datetime.datetime.fromtimestamp(when).strftime("%Y:%m:%d %H:%M:%S")
        exif[0x0132] = stamp
        exif.get_ifd(0x8769)[36867] = stamp         # DateTimeOriginal
    if description:
        exif[0x010E] = description.encode("ascii", "replace").decode("ascii")
        exif[0x9C9F] = description.encode("utf-16-le") + b"\0\0"      # XPSubject
    if comment:
        exif[0x9C9C] = comment.encode("utf-16-le") + b"\0\0"          # XPComment (any characters)
        exif.get_ifd(0x8769)[0x9286] = b"UNICODE\0" + comment.encode("utf-16-le")   # UserComment
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    tmp = target + ".part"
    try:
        img.convert("RGB").save(tmp, "JPEG", quality=RAW_JPEG_QUALITY, optimize=True, exif=exif.tobytes())
        os.replace(tmp, target)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def load_small_thumb(path: str, max_side: int = THUMB_MAX) -> Image.Image:
    """
    Small RGB thumbnail for grids and the filmstrip. JPEG data (including
    the preview embedded in RAW files) is decoded at reduced scale, which
    is several times faster than decoding at full size.
    """
    img = _open_thumb_unoriented(path)
    if getattr(img, "format", None) == "JPEG":
        img.draft("RGB", (max_side, max_side))
    img = auto_orient(img).convert("RGB")
    img.thumbnail((max_side, max_side), Image.BILINEAR)
    return img


def _exif_timestamp(img: Image.Image) -> Optional[float]:
    """DateTimeOriginal (or DateTime) from an image's EXIF as seconds, or None."""
    try:
        exif = img.getexif()
        dt = exif.get_ifd(0x8769).get(36867) or exif.get(306)
        if dt:
            return datetime.datetime.strptime(
                str(dt).strip("\x00 ")[:19], "%Y:%m:%d %H:%M:%S").timestamp()
    except Exception:
        pass
    return None


def _capture_timestamp(path: str, thumb: Optional[Image.Image] = None) -> Optional[float]:
    """
    Capture time of a file. Tries the (embedded) JPEG's EXIF first; many
    RAW previews carry none (e.g. Nikon), so TIFF-based RAW files (NEF, CR2,
    ARW, DNG, PEF, ORF…) fall back to the RAW container's own EXIF header.
    """
    ts = None
    try:
        ts = _exif_timestamp(thumb if thumb is not None else _open_thumb_unoriented(path))
    except Exception:
        pass
    if ts is None and Path(path).suffix.lower() in RAW_EXTS:
        try:
            with Image.open(path) as im:      # reads the header only
                ts = _exif_timestamp(im)
        except Exception:
            pass
    return ts


