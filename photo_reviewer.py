"""
Photo Reviewer
==============
A sleek triage tool for sorting wildlife photography.

Dependencies:
    conda install -c conda-forge pillow rawpy imagehash send2trash numpy
    pip install customtkinter

Supported formats:
    RAW  : .nef .cr2 .cr3 .arw .raf .rw2 .orf .dng .pef .srw .nrw
    JPEG : .jpg .jpeg
    TIFF : .tif .tiff

Keyboard shortcuts — Single view:
    ←  / 4           Previous image
    →  / 6           Next image
    B  / Enter        Toggle BEST
    D  / Backspace    Toggle DELETE
    I  / Space / +    Toggle ID
    G                 Gallery view (burst group)
    C                 Comparison view (burst top picks)
    Scroll wheel      Zoom in / out (towards cursor)
    Left/Mid drag     Pan when zoomed
    Escape            Reset zoom → exit fullscreen
    Q                 Quit

Keyboard shortcuts — Gallery view:
    Escape / G        Return to single view
    A                 Select all
    N                 Select none
"""

from __future__ import annotations

import io
import json
import logging
import os
import platform
import queue
import shutil
import time
import urllib.request
import urllib.parse
import urllib.error
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import customtkinter as ctk
import tkinter as tk
import tkinter.filedialog as fd
import tkinter.messagebox as mb
import tkinter.simpledialog as sd

from PIL import Image, ImageTk, ExifTags, ImageFilter
import imagehash
from send2trash import send2trash

try:
    import rawpy
    _RAWPY_AVAILABLE = True
except ImportError:
    _RAWPY_AVAILABLE = False
    logging.warning("rawpy not installed — RAW files will not load.")

try:
    import numpy as np
    _NUMPY_AVAILABLE = True
except ImportError:
    _NUMPY_AVAILABLE = False
    logging.warning("numpy not installed — quality scoring disabled.")

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

class TagBadge:
    """Lightweight state holder — tracks tag active state (not displayed directly)."""
    def __init__(self, parent=None, label: str = "", color: str = "") -> None:
        self._active = False
    def set_active(self, val: bool) -> None:
        self._active = val
    @property
    def active(self) -> bool:
        return self._active


# ── Constants ─────────────────────────────────────────────────────────────────

STATUS_FILENAME  = "photo_reviewer_status.json"
SCORE_CACHE_FILE = "photo_reviewer_scores.json"
HASH_CACHE_FILE  = "photo_reviewer_hashes.json"
# Average hash thresholds (faster, used at load time)
AHASH_DUP        = 4     # stricter — avg hash less discriminating than phash
AHASH_BURST      = 6    # tighter — reduces false positives from similar scenes
BURST_MAX_SECS   = 30   # images >30s apart cannot be same burst
# pHash thresholds (accurate, used when re-hashing on demand)
PHASH_DUP        = 8
PHASH_BURST      = 20
JPEG_QUALITY     = 95
ZOOM_STEP        = 1.15
ZOOM_MIN         = 0.1
ZOOM_MAX         = 10.0
GALLERY_COLS     = 4
FILMSTRIP_H      = 110
FILMSTRIP_THUMB_W = 130
COMPARISON_MAX   = 4
PRELOAD_RADIUS   = 3    # decode ±N images ahead/behind
FILMSTRIP_WINDOW = 20   # images shown each side of current in filmstrip

# App settings
TOPAZ_DEFAULT_PATH = r"C:\Program Files\Topaz Labs LLC\Topaz Photo AI\Topaz Photo AI.exe"
SETTINGS_FILE      = "photo_reviewer_settings.json"  # user prefs (Topaz path etc.)

# iNaturalist API settings
INAT_API_BASE     = "https://api.inaturalist.org/v1"
INAT_OAUTH_BASE   = "https://www.inaturalist.org"
INAT_SCORE_URL    = f"{INAT_API_BASE}/computervision/score_image"
INAT_SETTINGS_FILE = "photo_reviewer_inat.json"
RECENT_FILE        = Path(__file__).parent / "photo_reviewer_recent.json"
RECENT_MAX         = 5
INAT_IMAGE_SIZE   = 299   # iNat expects 299×299 squashed JPEG
INAT_TOP_N        = 3     # suggestions per image
INAT_RATE_LIMIT   = 1.1   # seconds between calls (stay under 60/min)

# Hashing — worker threads and polling interval
HASH_WORKERS     = 4    # parallel threads for file I/O + hashing
HASH_POLL_MS     = 50   # ms between result-queue drain ticks
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

# EXIF formatting handled in format_exif_display()

# Palette
C_BG       = "#0a0a0a"   # near-black canvas
C_SURFACE  = "#141414"   # surface / header / sidebar
C_BORDER   = "#222222"   # borders / separators
C_TEXT     = "#E8EAF0"   # primary text
C_MUTED    = "#6B7280"   # muted text / inactive
C_BEST     = "#22C55E"   # green — Best tag
C_DELETE   = "#EF4444"   # red — Delete tag
C_ID       = "#2D7A4F"   # forest green — ID tag (matches accent)
C_ACCENT   = "#2D7A4F"   # forest green — primary accent
C_INACTIVE = "#2A2D38"
C_WARN     = "#F59E0B"
C_COMPARE  = "#F97316"   # orange — compare tag
C_STAR     = "#FBBF24"   # amber — star ratings

STAR_COLORS = {1: "#ef4444", 2: "#f97316", 3: "#eab308",
               4: "#84cc16", 5: "#22c55e"}  # red→green gradient
C_SELECTED = "#8B5CF6"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")


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

def extract_preview(path: str) -> Image.Image:
    """Fast preview — uses embedded RAW thumbnail where available."""
    ext = Path(path).suffix.lower()
    if ext in RAW_EXTS:
        if not _RAWPY_AVAILABLE:
            raise RuntimeError("rawpy required for RAW files.")
        with rawpy.imread(path) as raw:
            try:
                tb = raw.extract_thumb()
                img = (Image.open(io.BytesIO(tb.data))
                       if tb.format == rawpy.ThumbFormat.JPEG
                       else Image.fromarray(tb.data))
            except rawpy.LibRawNoThumbnailError:
                img = Image.fromarray(raw.postprocess())
    else:
        img = Image.open(path)
    return auto_orient(img)


def convert_to_jpeg(src: str, out_dir: str) -> Optional[str]:
    """Full-quality JPEG conversion — full raw decode, not thumbnail."""
    out_path = os.path.join(out_dir, Path(src).stem + ".jpg")
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


# ── Quality scoring ───────────────────────────────────────────────────────────

def score_image(img: Image.Image) -> Optional[dict[str, int]]:
    """
    Three quality metrics, each 0-100 (higher = better).
      sharpness   — Laplacian variance (focus / edge definition)
      motion_blur — gradient isotropy (directional blur lowers score)
      exposure    — highlight / shadow clipping penalty
    """
    if not _NUMPY_AVAILABLE:
        return None

    thumb = img.copy()
    thumb.thumbnail((800, 800), Image.LANCZOS)
    arr      = np.asarray(thumb, dtype=np.float32)
    gray_arr = (arr @ np.array([0.299, 0.587, 0.114], np.float32)
                if arr.ndim == 3 else arr.copy())

    # Sharpness: centre-weighted local maximum Laplacian
    # Global variance penalises blurry backgrounds even when subject is sharp.
    # Instead: compute Laplacian, then take the 95th percentile of the
    # sharpest 40% central region (subject is usually centre-frame).
    lap_img  = thumb.convert("L").filter(ImageFilter.FIND_EDGES())
    lap      = np.asarray(lap_img, np.float32)
    h_lap, w_lap = lap.shape
    # Centre crop: middle 60% of each dimension
    cy0, cy1 = int(h_lap * 0.2), int(h_lap * 0.8)
    cx0, cx1 = int(w_lap * 0.2), int(w_lap * 0.8)
    centre_lap = lap[cy0:cy1, cx0:cx1]
    # Use 95th percentile — robust to noise spikes but captures sharp edges
    p95 = float(np.percentile(centre_lap, 95)) if centre_lap.size > 0 else 0
    # Calibrate: p95 ~5 = very blurry, ~25 = acceptable, ~60+ = sharp
    sharpness = int(min(100, max(0, (p95 - 3) / 60 * 100)))

    gx, gy      = np.abs(gray_arr[:, 1:] - gray_arr[:, :-1]), np.abs(gray_arr[1:, :] - gray_arr[:-1, :])
    ex, ey      = float(np.mean(gx)), float(np.mean(gy))
    motion_blur = 50 if ex + ey < 0.5 else int(min(ex, ey) / max(ex, ey) * 100)

    total    = gray_arr.size
    exposure = int(max(0, 100 - (float(np.sum(gray_arr > 250)) / total
                                + float(np.sum(gray_arr < 5))  / total) * 200))

    return {"sharpness": sharpness, "motion_blur": motion_blur, "exposure": exposure}


def composite_score(
    scores: Optional[dict[str, int]],
    use_sharpness: bool = True,
    use_motion: bool = True,
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
    if use_motion:    active.append(scores.get("motion_blur", 0))
    if use_exposure:  active.append(scores.get("exposure", 0))
    return sum(active) / len(active) if active else 0.0


def burst_score(scores: Optional[dict[str, int]]) -> float:
    """Sharpness-weighted composite for burst ranking (all metrics on)."""
    return composite_score(scores, True, True, True)


def build_histogram(img: Image.Image) -> list[list[int]]:
    hist = img.convert("RGB").histogram()
    return [hist[0:256], hist[256:512], hist[512:768]]


def _extract_full_img(path: str) -> Image.Image:
    """
    Extract a full-quality image for Detail scoring.
    For RAW files: decodes via rawpy (slow but accurate).
    For JPEGs/TIFFs: opens directly with PIL.
    Result resized to 1200px for reasonable score accuracy without huge memory use.
    """
    ext = Path(path).suffix.lower()
    if ext in RAW_EXTS and _RAWPY_AVAILABLE:
        try:
            with rawpy.imread(path) as raw:
                rgb = raw.postprocess(
                    use_camera_wb=True,
                    half_size=True,          # 2× faster, still full sensor data
                    no_auto_bright=True,
                    output_bps=8,
                )
            img = Image.fromarray(rgb)
            img.thumbnail((1200, 1200), Image.LANCZOS)
            return img
        except Exception:
            pass
    # Fallback: thumbnail
    return _extract_thumb_img(path)


# ── Hashing / burst detection ─────────────────────────────────────────────────

def _extract_thumb_img(path: str) -> Image.Image:
    """
    Extract a small image for hashing. Uses the embedded JPEG thumbnail
    from RAW files — fast, no full decode needed. Result is reused for
    both hashing and scoring to avoid double-loading the file.
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
                img = Image.fromarray(raw.postprocess(half_size=True))
    else:
        img = Image.open(path)
    return auto_orient(img)


def _get_exif_timestamp(path: str) -> Optional[float]:
    """Return DateTimeOriginal as a Unix-like float (seconds), or None."""
    try:
        import datetime
        with Image.open(path) as img:
            exif_data = img._getexif() or {}
        # Tag 36867 = DateTimeOriginal
        dt_str = exif_data.get(36867) or exif_data.get(306)
        if dt_str:
            dt = datetime.datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S")
            return dt.timestamp()
    except Exception:
        pass
    return None


def compute_ahash(path: str) -> Optional[imagehash.ImageHash]:
    """
    Fast average hash — used at load time for quick grouping.
    ~3-5x faster than pHash; slightly less accurate but fine for
    burst grouping where images are deliberately similar.
    """
    try:
        img = _extract_thumb_img(path)
        return imagehash.average_hash(img.convert("L"))
    except Exception as e:
        log.warning("aHash failed for %s: %s", path, e)
        return None


def compute_phash(path: str) -> Optional[imagehash.ImageHash]:
    """
    Accurate perceptual hash — used when re-hashing on demand.
    Slower than aHash but more discriminating; avoids false positives
    on wildlife shots with similar backgrounds (sky, water, grass).
    """
    try:
        img = _extract_thumb_img(path)
        return imagehash.phash(img.convert("L"))
    except Exception as e:
        log.warning("pHash failed for %s: %s", path, e)
        return None


# ── Hash cache ────────────────────────────────────────────────────────────────

def load_hash_cache(
    cache_file: str,
    valid_files: set[str],
) -> dict[str, imagehash.ImageHash]:
    """
    Load previously computed average hashes from disk.
    Keyed by absolute path; validated against mtime so changed files
    are re-hashed automatically.
    """
    if not os.path.exists(cache_file):
        return {}
    try:
        with open(cache_file, encoding="utf-8") as f:
            raw: dict = json.load(f)
    except Exception as e:
        log.warning("Could not load hash cache: %s", e)
        return {}

    result: dict[str, imagehash.ImageHash] = {}
    for path, entry in raw.items():
        if path not in valid_files:
            continue
        try:
            if abs(entry.get("_mtime", 0) - os.path.getmtime(path)) > 1:
                continue
            result[path] = imagehash.ImageHash(
                imagehash.hex_to_hash(entry["hash"])._hash
            )
        except Exception:
            continue
    log.info("Hash cache: loaded %d / %d entries", len(result), len(raw))
    return result


def save_hash_cache(
    cache_file: str,
    hashes: dict[str, imagehash.ImageHash],
) -> None:
    """Persist average hashes to disk with mtime for freshness checking."""
    out: dict[str, dict] = {}
    for path, h in hashes.items():
        try:
            out[path] = {"hash": str(h), "_mtime": os.path.getmtime(path)}
        except OSError:
            continue
    if out:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(out, f)
    elif os.path.exists(cache_file):
        os.remove(cache_file)


def load_status(status_file: str, valid_files: set[str]) -> dict[str, dict]:
    if not os.path.exists(status_file):
        return {}
    try:
        with open(status_file, encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if k in valid_files}
    except Exception as e:
        log.warning("Could not load status: %s", e)
        return {}


def save_status(status_file: str, status: dict[str, dict]) -> None:
    tagged = {k: v for k, v in status.items() if any(v.values())}
    if tagged:
        with open(status_file, "w", encoding="utf-8") as f:
            json.dump(tagged, f, indent=2)
    elif os.path.exists(status_file):
        os.remove(status_file)


def write_xmp_sidecar(image_path: str, tags: dict) -> None:
    """
    Write an XMP sidecar file alongside the image containing:
    - Lightroom Pick/Reject flag  (xmp:Label)
    - Star rating                 (xmp:Rating)
    - Keywords                    (dc:subject)
    - Caption                     (dc:description)
    - Copyright                   (dc:rights)
    Silently skips on any error.
    """
    try:
        p      = Path(image_path)
        xmp_p  = p.with_suffix(".xmp")
        rating = tags.get("star", 0)
        label  = ("Reject" if tags.get("delete")
                  else "Pick" if tags.get("best")
                  else "")
        keywords = tags.get("keywords", [])
        caption  = tags.get("caption", "")
        copyright_ = tags.get("copyright", "")

        kw_xml = "".join(
            f"      <rdf:li>{k}</rdf:li>\n" for k in keywords)
        xmp = f"""<?xpacket begin='\xef\xbb\xbf' id='W5M0MpCehiHzreSzNTczkc9d'?>
<x:xmpmeta xmlns:x='adobe:ns:meta/'>
 <rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>
  <rdf:Description rdf:about=''
    xmlns:xmp='http://ns.adobe.com/xap/1.0/'
    xmlns:dc='http://purl.org/dc/elements/1.1/'
    xmlns:lr='http://ns.adobe.com/lightroom/1.0/'>
   <xmp:Rating>{rating}</xmp:Rating>
   <xmp:Label>{label}</xmp:Label>
   <dc:rights><rdf:Alt><rdf:li xml:lang='x-default'>{copyright_}</rdf:li></rdf:Alt></dc:rights>
   <dc:description><rdf:Alt><rdf:li xml:lang='x-default'>{caption}</rdf:li></rdf:Alt></dc:description>
   <dc:subject>
    <rdf:Bag>
{kw_xml}    </rdf:Bag>
   </dc:subject>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end='w'?>"""
        xmp_p.write_text(xmp, encoding="utf-8")
    except Exception as e:
        log.debug("XMP write failed %s: %s", image_path, e)


def is_removable_drive(path: str) -> bool:
    """Return True if path is on a removable drive (Windows/Linux/macOS)."""
    try:
        import sys as _sys
        if _sys.platform == "win32":
            import ctypes as _ctypes
            drive = Path(path).anchor  # e.g. "D:\"
            DRIVE_REMOVABLE = 2
            return _ctypes.windll.kernel32.GetDriveTypeW(drive) == DRIVE_REMOVABLE
        else:
            # Linux/Mac: check if under /media/, /mnt/, /Volumes/
            abs_path = str(Path(path).resolve())
            return any(abs_path.startswith(p)
                       for p in ("/media/", "/mnt/", "/Volumes/", "/run/media/"))
    except Exception:
        return False


def load_app_settings() -> dict:
    """Load persistent app settings (Topaz path, keywords, etc.)."""
    p = Path(__file__).parent / SETTINGS_FILE
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_app_settings(settings: dict) -> None:
    p = Path(__file__).parent / SETTINGS_FILE
    try:
        p.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("Could not save settings: %s", e)


# ── Score cache ───────────────────────────────────────────────────────────────

def load_score_cache(
    cache_file: str,
    valid_files: set[str],
) -> dict[str, dict[str, int]]:
    """
    Load previously computed quality scores from disk.
    Each entry is keyed by absolute file path and stores a dict of metric→score
    plus a "mtime" field used to invalidate stale entries (file was modified).
    Only entries whose file still exists and whose mtime matches are kept.
    """
    if not os.path.exists(cache_file):
        return {}
    try:
        with open(cache_file, encoding="utf-8") as f:
            raw: dict = json.load(f)
    except Exception as e:
        log.warning("Could not load score cache: %s", e)
        return {}

    result: dict[str, dict[str, int]] = {}
    for path, entry in raw.items():
        if path not in valid_files:
            continue
        try:
            stored_mtime = entry.get("_mtime", 0)
            actual_mtime = os.path.getmtime(path)
            if abs(stored_mtime - actual_mtime) > 1:   # >1s difference = stale
                continue
            # Return scores without the internal _mtime key
            result[path] = {k: v for k, v in entry.items() if not k.startswith("_")}
        except OSError:
            continue
    log.info("Score cache: loaded %d / %d entries", len(result), len(raw))
    return result


def save_score_cache(
    cache_file: str,
    scores: dict[str, dict[str, int]],
) -> None:
    """Persist all computed scores to disk, including mtime for freshness."""
    out: dict[str, dict] = {}
    for path, metrics in scores.items():
        if metrics is None:
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        out[path] = dict(metrics)
        out[path]["_mtime"] = mtime

    if out:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(out, f)   # no indent — keep file compact
    elif os.path.exists(cache_file):
        os.remove(cache_file)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _darken(hex_color: str, factor: float = 0.7) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i+2], 16) for i in (0, 2, 4))
    return "#{:02x}{:02x}{:02x}".format(
        max(0, int(r*factor)), max(0, int(g*factor)), max(0, int(b*factor)))


def _score_color(score: Optional[int]) -> str:
    if score is None:
        return C_MUTED
    return C_BEST if score >= 70 else (C_WARN if score >= 40 else C_DELETE)


# ── Custom widgets ────────────────────────────────────────────────────────────


class CountRow(ctk.CTkFrame):
    def __init__(self, parent, label: str, color: str, **kw):
        super().__init__(parent, fg_color="transparent", **kw)
        ctk.CTkLabel(self, text="●", text_color=color,
                     font=ctk.CTkFont("Helvetica", 10)).pack(side="left", padx=(0, 6))
        ctk.CTkLabel(self, text=label, text_color=C_MUTED,
                     font=ctk.CTkFont("Helvetica", 10)).pack(side="left")
        self._val = ctk.CTkLabel(self, text="0", text_color=C_TEXT,
                                  font=ctk.CTkFont("Helvetica", 10, "bold"))
        self._val.pack(side="right")

    def set_value(self, n: int) -> None:
        self._val.configure(text=str(n))


class ScoreRow(ctk.CTkFrame):
    def __init__(self, parent, label: str, **kw):
        super().__init__(parent, fg_color="transparent", **kw)
        ctk.CTkLabel(self, text=label, text_color=C_MUTED,
                     font=ctk.CTkFont("Helvetica", 9),
                     width=80, anchor="w").pack(side="left")
        self._val = ctk.CTkLabel(self, text="—", text_color=C_MUTED,
                                  font=ctk.CTkFont("Helvetica", 9, "bold"))
        self._val.pack(side="right")

    def set_score(self, score: Optional[int]) -> None:
        if score is None:
            self._val.configure(text="—", text_color=C_MUTED)
        else:
            self._val.configure(text=str(score), text_color=_score_color(score))


# ── Country map widget ────────────────────────────────────────────────────────

def _load_country_data() -> Optional[dict]:
    """
    Load Natural Earth 110m country GeoJSON from the same folder as this
    script. Returns the parsed dict or None if file not found.
    Loaded once at module level for the whole session.
    """
    script_dir = Path(__file__).parent
    geojson_path = script_dir / "photo_reviewer_countries.geojson"
    if not geojson_path.exists():
        log.warning("Country data not found: %s", geojson_path)
        return None
    try:
        with open(geojson_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning("Could not load country data: %s", e)
        return None


# Load once at import time — fast after first load
_COUNTRY_DATA: Optional[dict] = None


def _get_country_data() -> Optional[dict]:
    global _COUNTRY_DATA
    if _COUNTRY_DATA is None:
        _COUNTRY_DATA = _load_country_data()
    return _COUNTRY_DATA


def _point_in_polygon(px: float, py: float,
                      polygon: list[list[float]]) -> bool:
    """Ray-casting point-in-polygon test."""
    n       = len(polygon)
    inside  = False
    j       = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > py) != (yj > py) and
                px < (xj - xi) * (py - yi) / (yj - yi + 1e-10) + xi):
            inside = not inside
        j = i
    return inside


def _find_country(lat: float, lng: float) -> Optional[tuple[str, list]]:
    """
    Return (country_name, [polygon_rings]) for the country containing
    the given lat/lng, or None if not found.
    Uses Natural Earth 110m data — fast O(n) scan, ~250 countries.
    """
    data = _get_country_data()
    if data is None:
        return None

    for feature in data.get("features", []):
        name  = (feature.get("properties") or {}).get("NAME", "Unknown")
        geom  = feature.get("geometry") or {}
        gtype = geom.get("type", "")
        coords = geom.get("coordinates", [])

        if gtype == "Polygon":
            polys = [coords]
        elif gtype == "MultiPolygon":
            polys = coords
        else:
            continue

        for poly in polys:
            if not poly:
                continue
            outer = poly[0]
            if _point_in_polygon(lng, lat, outer):
                # Return all rings of all sub-polygons for this country
                all_rings = []
                for p in polys:
                    all_rings.extend(p)
                return name, all_rings

    return None


def _os_grid_ref(lat: float, lng: float) -> Optional[str]:
    """Convert WGS84 lat/lng to 8-char OS National Grid reference. UK only."""
    import math
    a, b   = 6378137.0, 6356752.3141
    a2, b2 = 6377563.396, 6356256.910
    e2     = (a**2 - b**2) / a**2
    e2_2   = (a2**2 - b2**2) / a2**2
    r      = math.radians
    sinL, cosL = math.sin(r(lat)), math.cos(r(lat))
    nu = a / math.sqrt(1 - e2 * sinL**2)
    x  = nu * cosL * math.cos(r(lng))
    y  = nu * cosL * math.sin(r(lng))
    z  = nu * (1 - e2) * sinL
    # Helmert WGS84 → OSGB36
    s  = 1 + 20.4894e-6
    rx_, ry_, rz_ = r(-0.1502/3600), r(-0.2470/3600), r(-0.8421/3600)
    x2 = -446.448 + s*(x - rz_*y + ry_*z)
    y2 =  125.157 + s*(rz_*x + y - rx_*z)
    z2 = -542.060 + s*(-ry_*x + rx_*y + z)
    lon2 = math.atan2(y2, x2)
    p = math.sqrt(x2**2 + y2**2)
    lat2 = math.atan2(z2, p*(1 - e2_2))
    for _ in range(5):
        nu2  = a2 / math.sqrt(1 - e2_2 * math.sin(lat2)**2)
        lat2 = math.atan2(z2 + e2_2*nu2*math.sin(lat2), p)
    # OSGB36 → EN
    lo, F0 = r(-2.0), 0.9996012717
    n  = (a2 - b2) / (a2 + b2)
    lo = r(-2.0); la = r(49.0)
    sinL2, cosL2 = math.sin(lat2), math.cos(lat2)
    nu3  = a2*F0 / math.sqrt(1 - e2_2*sinL2**2)
    rho  = a2*F0*(1 - e2_2) / (1 - e2_2*sinL2**2)**1.5
    eta2 = nu3/rho - 1
    dl   = lon2 - lo
    M    = b2*F0*((1+n+1.25*n**2+1.25*n**3)*(lat2-la)
                  - (3*n+3*n**2+2.625*n**3)*math.sin(lat2-la)*math.cos(lat2+la)
                  + (1.875*n**2+1.875*n**3)*math.sin(2*(lat2-la))*math.cos(2*(lat2+la))
                  - (35/24)*n**3*math.sin(3*(lat2-la))*math.cos(3*(lat2+la)))
    I, II   = M - 100000, nu3/2*sinL2*cosL2
    III_    = nu3/24*sinL2*cosL2**3*(5 - cosL2**2 + 9*eta2)
    IIIA    = nu3/720*sinL2*cosL2**5*(61 - 58*cosL2**2 + cosL2**4)
    IV, V   = nu3*cosL2, nu3/6*cosL2**3*(nu3/rho - math.tan(lat2)**2)
    VI      = nu3/120*cosL2**5*(5 - 18*math.tan(lat2)**2 + math.tan(lat2)**4
                                 + 14*eta2 - 58*math.tan(lat2)**2*eta2)
    N = I + II*dl**2 + III_*dl**4 + IIIA*dl**6
    E = 400000 + IV*dl + V*dl**3 + VI*dl**5
    if not (0 <= E <= 700000 and 0 <= N <= 1300000):
        return None
    L = "ABCDEFGHJKLMNOPQRSTUVWXYZ"
    e1, n1 = int(E/100000), int(N/100000)
    l1 = L[19 - (n1//5)*5 + e1//5]
    l2 = L[(4 - n1%5)*5 + e1%5]
    return f"{l1}{l2}{int(E%100000/10):05d}{int(N%100000/10):05d}"[:8]


class CountryMapCanvas(tk.Canvas):
    """
    Small map canvas that:
    - Auto-detects which country a GPS point is in
    - Draws that country's outline scaled to fit the canvas
    - Plots a crosshair at the exact location
    - Shows OS grid reference for UK coordinates
    - Falls back to a simple world outline if country not found
    """
    W, H = 230, 140

    def __init__(self, parent, **kw):
        super().__init__(parent, width=self.W, height=self.H,
                         bg="#050505", highlightthickness=1,
                         highlightbackground=C_BORDER, **kw)
        self._last_lat: Optional[float] = None
        self._last_lng: Optional[float] = None

    def clear(self) -> None:
        self.delete("all")
        self.create_text(self.W // 2, self.H // 2,
                         text="No GPS data", fill=C_MUTED,
                         font=("Helvetica", 8))

    def set_location(self, lat: float, lng: float) -> None:
        self._last_lat = lat
        self._last_lng = lng
        self._draw(lat, lng)

    def _draw(self, lat: float, lng: float) -> None:
        self.delete("all")

        # Try to find country
        result = _find_country(lat, lng)

        PAD = 12
        W, H = self.W - PAD*2, self.H - PAD*2

        if result:
            country_name, rings = result

            # Compute bounding box of all rings
            all_pts = [pt for ring in rings for pt in ring]
            min_lng = min(p[0] for p in all_pts)
            max_lng = max(p[0] for p in all_pts)
            min_lat = min(p[1] for p in all_pts)
            max_lat = max(p[1] for p in all_pts)

            span_lng = max(max_lng - min_lng, 0.1)
            span_lat = max(max_lat - min_lat, 0.1)

            # Scale to fit with padding, preserving aspect ratio
            scale = min(W / span_lng, H / span_lat) * 0.88

            def proj(lo: float, la: float):
                x = PAD + (lo - min_lng) * scale + (W - span_lng * scale) / 2
                y = PAD + (max_lat - la) * scale + (H - span_lat * scale) / 2
                return x, y

            # Draw each ring
            for ring in rings:
                if len(ring) < 3:
                    continue
                pts = [proj(p[0], p[1]) for p in ring]
                flat = [c for xy in pts for c in xy]
                self.create_polygon(flat,
                                     fill="#1e2d1e",
                                     outline="#3a5a3a",
                                     width=1)

            # Crosshair at GPS location
            cx, cy = proj(lng, lat)
            r = 5
            self.create_line(cx - r*2, cy, cx + r*2, cy,
                             fill=C_ACCENT, width=1)
            self.create_line(cx, cy - r*2, cx, cy + r*2,
                             fill=C_ACCENT, width=1)
            self.create_oval(cx - r, cy - r, cx + r, cy + r,
                             fill=C_ACCENT, outline="white", width=1)

            # Country name label
            self.create_text(self.W // 2, self.H - 8,
                             text=country_name,
                             fill=C_MUTED, font=("Helvetica", 7),
                             anchor="center")

            # OS grid reference if UK
            if country_name in ("United Kingdom", "UK", "Great Britain"):
                grid = _os_grid_ref(lat, lng)
                if grid:
                    self.create_text(PAD, PAD,
                                     text=grid, fill=C_TEXT,
                                     font=("Helvetica", 8, "bold"),
                                     anchor="nw")

        else:
            # Fallback: world bounding box
            def proj_world(lo: float, la: float):
                x = PAD + (lo + 180) / 360 * W
                y = PAD + (90 - la) / 180 * H
                return x, y

            data = _get_country_data()
            if data:
                for feature in data.get("features", []):
                    geom  = feature.get("geometry") or {}
                    gtype = geom.get("type", "")
                    coords = geom.get("coordinates", [])
                    polys  = [coords] if gtype == "Polygon" else (
                             coords if gtype == "MultiPolygon" else [])
                    for poly in polys:
                        if not poly:
                            continue
                        pts  = [proj_world(p[0], p[1]) for p in poly[0]]
                        flat = [c for xy in pts for c in xy]
                        if len(flat) >= 6:
                            self.create_polygon(flat, fill="#1e2d1e",
                                                outline="#3a5a3a", width=1)

            cx, cy = proj_world(lng, lat)
            self.create_oval(cx - 4, cy - 4, cx + 4, cy + 4,
                             fill=C_ACCENT, outline="white", width=1)
            self.create_text(self.W // 2, self.H - 8,
                             text=f"{abs(lat):.2f}°{'N' if lat>=0 else 'S'}  "
                                  f"{abs(lng):.2f}°{'E' if lng>=0 else 'W'}",
                             fill=C_MUTED, font=("Helvetica", 7), anchor="center")


class HistogramCanvas(tk.Canvas):
    """
    Clean RGB histogram matching Lightroom/Capture One style:
    - Linear scale (intuitive, shows true distribution)
    - All channels normalised to a single shared peak
    - Layered R → G → B, each as a solid filled polygon
    - Overlap regions use colour-mixed fills for natural blending
    - Clipping indicators: bright red/blue triangles at edges
    - Axis ticks at 0, 64, 128, 192, 255
    """
    H      = 110   # fixed height; width fills parent via pack fill="x"
    PAD_B  = 16
    PAD_T  = 8
    PAD_LR = 8

    def __init__(self, parent, **kw):
        super().__init__(parent, height=self.H,
                         bg="#050505", highlightthickness=0, **kw)
        self.bind("<Configure>", self._on_resize)
        self._histogram_data: Optional[list[list[int]]] = None

    def _on_resize(self, event) -> None:
        """Redraw when canvas is resized so it always fills parent width."""
        if self._histogram_data is not None:
            self._draw(self._histogram_data)

    @property
    def W(self) -> int:
        w = self.winfo_width()
        return w if w > 10 else 240

    def _make_poly(self, values: list[float], draw_w: int,
                   draw_h: int) -> list[int]:
        """Build a closed polygon from normalised (0-1) height values."""
        pts = [self.PAD_LR, self.PAD_T + draw_h]
        for i, v in enumerate(values):
            px = self.PAD_LR + int(i / 255 * (draw_w - 1))
            py = self.PAD_T + draw_h - int(v * (draw_h - 1))
            pts += [px, py]
        pts += [self.PAD_LR + draw_w - 1, self.PAD_T + draw_h]
        return pts

    def set_data(self, histogram: Optional[list[list[int]]]) -> None:
        self._histogram_data = histogram
        self._draw(histogram)

    def _draw(self, histogram: Optional[list[list[int]]]) -> None:
        self.delete("all")
        draw_h = self.H - self.PAD_B - self.PAD_T
        draw_w = self.W - self.PAD_LR * 2
        if draw_w < 10:
            return

        if histogram is None:
            self.create_text(self.W // 2, self.PAD_T + draw_h // 2,
                             text="Analysing…", fill=C_MUTED,
                             font=("Helvetica", 8))
            self._draw_axes(draw_w, draw_h)
            return

        r_raw, g_raw, b_raw = histogram[0], histogram[1], histogram[2]

        # Single shared peak across all channels — same scale for all
        peak = max(max(r_raw), max(g_raw), max(b_raw), 1)

        r = [v / peak for v in r_raw]
        g = [v / peak for v in g_raw]
        b = [v / peak for v in b_raw]

        # Luminance underlay — drawn first as a reference shape
        lum_raw = [r_raw[i]*0.299 + g_raw[i]*0.587 + b_raw[i]*0.114
                   for i in range(256)]
        lum_peak = max(lum_raw) or 1
        lum = [v / lum_peak for v in lum_raw]
        lum_pts = self._make_poly(lum, draw_w, draw_h)
        self.create_polygon(lum_pts, fill="#1e2035", outline="")

        # Draw B → G → R layered (back to front like Lightroom)
        # Each channel: semi-dark fill body + bright top edge line
        for vals, fill, edge in [
            (b, "#0d1a55", "#3366ff"),   # blue
            (g, "#0d3a18", "#33aa55"),   # green
            (r, "#550d0d", "#ff4422"),   # red
        ]:
            pts = self._make_poly(vals, draw_w, draw_h)
            self.create_polygon(pts, fill=fill, outline="")
            # Extract just the top-edge points (skip first and last baseline pts)
            top_pts = []
            for i in range(256):
                top_pts.append(pts[2 + i*2])
                top_pts.append(pts[3 + i*2])
            self.create_line(top_pts, fill=edge, width=1)

        # Clipping indicators — triangles at edges
        total = max(sum(r_raw) + sum(g_raw) + sum(b_raw), 1)
        clip_hi = (sum(r_raw[252:]) + sum(g_raw[252:]) + sum(b_raw[252:])) / total
        clip_lo = (sum(r_raw[:4])   + sum(g_raw[:4])   + sum(b_raw[:4]))   / total

        if clip_hi > 0.0005:
            # Red triangle top-right
            rx = self.PAD_LR + draw_w - 1
            self.create_polygon(
                rx - 8, self.PAD_T,
                rx,     self.PAD_T,
                rx,     self.PAD_T + 8,
                fill="#ff3300", outline="")

        if clip_lo > 0.0005:
            # Blue triangle top-left
            lx = self.PAD_LR
            self.create_polygon(
                lx,     self.PAD_T,
                lx + 8, self.PAD_T,
                lx,     self.PAD_T + 8,
                fill="#3366ff", outline="")

        self._draw_axes(draw_w, draw_h)

        # R G B legend — top right corner
        for i, (letter, color) in enumerate([
            ("R", "#ff4422"), ("G", "#33aa55"), ("B", "#3366ff")
        ]):
            self.create_text(self.W - self.PAD_LR - 4 - (2-i)*13,
                             self.PAD_T + 6,
                             text=letter, fill=color,
                             font=("Helvetica", 8, "bold"), anchor="center")

    def _draw_axes(self, draw_w: int, draw_h: int) -> None:
        """Draw baseline and tick marks."""
        base_y = self.PAD_T + draw_h

        # Baseline
        self.create_line(self.PAD_LR, base_y,
                         self.PAD_LR + draw_w, base_y,
                         fill="#2a2d38", width=1)

        # Ticks + labels at 0, 64, 128, 192, 255
        for frac, label in [(0,"0"), (0.25,"64"), (0.5,"128"),
                             (0.75,"192"), (1.0,"255")]:
            x = self.PAD_LR + int(frac * (draw_w - 1))
            self.create_line(x, base_y, x, base_y + 3, fill="#3a3d48")
            self.create_text(x, self.H - 3, text=label,
                             fill="#505570", font=("Helvetica", 6),
                             anchor="center")

        # Grid lines — subtle vertical at 64, 128, 192
        for frac in (0.25, 0.5, 0.75):
            x = self.PAD_LR + int(frac * (draw_w - 1))
            self.create_line(x, self.PAD_T, x, base_y,
                             fill="#1a1d28", width=1)


# ── iNaturalist species suggestion ───────────────────────────────────────────

def _inat_load_settings(base_dir: str) -> dict:
    """Load stored iNat credentials and cached JWT from the session folder."""
    path = os.path.join(base_dir, INAT_SETTINGS_FILE)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _inat_save_settings(base_dir: str, settings: dict) -> None:
    path = os.path.join(base_dir, INAT_SETTINGS_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)


def _inat_get_jwt(app_id: str, app_secret: str,
                   username: str, password: str) -> Optional[str]:
    """
    Obtain a JWT from iNaturalist using Resource Owner Password Credentials.
    Step 1: get OAuth access token.
    Step 2: exchange for JWT.
    Returns the JWT string or None on failure.
    """
    # Step 1 — OAuth token
    data = urllib.parse.urlencode({
        "client_id":     app_id,
        "client_secret": app_secret,
        "grant_type":    "password",
        "username":      username,
        "password":      password,
    }).encode()
    try:
        req = urllib.request.Request(
            f"{INAT_OAUTH_BASE}/oauth/token",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            oauth = json.loads(resp.read())
        access_token = oauth.get("access_token")
        if not access_token:
            return None
    except Exception as e:
        log.error("iNat OAuth failed: %s", e)
        return None

    # Step 2 — JWT
    try:
        req2 = urllib.request.Request(
            f"{INAT_OAUTH_BASE}/users/api_token",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        with urllib.request.urlopen(req2, timeout=15) as resp2:
            jwt_data = json.loads(resp2.read())
        return jwt_data.get("api_token")
    except Exception as e:
        log.error("iNat JWT fetch failed: %s", e)
        return None


def _inat_score_image(
    pil_img: Image.Image,
    jwt: str,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    observed_on: Optional[str] = None,
) -> list[dict]:
    """
    Send a PIL image to iNaturalist computer vision API.
    Returns list of dicts: [{common_name, scientific_name, score, taxon_id}]
    """
    # Resize to 299×299 squashed (not letterboxed — iNat expects squash)
    thumb = pil_img.convert("RGB").resize(
        (INAT_IMAGE_SIZE, INAT_IMAGE_SIZE), Image.BILINEAR
    )
    buf = io.BytesIO()
    thumb.save(buf, "JPEG", quality=85)
    img_bytes = buf.getvalue()

    # Build multipart body manually
    boundary = "----PhotoReviewerBoundary"
    body_parts = []

    def add_field(name: str, value: str) -> None:
        part = f"--{boundary}\r\n"
        part += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
        part += f"{value}\r\n"
        body_parts.append(part)

    if lat is not None:
        add_field("lat", str(lat))
    if lng is not None:
        add_field("lng", str(lng))
    if observed_on:
        add_field("observed_on", observed_on)

    body = "".join(body_parts).encode()
    body = "".join(body_parts).encode()
    img_header = "--" + boundary + "\r\n"
    img_header += 'Content-Disposition: form-data; name="image"; filename="photo.jpg"\r\n'
    img_header += "Content-Type: image/jpeg\r\n\r\n"
    img_footer = "\r\n--" + boundary + "--\r\n"
    body += img_header.encode() + img_bytes + img_footer.encode()
    try:
        req = urllib.request.Request(
            INAT_SCORE_URL,
            data=body,
            headers={
                "Authorization": jwt,
                "Content-Type":  f"multipart/form-data; boundary={boundary}",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read())
    except Exception as e:
        log.error("iNat score_image failed: %s", e)
        return []

    suggestions = []
    for item in result.get("results", [])[:INAT_TOP_N]:
        taxon = item.get("taxon", {})
        suggestions.append({
            "common_name":     taxon.get("preferred_common_name",
                               taxon.get("name", "Unknown")),
            "scientific_name": taxon.get("name", ""),
            "score":           round(item.get("combined_score", 0) * 100, 1),
            "taxon_id":        taxon.get("id"),
            "iconic_taxon":    taxon.get("iconic_taxon_name", ""),
        })
    return suggestions


def _parse_gps_exif(exif: dict[str, str]) -> tuple[Optional[float], Optional[float]]:
    """Extract decimal lat/lng from EXIF dict if GPS data present."""
    try:
        def dms_to_dec(dms_str: str, ref: str) -> float:
            # DMS may come as "(51.0, 30.0, 45.6)" or similar
            parts = [float(x.strip("() ")) for x in dms_str.split(",")]
            dec = parts[0] + parts[1] / 60 + parts[2] / 3600
            if ref in ("S", "W"):
                dec = -dec
            return dec

        lat_str  = exif.get("GPSLatitude")
        lat_ref  = exif.get("GPSLatitudeRef", "N")
        lng_str  = exif.get("GPSLongitude")
        lng_ref  = exif.get("GPSLongitudeRef", "E")

        if lat_str and lng_str:
            return dms_to_dec(lat_str, lat_ref), dms_to_dec(lng_str, lng_ref)
    except Exception:
        pass
    return None, None


# ── Shared thumbnail grid helpers ────────────────────────────────────────────

def make_thumb_cell(parent, path: str, app, cell_w: int, cell_h: int,
                    on_click=None, show_scores: bool = True,
                    is_current: bool = False) -> "tk.Frame":
    """
    Build a thumbnail cell frame for use in gallery/scoring/burst views.
    Returns the frame with a _canvas attribute for later thumbnail loading.
    """
    import tkinter as tk
    scores  = app._scores.get(path)
    bs      = composite_score(scores)
    tags    = app.status.get(path, {})
    star    = tags.get("star", 0)
    tag_str = ("★" if tags.get("best") else "") + \
              ("✕" if tags.get("delete") else "") + \
              ("⚑" if tags.get("id") else "") + \
              (f' {"★"*star}' if star else "")

    border = (C_ACCENT if is_current else
              C_BEST   if tags.get("best")   else
              C_DELETE if tags.get("delete") else
              C_ID     if tags.get("id")     else "#1c1c1c")

    cell = tk.Frame(parent, bg=C_SURFACE,
                    highlightthickness=2, highlightbackground=border)

    canvas = tk.Canvas(cell, width=cell_w, height=cell_h,
                       bg=C_BG, highlightthickness=0)
    canvas.pack(padx=3, pady=3)
    canvas.create_text(cell_w // 2, cell_h // 2, text="…",
                       fill=C_MUTED, font=("Helvetica", 8))

    if show_scores:
        info = tk.Frame(cell, bg=C_SURFACE)
        info.pack(fill="x", padx=4, pady=(0, 2))
        fg = _score_color(int(bs)) if bs > 0 else C_MUTED
        tk.Label(info, text=f"▣ {bs:.0f}" if bs > 0 else "▣ —",
                 bg=C_SURFACE, fg=fg,
                 font=("Helvetica", 8, "bold")).pack(side="left")
        if scores:
            for key, short in [("sharpness","S"),("motion_blur","M"),("exposure","E")]:
                v = scores.get(key, 0)
                tk.Label(info, text=f"{short}:{v}",
                         bg=C_SURFACE, fg=_score_color(v),
                         font=("Helvetica", 7)).pack(side="left", padx=1)
        tk.Label(info, text=tag_str, bg=C_SURFACE, fg=C_TEXT,
                 font=("Helvetica", 8)).pack(side="right", padx=2)

    tk.Label(cell, text=Path(path).name, bg=C_SURFACE, fg=C_MUTED,
             font=("Helvetica", 7), wraplength=cell_w - 6).pack(
        padx=4, pady=(0, 4))

    if on_click:
        canvas.bind("<Button-1>", lambda e: on_click(path))

    cell._canvas   = canvas   # type: ignore[attr-defined]
    cell._path     = path     # type: ignore[attr-defined]
    return cell


def load_thumbs_incrementally(cells: list, tk_images: list,
                               app, after_fn, interval_ms: int = 4) -> None:
    """
    Load thumbnails into gallery cells. Uses preload cache where available.
    Processes multiple cached images per tick for speed.
    Uses cell canvas requested dimensions (set at cell creation time) so
    images render correctly even before the widget is mapped on screen.
    """
    queue = list(range(len(cells)))

    def _tick():
        if not queue:
            return
        processed = 0
        while queue and processed < 4:
            idx  = queue[0]
            cell = cells[idx]
            path = cell._path
            cached = app._preload_cache.get(path)
            if cached is None and processed >= 1:
                break   # max 1 file-open per tick
            queue.pop(0)
            try:
                img = cached or _extract_thumb_img(path)
                img = img.copy()
                c   = cell._canvas
                # Use configured dimensions — reliable even before mapping
                w   = c.cget("width")  or c.winfo_reqwidth()  or 200
                h   = c.cget("height") or c.winfo_reqheight() or 150
                img.thumbnail((int(w), int(h)), Image.BILINEAR)
                tk_img = ImageTk.PhotoImage(img)
                tk_images.append(tk_img)
                c.delete("all")
                c.create_image(int(w)//2, int(h)//2, image=tk_img, anchor="center")
                processed += 1
            except Exception:
                processed += 1
        if queue:
            after_fn(interval_ms, _tick)

    after_fn(10, _tick)


class SpeciesSuggestView(ctk.CTkToplevel):
    """
    Batch species suggestion using the iNaturalist computer vision API.

    Workflow:
      1. First-time setup: enter iNat app credentials (app_id, app_secret,
         username, password) — stored in photo_reviewer_inat.json.
         One-time setup; credentials re-used each session.
      2. Choose scope: All images or Best-tagged only.
      3. Progress bar runs through images, calls iNat API for each.
      4. Results shown as a scrollable list: thumbnail + top-3 suggestions.
      5. Click a suggestion to pre-fill the rename field for that image.
      6. Batch apply: accept top suggestion for all, or review one by one.
    """

    THUMB_W = 120
    THUMB_H = 90

    def __init__(self, master: "PhotoReviewer"):
        super().__init__(master)
        self._app      = master
        self._settings = _inat_load_settings(master.base_dir)
        self._jwt: Optional[str] = self._settings.get("jwt")
        self._results:  list[dict] = []   # [{path, suggestions, img}]
        self._tk_images: list[ImageTk.PhotoImage] = []
        self._worker_queue: queue.Queue = queue.Queue()
        self._running = False

        self.title("Species Suggestion — iNaturalist")
        self.geometry("900x720")
        self.configure(fg_color=C_BG)
        self.grab_set()

        self._build_ui()
        self.bind("<Escape>", lambda _: self.destroy())

        # If already have credentials, skip to scope selection
        if self._jwt and self._settings.get("app_id"):
            self._show_scope_panel()
        else:
            self._show_setup_panel()

    # ── UI panels ─────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Header
        hdr = ctk.CTkFrame(self, fg_color=C_SURFACE, corner_radius=0, height=48)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        ctk.CTkLabel(hdr, text="🔍  Species Suggestion",
                     font=ctk.CTkFont("Helvetica", 14, "bold"),
                     text_color=C_TEXT).pack(side="left", padx=16, pady=14)

        ctk.CTkButton(hdr, text="✕ Close", width=80, height=32,
                      fg_color=C_BORDER, hover_color=C_DELETE,
                      text_color=C_TEXT, command=self.destroy
                      ).pack(side="right", padx=12, pady=8)

        ctk.CTkButton(hdr, text="⚙ Credentials", width=100, height=32,
                      fg_color=C_BORDER, hover_color=C_ACCENT,
                      text_color=C_MUTED, command=self._show_setup_panel
                      ).pack(side="right", padx=4, pady=8)

        # Content area — swapped between setup/scope/results panels
        self._content = tk.Frame(self, bg=C_BG)
        self._content.pack(fill="both", expand=True)

    def _clear_content(self) -> None:
        for w in self._content.winfo_children():
            w.destroy()

    # ── Setup panel ───────────────────────────────────────────────────────────

    def _show_setup_panel(self) -> None:
        self._clear_content()
        frame = ctk.CTkFrame(self._content, fg_color=C_SURFACE,
                              corner_radius=12, width=500)
        frame.place(relx=0.5, rely=0.5, anchor="center")
        frame.pack_propagate(False)

        ctk.CTkLabel(frame, text="iNaturalist API Setup",
                     font=ctk.CTkFont("Helvetica", 14, "bold"),
                     text_color=C_TEXT).pack(pady=(24, 4))

        ctk.CTkLabel(frame,
                     text="Register a free app at inaturalist.org/oauth/applications\n"
                          "then enter your credentials below. Stored locally.",
                     font=ctk.CTkFont("Helvetica", 10),
                     text_color=C_MUTED, justify="center").pack(pady=(0, 16))
        fields: dict[str, ctk.CTkEntry] = {}
        for label, key, show in [
            ("App ID",     "app_id",     ""),
            ("App Secret", "app_secret", "*"),
            ("Username",   "username",   ""),
            ("Password",   "password",  "*"),
        ]:
            row = ctk.CTkFrame(frame, fg_color="transparent")
            row.pack(fill="x", padx=32, pady=4)
            ctk.CTkLabel(row, text=label, width=90, anchor="w",
                         font=ctk.CTkFont("Helvetica", 10),
                         text_color=C_MUTED).pack(side="left")
            e = ctk.CTkEntry(row, show=show, fg_color=C_BG,
                              text_color=C_TEXT, border_width=1,
                              border_color=C_BORDER)
            e.insert(0, self._settings.get(key, ""))
            e.pack(side="left", fill="x", expand=True)
            fields[key] = e

        status_lbl = ctk.CTkLabel(frame, text="",
                                   font=ctk.CTkFont("Helvetica", 10),
                                   text_color=C_DELETE)
        status_lbl.pack(pady=4)

        def connect():
            creds = {k: v.get().strip() for k, v in fields.items()}
            if not all(creds.values()):
                status_lbl.configure(text="All fields required.")
                return
            status_lbl.configure(text="Connecting…", text_color=C_WARN)
            self.update_idletasks()
            jwt = _inat_get_jwt(creds["app_id"], creds["app_secret"],
                                creds["username"], creds["password"])
            if jwt:
                self._jwt = jwt
                self._settings.update(creds)
                self._settings["jwt"] = jwt
                _inat_save_settings(self._app.base_dir, self._settings)
                status_lbl.configure(text="✓ Connected!", text_color=C_BEST)
                self.after(800, self._show_scope_panel)
            else:
                status_lbl.configure(text="✗ Failed — check credentials.",
                                     text_color=C_DELETE)

        ctk.CTkButton(frame, text="Connect to iNaturalist",
                      fg_color=C_ACCENT, hover_color=_darken(C_ACCENT),
                      font=ctk.CTkFont("Helvetica", 12, "bold"),
                      height=40, command=connect).pack(
            fill="x", padx=32, pady=(8, 24))

    # ── Scope panel ───────────────────────────────────────────────────────────

    def _show_scope_panel(self) -> None:
        self._clear_content()
        frame = ctk.CTkFrame(self._content, fg_color=C_SURFACE,
                              corner_radius=12, width=480)
        frame.place(relx=0.5, rely=0.5, anchor="center")
        frame.pack_propagate(False)

        n_all  = self._app.total
        n_best = sum(1 for v in self._app.status.values() if v.get("best"))

        ctk.CTkLabel(frame, text="Run Species Identification",
                     font=ctk.CTkFont("Helvetica", 14, "bold"),
                     text_color=C_TEXT).pack(pady=(24, 8))

        ctk.CTkLabel(frame,
                     text="Images are sent to the iNaturalist API for identification.\n"
                          "GPS coordinates from EXIF are included where available\n"
                          "to improve accuracy with location priors.",
                     font=ctk.CTkFont("Helvetica", 10),
                     text_color=C_MUTED, justify="center").pack(pady=(0, 20))

        scope_var = tk.StringVar(value="best" if n_best > 0 else "all")

        for label, value, count in [
            (f"All images  ({n_all})",          "all",  n_all),
            (f"★ Best-tagged only  ({n_best})", "best", n_best),
        ]:
            rb = ctk.CTkRadioButton(frame, text=label, variable=scope_var,
                                     value=value,
                                     font=ctk.CTkFont("Helvetica", 11),
                                     text_color=C_TEXT if count > 0 else C_MUTED,
                                     state="normal" if count > 0 else "disabled")
            rb.pack(anchor="w", padx=48, pady=4)

        est_lbl = ctk.CTkLabel(frame, text="",
                                font=ctk.CTkFont("Helvetica", 9),
                                text_color=C_MUTED)
        est_lbl.pack(pady=4)

        def update_estimate(*_):
            n = n_all if scope_var.get() == "all" else n_best
            mins = int(n * INAT_RATE_LIMIT / 60)
            secs = int(n * INAT_RATE_LIMIT % 60)
            est_lbl.configure(
                text=f"Estimated time: ~{mins}m {secs}s  ({n} images at 1/sec)")

        scope_var.trace_add("write", update_estimate)
        update_estimate()

        ctk.CTkButton(frame, text="▶  Start Identification",
                      fg_color=C_BEST, hover_color=_darken(C_BEST),
                      font=ctk.CTkFont("Helvetica", 12, "bold"),
                      height=40,
                      command=lambda: self._start_batch(scope_var.get())
                      ).pack(fill="x", padx=32, pady=(16, 24))

    # ── Batch processing ──────────────────────────────────────────────────────

    def _start_batch(self, scope: str) -> None:
        files = (
            self._app.files if scope == "all"
            else [p for p, t in self._app.status.items() if t.get("best")]
        )
        if not files:
            mb.showinfo("Nothing to process", "No images in selected scope.")
            return

        self._clear_content()
        self._results.clear()
        self._tk_images.clear()

        # Progress UI
        prog_frame = ctk.CTkFrame(self._content, fg_color=C_SURFACE,
                                   corner_radius=12, width=520)
        prog_frame.place(relx=0.5, rely=0.3, anchor="center")
        prog_frame.pack_propagate(False)

        ctk.CTkLabel(prog_frame, text="Identifying species…",
                     font=ctk.CTkFont("Helvetica", 13, "bold"),
                     text_color=C_TEXT).pack(pady=(20, 8))

        self._prog_lbl = ctk.CTkLabel(prog_frame, text="",
                                       font=ctk.CTkFont("Helvetica", 10),
                                       text_color=C_MUTED)
        self._prog_lbl.pack()

        self._prog_bar = ctk.CTkProgressBar(prog_frame, mode="determinate",
                                             height=8, corner_radius=4,
                                             fg_color=C_BORDER,
                                             progress_color=C_BEST,
                                             width=400)
        self._prog_bar.set(0)
        self._prog_bar.pack(pady=(8, 4))

        self._prog_sub = ctk.CTkLabel(prog_frame, text="",
                                       font=ctk.CTkFont("Helvetica", 9),
                                       text_color=C_MUTED)
        self._prog_sub.pack(pady=(0, 20))

        ctk.CTkButton(prog_frame, text="Cancel", width=80, height=28,
                      fg_color=C_BORDER, hover_color=C_DELETE,
                      text_color=C_TEXT,
                      command=self._cancel_batch).pack(pady=(0, 16))

        self._running = True
        self._batch_files = files
        self._batch_index = 0
        self._batch_total = len(files)
        self.after(100, self._process_next)

    def _process_next(self) -> None:
        if not self._running:
            return
        i = self._batch_index
        if i >= self._batch_total:
            self._show_results()
            return

        path = self._batch_files[i]
        self._prog_lbl.configure(
            text=f"{i + 1} / {self._batch_total}  —  {Path(path).name}")
        self._prog_bar.set((i + 1) / self._batch_total)

        # Extract GPS from EXIF
        exif = self._app._exif_cache.get(path) or read_exif(path)
        lat, lng = _parse_gps_exif(exif)
        obs_date = exif.get("DateTimeOriginal", "")[:10].replace(":", "-") or None

        # Get image (from preload cache or decode fresh)
        try:
            img = self._app._preload_cache.get(path) or _extract_thumb_img(path)
        except Exception:
            img = None

        suggestions = []
        if img and self._jwt:
            self._prog_sub.configure(
                text=f"Sending to iNaturalist…"
                     + (f"  GPS: {lat:.3f}, {lng:.3f}" if lat else "  (no GPS)"))
            self.update_idletasks()
            suggestions = _inat_score_image(img, self._jwt, lat, lng, obs_date)

        self._results.append({
            "path":        path,
            "img":         img,
            "suggestions": suggestions,
            "lat":         lat,
            "lng":         lng,
        })

        self._batch_index += 1
        # Rate limit — schedule next call after INAT_RATE_LIMIT seconds
        self.after(int(INAT_RATE_LIMIT * 1000), self._process_next)

    def _cancel_batch(self) -> None:
        self._running = False
        self._show_results()

    # ── Results panel ─────────────────────────────────────────────────────────

    def _show_results(self) -> None:
        self._running = False
        self._clear_content()

        if not self._results:
            ctk.CTkLabel(self._content, text="No results.",
                         font=ctk.CTkFont("Helvetica", 12),
                         text_color=C_MUTED).pack(pady=40)
            return

        # Toolbar
        toolbar = ctk.CTkFrame(self._content, fg_color=C_BORDER,
                                corner_radius=0, height=36)
        toolbar.pack(fill="x")
        toolbar.pack_propagate(False)

        ctk.CTkLabel(toolbar,
                     text=f"{len(self._results)} images identified",
                     font=ctk.CTkFont("Helvetica", 10),
                     text_color=C_MUTED).pack(side="left", padx=12, pady=8)

        ctk.CTkButton(toolbar, text="✓ Apply top suggestion to all",
                      width=200, height=26,
                      fg_color=C_BEST, hover_color=_darken(C_BEST),
                      text_color="white", font=ctk.CTkFont("Helvetica", 9, "bold"),
                      command=self._apply_all_top).pack(side="right", padx=8, pady=5)

        # Scrollable results list
        scroll_outer = tk.Frame(self._content, bg=C_BG)
        scroll_outer.pack(fill="both", expand=True)
        scroll_outer.columnconfigure(0, weight=1)
        scroll_outer.rowconfigure(0, weight=1)

        grid_canvas = tk.Canvas(scroll_outer, bg=C_BG, highlightthickness=0)
        vsb = tk.Scrollbar(scroll_outer, orient="vertical",
                           command=grid_canvas.yview)
        grid_canvas.configure(yscrollcommand=vsb.set)
        vsb.grid(row=0, column=1, sticky="ns")
        grid_canvas.grid(row=0, column=0, sticky="nsew")

        inner = tk.Frame(grid_canvas, bg=C_BG)
        win_id = grid_canvas.create_window((0, 0), window=inner, anchor="nw")

        inner.bind("<Configure>",
                   lambda e: grid_canvas.configure(
                       scrollregion=grid_canvas.bbox("all")))
        grid_canvas.bind("<Configure>",
                         lambda e: grid_canvas.itemconfig(win_id, width=e.width))
        grid_canvas.bind_all("<MouseWheel>",
                             lambda e: grid_canvas.yview_scroll(
                                 int(-1 * e.delta / 120), "units"))

        for result in self._results:
            self._make_result_row(inner, result)

    def _make_result_row(self, parent: tk.Frame, result: dict) -> None:
        path        = result["path"]
        img         = result["img"]
        suggestions = result["suggestions"]

        row = tk.Frame(parent, bg=C_SURFACE,
                       highlightthickness=1,
                       highlightbackground=C_BORDER)
        row.pack(fill="x", padx=8, pady=3)

        # Thumbnail
        thumb_frame = tk.Frame(row, bg=C_BG,
                                width=self.THUMB_W, height=self.THUMB_H)
        thumb_frame.pack(side="left", padx=8, pady=8)
        thumb_frame.pack_propagate(False)

        canvas = tk.Canvas(thumb_frame, width=self.THUMB_W, height=self.THUMB_H,
                           bg=C_BG, highlightthickness=0)
        canvas.pack()

        if img:
            try:
                t = img.copy()
                t.thumbnail((self.THUMB_W, self.THUMB_H), Image.BILINEAR)
                tk_img = ImageTk.PhotoImage(t)
                self._tk_images.append(tk_img)
                canvas.create_image(self.THUMB_W // 2, self.THUMB_H // 2,
                                    image=tk_img, anchor="center")
            except Exception:
                pass

        # File info
        info = tk.Frame(row, bg=C_SURFACE)
        info.pack(side="left", fill="both", expand=True, padx=4, pady=8)

        tk.Label(info, text=Path(path).name,
                 bg=C_SURFACE, fg=C_TEXT,
                 font=("Helvetica", 9, "bold"),
                 anchor="w").pack(fill="x")

        gps_txt = (f"GPS: {result['lat']:.4f}, {result['lng']:.4f}"
                   if result.get("lat") else "No GPS")
        tk.Label(info, text=gps_txt,
                 bg=C_SURFACE, fg=C_MUTED,
                 font=("Helvetica", 7), anchor="w").pack(fill="x")

        if not suggestions:
            tk.Label(info, text="No suggestions returned",
                     bg=C_SURFACE, fg=C_DELETE,
                     font=("Helvetica", 9)).pack(anchor="w", pady=4)
            return

        # Suggestion buttons
        sug_frame = tk.Frame(info, bg=C_SURFACE)
        sug_frame.pack(fill="x", pady=(6, 0))

        for sug in suggestions:
            score    = sug["score"]
            common   = sug["common_name"]
            sci      = sug["scientific_name"]
            iconic   = sug.get("iconic_taxon", "")
            icon_map = {
                "Aves": "🐦", "Mammalia": "🦌", "Reptilia": "🦎",
                "Amphibia": "🐸", "Insecta": "🦋", "Plantae": "🌿",
                "Fungi": "🍄", "Actinopterygii": "🐟",
            }
            icon = icon_map.get(iconic, "🔬")
            color = C_BEST if score >= 70 else (C_WARN if score >= 40 else C_MUTED)
            label = f"{icon}  {common}  ({sci})  —  {score:.0f}%"

            def apply_name(p=path, name=common):
                # Jump to image and pre-fill rename field
                if p in self._app.files:
                    self._app._jump_to_index(self._app.files.index(p))
                stem = name.replace(" ", "_")
                self._app._rename_var.set(stem)
                self._app._rename_entry.focus_set()
                self._app._rename_entry.selection_range(0, "end")

            tk.Button(sug_frame, text=label,
                      bg=C_BG, fg=color,
                      font=("Helvetica", 9), relief="flat", bd=0,
                      anchor="w", padx=4,
                      command=apply_name).pack(fill="x", pady=1)

    def _apply_all_top(self) -> None:
        """Apply the top suggestion name to all results that have one."""
        count = 0
        for result in self._results:
            if not result["suggestions"]:
                continue
            path = result["path"]
            top  = result["suggestions"][0]
            stem = top["common_name"].replace(" ", "_")
            # Use the app's rename logic
            if path in self._app.files:
                self._app._jump_to_index(self._app.files.index(path))
                self._app._rename_var.set(stem)
                self._app._apply_rename()
                count += 1
        mb.showinfo("Applied", f"Applied top suggestions to {count} image(s).")


# ── General Gallery View ──────────────────────────────────────────────────────

class ComparisonView(ctk.CTkToplevel):
    """Top-scored images from a burst shown side by side."""

    def __init__(self, master: "PhotoReviewer", group: list[str]):
        super().__init__(master)
        self._app = master

        scores  = {p: master._scores.get(p) for p in group}
        ranked  = sorted(group, key=lambda p: burst_score(scores.get(p)), reverse=True)
        self._picks = ranked[:COMPARISON_MAX]

        n = len(self._picks)
        self.title(f"Comparison — top {n} from burst")
        self.geometry("1200x720")
        self.configure(fg_color=C_BG)
        self.grab_set()

        self._tk_images: list[ImageTk.PhotoImage] = []
        self._panels: list[dict] = []
        self._build_ui()
        self.bind("<Escape>", lambda _: self.destroy())

        # Load images incrementally via after()
        self._panel_queue = list(self._panels)
        self.after(100, self._load_next_panel)

    def _build_ui(self) -> None:
        hdr = ctk.CTkFrame(self, fg_color=C_SURFACE, corner_radius=0, height=44)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        ctk.CTkLabel(hdr, text="Comparison — click a panel to jump to that image",
                     font=ctk.CTkFont("Helvetica", 11),
                     text_color=C_MUTED).pack(side="left", padx=16, pady=12)
        ctk.CTkButton(hdr, text="✕ Close", width=80, height=28,
                      fg_color=C_BORDER, hover_color=C_DELETE,
                      text_color=C_TEXT, command=self.destroy).pack(side="right", padx=12, pady=8)

        grid = tk.Frame(self, bg=C_BG)
        grid.pack(fill="both", expand=True, padx=8, pady=8)

        cols = min(len(self._picks), 2)
        for idx, path in enumerate(self._picks):
            col = idx % cols
            row = idx // cols
            panel = self._make_panel(grid, path)
            panel["frame"].grid(row=row, column=col, padx=6, pady=6, sticky="nsew")
            grid.rowconfigure(row, weight=1)
            grid.columnconfigure(col, weight=1)
            self._panels.append(panel)

    def _make_panel(self, parent: tk.Frame, path: str) -> dict:
        frame = tk.Frame(parent, bg=C_SURFACE, bd=0,
                         highlightthickness=1, highlightbackground=C_BORDER)

        canvas = tk.Canvas(frame, bg=C_BG, highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        canvas.create_text(200, 150, text="Loading…",
                           fill=C_MUTED, font=("Helvetica", 10))

        info = tk.Frame(frame, bg=C_SURFACE, height=56)
        info.pack(fill="x")
        info.pack_propagate(False)

        tk.Label(info, text=Path(path).name, bg=C_SURFACE,
                 fg=C_MUTED, font=("Helvetica", 8)).pack(side="left", padx=8)

        sc = self._app._scores.get(path)
        bs = burst_score(sc)
        score_lbl = tk.Label(info,
                             text=f"Score {bs:.0f}" if bs > 0 else "Scoring…",
                             bg=C_SURFACE,
                             fg=_score_color(int(bs)) if bs > 0 else C_MUTED,
                             font=("Helvetica", 9, "bold"))
        score_lbl.pack(side="left", padx=4)

        btn_frame = tk.Frame(info, bg=C_SURFACE)
        btn_frame.pack(side="right", padx=6)
        for sym, tag, color in [("★", "best", C_BEST), ("✕", "delete", C_DELETE), ("⚑", "id", C_ID)]:
            tk.Button(btn_frame, text=sym, bg=color, fg="white",
                      font=("Helvetica", 9, "bold"), bd=0, padx=6,
                      command=lambda p=path, t=tag: self._toggle_tag(p, t)
                      ).pack(side="left", padx=2, pady=4)

        canvas.bind("<Button-1>", lambda e, p=path: self._jump_to(p))

        return {"frame": frame, "canvas": canvas, "path": path, "score_lbl": score_lbl}

    def _load_next_panel(self) -> None:
        """Load one comparison panel image per tick."""
        if not self._panel_queue:
            return
        panel = self._panel_queue.pop(0)
        path  = panel["path"]
        try:
            img    = extract_preview(path)
            c      = panel["canvas"]
            # Use actual canvas size if available, else fallback
            self.update_idletasks()
            cw = c.winfo_width()  or 580
            ch = c.winfo_height() or 400
            iw, ih = img.size
            scale  = min(cw / iw, ch / ih)
            resized = img.resize((max(1, int(iw * scale)), max(1, int(ih * scale))),
                                  Image.LANCZOS)
            tk_img = ImageTk.PhotoImage(resized)
            self._tk_images.append(tk_img)
            c.delete("all")
            c.create_image(cw // 2, ch // 2, image=tk_img, anchor="center")

            sc = self._app._scores.get(path)
            bs = burst_score(sc)
            if bs > 0:
                panel["score_lbl"].configure(
                    text=f"Score {bs:.0f}", fg=_score_color(int(bs)))
        except Exception as e:
            log.warning("Comparison load failed %s: %s", path, e)

        if self._panel_queue:
            self.after(10, self._load_next_panel)

    def _toggle_tag(self, path: str, tag: str) -> None:
        self._app.status[path][tag] = not self._app.status[path][tag]
        save_status(self._app.status_file, self._app.status)
        self._app._update_counts()

    def _jump_to(self, path: str) -> None:
        if path in self._app.files:
            self._app.index = self._app.files.index(path)
            self._app._show_current()
        self.destroy()


# ── Scoring review window ─────────────────────────────────────────────────────

# ── ThumbnailGrid base class ──────────────────────────────────────────────────

class ThumbnailGrid(tk.Frame):
    """
    Reusable scrollable thumbnail grid for gallery, scoring, and burst views.

    Subclasses must implement:
        _make_toolbar(parent) -> None   — populate the toolbar row
        _make_cell(parent, path) -> tk.Frame  — build one thumbnail cell

    The base class provides:
        - Scrollable canvas + scrollbar
        - Dynamic column/width calculation from canvas Configure
        - Parallel prefetch of all images into preload cache
        - Incremental thumbnail loading (cached = fast, uncached = throttled)
        - _rebuild_grid() to refresh after filter/data changes
    """
    MIN_CELL = 160
    MAX_CELL = 300
    CELL_PAD = 6

    def __init__(self, parent: tk.Frame, app: "PhotoReviewer",
                 toolbar_title: str = ""):
        super().__init__(parent, bg=C_BG)
        self._app        = app
        self._tk_images: list[ImageTk.PhotoImage] = []
        self._cells:     list[tk.Frame]            = []
        self._paths:     list[str]                 = []   # paths to show
        self.COLS        = 4
        self.THUMB_W     = 200
        self.THUMB_H     = 150
        self._first_layout = True

        # ── Toolbar ───────────────────────────────────────────────────────────
        self._toolbar = tk.Frame(self, bg=C_SURFACE, height=44)
        self._toolbar.pack(fill="x")
        self._toolbar.pack_propagate(False)

        tk.Button(self._toolbar, text="← Back",
                  bg=C_BORDER, fg=C_TEXT,
                  font=("Helvetica", 10, "bold"),
                  relief="flat", bd=0, padx=12,
                  command=self._app._hide_overlay
                  ).pack(side="left", padx=8, pady=8)

        self._title_lbl = tk.Label(self._toolbar, text=toolbar_title,
                                    bg=C_SURFACE, fg=C_TEXT,
                                    font=("Helvetica", 12, "bold"))
        self._title_lbl.pack(side="left", padx=8)

        self._make_toolbar(self._toolbar)

        # ── Scrollable grid ───────────────────────────────────────────────────
        self._canvas = tk.Canvas(self, bg=C_BG, highlightthickness=0)
        vsb = tk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)

        self._inner = tk.Frame(self._canvas, bg=C_BG)
        self._win   = self._canvas.create_window((0, 0), window=self._inner,
                                                   anchor="nw")

        self._inner.bind("<Configure>",
            lambda e: self._canvas.configure(
                scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>", self._on_configure)
        self._canvas.bind("<MouseWheel>",
            lambda e: self._canvas.yview_scroll(int(-1*e.delta/120), "units"))
        # Force first layout now that canvas is properly set up
        self.after(50, lambda: self._on_configure(
            type("E", (), {"width": self._canvas.winfo_width() or 800})()))

    def _on_configure(self, event) -> None:
        """Recompute column count and cell dimensions from actual canvas width."""
        self._canvas.itemconfig(self._win, width=event.width)
        w = event.width
        if w < 10:
            return
        cw   = max(self.MIN_CELL, min(self.MAX_CELL, w // 4))
        cols = max(1, w // (cw + self.CELL_PAD * 2))
        cw   = max(self.MIN_CELL, (w - self.CELL_PAD * 2 * cols) // cols)
        changed = (cols != self.COLS or abs(cw - self.THUMB_W) > 4)
        self.COLS    = cols
        self.THUMB_W = cw
        self.THUMB_H = int(cw * 0.70)
        was_first = self._first_layout
        self._first_layout = False
        # Only rebuild if we have paths to show
        if self._paths and (was_first or changed):
            self._rebuild_grid()

    def _prefetch(self, paths: list[str]) -> None:
        """Submit all uncached images to the preload thread pool."""
        executor = getattr(self._app, "_preload_executor", None)
        if executor:
            for p in paths:
                if p not in self._app._preload_cache:
                    executor.submit(self._app._preload_worker, p)

    def _rebuild_grid(self) -> None:
        """Rebuild grid from self._paths. Call after filtering or scoring."""
        for w in self._inner.winfo_children():
            w.destroy()
        self._cells.clear()
        self._tk_images.clear()
        # Detect sequence breaks (>5 min gap) for gallery separators
        seq_breaks: set[int] = set()
        prev_dt = None
        for i, path in enumerate(self._paths):
            exif = self._app._exif_cache.get(path, {})
            dt_str = exif.get("DateTimeOriginal", "")
            if dt_str:
                try:
                    import datetime
                    dt = datetime.datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S")
                    if prev_dt and (dt - prev_dt).total_seconds() > 300:
                        seq_breaks.add(i)
                    prev_dt = dt
                except Exception:
                    pass

        grid_row = 0
        col      = 0
        for slot, path in enumerate(self._paths):
            if slot in seq_breaks:
                # Insert full-width sequence separator
                exif   = self._app._exif_cache.get(path, {})
                dt_str = exif.get("DateTimeOriginal", "")[:16].replace(":", "-", 2).replace(":", " ", 1) if exif else ""
                sep = tk.Frame(self._inner, bg="#1a2a1a", height=28)
                sep.grid(row=grid_row, column=0, columnspan=self.COLS,
                         sticky="ew", pady=(8, 2), padx=self.CELL_PAD)
                tk.Label(sep, text=f"  ▸ New sequence  {dt_str}",
                         bg="#1a2a1a", fg="#66aa66",
                         font=("Helvetica", 8, "italic")).pack(side="left", padx=4, pady=4)
                grid_row += 1
                col = 0

            cell = self._make_cell(self._inner, path)
            cell.grid(row=grid_row, column=col,
                      padx=self.CELL_PAD, pady=self.CELL_PAD, sticky="n")
            self._cells.append(cell)
            col += 1
            if col >= self.COLS:
                col = 0
                grid_row += 1

        load_thumbs_incrementally(self._cells, self._tk_images,
                                   self._app, self.after)

    def _scroll_to_path(self, path: str) -> None:
        """Scroll so the given path is visible."""
        if path not in self._paths:
            return
        slot = self._paths.index(path)
        total_rows = max(1, (len(self._paths) + self.COLS - 1) // self.COLS)
        frac = (slot // self.COLS) / total_rows
        self.after(150, lambda: self._canvas.yview_moveto(max(0, frac - 0.1)))

    def _jump_and_close(self, path: str) -> None:
        """Jump to image in main viewer and close overlay."""
        self._app._hide_overlay()
        if path in self._app.files:
            self._app._jump_to_index(self._app.files.index(path))

    # ── Subclass interface ────────────────────────────────────────────────────

    def _make_toolbar(self, toolbar: tk.Frame) -> None:
        """Override to add toolbar widgets (right-aligned)."""
        pass

    def _make_cell(self, parent: tk.Frame, path: str) -> tk.Frame:
        """Override to build a thumbnail cell. Must return the cell frame."""
        return make_thumb_cell(parent, path, self._app,
                               self.THUMB_W, self.THUMB_H,
                               on_click=self._jump_and_close)



class InlineGalleryView(ThumbnailGrid):
    """Gallery view — all images, filterable by tag and filename."""

    def __init__(self, parent: tk.Frame, app: "PhotoReviewer"):
        self._filter_var = tk.StringVar()
        self._tag_filter = tk.StringVar(value="all")
        super().__init__(parent, app, f"Gallery  —  {app.total} images")
        self._prefetch(app.files)
        self._apply_filter()

    def _make_toolbar(self, bar: tk.Frame) -> None:
        # Tag filter pills (right side)
        for label, value, color in [
            ("Untagged","none",C_MUTED), ("⚑ ID","id",C_ID),
            ("✕ Delete","delete",C_DELETE), ("★ Best","best",C_BEST),
            ("All","all",C_BORDER),
        ]:
            tk.Button(bar, text=label, bg=color, fg="white",
                      font=("Helvetica", 9), relief="flat", bd=0,
                      padx=8, pady=2,
                      command=lambda v=value: self._set_tag(v)
                      ).pack(side="right", padx=2, pady=10)

        # Search bar below toolbar — pack after toolbar
        search = tk.Frame(self, bg=C_BORDER, height=30)
        search.pack(fill="x")
        search.pack_propagate(False)
        tk.Label(search, text="Filter:", bg=C_BORDER, fg=C_MUTED,
                 font=("Helvetica", 9)).pack(side="left", padx=8, pady=5)
        ctk.CTkEntry(search, textvariable=self._filter_var,
                     placeholder_text="filename…",
                     font=ctk.CTkFont("Helvetica", 10),
                     border_width=0, fg_color=C_BG,
                     text_color=C_TEXT, width=200).pack(side="left", pady=4)
        self._filter_var.trace_add("write", lambda *_: self._apply_filter())
        self._count_lbl = tk.Label(search, text="", bg=C_BORDER, fg=C_MUTED,
                                    font=("Helvetica", 9))
        self._count_lbl.pack(side="left", padx=8)

    def _set_tag(self, v: str) -> None:
        self._tag_filter.set(v)
        self._apply_filter()

    def _apply_filter(self) -> None:
        query    = self._filter_var.get().strip().lower()
        tag_mode = self._tag_filter.get()
        paths = []
        for path in self._app.files:
            if query and query not in Path(path).name.lower():
                continue
            tags = self._app.status.get(path, {})
            if tag_mode == "best"   and not tags.get("best"):   continue
            if tag_mode == "delete" and not tags.get("delete"): continue
            if tag_mode == "id"     and not tags.get("id"):     continue
            if tag_mode == "none"   and any(tags.values()):     continue
            paths.append(path)
        self._paths = paths
        if hasattr(self, "_count_lbl"):
            self._count_lbl.configure(text=f"{len(paths)} images")
        # Always rebuild — _on_configure will also rebuild if dimensions change
        # but we need this to run when filter changes after first layout
        if not self._first_layout:
            self._rebuild_grid()
            self._scroll_to_path(self._app.files[self._app.index])
        else:
            # First layout: rebuild now so grid is populated before Configure fires
            self._rebuild_grid()

    def _make_cell(self, parent: tk.Frame, path: str) -> tk.Frame:
        cell = make_thumb_cell(parent, path, self._app,
                               self.THUMB_W, self.THUMB_H,
                               on_click=self._jump_and_close,
                               is_current=(path == self._app.files[self._app.index]))
        # Inline tag buttons — tag without opening the image
        btn_row = tk.Frame(cell, bg=C_SURFACE)
        btn_row.pack(fill="x", padx=4, pady=(0, 4))
        tags = self._app.status.get(path, {})
        for sym, tag, col in [
            ("★", "best",   C_BEST),
            ("✕", "delete", C_DELETE),
            ("⚑", "id",     C_ID),
        ]:
            active = tags.get(tag, False)
            tk.Button(
                btn_row, text=sym,
                bg=col if active else C_BORDER,
                fg="white",
                font=("Helvetica", 9, "bold"), relief="flat", bd=0,
                command=lambda p=path, t=tag: self._tag(p, t)
            ).pack(side="left", fill="x", expand=True, padx=1, pady=2)
        return cell

    def _tag(self, path: str, tag: str) -> None:
        """Toggle a tag on a path and refresh the cell."""
        self._app.status[path][tag] = not self._app.status[path][tag]
        save_status(self._app.status_file, self._app.status)
        self._app._update_counts()
        if path in self._app.files:
            self._app._update_filmstrip_slot(self._app.files.index(path))
        # Partial refresh — just rebuild the grid to update button colours
        self._rebuild_grid()


class InlineScoringView(ThumbnailGrid):
    """Scoring view — progress bar then filterable scored grid with tag buttons."""

    def __init__(self, parent: tk.Frame, app: "PhotoReviewer", detail_mode: bool):
        self._detail_mode  = detail_mode
        self._use_sharp    = tk.BooleanVar(value=True)
        self._use_motion   = tk.BooleanVar(value=True)
        self._use_exposure = tk.BooleanVar(value=True)
        self._threshold    = tk.IntVar(value=40)
        self._scored       = False
        title = "Detail Photo Clean" if detail_mode else "Clean Photo Load"
        super().__init__(parent, app, title)
        # Progress bar — above the grid
        self._prog_lbl = tk.Label(self, text="Scoring images…",
                                   bg=C_BG, fg=C_MUTED, font=("Helvetica", 9))
        self._prog_lbl.pack(fill="x", padx=12, pady=2)
        self._prog_bar = ctk.CTkProgressBar(self, mode="determinate", height=6,
                                             fg_color=C_BORDER, progress_color=C_BEST)
        self._prog_bar.set(0)
        self._prog_bar.pack(fill="x", padx=12, pady=(0, 4))
        self._prog_lbl.lift(); self._prog_bar.lift()
        self._prefetch(app.files)
        self.after(100, self._run_scoring)

    def _make_toolbar(self, bar: tk.Frame) -> None:
        tk.Label(bar, text="Threshold:", bg=C_SURFACE, fg=C_MUTED,
                 font=("Helvetica", 9)).pack(side="right", padx=(8,2))
        tk.Entry(bar, textvariable=self._threshold, width=4,
                 bg=C_BG, fg=C_TEXT, relief="flat",
                 font=("Helvetica", 9)).pack(side="right", pady=8)
        for label, var, col in [
            ("Sharpness", self._use_sharp,    C_BEST),
            ("Motion",    self._use_motion,   C_ID),
            ("Exposure",  self._use_exposure, C_WARN),
        ]:
            tk.Checkbutton(bar, text=label, variable=var, bg=C_SURFACE, fg=col,
                           activebackground=C_SURFACE, selectcolor=C_BG,
                           font=("Helvetica", 9),
                           command=lambda: self._rebuild_grid() if self._scored else None
                           ).pack(side="right", padx=6, pady=8)

    def _run_scoring(self) -> None:
        files = self._app.files
        total = len(files)
        # Detail mode always re-scores — full RAW gives different results than thumbnail
        force_rescore = self._detail_mode
        for i, path in enumerate(files):
            if path not in self._app._scores or force_rescore:
                try:
                    img = _extract_full_img(path) if self._detail_mode else _extract_thumb_img(path)
                    self._app._scores[path] = score_image(img)
                    self._app._histograms[path] = build_histogram(img)
                except Exception:
                    pass
            if i % 3 == 0 or i == total - 1:
                self._prog_bar.set((i + 1) / total)
                self._prog_lbl.configure(text=f"Scoring… {i+1} / {total}")
                self.update_idletasks()
        save_score_cache(self._app.score_file, self._app._scores)
        self._prog_lbl.configure(text=f"✓ Complete — {total} images scored")
        self._scored = True
        self._paths  = list(self._app.files)
        self._first_layout = False   # ensure Configure won't skip rebuild
        self._rebuild_grid()

    def _make_cell(self, parent: tk.Frame, path: str) -> tk.Frame:
        try:
            thresh = int(self._threshold.get())
        except Exception:
            thresh = 40
        sc     = self._app._scores.get(path)
        bs     = composite_score(sc, self._use_sharp.get(),
                                  self._use_motion.get(), self._use_exposure.get())
        border = C_DELETE if bs < thresh else C_BEST
        cell   = make_thumb_cell(parent, path, self._app,
                                  self.THUMB_W, self.THUMB_H,
                                  on_click=self._jump_and_close)
        cell.configure(highlightbackground=border)
        btn_row = tk.Frame(cell, bg=C_SURFACE)
        btn_row.pack(fill="x", padx=4, pady=(0, 4))
        tk.Button(btn_row, text="★ Best", bg=C_BEST, fg="white",
                  font=("Helvetica", 8, "bold"), relief="flat", bd=0,
                  command=lambda p=path: self._tag(p, "best")
                  ).pack(side="left", fill="x", expand=True, padx=(0, 1))
        tk.Button(btn_row, text="✕ Delete", bg=C_DELETE, fg="white",
                  font=("Helvetica", 8, "bold"), relief="flat", bd=0,
                  command=lambda p=path: self._tag(p, "delete")
                  ).pack(side="left", fill="x", expand=True, padx=(1, 0))
        return cell

    def _tag(self, path: str, tag: str) -> None:
        self._app.status[path][tag] = not self._app.status[path][tag]
        save_status(self._app.status_file, self._app.status)
        self._app._update_counts()
        if path in self._app.files:
            self._app._update_filmstrip_slot(self._app.files.index(path))
        self._rebuild_grid()


class InlineBurstTriageView(ThumbnailGrid):
    """Burst triage — step through burst groups, select keepers."""

    def __init__(self, parent: tk.Frame, app: "PhotoReviewer"):
        self._bursts     = [g for g in app._bursts if len(g) >= 2]
        self._burst_idx  = 0
        self._selected: set[str] = set()
        self._listbox    = None
        super().__init__(parent, app, f"Review Bursts  —  {len(self._bursts)} groups")
        if not self._bursts:
            tk.Label(self, text="No burst groups found. Run Analyse Bursts first.",
                     bg=C_BG, fg=C_MUTED, font=("Helvetica", 11)).pack(pady=40)
            return
        self._build_list_panel()
        self._load_burst(0)

    def _make_toolbar(self, bar: tk.Frame) -> None:
        tk.Button(bar, text="✓ Mark Best & Next", bg=C_BEST, fg="white",
                  font=("Helvetica", 9, "bold"), relief="flat", bd=0, padx=10,
                  command=self._confirm_next).pack(side="right", padx=4, pady=8)
        tk.Button(bar, text="Skip →", bg=C_BORDER, fg=C_TEXT,
                  font=("Helvetica", 9), relief="flat", bd=0, padx=8,
                  command=self._skip).pack(side="right", padx=4, pady=8)
        self._hdr_lbl = tk.Label(bar, text="", bg=C_SURFACE, fg=C_MUTED,
                                  font=("Helvetica", 9))
        self._hdr_lbl.pack(side="right", padx=10)

    def _build_list_panel(self) -> None:
        """Inject a left-side burst list between toolbar and scroll canvas."""
        # We need to restructure: canvas becomes part of a horizontal split
        # Unpack the canvas and vsb, add a left panel, repack
        self._canvas.pack_forget()
        for w in self.pack_slaves():
            if isinstance(w, tk.Scrollbar):
                w.pack_forget()

        body = tk.Frame(self, bg=C_BG)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # Left list
        lf = tk.Frame(body, bg=C_SURFACE, width=180)
        lf.grid(row=0, column=0, sticky="ns")
        lf.grid_propagate(False)
        tk.Label(lf, text="BURSTS", bg=C_SURFACE, fg=C_MUTED,
                 font=("Helvetica", 8, "bold")).pack(anchor="w", padx=10, pady=(8,2))
        sb2 = tk.Scrollbar(lf)
        sb2.pack(side="right", fill="y")
        self._listbox = tk.Listbox(lf, yscrollcommand=sb2.set,
                                    bg=C_BG, fg=C_TEXT,
                                    selectbackground=C_ACCENT,
                                    font=("Helvetica", 9), bd=0,
                                    highlightthickness=0, activestyle="none")
        self._listbox.pack(fill="both", expand=True, padx=4)
        sb2.config(command=self._listbox.yview)
        for i, g in enumerate(self._bursts):
            self._listbox.insert("end", f"  {i+1:>3}.  {len(g)} images")
        self._listbox.bind("<<ListboxSelect>>", self._on_list_select)
        nav = tk.Frame(lf, bg=C_SURFACE)
        nav.pack(fill="x", pady=4)
        tk.Button(nav, text="◀ Prev", bg=C_BORDER, fg=C_TEXT, relief="flat", bd=0,
                  command=lambda: self._load_burst(self._burst_idx-1)
                  ).pack(side="left", padx=3, fill="x", expand=True)
        tk.Button(nav, text="Next ▶", bg=C_BORDER, fg=C_TEXT, relief="flat", bd=0,
                  command=lambda: self._load_burst(self._burst_idx+1)
                  ).pack(side="right", padx=3, fill="x", expand=True)

        # Right: re-parent the canvas into body
        self._canvas.pack_forget()
        right = tk.Frame(body, bg=C_BG)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)
        self._canvas = tk.Canvas(right, bg=C_BG, highlightthickness=0)
        vsb = tk.Scrollbar(right, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=vsb.set)
        vsb.grid(row=0, column=1, sticky="ns")
        self._canvas.grid(row=0, column=0, sticky="nsew")
        self._inner = tk.Frame(self._canvas, bg=C_BG)
        self._win   = self._canvas.create_window((0,0), window=self._inner, anchor="nw")
        self._inner.bind("<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>", self._on_configure)
        self._canvas.bind("<MouseWheel>",
            lambda e: self._canvas.yview_scroll(int(-1*e.delta/120), "units"))

    def _load_burst(self, idx: int) -> None:
        idx = max(0, min(len(self._bursts)-1, idx))
        self._burst_idx = idx
        self._selected.clear()
        group = self._bursts[idx]
        self._paths = group
        if hasattr(self, "_hdr_lbl"):
            self._hdr_lbl.configure(
                text=f"Burst {idx+1}/{len(self._bursts)}  —  {len(group)} images")
        if self._listbox:
            self._listbox.selection_clear(0, "end")
            self._listbox.selection_set(idx)
            self._listbox.see(idx)
        self._prefetch(group)
        self._first_layout = False
        self._rebuild_grid()

    def _make_cell(self, parent: tk.Frame, path: str) -> tk.Frame:
        cell = make_thumb_cell(parent, path, self._app,
                               self.THUMB_W, self.THUMB_H, show_scores=True)
        tags = self._app.status.get(path, {})

        # Tag buttons row
        btn_row = tk.Frame(cell, bg=C_SURFACE)
        btn_row.pack(fill="x", padx=4, pady=(0, 2))
        for sym, tag, col in [
            ("★ Best",   "best",   C_BEST),
            ("✕ Delete", "delete", C_DELETE),
            ("⚑ ID",     "id",     C_ID),
        ]:
            active = tags.get(tag, False)
            tk.Button(
                btn_row, text=sym,
                bg=col if active else C_BORDER, fg="white",
                font=("Helvetica", 8, "bold"), relief="flat", bd=0,
                command=lambda p=path, t=tag: self._tag_burst(p, t)
            ).pack(side="left", fill="x", expand=True, padx=1)

        # Keep checkbox (marks for batch Best on confirm)
        var = tk.BooleanVar(value=path in self._selected)
        tk.Checkbutton(cell, text="Select for batch Best",
                        variable=var,
                        bg=C_SURFACE, fg=C_MUTED,
                        activebackground=C_SURFACE, selectcolor=C_BG,
                        font=("Helvetica", 7),
                        command=lambda p=path, v=var: (
                            self._selected.add(p) if v.get()
                            else self._selected.discard(p))
                        ).pack(pady=(0, 4))
        cell._var = var
        return cell

    def _tag_burst(self, path: str, tag: str) -> None:
        """Toggle a tag directly from burst view."""
        self._app.status[path][tag] = not self._app.status[path][tag]
        save_status(self._app.status_file, self._app.status)
        self._app._update_counts()
        if path in self._app.files:
            self._app._update_filmstrip_slot(self._app.files.index(path))
        self._rebuild_grid()

    def _on_list_select(self, _=None) -> None:
        sel = self._listbox.curselection()
        if sel and sel[0] != self._burst_idx:
            self._load_burst(sel[0])

    def _skip(self) -> None:
        self._load_burst(self._burst_idx + 1)

    def _confirm_next(self) -> None:
        for path in self._selected:
            self._app.status[path]["best"] = True
        save_status(self._app.status_file, self._app.status)
        self._app._update_counts()
        if self._listbox:
            self._listbox.itemconfig(
                self._burst_idx, fg=C_BEST if self._selected else C_MUTED)
        if self._burst_idx < len(self._bursts) - 1:
            self._load_burst(self._burst_idx + 1)
        else:
            mb.showinfo("Complete", f"All {len(self._bursts)} bursts reviewed.")
            self._app._hide_overlay()


class InlineSpeciesView(tk.Frame):
    """Inline species suggestion view — setup, scope, progress, results."""

    def __init__(self, parent: tk.Frame, app: "PhotoReviewer"):
        super().__init__(parent, bg=C_BG)
        self._app      = app
        self._settings = _inat_load_settings(app.base_dir)
        self._jwt      = self._settings.get("jwt")
        self._results: list[dict] = []
        self._tk_images: list[ImageTk.PhotoImage] = []
        self._running  = False
        self._build()

    def _build(self) -> None:
        _inline_toolbar(self, "🔍  Species Suggestion  —  iNaturalist API",
                        self._app._hide_overlay,
                        lambda bar: tk.Button(bar, text="⚙ Credentials",
                                              bg=C_BORDER, fg=C_TEXT,
                                              font=("Helvetica", 9), relief="flat",
                                              bd=0, padx=8,
                                              command=self._show_setup
                                              ).pack(side="right", padx=8, pady=8))
        self._content = tk.Frame(self, bg=C_BG)
        self._content.pack(fill="both", expand=True)
        if self._jwt and self._settings.get("app_id"):
            self._show_scope()
        else:
            self._show_setup()

    def _clear(self) -> None:
        for w in self._content.winfo_children():
            w.destroy()

    def _show_setup(self) -> None:
        self._clear()
        # Reuse existing SpeciesSuggestView setup panel logic
        sv = SpeciesSuggestView.__new__(SpeciesSuggestView)
        sv._app = self._app
        sv._settings = self._settings
        sv._content = self._content
        sv._jwt = self._jwt
        # Build setup panel into self._content
        frame = ctk.CTkFrame(self._content, fg_color=C_SURFACE, corner_radius=12)
        frame.place(relx=0.5, rely=0.5, anchor="center")
        SpeciesSuggestView._show_setup_panel(sv)

    def _show_scope(self) -> None:
        self._clear()
        sv = SpeciesSuggestView.__new__(SpeciesSuggestView)
        sv._app = self._app; sv._settings = self._settings
        sv._jwt = self._jwt; sv._content = self._content
        sv._results = self._results; sv._tk_images = self._tk_images
        sv._running = False
        SpeciesSuggestView._show_scope_panel(sv)


# ── Main application ──────────────────────────────────────────────────────────

class PhotoReviewer(ctk.CTkToplevel):

    def __init__(self, master, base_dir: str, files: list[str]):
        super().__init__(master)
        self.base_dir    = base_dir
        self.files       = files
        self.total       = len(files)
        self.index       = 0
        self.status_file = os.path.join(base_dir, STATUS_FILENAME)

        # Display state
        self._pil_image: Optional[Image.Image]        = None
        self._tk_thumb:  Optional[ImageTk.PhotoImage] = None
        self._zoom    = 1.0
        self._pan_x   = 0.0
        self._pan_y   = 0.0
        self._pan_start: Optional[tuple[int, int]]    = None

        # Persistent cache file paths
        self.score_file  = os.path.join(base_dir, SCORE_CACHE_FILE)

        # Analysis caches — pre-populated from disk cache where available
        self._dups:       dict[str, list[str]]           = {}
        self._bursts:     list[list[str]]                = []
        self._file_burst: dict[str, list[str]]           = {}
        self._histograms: dict[str, list[list[int]]]     = {}
        self._hash_ready  = False

        # Persistent cache paths
        self.hash_file  = os.path.join(base_dir, HASH_CACHE_FILE)

        # Load caches — only queue files not already cached
        valid = set(files)
        self._scores: dict[str, dict[str, int]] = load_score_cache(self.score_file, valid)
        self._hashes: dict[str, imagehash.ImageHash] = load_hash_cache(self.hash_file, valid)

        self._score_queue: list[str] = [f for f in files if f not in self._scores]
        self._hash_queue:  list[str] = [f for f in files if f not in self._hashes]

        log.info("Cache hit: %d/%d scores, %d/%d hashes pre-loaded",
                 len(self._scores), len(files),
                 len(self._hashes), len(files))

        # Thread-safe queue: worker threads post (path, hash) results here;
        # main thread drains it via after() — no Tkinter calls from workers.
        self._hash_result_q: queue.Queue = queue.Queue()
        self._hash_total_needed = len(self._hash_queue)

        # Filmstrip GC anchor
        self._fs_tk_images: list[ImageTk.PhotoImage] = []
        # Filmstrip slot → canvas tag-text item id (for partial update)
        self._fs_tag_items: dict[int, int] = {}   # slot_index → canvas item id
        self._fs_stripe_items: dict[int, int] = {}  # slot_index → stripe item id
        self._fs_window_lo: int = 0               # first index of current window
        # Undo stack: list of (path, tag, old_value)
        self._undo_stack: list[tuple[str, str, bool]] = []
        # Compare set — purely in-memory, never saved
        self._compare_set: list[str] = []   # ordered, max 4
        self._compare_window: Optional[object] = None  # kept for cleanup
        self._compare_mode: bool = False
        self._compare_tk_images: list[ImageTk.PhotoImage] = []
        self._shortcuts_overlay = None
        self._focus_peaking: bool = False
        self._shadow_boost: int  = 0
        self._noise_mode: bool   = False
        self._loupe_active: bool = False
        self._loupe_tk_img       = None
        self._multi_pass_on: bool = False   # multi-pass culling mode toggle
        self._pass_num: int       = 1       # 1=all, 2=not-deleted, 3=best-only
        self._auto_advance: bool  = True    # advance to next after delete
        self._app_settings: dict = load_app_settings()
        self._keywords: list[str] = self._app_settings.get("keywords", [])
        self._folder_tree_visible: bool = True

        # Preload cache: path → pre-sized PIL Image ready for canvas
        self._preload_cache: dict[str, Image.Image] = {}
        # EXIF cache — read once per file, never again
        self._exif_cache: dict[str, dict[str, str]] = {}
        self._preload_executor = ThreadPoolExecutor(max_workers=4)
        self._lanczos_job: Optional[str] = None  # after() job id
        # Canvas size hint for preload workers (safe to read from threads)
        self._canvas_w: int = 1200
        self._canvas_h: int = 800

        # Fullscreen state
        self._is_fullscreen     = False
        self._pre_fs_geom: Optional[str] = None

        self._build_status()
        self._build_ui()
        self.bind_all("<KeyPress>", self._on_key)

        # _show_current is called after fullscreen is applied (in _go_fullscreen)
        # so canvas dimensions are real when we first render
        self.after(300, self._start_hash_workers)

    # ── Status ────────────────────────────────────────────────────────────────

    def _build_status(self) -> None:
        valid  = set(self.files)
        stored = load_status(self.status_file, valid)

        def folder_lower(name: str) -> set[str]:
            d = os.path.join(self.base_dir, name)
            if not os.path.isdir(d):
                return set()
            return {fn.lower() for fn in os.listdir(d)
                    if Path(fn).suffix.lower() in SUPPORTED_EXTS}

        best_set = folder_lower("Best")
        id_set   = folder_lower("ID")
        self.status: dict[str, dict[str, bool]] = {}
        for f in self.files:
            bl  = Path(f).name.lower()
            was = stored.get(f, {})
            self.status[f] = {
                "best":   was.get("best",   False) or (bl in best_set),
                "delete": was.get("delete", False),
                "id":     was.get("id",     False) or (bl in id_set),
            }

    # ── Incremental hashing ───────────────────────────────────────────────────

    def _start_hash_workers(self) -> None:
        """
        Dispatch all unhashed files to a ThreadPoolExecutor.
        Workers do file I/O + hashing off the main thread and post
        (path, hash) tuples into self._hash_result_q.
        The main thread drains that queue via _poll_hash_results()
        using after() — workers never touch Tkinter at all.
        """
        if not self._hash_queue:
            # Everything was loaded from cache — hashing done, hide overlay
            log.info("All hashes cached — skipping hash workers.")
            self._update_loading_overlay("Complete!", 1.0)
            self.after(250, self._on_hashing_complete)
            return

        n_workers = min(HASH_WORKERS, len(self._hash_queue))
        log.info("Hashing %d files with %d workers…",
                 len(self._hash_queue), n_workers)

        # Keep a reference so GC doesn't collect it mid-run
        self._executor = ThreadPoolExecutor(max_workers=n_workers)

        def _worker(path: str) -> None:
            """Run in thread — NO Tkinter calls."""
            h = compute_ahash(path)
            self._hash_result_q.put((path, h))

        for path in self._hash_queue:
            self._executor.submit(_worker, path)

        # Start polling for results on the main thread
        self.after(HASH_POLL_MS, self._poll_hash_results)

    def _poll_hash_results(self) -> None:
        """
        Drain whatever results the worker threads have produced since last tick.
        Called every HASH_POLL_MS ms on the main thread — safe to update UI.
        """
        # Drain all available results without blocking
        newly_done = 0
        while True:
            try:
                path, h = self._hash_result_q.get_nowait()
                if h is not None:
                    self._hashes[path] = h
                newly_done += 1
            except queue.Empty:
                break

        total_needed = self._hash_total_needed
        done = total_needed - self._hash_result_q.qsize() if total_needed else 0
        # Count how many we've actually stored
        done = sum(1 for f in self._hash_queue if f in self._hashes)
        # Add files that were already cached
        done_all = len(self._hashes)
        frac = done_all / self.total if self.total else 1.0

        self._update_loading_overlay(
            f"Analysing images…  {done_all} / {self.total}",
            frac * 0.85,
        )

        # Check if all workers are done
        remaining = sum(
            1 for f in self._hash_queue if f not in self._hashes
        )

        if remaining > 0:
            self.after(HASH_POLL_MS, self._poll_hash_results)
        else:
            # All done — shut down executor and save cache
            self._executor.shutdown(wait=False)
            save_hash_cache(self.hash_file, self._hashes)
            self._update_loading_overlay("Complete!", 1.0)
            self.after(250, self._on_hashing_complete)

    def _run_burst_analysis(self) -> None:
        """
        On-demand O(n²) pairwise comparison — only called when user presses
        'Analyse Bursts'. Fast: pure integer arithmetic, no file I/O.
        """
        file_list = [f for f in self.files if f in self._hashes]
        n = len(file_list)

        dup_map:   dict[str, list[str]] = {f: [] for f in self.files}
        burst_map: dict[str, list[str]] = {f: [] for f in self.files}

        # Pre-fetch timestamps for time-gap filtering
        ts_cache: dict[str, Optional[float]] = {}
        for f in file_list:
            ts_cache[f] = (self._exif_cache.get(f, {}).get("_ts")
                           or _get_exif_timestamp(f))
            if ts_cache[f] and f in self._exif_cache:
                self._exif_cache.setdefault(f, {})["_ts"] = ts_cache[f]

        for i, fa in enumerate(file_list):
            for fb in file_list[i + 1:]:
                dist = abs(self._hashes[fa] - self._hashes[fb])
                if dist <= AHASH_DUP:
                    dup_map[fa].append(fb)
                    dup_map[fb].append(fa)
                if dist <= AHASH_BURST:
                    # Timestamp gate — only burst if within BURST_MAX_SECS
                    ta, tb = ts_cache.get(fa), ts_cache.get(fb)
                    if ta and tb and abs(ta - tb) > BURST_MAX_SECS:
                        continue   # same scene but shot far apart — not a burst
                    burst_map[fa].append(fb)
                    burst_map[fb].append(fa)

        self._dups = dup_map

        # Union-find burst groups
        parent = {f: f for f in file_list}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            parent[find(a)] = find(b)

        for fa, neighbours in burst_map.items():
            for fb in neighbours:
                if fa in parent and fb in parent:
                    union(fa, fb)

        components: dict[str, list[str]] = defaultdict(list)
        for f in self.files:
            root = find(f) if f in self._hashes else f
            components[root].append(f)

        seen: list[str] = []
        for f in self.files:
            root = find(f) if f in self._hashes else f
            if root not in seen:
                seen.append(root)

        self._bursts     = [components[r] for r in seen]
        self._file_burst = {f: grp for grp in self._bursts for f in grp}
        self._hash_ready = True

        n_bursts = sum(1 for g in self._bursts if len(g) > 1)
        n_dups   = sum(len(v) for v in self._dups.values()) // 2
        log.info("Burst analysis complete — %d burst groups, %d duplicate pairs.",
                 n_bursts, n_dups)

        self._hide_loading_overlay()
        self._refresh_sidebar(self.files[self.index])
        self._refresh_filmstrip(self.index)
        # Update title briefly then open burst view directly
        self._title_label.configure(
            text=f"Found {n_bursts} burst groups  ·  {n_dups} duplicate pairs")
        self.after(50, self._open_burst_after_analysis)

    def _open_burst_after_analysis(self) -> None:
        """Called after burst analysis — replace loading content with burst view.
        Cannot use _show_overlay() here because _active_overlay is already
        'burst_triage' from the loading screen, which would trigger the toggle
        and hide the overlay instead of showing the view.
        """
        # Clear loading spinner content
        for w in self._overlay_frame.winfo_children():
            w.destroy()
        # Build burst view directly into the already-visible overlay frame
        view = InlineBurstTriageView(self._overlay_frame, self)
        view.pack(fill="both", expand=True)
        self._overlay_frame.update_idletasks()

    def _on_canvas_resize(self, event) -> None:
        """
        Called when the canvas gets its real dimensions — fires on first draw
        and on window resize. Updates size hint for preload workers and
        re-renders the current image at the correct size.
        """
        cw, ch = event.width, event.height
        if cw < 10 or ch < 10:
            return
        self._canvas_w = cw
        self._canvas_h = ch
        # Re-render current image now canvas has real dimensions
        if self._pil_image is not None:
            if hasattr(self, "_lanczos_job") and self._lanczos_job:
                self.after_cancel(self._lanczos_job)
            self._render_image(quality=Image.BILINEAR)
            self._lanczos_job = self.after(200, self._render_image)

    def _on_hashing_complete(self) -> None:
        """Hashing done. Burst maps not built yet — that's on-demand."""
        self._hide_loading_overlay()
        self._refresh_sidebar(self.files[self.index])
        self._refresh_filmstrip(self.index)

    def _analyse_bursts(self) -> None:
        """
        On-demand burst analysis — triggered by 'Analyse Bursts' button.
        Runs the O(n²) pairwise comparison on already-computed hashes.
        Shows a brief progress message then updates burst UI.
        """
        if not self._hashes:
            mb.showinfo("Not ready",
                        "Images are still being analysed.\nPlease wait and try again.")
            return
        self._update_loading_overlay("Building burst groups…", 0.5)
        self._overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.update_idletasks()
        self._run_burst_analysis()

    # ── Preloading ───────────────────────────────────────────────────────────

    def _schedule_preload(self) -> None:
        """
        Submit decode jobs for ±PRELOAD_RADIUS images around current index.
        Workers decode into _preload_cache via thread pool — no Tkinter calls.
        Evicts images outside the window to keep memory bounded.
        """
        targets = []
        for offset in range(1, PRELOAD_RADIUS + 1):
            for direction in (offset, -offset):
                idx = self.index + direction
                if 0 <= idx < self.total:
                    targets.append(self.files[idx])

        # Evict images outside the new window
        keep = set(targets) | {self.files[self.index]}
        for path in list(self._preload_cache):
            if path not in keep:
                del self._preload_cache[path]

        # Submit decode jobs for any not yet cached
        for path in targets:
            if path not in self._preload_cache:
                self._preload_executor.submit(self._preload_worker, path)

    def _preload_worker(self, path: str) -> None:
        """
        Runs in thread — decodes and pre-sizes image to canvas dimensions.
        No Tkinter calls. After storing, schedules a filmstrip refresh on
        the main thread so newly preloaded thumbnails appear immediately.
        """
        try:
            img = _extract_thumb_img(path)
            cw = self._canvas_w or 1200
            ch = self._canvas_h or 800
            iw, ih = img.size
            cap_w = min(iw, cw * 2)
            cap_h = min(ih, ch * 2)
            if cap_w < iw or cap_h < ih:
                img = img.copy()
                img.thumbnail((int(cap_w), int(cap_h)), Image.BILINEAR)
                iw, ih = img.size
            scale = min(cw / iw, ch / ih)
            tw = max(1, int(iw * scale))
            th = max(1, int(ih * scale))
            img = img.resize((tw, th), Image.BILINEAR)
            self._preload_cache[path] = img
            # Refresh filmstrip on main thread — safe, no Tkinter in worker
            self.after(0, lambda: self._refresh_filmstrip(self.index))
        except Exception as e:
            log.debug("Preload failed %s: %s", path, e)

    # ── On-demand scoring ────────────────────────────────────────────────────

    def _score_file(self, path: str) -> None:
        """
        Score a single file and cache the result. No-op if already scored.
        Reuses _extract_thumb_img so the file is only opened once for both
        scoring and histogram — avoids double-loading large RAW files.
        """
        if path in self._scores:
            return
        try:
            img = _extract_thumb_img(path)   # single open, shared below
            self._scores[path]     = score_image(img)
            self._histograms[path] = build_histogram(img)
        except Exception as e:
            log.warning("Scoring failed %s: %s", path, e)


    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.title("Photo Reviewer")
        # Set taskbar icon
        try:
            from PIL import Image as _LPI, ImageTk as _LPT
            _li = _LPI.open(
                str(Path(__file__).parent / "photo_reviewer_icon.ico"))
            self._taskbar_icon = _LPT.PhotoImage(_li)
            self.iconphoto(True, self._taskbar_icon)
        except Exception:
            pass
        self.configure(fg_color=C_BG)
        # col 0 = folder tree  col 1 = canvas  col 2 = sidebar
        # row 0 = primary header row
        # row 1 = workflow bar (built inside _build_header)
        # row 2 = rename bar
        # row 3 = camera bar
        # row 4 = canvas + sidebar (weight=1)
        # row 5 = footer
        self.grid_rowconfigure(4, weight=1)
        self.grid_rowconfigure(5, weight=0, minsize=64)
        self.grid_columnconfigure(0, weight=0)   # folder tree
        self.grid_columnconfigure(1, weight=1)   # canvas
        self.grid_columnconfigure(2, weight=0)   # sidebar
        self._build_header()
        self._build_rename_bar()
        self._build_camera_bar()
        self._build_folder_tree()
        self._build_canvas_area()
        self._build_tag_strip()
        self._build_sidebar()
        self._build_footer()
        self._build_loading_overlay()
        self._check_backup_warning()
        # Longer delay on Windows — CTkToplevel needs time to fully initialise
        # before state("zoomed") takes effect
        self.after(200, self._go_fullscreen)

    def _build_folder_tree(self) -> None:
        """
        Collapsible left panel showing sibling folders of the current session.
        Click any folder to switch session. Toggle with the ◀/▶ button.
        """
        self._tree_frame = tk.Frame(self, bg=C_SURFACE, width=180)
        self._tree_frame.grid(row=4, column=0, rowspan=2, sticky="ns")
        self._tree_frame.grid_propagate(False)

        # Header with collapse button
        hdr = tk.Frame(self._tree_frame, bg="#111111", height=30)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text="FOLDERS", bg="#111111", fg=C_MUTED,
                 font=("Helvetica", 8, "bold")).pack(side="left", padx=8)
        self._tree_collapse_btn = tk.Button(
            hdr, text="◀", bg="#111111", fg=C_MUTED,
            font=("Helvetica", 9), relief="flat", bd=0,
            command=self._toggle_folder_tree)
        self._tree_collapse_btn.pack(side="right", padx=4)

        # Also add a persistent expand tab on the canvas left edge
        # (visible even when tree is collapsed)
        self._tree_tab = tk.Button(
            self, text="\u25b6  Folders",
            bg=C_ACCENT, fg="white",
            font=("Helvetica", 9, "bold"), relief="flat", bd=0,
            cursor="hand2", padx=8, pady=4,
            command=self._toggle_folder_tree)
        self._tree_tab.grid(row=4, column=0, rowspan=2, sticky="nw",
                            padx=2, pady=4)
        self._tree_tab.grid_remove()

        # Folder list
        sb = tk.Scrollbar(self._tree_frame)
        sb.pack(side="right", fill="y")
        self._folder_listbox = tk.Listbox(
            self._tree_frame, yscrollcommand=sb.set,
            bg=C_BG, fg=C_TEXT, selectbackground=C_ACCENT,
            font=("Helvetica", 9), bd=0, highlightthickness=0,
            activestyle="none")
        self._folder_listbox.pack(fill="both", expand=True)
        sb.config(command=self._folder_listbox.yview)
        self._folder_listbox.bind("<<ListboxSelect>>", self._on_folder_select)
        self._populate_folder_tree()

    def _populate_folder_tree(self) -> None:
        """Fill folder tree with sibling folders. Auto-collapse if <3 siblings."""
        self._folder_listbox.delete(0, "end")
        try:
            parent = Path(self.base_dir).parent
            dirs   = sorted([d for d in parent.iterdir() if d.is_dir()])
            # Auto-collapse if only 1-2 siblings (not useful to show)
            if len(dirs) < 3 and self._folder_tree_visible:
                self._toggle_folder_tree()
            for d in dirs:
                prefix = "▶ " if str(d) == self.base_dir else "  "
                self._folder_listbox.insert("end", f"{prefix}{d.name}")
            # Highlight current
            for i, d in enumerate(dirs):
                if str(d) == self.base_dir:
                    self._folder_listbox.selection_set(i)
                    self._folder_listbox.see(i)
                    break
            self._tree_dirs = dirs
        except Exception:
            self._tree_dirs = []

    def _on_folder_select(self, _=None) -> None:
        """Switch session to selected folder."""
        sel = self._folder_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if not hasattr(self, "_tree_dirs") or idx >= len(self._tree_dirs):
            return
        new_dir = str(self._tree_dirs[idx])
        if new_dir == self.base_dir:
            return
        # Reopen with new folder
        files = sorted(
            str(Path(new_dir) / f)
            for f in os.listdir(new_dir)
            if Path(f).suffix.lower() in SUPPORTED_EXTS)
        if not files:
            return
        # Reinitialise session
        self._init_session(new_dir, files)
        self._populate_folder_tree()

    def _toggle_folder_tree(self) -> None:
        """Show/hide the folder tree panel."""
        if self._folder_tree_visible:
            self._tree_frame.grid_remove()
            self._folder_tree_visible = False
            if hasattr(self, "_tree_tab"):
                self._tree_tab.grid()
        else:
            self._tree_frame.grid()
            self._folder_tree_visible = True
            if hasattr(self, "_tree_tab"):
                self._tree_tab.grid_remove()

    def _check_backup_warning(self) -> None:
        """Show a warning banner if source is on removable media."""
        if is_removable_drive(self.base_dir):
            self._show_backup_warning()

    def _show_backup_warning(self) -> None:
        """Amber banner warning about removable drive source."""
        banner = tk.Frame(self, bg="#7a4a00", height=24)
        banner.grid(row=2, column=0, columnspan=3, sticky="ew")
        tk.Label(banner,
                 text="⚠  Source folder is on a removable drive — back up your images before marking any as Delete",
                 bg="#7a4a00", fg="#ffd080",
                 font=("Helvetica", 9)).pack(side="left", padx=12, pady=3)
        tk.Button(banner, text="✓ I've backed up", bg="#7a4a00", fg="#ffd080",
                  font=("Helvetica", 8), relief="flat", bd=0,
                  command=banner.destroy).pack(side="right", padx=8)

    def _build_rename_bar(self) -> None:
        """
        Thin bar below the main header containing:
        - Editable filename field (stem only, no extension)
        - Confirm button (Enter key also works)
        - Extension label (read-only)
        - Species suggest button (placeholder for batch workflow)
        - Status label for feedback
        Sits at row=1, always visible.
        """
        bar = tk.Frame(self, bg="#111111", height=32)
        bar.grid(row=2, column=0, columnspan=3, sticky="ew")
        bar.grid_propagate(False)

        tk.Label(bar, text="Name:", bg="#111111", fg=C_MUTED,
                 font=("Helvetica", 9)).pack(side="left", padx=(12, 4), pady=6)

        # Editable stem field
        self._rename_var = tk.StringVar()
        self._rename_entry = tk.Entry(
            bar, textvariable=self._rename_var,
            bg="#050505", fg=C_TEXT,
            insertbackground=C_TEXT,
            relief="flat", bd=0,
            font=("Helvetica", 10),
            width=32,
        )
        self._rename_entry.pack(side="left", pady=5, ipady=2)
        self._rename_entry.bind("<Return>",    lambda _: self._apply_rename())
        self._rename_entry.bind("<Tab>",       lambda _: self._apply_rename())
        self._rename_entry.bind("<Escape>",    lambda _: self._cancel_rename())
        self._rename_entry.bind("<FocusIn>",   lambda _: self._rename_focus_in())
        self._rename_entry.bind("<FocusOut>",  lambda _: self._rename_focus_out())

        # Extension badge — styled as a small pill
        self._ext_frame = tk.Frame(bar, bg="#2a2d38", bd=0)
        self._ext_frame.pack(side="left", padx=(4, 8), pady=8)
        self._ext_label = tk.Label(
            self._ext_frame, text="", bg="#2a2d38", fg=C_MUTED,
            font=("Helvetica", 8, "bold"), padx=6, pady=2)
        self._ext_label.pack()

        # Confirm button — hidden until name is modified
        self._rename_confirm_btn = tk.Button(
            bar, text="✓ Rename", bg=C_BEST, fg="white",
            font=("Helvetica", 9, "bold"), relief="flat", bd=0,
            padx=10, command=self._apply_rename)
        # Don't pack yet — shown on first edit

        def _on_rename_key(*_):
            current = self._rename_var.get()
            if current != self._rename_original:
                self._rename_confirm_btn.pack(side="left", pady=4, padx=(0, 4))
            else:
                self._rename_confirm_btn.pack_forget()

        self._rename_var.trace_add("write", _on_rename_key)

        # Divider
        tk.Frame(bar, bg=C_BORDER, width=1).pack(
            side="left", fill="y", padx=8, pady=6)

        # Batch species suggest button (opens batch workflow)
        tk.Button(bar, text="🔍 Suggest names…",
                  bg="#111111", fg="#7BA3E0",
                  font=("Helvetica", 9), relief="flat", bd=0,
                  padx=6,
                  command=self._open_species_suggest).pack(side="left", pady=4)

        # Status feedback label (right-aligned)
        self._rename_status = tk.Label(
            bar, text="", bg="#111111", fg=C_MUTED,
            font=("Helvetica", 9))
        self._rename_status.pack(side="right", padx=12)

        self._rename_original: str = ""   # track original name for cancel
        self._rename_active   = False      # True while entry has focus

    def _build_tag_strip(self) -> None:
        """
        Create tag badge state objects. They are not displayed in the UI
        (tag state is shown via canvas flash + filmstrip stripe instead)
        but the set_active() calls in _update_tags_only still reference them.
        Using a hidden frame as parent avoids polluting the main grid.
        """
        _hidden = tk.Frame(self, width=0, height=0)
        _hidden.place(x=-999, y=-999)   # off-screen, never visible
        self._badge_best   = TagBadge(_hidden, "BEST",   C_BEST)
        self._badge_delete = TagBadge(_hidden, "DELETE", C_DELETE)
        self._badge_id     = TagBadge(_hidden, "ID",     C_ID)

    def _build_header(self) -> None:
        """
        Two-row header:
        Row 0 (46px): app name | Viewer | Pass | stars | filename | display tools | window controls
        Row 1 (34px): workflow buttons
        """
        # ── Row 0: primary ───────────────────────────────────────────────────
        hdr = tk.Frame(self, bg=C_SURFACE, height=46)
        hdr.grid(row=0, column=0, columnspan=3, sticky="ew")
        hdr.grid_propagate(False)

        # Icon: build as PhotoImage using Pillow, display in tk.Label
        # PhotoImage is more reliable than canvas.create_* before mapping
        try:
            from PIL import Image as _PILImg, ImageDraw as _PILDraw, ImageTk as _PILTk
            _ico = _PILImg.new("RGBA", (22, 22), (0, 0, 0, 0))
            _d   = _PILDraw.Draw(_ico)
            # Near-black bg
            _d.rounded_rectangle([0, 0, 21, 21], radius=4, fill="#0a0f0b")
            # Film frame
            _d.rounded_rectangle([1, 3, 21, 19], radius=2,
                                  outline="#2D7A4F", width=1, fill="#0d1a0e")
            # Sprocket holes
            for _y in (5, 9, 13, 17):
                _d.rectangle([2, _y, 4, _y+2], fill="#2D7A4F")
                _d.rectangle([18, _y, 20, _y+2], fill="#2D7A4F")
            # Tick
            _d.line([(6, 12), (9, 15), (16, 7)],
                    fill="#2D7A4F", width=2)
            self._header_icon_img = _PILTk.PhotoImage(_ico)
            tk.Label(hdr, image=self._header_icon_img,
                     bg=C_SURFACE).pack(side="left", padx=(10, 4), pady=10)
        except Exception:
            # Fallback: simple text if Pillow draw fails
            tk.Label(hdr, text="✓", bg=C_SURFACE, fg=C_ACCENT,
                     font=("Helvetica", 12, "bold")).pack(side="left", padx=(10, 4))
        # Wordmark: "Photo" in accent, "Reviewer" in muted
        tk.Label(hdr, text="Photo", bg=C_SURFACE, fg=C_ACCENT,
                 font=("Helvetica", 11, "bold")).pack(side="left", padx=(0, 2), pady=8)
        tk.Label(hdr, text="Reviewer", bg=C_SURFACE, fg=C_MUTED,
                 font=("Helvetica", 11)).pack(side="left", padx=(0, 8), pady=8)
        tk.Frame(hdr, bg=C_BORDER, width=1).pack(side="left", fill="y", pady=8)

        self._viewer_btn = ctk.CTkButton(
            hdr, text="📷 Viewer", width=86, height=28,
            fg_color=C_ACCENT, hover_color=_darken(C_ACCENT),
            text_color="white", font=ctk.CTkFont("Helvetica", 10, "bold"),
            command=self._hide_overlay)
        self._viewer_btn.pack(side="left", padx=(6,2), pady=9)

        # Pass mode — compact
        self._pass_btn = ctk.CTkButton(
            hdr, text="⊏ Pass", width=60, height=26,
            fg_color=C_BORDER, hover_color=C_ACCENT,
            text_color=C_MUTED, font=ctk.CTkFont("Helvetica", 9),
            command=self._cycle_pass)
        self._pass_btn.pack(side="left", padx=2, pady=10)

        # Auto-advance — small dot indicator
        # Auto-advance toggle — button style, clear on/off
        self._adv_btn_lbl = tk.Button(
            hdr, text="Auto→ ON",
            bg="#166534", fg="white",
            font=("Helvetica", 8, "bold"), relief="flat", bd=0,
            padx=6, pady=2, cursor="hand2",
            command=self._toggle_auto_advance)
        self._adv_btn_lbl.pack(side="left", padx=(2, 6), pady=12)
        tk.Frame(hdr, bg=C_BORDER, width=1).pack(side="left", fill="y", pady=8)

        # Centre: filename
        centre = tk.Frame(hdr, bg=C_SURFACE)
        centre.pack(side="left", fill="both", expand=True)
        self._burst_label = ctk.CTkLabel(centre, text="",
                                          font=ctk.CTkFont("Helvetica", 9),
                                          text_color=C_ACCENT)
        self._burst_label.pack(side="left", padx=(8,2))
        self._title_label = ctk.CTkLabel(centre, text="",
                                          font=ctk.CTkFont("Helvetica", 13, "bold"),
                                          text_color=C_TEXT)
        self._title_label.pack(side="left", padx=2)

        # Right: window controls
        ctk.CTkButton(hdr, text="✕", width=36, height=46,
                      fg_color="transparent", hover_color=C_DELETE,
                      text_color=C_MUTED, font=ctk.CTkFont("Helvetica", 12),
                      corner_radius=0, command=self._quit).pack(side="right")
        ctk.CTkButton(hdr, text="⛶", width=36, height=46,
                      fg_color="transparent", hover_color=C_BORDER,
                      text_color=C_MUTED, font=ctk.CTkFont("Helvetica", 12),
                      corner_radius=0, command=self._toggle_fullscreen).pack(side="right")
        tk.Frame(hdr, bg=C_BORDER, width=1).pack(side="right", fill="y", pady=8, padx=2)

        # Star rating — "Rate:" label + clickable stars
        sf = tk.Frame(hdr, bg=C_SURFACE)
        sf.pack(side="right", padx=(2, 4), pady=8)
        tk.Label(sf, text="Rate:", bg=C_SURFACE, fg=C_MUTED,
                 font=("Helvetica", 8)).pack(side="left", padx=(0, 3))
        self._star_lbl = tk.Label(sf, text="☆☆☆☆☆",
                                   bg=C_SURFACE, fg="#555",
                                   font=("Helvetica", 13), cursor="hand2")
        self._star_lbl.pack(side="left")
        self._star_lbl.bind("<Button-1>", self._star_click)
        self._star_lbl.bind("<Motion>",   self._star_hover)
        self._star_lbl.bind("<Leave>",    lambda e: self._update_star_display())
        self._star_btns = []
        tk.Frame(hdr, bg=C_BORDER, width=1).pack(side="right", fill="y", pady=8, padx=2)

        # Display tools — labelled buttons showing active state
        for label, active_label, cmd, attr in [
            ("1:1",     "1:1",      self._zoom_to_100,            None),
            ("Loupe",   "Loupe ON", self._toggle_loupe,           "_loupe_btn"),
            ("Noise",   "Noise ON", self._toggle_noise_visualiser, "_noise_btn"),
            ("Peak",    "Peak ON",  self._toggle_focus_peaking,   "_peak_btn"),
            ("Expose",  None,       self._cycle_exposure_inspect,  "_expose_btn"),
        ]:
            b = ctk.CTkButton(hdr, text=label, width=58, height=26,
                              fg_color=C_BORDER, hover_color=C_ACCENT,
                              text_color=C_MUTED,
                              font=ctk.CTkFont("Helvetica", 8),
                              command=cmd)
            b.pack(side="right", padx=1, pady=10)
            if attr:
                setattr(self, attr, b)
                setattr(self, attr + "_off_label", label)
                setattr(self, attr + "_on_label", active_label or label)
        tk.Frame(hdr, bg=C_BORDER, width=1).pack(side="right", fill="y", pady=8, padx=2)

        # Progress + zoom
        self._zoom_label = ctk.CTkLabel(hdr, text="100%", width=38,
                                         font=ctk.CTkFont("Helvetica", 9, "bold"),
                                         text_color=C_TEXT)
        self._zoom_label.pack(side="right", padx=1)
        self._progress_label = ctk.CTkLabel(hdr, text="",
                                             font=ctk.CTkFont("Helvetica", 9),
                                             text_color=C_MUTED, width=90)
        self._progress_label.pack(side="right", padx=(4,2))

        # ── Row 1: workflow bar ───────────────────────────────────────────────
        wf = tk.Frame(self, bg="#111111", height=34)
        wf.grid(row=1, column=0, columnspan=3, sticky="ew")
        wf.grid_propagate(False)

        self._header_toggle_btns = {}

        def _wf_btn(text, cmd, overlay_name=None, width=100):
            b = ctk.CTkButton(wf, text=text, width=width, height=26,
                              fg_color="transparent", hover_color=C_BORDER,
                              text_color=C_MUTED, font=ctk.CTkFont("Helvetica", 9),
                              command=cmd)
            b.pack(side="left", padx=2, pady=4)
            if overlay_name:
                self._header_toggle_btns[b] = overlay_name
            return b

        self._gallery_btn = _wf_btn("⊞ Gallery  G",    self._open_gallery,         "gallery",        96)
        self._compare_btn = _wf_btn("⊟ Compare  C",    self._open_comparison,      None,             96)
        tk.Frame(wf, bg=C_BORDER, width=1).pack(side="left", fill="y", pady=6)
        self._scoring_btn = _wf_btn("⚡ Clean Load",    self._open_scoring_quick,   "scoring_quick",  92)
        self._detail_btn  = _wf_btn("🔬 Detail Clean",  self._open_scoring_detail,  "scoring_detail", 102)
        self._burst_btn   = _wf_btn("⊹ Bursts",         self._open_burst_triage,    "burst_triage",   72)
        self._species_btn = _wf_btn("🔍 Species",        self._open_species_suggest, "species",        72)
        tk.Frame(wf, bg=C_BORDER, width=1).pack(side="left", fill="y", pady=6)
        ctk.CTkButton(wf, text="?", width=28, height=26,
                      fg_color="transparent", hover_color=C_BORDER,
                      text_color=C_MUTED, font=ctk.CTkFont("Helvetica", 10, "bold"),
                      command=self._toggle_shortcuts_overlay).pack(side="left", padx=2, pady=4)

    def _build_loading_overlay(self) -> None:
        """
        Translucent overlay shown during hashing/scoring.
        Sits on top of the main grid via place() so it covers everything.
        Hidden once both hash and score passes complete.
        """
        self._overlay = tk.Frame(self, bg="#0D0F14")
        self._overlay.place(relx=0, rely=0, relwidth=1, relheight=1)

        inner = ctk.CTkFrame(self._overlay, fg_color=C_SURFACE,
                              corner_radius=16, width=360, height=180)
        inner.place(relx=0.5, rely=0.5, anchor="center")
        inner.pack_propagate(False)

        ctk.CTkLabel(inner, text="📷  Photo Reviewer",
                     font=ctk.CTkFont("Helvetica", 15, "bold"),
                     text_color=C_TEXT).pack(pady=(28, 4))

        self._loading_label = ctk.CTkLabel(inner, text="Loading images…",
                                            font=ctk.CTkFont("Helvetica", 11),
                                            text_color=C_MUTED)
        self._loading_label.pack(pady=(0, 12))

        self._loading_bar = ctk.CTkProgressBar(inner, mode="determinate",
                                                height=6, corner_radius=3,
                                                fg_color=C_BORDER,
                                                progress_color=C_ACCENT,
                                                width=280)
        self._loading_bar.set(0)
        self._loading_bar.pack(pady=(0, 24))

    def _update_loading_overlay(self, message: str, progress: float = -1) -> None:
        """Update status text and optionally the progress bar (0.0–1.0)."""
        if hasattr(self, "_loading_label"):
            self._loading_label.configure(text=message)
        if progress >= 0 and hasattr(self, "_loading_bar"):
            self._loading_bar.set(min(1.0, progress))
        self.update_idletasks()

    def _hide_loading_overlay(self) -> None:
        if hasattr(self, "_overlay"):
            self._overlay.place_forget()

    def _build_camera_bar(self) -> None:
        """
        Thin bar showing key shooting parameters for the current image.
        Always visible — like darktable's bottom status bar.
        Shows: shutter · aperture · ISO · focal length · date · model
        """
        bar = tk.Frame(self, bg="#050505", height=22)
        bar.grid(row=3, column=0, columnspan=3, sticky="ew")
        bar.grid_propagate(False)

        self._camera_bar_lbl = tk.Label(
            bar, text="",
            bg="#050505", fg="#6B7280",
            font=("Helvetica", 9),
            anchor="center")
        self._camera_bar_lbl.pack(fill="both", expand=True)

    def _update_camera_bar(self, exif: dict[str, str]) -> None:
        """Update camera bar using format_exif_display output."""
        if not hasattr(self, "_camera_bar_lbl"):
            return
        rows = format_exif_display(exif)
        # Pick the most useful fields for the compact bar
        want = {"Camera", "Shutter", "Aperture", "ISO", "Focal len.", "Date"}
        parts = [v for label, v in rows if label in want]
        self._camera_bar_lbl.configure(
            text="  ·  ".join(parts) if parts else "")


    def _build_canvas_area(self) -> None:
        # area = main grid row=2, col=0 only
        area = tk.Frame(self, bg=C_BG)
        area.grid(row=4, column=1, sticky="nsew")
        area.rowconfigure(0, weight=1)
        area.rowconfigure(1, weight=0)
        area.rowconfigure(2, weight=0)
        area.columnconfigure(0, weight=1)

        self._canvas = tk.Canvas(area, bg=C_BG, highlightthickness=0, cursor="crosshair")
        self._canvas.grid(row=0, column=0, sticky="nsew")
        self._canvas.bind("<MouseWheel>",      self._on_scroll)
        self._canvas.bind("<Button-4>",        self._on_scroll)
        self._canvas.bind("<Button-5>",        self._on_scroll)
        self._canvas.bind("<ButtonPress-1>",   self._pan_start_cb)
        self._canvas.bind("<B1-Motion>",       self._pan_move_cb)
        self._canvas.bind("<ButtonRelease-1>", self._pan_end_cb)
        self._canvas.bind("<ButtonPress-2>",   self._pan_start_cb)
        self._canvas.bind("<B2-Motion>",       self._pan_move_cb)
        self._canvas.bind("<ButtonRelease-2>", self._pan_end_cb)
        self._canvas.bind("<Configure>",       self._on_canvas_resize)

        # Inline compare overlay
        self._compare_frame = tk.Frame(area, bg=C_BG)

        # Generic overlay frame — used by gallery, scoring, burst triage, species
        self._overlay_frame = tk.Frame(area, bg=C_BG)
        self._active_overlay: Optional[str] = None   # which overlay is shown

        # Filmstrip — area row=1, hidden until hashing done
        filmstrip_outer = tk.Frame(area, bg="#111111")
        filmstrip_outer.grid(row=1, column=0, sticky="ew")
        filmstrip_outer.grid_remove()
        filmstrip_outer.columnconfigure(0, weight=1)

        # Session progress bar — thin strip above filmstrip
        prog_bg = tk.Frame(filmstrip_outer, bg="#1a1a1a", height=4)
        prog_bg.grid(row=0, column=0, sticky="ew")
        prog_bg.columnconfigure(0, weight=1)
        self._session_prog_bar = tk.Frame(prog_bg, bg=C_ACCENT, height=4)
        self._session_prog_bar.place(relx=0, rely=0, relheight=1, relwidth=0)
        self._filmstrip_frame = tk.Frame(filmstrip_outer, bg=C_SURFACE,
                                          height=FILMSTRIP_H)
        self._filmstrip_frame.grid(row=1, column=0, sticky="ew")
        self._filmstrip_frame.grid_propagate(False)
        self._filmstrip_frame.columnconfigure(0, weight=1)
        self._filmstrip_frame.rowconfigure(0, weight=1)
        self._filmstrip_outer = filmstrip_outer
        self._filmstrip_canvas = tk.Canvas(
            self._filmstrip_frame, bg="#080808",
            height=FILMSTRIP_H, highlightthickness=0)
        self._filmstrip_canvas.grid(row=0, column=0, sticky="nsew")

        # Scrubber removed — progress shown in header as text

    def _build_sidebar(self) -> None:
        """
        Sidebar built entirely with tk primitives for reliable accordion
        behaviour on Windows. CTkScrollableFrame is replaced with a plain
        tk.Canvas + Scrollbar so pack_forget() works correctly.
        """
        # ── Outer container ───────────────────────────────────────────────────
        outer = tk.Frame(self, bg="#2a2d38", width=271)
        outer.grid(row=4, column=2, rowspan=2, sticky="ns")
        # Inner frame with 1px left border (the parent bg shows through)
        inner_border = tk.Frame(outer, bg=C_SURFACE, width=270)
        inner_border.pack(side="right", fill="both", expand=True)
        outer.grid_propagate(False)

        # Scrollable canvas + scrollbar
        sb_canvas = tk.Canvas(inner_border, bg=C_SURFACE, highlightthickness=0, width=254)
        scrollbar = tk.Scrollbar(inner_border, orient="vertical",
                                  command=sb_canvas.yview)
        sb_canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.grid(row=0, column=1, sticky="ns")
        sb_canvas.grid(row=0, column=0, sticky="nsew")

        inner_border.columnconfigure(0, weight=1)
        inner_border.rowconfigure(0, weight=1)

        # Inner frame — all widgets go here
        sb = tk.Frame(sb_canvas, bg=C_SURFACE)
        sb_win = sb_canvas.create_window((0, 0), window=sb, anchor="nw")

        def _on_sb_configure(e):
            # Only scroll to actual content — prevent blank space at top
            bbox = sb_canvas.bbox("all")
            if bbox:
                sb_canvas.configure(scrollregion=(0, 0, bbox[2], bbox[3]))
            sb_canvas.itemconfig(sb_win, width=sb_canvas.winfo_width())

        sb.bind("<Configure>", _on_sb_configure)
        sb_canvas.bind("<Configure>",
                        lambda e: sb_canvas.itemconfig(sb_win, width=e.width))

        # Mouse wheel — only when pointer is over sidebar
        def _on_mousewheel(e):
            sb_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        sb_canvas.bind("<MouseWheel>", _on_mousewheel)
        sb.bind("<MouseWheel>", _on_mousewheel)

        # ── Accordion helper ──────────────────────────────────────────────────
        # Use grid instead of pack so order is always preserved on toggle.
        # Each accordion occupies two rows: header row and body row.
        _row = [0]   # mutable counter shared across closures

        def accordion(title: str, start_open: bool = True) -> tk.Frame:
            """
            Collapsible section using grid rows.
            grid_remove() / grid() preserves widget order — pack_forget/pack
            does not (re-pack always appends to bottom).
            """
            state = {"open": start_open}

            # Header row
            hdr_row = _row[0]
            _row[0] += 1
            hdr = tk.Frame(sb, bg=C_BORDER, height=28)
            hdr.grid(row=hdr_row, column=0, sticky="ew", pady=(6, 0))
            hdr.grid_propagate(False)

            arrow = tk.Label(hdr, text="▾" if start_open else "▸",
                              bg=C_BORDER, fg=C_MUTED, font=("Helvetica", 9))
            arrow.pack(side="left", padx=(10, 4))
            tk.Label(hdr, text=title, bg=C_BORDER, fg=C_MUTED,
                     font=("Helvetica", 9, "bold")).pack(side="left")

            # Body row
            body_row = _row[0]
            _row[0] += 1
            body = tk.Frame(sb, bg=C_SURFACE)
            body.grid(row=body_row, column=0, sticky="ew")
            if not start_open:
                body.grid_remove()

            def toggle(_event=None):
                state["open"] = not state["open"]
                if state["open"]:
                    body.grid()
                    arrow.configure(text="▾")
                else:
                    body.grid_remove()
                    arrow.configure(text="▸")
                sb.update_idletasks()
                sb_canvas.configure(scrollregion=sb_canvas.bbox("all"))

            for w in (hdr, arrow):
                w.bind("<Button-1>", toggle)
            for child in hdr.winfo_children():
                child.bind("<Button-1>", toggle)

            return body

        sb.columnconfigure(0, weight=1)

        # ── Helper for consistent labels inside sidebar ────────────────────────
        def sb_label(parent, text, fg=C_MUTED, font=("Helvetica", 10)):
            return tk.Label(parent, text=text, bg=C_SURFACE,
                            fg=fg, font=font)

        # ── Search ────────────────────────────────────────────────────────────
        body = accordion("SEARCH", start_open=True)
        sf_outer = tk.Frame(body, bg=C_BG, bd=0)
        sf_outer.pack(fill="x", padx=12, pady=(6, 4))
        self._search_entry = ctk.CTkEntry(sf_outer, placeholder_text="Filename…",
                                           font=ctk.CTkFont("Helvetica", 11),
                                           border_width=0, fg_color=C_BG,
                                           text_color=C_TEXT)
        self._search_entry.pack(side="left", fill="x", expand=True, padx=(4, 0), pady=4)
        self._search_entry.bind("<Return>", lambda _: self._perform_search())
        tk.Button(sf_outer, text="↵", bg=C_ACCENT, fg="white",
                  font=("Helvetica", 13), relief="flat", bd=0,
                  command=self._perform_search).pack(side="left", padx=4, pady=4)

        tk.Button(body, text="📂  Browse images…",
                  bg=C_BORDER, fg=C_TEXT,
                  font=("Helvetica", 10), relief="flat", bd=0,
                  command=self._browse_images).pack(
            fill="x", padx=12, pady=(2, 8), ipady=4)

        # ── Session ───────────────────────────────────────────────────────────
        body = accordion("SESSION", start_open=True)
        self._count_best   = CountRow(body, "Best",   C_BEST)
        self._count_delete = CountRow(body, "Delete", C_DELETE)
        self._count_id     = CountRow(body, "ID",     C_ID)
        for w in (self._count_best, self._count_delete, self._count_id):
            w.pack(fill="x", padx=20, pady=4)
        tk.Frame(body, bg=C_SURFACE, height=8).pack()

        # ── Quality ───────────────────────────────────────────────────────────
        body = accordion("QUALITY  (0–100)", start_open=True)
        self._score_rows: dict[str, ScoreRow] = {}
        for metric, label in [
            ("sharpness",   "Sharpness"),
            ("motion_blur", "Motion blur"),
            ("exposure",    "Exposure"),
        ]:
            row = ScoreRow(body, label)
            row.pack(fill="x", padx=20, pady=3)
            self._score_rows[metric] = row
        tk.Frame(body, bg=C_SURFACE, height=6).pack()

        # ── Histogram ─────────────────────────────────────────────────────────
        body = accordion("HISTOGRAM  (R / G / B)", start_open=True)
        self._histogram = HistogramCanvas(body)
        self._histogram.pack(fill="x", padx=8, pady=(4, 8))

        # ── Location map ──────────────────────────────────────────────────────
        body = accordion("LOCATION", start_open=True)
        self._map_canvas = CountryMapCanvas(body)
        self._map_canvas.pack(padx=12, pady=(4, 4))
        self._map_canvas.clear()

        self._grid_ref_label = tk.Label(body, text="",
                                         bg=C_SURFACE, fg=C_MUTED,
                                         font=("Helvetica", 8))
        self._grid_ref_label.pack(anchor="w", padx=14, pady=(0, 8))

        # ── Metadata ──────────────────────────────────────────────────────────
        body = accordion("METADATA", start_open=True)

        for label, key, multiline in [
            ("Species",    "species",   False),
            ("Caption",    "caption",   True),
            ("Copyright",  "copyright", False),
            ("Location",   "location",  False),
        ]:
            tk.Label(body, text=label, bg=C_SURFACE, fg=C_MUTED,
                     font=("Helvetica", 8)).pack(anchor="w", padx=12, pady=(4,0))
            if multiline:
                w = tk.Text(body, height=2, bg=C_BG, fg=C_TEXT,
                            font=("Helvetica", 9), relief="flat",
                            insertbackground=C_TEXT)
                w.pack(fill="x", padx=12, pady=(0,2))
            else:
                w = tk.Entry(body, bg=C_BG, fg=C_TEXT,
                             font=("Helvetica", 9), relief="flat",
                             insertbackground=C_TEXT)
                w.pack(fill="x", padx=12, pady=(0,2))
            setattr(self, f"_meta_{key}", w)
            # Save on focus-out
            w.bind("<FocusOut>", lambda e, k=key: self._save_metadata(k))

        tk.Button(body, text="Apply copyright to all Best",
                  bg=C_BORDER, fg=C_TEXT, font=("Helvetica", 8),
                  relief="flat", bd=0, padx=8,
                  command=self._apply_copyright_to_best
                  ).pack(anchor="w", padx=12, pady=4)

        # ── Keywords ──────────────────────────────────────────────────────────
        body = accordion("KEYWORDS", start_open=True)

        self._kw_frame = tk.Frame(body, bg=C_SURFACE)
        self._kw_frame.pack(fill="x", padx=8, pady=4)
        self._refresh_keyword_buttons()

        kw_add_row = tk.Frame(body, bg=C_SURFACE)
        kw_add_row.pack(fill="x", padx=8, pady=(0,4))
        self._kw_entry = tk.Entry(kw_add_row, bg=C_BG, fg=C_TEXT,
                                   font=("Helvetica", 9), relief="flat",
                                   insertbackground=C_TEXT)
        self._kw_entry.pack(side="left", fill="x", expand=True)
        tk.Button(kw_add_row, text="+ Add", bg=C_ACCENT, fg="white",
                  font=("Helvetica", 8), relief="flat", bd=0, padx=6,
                  command=self._add_keyword).pack(side="left", padx=4)

        # ── EXIF ──────────────────────────────────────────────────────────────
        body = accordion("EXIF", start_open=False)
        self._meta_box = tk.Text(body, font=("Courier", 8),
                                  bg=C_BG, fg=C_MUTED,
                                  relief="flat", bd=0, padx=8, pady=4,
                                  wrap="none", state="disabled", height=10)
        self._meta_box.pack(fill="x", padx=12, pady=(4, 8))

        # ── Duplicates ────────────────────────────────────────────────────────
        body = accordion("DUPLICATES", start_open=False)
        self._dup_box = tk.Text(body, font=("Courier", 8),
                                 bg=C_BG, fg=C_MUTED,
                                 relief="flat", bd=0, padx=8, pady=4,
                                 wrap="none", state="disabled", height=4)
        self._dup_box.pack(fill="x", padx=12, pady=(4, 8))


    def _build_footer(self) -> None:
        footer = tk.Frame(self, bg=C_SURFACE, height=64)
        footer.grid(row=5, column=0, columnspan=3, sticky="ew")
        footer.grid_propagate(False)
        footer.pack_propagate(False)

        # (label, shortcut_hint, command, bg, fg)
        btn_data = [
            ("← Prev",    "◄ ←",      self._prev_image,    "#1e1e1e",  "#888888"),
            ("Next →",    "→ ►",      self._next_image,    "#1e1e1e",  "#888888"),
            ("★ Best",    "B / Enter", self._mark_best,     "#16a34a",  "white"),
            ("✕ Delete",  "D / Back",  self._mark_delete,   "#dc2626",  "white"),
            ("⚑ ID",      "I / Space", self._mark_id,       "#2D7A4F",  "white"),
            ("⊞ Compare", "X / C",     self._mark_compare,  "#ea580c",  "white"),
            ("→ Topaz",   "",          self._send_to_topaz, "#1e1e1e",  "#4ade80"),
            ("▶ Process", "",          self._process,       "#2D7A4F",  "white"),
        ]
        for txt, hint, cmd, bg, fg in btn_data:
            cell = tk.Frame(footer, bg=bg)
            cell.pack(side="left", fill="both", expand=True, padx=2, pady=6)
            tk.Button(
                cell, text=txt, command=cmd,
                bg=bg, fg=fg, activebackground=_darken(bg),
                activeforeground=fg,
                font=("Helvetica", 10, "bold"),
                relief="flat", bd=0,
            ).pack(fill="x", expand=True, ipady=4)
            if hint:
                tk.Label(cell, text=hint, bg=bg,
                         fg="#888888" if fg == "white" else "#666",
                         font=("Helvetica", 7)).pack()

    # ── Image display ─────────────────────────────────────────────────────────

    def _show_current(self) -> None:
        path        = self.files[self.index]
        show_index  = self.index   # snapshot for deferred callbacks
        pct         = int((self.index + 1) / self.total * 100)
        self._title_label.configure(text=Path(path).name)
        self._progress_label.configure(text=f"{self.index + 1} / {self.total}  ({pct}%)")
        # Update session progress bar
        if hasattr(self, "_session_prog_bar") and self.total > 0:
            self._session_prog_bar.place(relwidth=(self.index + 1) / self.total)
        # Update rename entry (unless user is currently editing it)
        if not self._rename_active:
            self._populate_rename_entry(path)

        # ── Image: use pre-sized cache if available ────────────────────────────
        cached = self._preload_cache.get(path)
        if cached is not None:
            # Already decoded and resized by worker — store as _pil_image so
            # _render_image works normally, but mark it pre-sized so we skip
            # the heavy capping/resizing step
            self._pil_image        = cached
            self._pil_image_presized = True
        else:
            try:
                self._pil_image = _extract_thumb_img(path)
            except Exception as e:
                self._pil_image = None
                self._canvas.delete("all")
                cw = self._canvas_w or 800
                ch = self._canvas_h or 600
                self._canvas.create_text(cw / 2, ch / 2,
                                         text=f"Cannot load image\n{e}",
                                         fill=C_MUTED, font=("Helvetica", 11),
                                         justify="center")
            self._pil_image_presized = False

        self._zoom = 1.0; self._pan_x = 0.0; self._pan_y = 0.0

        # Cancel any pending quality upgrade from previous image
        if hasattr(self, "_lanczos_job") and self._lanczos_job:
            self.after_cancel(self._lanczos_job)
            self._lanczos_job = None

        # Render immediately (pre-sized = just ImageTk + draw; else BILINEAR)
        self._render_image(quality=Image.BILINEAR)

        # Schedule LANCZOS upgrade only if index unchanged after 200ms
        def _upgrade_if_same():
            if self.index == show_index and self._pil_image is not None:
                self._pil_image_presized = False   # force full quality path
                self._render_image(quality=Image.LANCZOS)
        self._lanczos_job = self.after(200, _upgrade_if_same)

        # Kick off preloading — highest priority: next image, then prev
        self._schedule_preload()

        # Tags — fast, no I/O
        tags = self.status[path]
        self._badge_best.set_active(tags["best"])
        self._badge_delete.set_active(tags["delete"])
        self._badge_id.set_active(tags["id"])
        self._update_counts()
        self._update_star_display()

        # Both sidebar and filmstrip called immediately
        self._refresh_sidebar(path)
        self._refresh_filmstrip(self.index)

    def _render_image(self, quality: int = Image.LANCZOS) -> None:
        if self._pil_image is None:
            return
        self._canvas.delete("all")
        cw = self._canvas_w or self._canvas.winfo_width()  or 1200
        ch = self._canvas_h or self._canvas.winfo_height() or 800

        presized = getattr(self, "_pil_image_presized", False)

        if presized and self._zoom == 1.0:
            # Fast path: worker already decoded and resized — just convert + draw
            img = self._pil_image
        else:
            # Full path: cap then resize (used for LANCZOS upgrade, zoom, or cache miss)
            img = self._pil_image
            iw, ih = img.size
            cap_w  = min(iw, cw * 2)
            cap_h  = min(ih, ch * 2)
            if cap_w < iw or cap_h < ih:
                img = img.copy()
                img.thumbnail((int(cap_w), int(cap_h)), Image.BILINEAR)
                iw, ih = img.size
            scale  = min(cw / iw, ch / ih) * self._zoom
            tw, th = max(1, int(iw * scale)), max(1, int(ih * scale))
            img    = img.resize((tw, th), quality)

        # Apply display transforms (exposure inspect, focus peaking, noise)
        if getattr(self, "_shadow_boost", 0) != 0:
            try:
                img = self._apply_exposure_transform(img)
            except Exception:
                pass
        if getattr(self, "_focus_peaking", False):
            try:
                img = self._apply_focus_peaking(img)
            except Exception:
                pass
        if getattr(self, "_noise_mode", False):
            try:
                img = self._apply_noise_visualiser(img)
            except Exception:
                pass

        self._tk_thumb = ImageTk.PhotoImage(img)
        self._canvas.create_image(
            cw / 2 + self._pan_x, ch / 2 + self._pan_y,
            image=self._tk_thumb, anchor="center")

        self._zoom_label.configure(text=f"{self._zoom * 100:.0f}%")

    # ── Burst UI ──────────────────────────────────────────────────────────────

    def _refresh_burst_ui(self, path: str) -> None:
        """Update burst label only. Filmstrip is now always shown (all images)."""
        if self._hash_ready:
            group = self._file_burst.get(path, [path])
            if len(group) > 1:
                pos = (group.index(path) + 1) if path in group else "?"
                self._burst_label.configure(text=f"Burst {pos}/{len(group)}")
                self._gallery_btn.configure(text_color=C_ACCENT)
                self._compare_btn.configure(text_color=C_ACCENT)
                return
        self._burst_label.configure(text="")
        self._gallery_btn.configure(text_color=C_TEXT)   # always active
        self._compare_btn.configure(text_color=C_MUTED)

    def _refresh_filmstrip(self, current_idx: int) -> None:
        """
        Draw a clean windowed filmstrip centred on current_idx.
        Cell width is computed to fill the full canvas width exactly.
        Design:
          - Dark background for all cells
          - Thumbnail centred and cropped to fill cell (no letterboxing)
          - Current image: 2px accent border, no background bleed
          - Tag stripe: 5px coloured bar at very bottom
          - Score badge: small semi-transparent pill top-left, only if scored
          - Unloaded cells: plain dark rectangle, no text
        """
        self._fs_tk_images.clear()
        self._filmstrip_canvas.delete("all")
        if hasattr(self, '_filmstrip_outer'):
            self._filmstrip_outer.grid()

        lo     = max(0, current_idx - FILMSTRIP_WINDOW)
        hi     = min(self.total - 1, current_idx + FILMSTRIP_WINDOW)
        all_indices = list(range(lo, hi + 1))

        # Only include indices that have a thumbnail available
        # Always include current image regardless
        window = [
            idx for idx in all_indices
            if idx == current_idx
            or self.files[idx] in self._preload_cache
        ]
        if not window:
            window = [current_idx]

        n = len(window)

        self._fs_tag_items.clear()
        self._fs_stripe_items.clear()
        self._fs_idx_to_slot: dict[int, int] = {}   # file_idx → slot position
        self._fs_window_lo    = window[0]
        self._fs_cell_w       = 0
        self._fs_cell_widths: list[int] = []
        self._fs_x_offsets:   list[int] = []

        th = FILMSTRIP_H - 6   # thumbnail height

        # Compute natural width per slot from each image's aspect ratio
        MIN_CW, MAX_CW = 60, 200
        cell_widths: list[int] = []
        for idx in window:
            p   = self.files[idx]
            img = self._preload_cache.get(p)
            if img is None and idx == current_idx and self._pil_image is not None:
                img = self._pil_image
            if img is not None:
                iw2, ih2 = img.size
                natural_w = int(iw2 / ih2 * th)
                cell_widths.append(max(MIN_CW, min(MAX_CW, natural_w)))
            else:
                cell_widths.append(MIN_CW)

        # x offsets from natural widths
        x_offsets: list[int] = []
        x = 0
        for cw_i in cell_widths:
            x_offsets.append(x)
            x += cw_i + 2
        total_w = x

        self._filmstrip_canvas.configure(
            scrollregion=(0, 0, total_w, FILMSTRIP_H),
            width=total_w)
        self._fs_cell_widths = cell_widths
        self._fs_x_offsets   = x_offsets

        for slot, idx in enumerate(window):
            self._fs_idx_to_slot[idx] = slot
            p          = self.files[idx]
            cw         = cell_widths[slot]
            x0         = x_offsets[slot]
            x1         = x0 + cw
            is_current = (idx == current_idx)
            tags       = self.status.get(p, {})
            in_compare = p in self._compare_set
            tag_color  = (C_COMPARE if in_compare        else
                          C_BEST    if tags.get("best")   else
                          C_DELETE  if tags.get("delete") else
                          C_ID      if tags.get("id")     else None)
            tag_str    = (("⊞" if in_compare            else "") +
                          ("★" if tags.get("best")       else "") +
                          ("✕" if tags.get("delete")     else "") +
                          ("⚑" if tags.get("id")         else ""))

            # ── Cell background ───────────────────────────────────────────────
            bg = "#1e2130" if is_current else C_BG
            self._filmstrip_canvas.create_rectangle(
                x0, 0, x1, FILMSTRIP_H, fill=bg, outline="")

            # ── Thumbnail ─────────────────────────────────────────────────────
            thumb_img = self._preload_cache.get(p)
            if thumb_img is None and is_current and self._pil_image is not None:
                thumb_img = self._pil_image

            if thumb_img is not None:
                try:
                    # Fit to exact cell size — no cropping needed since cw
                    # was computed from the image's own aspect ratio
                    resized = thumb_img.resize((cw, th), Image.BILINEAR)
                    tk_img  = ImageTk.PhotoImage(resized)
                    self._fs_tk_images.append(tk_img)
                    self._filmstrip_canvas.create_image(x0, 0, image=tk_img, anchor="nw")
                except Exception:
                    pass

            # ── Current image border ──────────────────────────────────────────
            if is_current:
                self._filmstrip_canvas.create_rectangle(
                    x0 + 1, 1, x1 - 1, th - 1,
                    fill="", outline=C_ACCENT, width=3)

            # ── Score badge (only if scored) ──────────────────────────────────
            sc = self._scores.get(p)
            bs = composite_score(sc)
            if bs > 0:
                # Small pill: dark background rectangle + coloured score text
                badge_text = f"{bs:.0f}"
                self._filmstrip_canvas.create_rectangle(
                    x0 + 2, 2, x0 + 22, 13,
                    fill="#000000", outline="", stipple="gray50")
                self._filmstrip_canvas.create_text(
                    x0 + 3, 3,
                    text=badge_text,
                    fill=_score_color(int(bs)),
                    font=("Helvetica", 7, "bold"), anchor="nw")

            # ── Tag stripe (5px at bottom) ────────────────────────────────────
            stripe_fill = tag_color if tag_color else "#1a1a1a"
            stripe_item = self._filmstrip_canvas.create_rectangle(
                x0, FILMSTRIP_H - 6, x1, FILMSTRIP_H,
                fill=stripe_fill, outline="")
            self._fs_stripe_items[slot] = stripe_item

            # Tag symbols centred on stripe
            tag_item = self._filmstrip_canvas.create_text(
                x0 + cw // 2, FILMSTRIP_H - 3,
                text=tag_str,
                fill="white", font=("Helvetica", 6, "bold"), anchor="center")
            self._fs_tag_items[slot] = tag_item

            # ── Separator line between cells ──────────────────────────────────
            if slot > 0:
                self._filmstrip_canvas.create_line(
                    x0, 0, x0, FILMSTRIP_H,
                    fill="#2a2d38", width=1)

            # ── Click to navigate ─────────────────────────────────────────────
            hit = self._filmstrip_canvas.create_rectangle(
                x0, 0, x1, FILMSTRIP_H, fill="", outline="")
            self._filmstrip_canvas.tag_bind(
                hit, "<Button-1>",
                lambda e, _i=idx: self._jump_to_index(_i))


    def _update_filmstrip_slot(self, file_idx: int) -> None:
        """Partial update — only tag stripe + text, no thumbnail redraw."""
        slot = getattr(self, "_fs_idx_to_slot", {}).get(file_idx)
        if slot is None:
            return

        p         = self.files[file_idx]
        tags      = self.status.get(p, {})
        tag_str   = (("★" if tags.get("best") else "") +
                     ("✕" if tags.get("delete") else "") +
                     ("⚑" if tags.get("id") else ""))
        in_compare = self.files[file_idx] in self._compare_set
        tag_color  = (C_COMPARE if in_compare           else
                      C_BEST    if tags.get("best")      else
                      C_DELETE  if tags.get("delete")    else
                      C_ID      if tags.get("id")        else "#2a2d38")
        tag_str    = (("⊞" if in_compare else "") + tag_str)

        tag_item = self._fs_tag_items.get(slot)
        if tag_item:
            self._filmstrip_canvas.itemconfig(tag_item, text=tag_str)

        stripe_item = self._fs_stripe_items.get(slot)
        if stripe_item:
            self._filmstrip_canvas.itemconfig(stripe_item, fill=tag_color)

    def _jump_to_index(self, idx: int) -> None:
        self.index = idx
        self._show_current()


    # ── Gallery / comparison ──────────────────────────────────────────────────

    def _get_current_burst(self) -> list[str]:
        path = self.files[self.index]
        return self._file_burst.get(path, [path])

    def _open_gallery(self) -> None:
        """Toggle inline gallery overlay — available immediately, no waiting."""
        self._show_overlay("gallery", self._build_gallery_overlay)

    def _build_gallery_overlay(self, frame: tk.Frame) -> None:
        view = InlineGalleryView(frame, self)
        view.pack(fill="both", expand=True)
        if not self._hash_ready:
            # Show subtle banner that scoring is still in progress
            banner = tk.Frame(frame, bg="#1a3a1a", height=24)
            banner.place(relx=0, rely=1.0, relwidth=1.0, anchor="sw")
            tk.Label(banner, text="⟳  Analysing images in background — scores update as they complete",
                     bg="#1a3a1a", fg="#88cc88",
                     font=("Helvetica", 8)).pack(side="left", padx=8)

    def _open_comparison(self) -> None:
        """Header button — same as C key."""
        self._toggle_compare()

    def _toggle_compare(self) -> None:
        """
        C key: open or close the compare view. Always does exactly this.
        If fewer than 2 images are selected, shows a helpful message instead.
        Never adds to the compare set — that is X's job exclusively.
        """
        if self._compare_mode:
            self._exit_compare_mode()
        elif len(self._compare_set) >= 2:
            self._enter_compare_mode()
        elif self._hash_ready and len(self._get_current_burst()) >= 2:
            # No compare set but in a burst — show burst comparison
            ComparisonView(self, self._get_current_burst())
        else:
            # Not enough images selected — show hint in title bar
            n = len(self._compare_set)
            if n == 0:
                self._title_label.configure(
                    text="Compare: press X on images to select them, then C to compare")
            else:
                self._title_label.configure(
                    text=f"1 image selected — press X on another image, then C to compare")
            self.after(4000, lambda: self._title_label.configure(
                text=Path(self.files[self.index]).name))

    # ── Zoom / pan ────────────────────────────────────────────────────────────

    def _toggle_focus_peaking(self) -> None:
        """Toggle focus peaking overlay — highlights sharpest edges in red."""
        self._focus_peaking = not self._focus_peaking
        self._set_tool_btn("_peak_btn", self._focus_peaking)
        self._render_image()

    def _cycle_exposure_inspect(self) -> None:
        """Cycle: normal → shadow boost → highlight inspect → normal."""
        self._shadow_boost = (self._shadow_boost + 1) % 3
        labels = {0: ("Expose",      C_BORDER,   C_MUTED),
                  1: ("☀ Shadows",   "#1a5fa0",  "white"),
                  2: ("◐ Highlights","#8b1a0a",  "white")}
        txt, bg, fg = labels[self._shadow_boost]
        if hasattr(self, "_expose_btn"):
            self._expose_btn.configure(text=txt, fg_color=bg, text_color=fg)
        self._render_image()

    def _apply_exposure_transform(self, img: Image.Image) -> Image.Image:
        """Apply shadow boost or highlight inspect to image for display."""
        if self._shadow_boost == 0:
            return img
        import numpy as np
        arr = np.asarray(img.convert("RGB"), dtype=np.float32)
        if self._shadow_boost == 1:  # shadow boost — lift darks
            arr = np.power(arr / 255.0, 0.4) * 255.0
        else:  # highlight inspect — crush brights, show clipping
            bright = arr > 230
            arr = arr * 0.3
            arr[bright] = 255   # clipped highlights go bright red
            # Tint clipped pixels red
            clip_mask = bright.any(axis=2)
            arr[clip_mask, 0] = 255
            arr[clip_mask, 1] = 0
            arr[clip_mask, 2] = 0
        arr = np.clip(arr, 0, 255).astype(np.uint8)
        return Image.fromarray(arr)

    def _apply_focus_peaking(self, img: Image.Image) -> Image.Image:
        """Overlay focus peaking edges on the image."""
        import numpy as np
        # Laplacian edge detection on luminance
        gray = np.asarray(img.convert("L"), dtype=np.float32)
        # Simple Laplacian kernel
        lap = np.abs(
            4*gray[1:-1, 1:-1]
            - gray[:-2, 1:-1] - gray[2:, 1:-1]
            - gray[1:-1, :-2] - gray[1:-1, 2:])
        # Threshold: top 5% of edges shown
        threshold = np.percentile(lap, 95)
        mask = lap > threshold
        # Overlay onto a copy of the image
        out = np.asarray(img.convert("RGB")).copy()
        # Pad mask to match image size
        padded = np.zeros(out.shape[:2], dtype=bool)
        padded[1:-1, 1:-1] = mask
        out[padded, 0] = 255   # red channel
        out[padded, 1] = int(out[padded, 1].mean() * 0.3) if padded.any() else 0
        out[padded, 2] = int(out[padded, 2].mean() * 0.3) if padded.any() else 0
        return Image.fromarray(out)

    def _set_tool_btn(self, attr: str, active: bool) -> None:
        """Update a display tool button label and colour to reflect state."""
        btn = getattr(self, attr, None)
        if btn is None:
            return
        on_lbl  = getattr(self, attr + "_on_label",  "ON")
        off_lbl = getattr(self, attr + "_off_label", "OFF")
        btn.configure(
            text=on_lbl if active else off_lbl,
            fg_color=C_ACCENT if active else C_BORDER,
            text_color="white" if active else C_MUTED)

    def _toggle_loupe(self, event=None) -> None:
        """Toggle loupe tool — magnified circle follows cursor."""
        self._loupe_active = not self._loupe_active
        if not self._loupe_active:
            self._canvas.delete("loupe")
            self._canvas.unbind("<Motion>")
        else:
            self._canvas.bind("<Motion>", self._draw_loupe)
        self._set_tool_btn("_loupe_btn", self._loupe_active)

    def _draw_loupe(self, event) -> None:
        """Draw a 3× magnified circle at the cursor position."""
        if self._pil_image is None or not self._loupe_active:
            return
        self._canvas.delete("loupe")
        LOUPE_R  = 120    # radius of loupe circle on screen
        ZOOM     = 3.0    # magnification factor
        cx, cy   = event.x, event.y
        cw = self._canvas_w or 1200
        ch = self._canvas_h or 800

        # Map canvas coords to image pixel coords
        iw, ih   = self._pil_image.size
        scale    = min(cw / iw, ch / ih) * self._zoom
        img_x    = (cx - cw/2 - self._pan_x) / scale + iw/2
        img_y    = (cy - ch/2 - self._pan_y) / scale + ih/2

        # Crop a region from the original image
        crop_r   = int(LOUPE_R / ZOOM)
        x0, y0   = max(0, int(img_x - crop_r)), max(0, int(img_y - crop_r))
        x1, y1   = min(iw, int(img_x + crop_r)), min(ih, int(img_y + crop_r))
        if x1 <= x0 or y1 <= y0:
            return
        crop = self._pil_image.crop((x0, y0, x1, y1))
        crop = crop.resize(
            (int((x1-x0)*ZOOM), int((y1-y0)*ZOOM)), Image.LANCZOS)

        # Convert to PhotoImage and display in bottom-right corner
        self._loupe_tk_img = ImageTk.PhotoImage(crop)
        lx = cw - LOUPE_R - 20
        ly = ch - LOUPE_R - 20
        # Draw white circle border then image
        self._canvas.create_oval(
            lx - LOUPE_R, ly - LOUPE_R,
            lx + LOUPE_R, ly + LOUPE_R,
            outline="white", width=2, tags="loupe")
        self._canvas.create_image(lx, ly, image=self._loupe_tk_img,
                                   anchor="center", tags="loupe")
        # Crosshair on main canvas at cursor
        self._canvas.create_line(cx-10, cy, cx+10, cy,
                                  fill="white", width=1, tags="loupe")
        self._canvas.create_line(cx, cy-10, cx, cy+10,
                                  fill="white", width=1, tags="loupe")

    def _toggle_noise_visualiser(self) -> None:
        """Toggle false-colour noise heat map overlay."""
        self._noise_mode = not self._noise_mode
        self._set_tool_btn("_noise_btn", self._noise_mode)
        self._render_image()

    def _apply_noise_visualiser(self, img: Image.Image) -> Image.Image:
        """
        False-colour noise heat map:
        Blue = low noise (clean), Yellow = moderate, Red = heavy noise.
        Uses local variance in 8×8 blocks.
        """
        try:
            import numpy as np
            gray = np.asarray(img.convert("L"), dtype=np.float32)
            h, w = gray.shape
            block = 8
            noise_map = np.zeros((h, w), dtype=np.float32)
            for y in range(0, h - block, block):
                for x in range(0, w - block, block):
                    patch  = gray[y:y+block, x:x+block]
                    var    = float(np.var(patch))
                    noise_map[y:y+block, x:x+block] = var
            # Normalise 0-1
            peak = noise_map.max() or 1
            noise_map /= peak
            # Map to RGB: blue→yellow→red
            r = np.clip(noise_map * 2, 0, 1)
            g = np.clip(1 - np.abs(noise_map - 0.5) * 2, 0, 1)
            b = np.clip(1 - noise_map * 2, 0, 1)
            rgb = (np.stack([r, g, b], axis=2) * 255).astype(np.uint8)
            heat = Image.fromarray(rgb).resize(img.size, Image.BILINEAR)
            # Blend 60% heat map over original
            return Image.blend(img.convert("RGB"), heat, 0.65)
        except Exception:
            return img

    def _zoom_to_100(self) -> None:
        """
        Toggle between fit-to-screen and 100% (1:1 pixel) zoom.
        At 100%, the image pixel maps 1:1 to screen pixels.
        Centres on the middle of the canvas.
        Second press returns to fit view.
        """
        if self._pil_image is None:
            return

        cw = self._canvas_w or self._canvas.winfo_width() or 1200
        ch = self._canvas_h or self._canvas.winfo_height() or 800
        iw, ih = self._pil_image.size

        # Compute what zoom level = fit-to-screen
        fit_zoom = min(cw / iw, ch / ih)

        # If already close to 100%, return to fit
        if abs(self._zoom - 1.0) < 0.05:
            self._zoom  = fit_zoom
            self._pan_x = 0.0
            self._pan_y = 0.0
            self._pil_image_presized = False
            self._render_image(quality=Image.LANCZOS)
            self._zoom_label.configure(text=f"{self._zoom * 100:.0f}%")
        else:
            # Jump to 100% — centre on image centre
            self._zoom  = 1.0
            self._pan_x = 0.0
            self._pan_y = 0.0
            self._pil_image_presized = False
            self._render_image(quality=Image.LANCZOS)
            self._zoom_label.configure(text="100%")

    def _on_scroll(self, event) -> None:
        up  = event.num == 4 or (hasattr(event, "delta") and event.delta > 0)
        fac = ZOOM_STEP if up else 1 / ZOOM_STEP
        new = max(ZOOM_MIN, min(ZOOM_MAX, self._zoom * fac))
        if new == self._zoom:
            return
        cw, ch = self._canvas.winfo_width(), self._canvas.winfo_height()
        mx, my = event.x - cw / 2, event.y - ch / 2
        ratio  = new / self._zoom
        self._pan_x = mx + (self._pan_x - mx) * ratio
        self._pan_y = my + (self._pan_y - my) * ratio
        self._zoom  = new
        self._render_image()

    def _pan_start_cb(self, event) -> None:
        self._pan_start = (event.x, event.y)

    def _pan_move_cb(self, event) -> None:
        if self._pan_start is None or self._zoom <= 1.0:
            return
        self._pan_x += event.x - self._pan_start[0]
        self._pan_y += event.y - self._pan_start[1]
        self._pan_start = (event.x, event.y)
        self._render_image()

    def _pan_end_cb(self, _event) -> None:
        self._pan_start = None

    # ── Sidebar refresh ───────────────────────────────────────────────────────

    def _refresh_sidebar(self, path: str) -> None:
        scores = self._scores.get(path)
        hist   = self._histograms.get(path)

        # Score on first view if not yet scored — deferred so image shows first
        if scores is None:
            self.after(100, lambda p=path: self._score_and_refresh(p))

        for metric, row in self._score_rows.items():
            row.set_score(scores.get(metric) if scores else None)
        self._histogram.set_data(hist)

        # EXIF — always refresh here using cached data
        if path not in self._exif_cache:
            self._exif_cache[path] = read_exif(path)
        exif = self._exif_cache[path]
        self._draw_exif(exif)
        self._update_location_map(exif)
        self._update_camera_bar(exif)
        if not self._rename_active:
            self._load_metadata_fields(path)

        # Duplicates
        self._dup_box.configure(state="normal")
        self._dup_box.delete("1.0", "end")
        if not self._hash_ready:
            self._dup_box.insert("end", "Scanning…")
        else:
            dups = self._dups.get(path, [])
            self._dup_box.insert(
                "end", "\n".join(Path(d).name for d in dups) if dups else "None detected")
        self._dup_box.configure(state="disabled")

    def _save_metadata(self, key: str) -> None:
        """Save metadata field to status and XMP sidecar."""
        if not self.files:
            return
        path = self.files[self.index]
        w    = getattr(self, f"_meta_{key}", None)
        if w is None:
            return
        val = w.get("1.0", "end-1c") if isinstance(w, tk.Text) else w.get()
        self.status[path][key] = val.strip()
        save_status(self.status_file, self.status)
        write_xmp_sidecar(path, self.status[path])

    def _load_metadata_fields(self, path: str) -> None:
        """Populate metadata fields from status for the current image."""
        tags = self.status.get(path, {})
        for key in ("species", "caption", "copyright", "location"):
            w = getattr(self, f"_meta_{key}", None)
            if w is None:
                continue
            val = tags.get(key, "")
            if isinstance(w, tk.Text):
                w.configure(state="normal")
                w.delete("1.0", "end")
                w.insert("end", val)
            else:
                w.delete(0, "end")
                w.insert(0, val)

    def _apply_copyright_to_best(self) -> None:
        """Copy copyright from current image to all Best-tagged images."""
        w = getattr(self, "_meta_copyright", None)
        if w is None:
            return
        val = w.get().strip() if isinstance(w, tk.Entry) else w.get("1.0","end-1c").strip()
        if not val:
            return
        count = 0
        for p, tags in self.status.items():
            if tags.get("best"):
                self.status[p]["copyright"] = val
                write_xmp_sidecar(p, self.status[p])
                count += 1
        save_status(self.status_file, self.status)
        self._title_label.configure(text=f"Copyright applied to {count} Best-tagged images")
        self.after(2500, lambda: self._title_label.configure(
            text=Path(self.files[self.index]).name))

    def _refresh_keyword_buttons(self) -> None:
        """Rebuild keyword palette buttons."""
        if not hasattr(self, "_kw_frame"):
            return
        for w in self._kw_frame.winfo_children():
            w.destroy()
        for kw in self._keywords:
            row = tk.Frame(self._kw_frame, bg=C_SURFACE)
            row.pack(fill="x", pady=1)
            tk.Button(row, text=kw, bg=C_BORDER, fg=C_TEXT,
                      font=("Helvetica", 9), relief="flat", bd=0, padx=6,
                      command=lambda k=kw: self._apply_keyword(k)
                      ).pack(side="left", fill="x", expand=True)
            tk.Button(row, text="✕", bg=C_SURFACE, fg=C_MUTED,
                      font=("Helvetica", 8), relief="flat", bd=0,
                      command=lambda k=kw: self._remove_keyword(k)
                      ).pack(side="right")

    def _apply_keyword(self, kw: str) -> None:
        """Toggle keyword on current image."""
        if not self.files:
            return
        path = self.files[self.index]
        kws  = list(self.status[path].get("keywords", []))
        if kw in kws:
            kws.remove(kw)
        else:
            kws.append(kw)
        self.status[path]["keywords"] = kws
        save_status(self.status_file, self.status)
        write_xmp_sidecar(path, self.status[path])

    def _add_keyword(self) -> None:
        """Add a new keyword to the palette."""
        kw = self._kw_entry.get().strip()
        if not kw or kw in self._keywords:
            return
        self._keywords.append(kw)
        self._app_settings["keywords"] = self._keywords
        save_app_settings(self._app_settings)
        self._kw_entry.delete(0, "end")
        self._refresh_keyword_buttons()

    def _remove_keyword(self, kw: str) -> None:
        if kw in self._keywords:
            self._keywords.remove(kw)
            self._app_settings["keywords"] = self._keywords
            save_app_settings(self._app_settings)
        self._refresh_keyword_buttons()

    def _update_location_map(self, exif: dict[str, str]) -> None:
        """Update the location map and grid reference from EXIF GPS data."""
        if not hasattr(self, "_map_canvas"):
            return
        lat_str = exif.get("GPSLatitude")
        lat_ref = exif.get("GPSLatitudeRef", "N")
        lng_str = exif.get("GPSLongitude")
        lng_ref = exif.get("GPSLongitudeRef", "E")

        if not (lat_str and lng_str):
            self._map_canvas.clear()
            self._grid_ref_label.configure(text="No GPS data")
            return

        try:
            def dms(s: str, ref: str) -> float:
                parts = [float(x.strip("() ")) for x in s.split(",")]
                dec = parts[0] + parts[1]/60 + (parts[2] if len(parts) > 2 else 0)/3600
                return -dec if ref in ("S", "W") else dec

            lat = dms(lat_str, lat_ref)
            lng = dms(lng_str, lng_ref)
            self._map_canvas.set_location(lat, lng)

            # OS grid reference for UK
            grid = _os_grid_ref(lat, lng)
            if grid:
                # Format as e.g. SU 913 413
                gr = f"{grid[:2]} {grid[2:5]} {grid[5:]}"
                self._grid_ref_label.configure(
                    text=f"OS Grid: {gr}", fg=C_TEXT)
            else:
                self._grid_ref_label.configure(
                    text=f"{abs(lat):.4f}°{'N' if lat>=0 else 'S'}  "
                         f"{abs(lng):.4f}°{'E' if lng>=0 else 'W'}",
                    fg=C_MUTED)
        except Exception as e:
            log.debug("Map update failed: %s", e)
            self._map_canvas.clear()
            self._grid_ref_label.configure(text="GPS parse error")

    def _draw_exif(self, exif: dict[str, str]) -> None:
        """Render formatted EXIF into the sidebar meta box."""
        rows = format_exif_display(exif)
        self._meta_box.configure(state="normal")
        self._meta_box.delete("1.0", "end")
        for label, value in rows:
            # Label in muted colour, value in text colour using tags
            self._meta_box.insert("end", f"{label:<12}", "lbl")
            self._meta_box.insert("end", f"  {value}\n", "val")
        self._meta_box.tag_configure("lbl", foreground=C_MUTED)
        self._meta_box.tag_configure("val", foreground=C_TEXT)
        self._meta_box.configure(state="disabled")

    def _score_and_refresh(self, path: str) -> None:
        """Score a single image and refresh scores + histogram if still viewing."""
        self._score_file(path)
        if self.files[self.index] == path:
            scores = self._scores.get(path)
            hist   = self._histograms.get(path)
            for metric, row in self._score_rows.items():
                row.set_score(scores.get(metric) if scores else None)
            self._histogram.set_data(hist)

    def _update_counts(self) -> None:
        self._count_best.set_value(sum(v["best"]   for v in self.status.values()))
        self._count_delete.set_value(sum(v["delete"] for v in self.status.values()))
        self._count_id.set_value(sum(v["id"]     for v in self.status.values()))

    # ── Keyboard ──────────────────────────────────────────────────────────────

    def _on_key(self, event) -> None:
        try:
            if "entry" in str(self.focus_get()).lower():
                return
        except Exception:
            pass
        ks = event.keysym
        # One key, one function — no context-dependent behaviour
        if   ks in ("Left", "4"):                         self._prev_image()
        elif ks in ("Right", "6"):                        self._next_image()
        elif ks in ("Return", "b", "B"):                  self._mark_best()
        elif ks in ("BackSpace", "d", "D"):               self._mark_delete()
        elif ks in ("i", "I", "plus", "KP_Add", "space"): self._mark_id()
        elif ks in ("x", "X"):                            self._mark_compare()
        elif ks in ("c", "C"):                            self._toggle_compare()
        elif ks in ("1","2","3","4","5") and not (event.state & 0x4):
            self._set_star(int(ks))
        elif ks == "0" and not (event.state & 0x4):
            self._set_star(self.status[self.files[self.index]].get("star", 0))  # toggle off
        elif ks in ("z", "Z") and not (event.state & 0x4): self._zoom_to_100()
        elif ks == "z" and event.state & 0x4:            self._undo_tag()       # Ctrl+Z
        elif ks in ("g", "G"):                            self._open_gallery()
        elif ks in ("f", "F"):                            self._toggle_focus_peaking()
        elif ks in ("l", "L"):                            self._toggle_loupe()
        elif ks in ("n", "N"):                            self._toggle_noise_visualiser()
        elif ks in ("h", "H") and not (event.state & 0x4):
            self._cycle_exposure_inspect()
        elif ks in ("question", "slash"):                 self._toggle_shortcuts_overlay()
        elif ks == "Escape":
            # Escape only resets zoom — use ⛶ button for fullscreen
            if self._zoom > 1.01:
                self._zoom = 1.0; self._pan_x = 0.0; self._pan_y = 0.0
                self._render_image()
        elif ks in ("q", "Q"):                            self._quit()

    # ── Navigation ────────────────────────────────────────────────────────────

    def _prev_image(self) -> None:
        if self.index > 0:
            self.index -= 1
            self._show_current()

    def _next_image(self) -> None:
        if self.index < self.total - 1:
            self.index += 1
            self._show_current()

    # ── Tagging ───────────────────────────────────────────────────────────────

    _tag_count = 0

    def _toggle_tag(self, tag: str) -> None:
        path    = self.files[self.index]
        old_val = self.status[path][tag]
        self.status[path][tag] = not old_val
        self._undo_stack.append((path, tag, old_val))
        if len(self._undo_stack) > 50:
            self._undo_stack.pop(0)
        save_status(self.status_file, self.status)
        write_xmp_sidecar(path, self.status[path])
        self._update_tags_only()
        PhotoReviewer._tag_count += 1
        if PhotoReviewer._tag_count % 10 == 0:
            self._suggest_threshold_calibration()

    def _undo_tag(self) -> None:
        if not self._undo_stack:
            return
        path, tag, old_val = self._undo_stack.pop()
        self.status[path][tag] = old_val
        save_status(self.status_file, self.status)
        if path == self.files[self.index]:
            self._update_tags_only()
        else:
            # Undo was for a different image — just refresh counts + filmstrip
            self._update_counts()
            self._refresh_filmstrip(self.index)

    def _update_tags_only(self) -> None:
        """
        Fast path after a tag toggle — updates badges, counts, canvas flash,
        and filmstrip tag overlay without touching the image or preloader.
        """
        path = self.files[self.index]
        tags = self.status[path]
        self._badge_best.set_active(tags["best"])
        self._badge_delete.set_active(tags["delete"])
        self._badge_id.set_active(tags["id"])
        self._update_counts()
        # Flash canvas border
        color = (C_BEST   if tags["best"]   else
                 C_DELETE if tags["delete"] else
                 C_ID     if tags["id"]     else C_BG)
        self._flash_canvas(color)
        self._update_filmstrip_slot(self.index)

    def _flash_canvas(self, color: str) -> None:
        """Briefly tint the canvas border to confirm a tag action."""
        self._canvas.configure(highlightthickness=4, highlightbackground=color)
        self.after(220, lambda: self._canvas.configure(highlightthickness=0))

    def _mark_best(self)   -> None: self._toggle_tag("best")
    def _mark_delete(self) -> None:
        self._toggle_tag("delete")
        if self._auto_advance and self.status[self.files[self.index]].get("delete"):
            self._flash_canvas(C_DELETE)
            self.after(120, lambda: self._next_pass_image(1))
    def _mark_id(self)     -> None: self._toggle_tag("id")

    def _set_star(self, n: int) -> None:
        """Set star rating 1-5; calling with current rating clears it (toggle)."""
        path = self.files[self.index]
        cur  = self.status[path].get("star", 0)
        self.status[path]["star"] = 0 if cur == n else n
        save_status(self.status_file, self.status)
        self._update_star_display()
        self._update_filmstrip_slot(self.index)
        self._update_counts()

    def _update_star_display(self, hover: int = 0) -> None:
        """Update star label to reflect current rating (or hover preview)."""
        if not hasattr(self, "_star_lbl"):
            return
        path = self.files[self.index] if self.files else None
        cur  = self.status.get(path, {}).get("star", 0) if path else 0
        n    = hover if hover else cur
        col  = STAR_COLORS.get(cur, "#555") if cur else "#555"
        hcol = STAR_COLORS.get(hover, C_STAR) if hover else col
        display = ("★" * n + "☆" * (5 - n)) if hover else                   ("★" * cur + "☆" * (5 - cur))
        self._star_lbl.configure(
            text=display,
            fg=hcol if hover else col)

    def _star_click(self, event) -> None:
        """Click star label — compute which star was clicked."""
        w    = event.widget.winfo_width()
        star = max(1, min(5, int(event.x / (w / 5)) + 1))
        self._set_star(star)

    def _star_hover(self, event) -> None:
        """Preview stars on hover."""
        w    = event.widget.winfo_width()
        star = max(1, min(5, int(event.x / (w / 5)) + 1))
        self._update_star_display(hover=star)

    def _mark_compare(self) -> None:
        """
        Toggle the current image in/out of the compare set (max 4).
        Does NOT automatically enter compare mode — user presses C explicitly.
        If already in compare mode, rebuilds panels to reflect the change.
        """
        path = self.files[self.index]
        if path in self._compare_set:
            self._compare_set.remove(path)
        else:
            if len(self._compare_set) >= 4:
                self._compare_set.pop(0)
            self._compare_set.append(path)
        # Flash orange to confirm
        self._flash_canvas(C_COMPARE if path in self._compare_set else C_BG)
        self._update_filmstrip_slot(self.index)
        # Update title to show compare count briefly
        n = len(self._compare_set)
        if n == 0:
            self._title_label.configure(text=Path(self.files[self.index]).name)
        else:
            self._title_label.configure(text=f"{n} image{'s' if n>1 else ''} in compare set  —  C to view")
            self.after(2500, lambda: self._title_label.configure(
                text=Path(self.files[self.index]).name))
        # If already in compare mode, refresh panels live
        if self._compare_mode:
            if len(self._compare_set) >= 2:
                self._build_compare_panels()
            else:
                self._exit_compare_mode()

    # ── Generic overlay system ───────────────────────────────────────────────

    def _show_overlay(self, name: str, builder) -> None:
        """
        Show a full-canvas overlay view. If the same overlay is already
        showing, hide it (toggle).
        The overlay frame spans all three rows of the area frame (canvas,
        filmstrip) so it covers everything completely.
        """
        if self._active_overlay == name:
            self._hide_overlay()
            return
        for w in self._overlay_frame.winfo_children():
            w.destroy()
        self._active_overlay = name
        # Span all rows of area so filmstrip is covered
        # overlay is a child of area, so coords are local to area
        self._overlay_frame.grid(row=0, column=0,
                                  rowspan=3, sticky="nsew")
        self._overlay_frame.tkraise()
        builder(self._overlay_frame)
        self._update_header_buttons()

    def _hide_overlay(self) -> None:
        """Hide any active overlay and return to normal image view."""
        if self._active_overlay is None:
            return
        self._active_overlay = None
        self._overlay_frame.grid_remove()
        for w in self._overlay_frame.winfo_children():
            w.destroy()
        if not self._compare_mode:
            self._canvas.grid(row=0, column=0, sticky="nsew")
        self._update_header_buttons()
        self._show_current()

    def _update_header_buttons(self) -> None:
        """Highlight active overlay button; highlight Viewer when in normal view."""
        active = self._active_overlay
        for btn, name in getattr(self, "_header_toggle_btns", {}).items():
            try:
                btn.configure(
                    fg_color=C_ACCENT if active == name else "transparent",
                    text_color="white" if active == name else C_MUTED)
            except Exception:
                pass
        # Viewer button highlighted when no overlay is active
        if hasattr(self, "_viewer_btn"):
            if active is None and not self._compare_mode:
                self._viewer_btn.configure(fg_color=C_ACCENT, text_color="white")
            else:
                self._viewer_btn.configure(fg_color="transparent", text_color=C_MUTED)

    def _suggest_threshold_calibration(self) -> None:
        """
        After enough tagging history, suggest a calibrated threshold.
        Computes average composite score of Best vs Delete images.
        Called after every 10th tag action.
        """
        best_scores   = [composite_score(self._scores.get(p))
                         for p, t in self.status.items()
                         if t.get("best") and self._scores.get(p)]
        delete_scores = [composite_score(self._scores.get(p))
                         for p, t in self.status.items()
                         if t.get("delete") and self._scores.get(p)]
        if len(best_scores) < 5 or len(delete_scores) < 5:
            return
        avg_best   = sum(best_scores)   / len(best_scores)
        avg_delete = sum(delete_scores) / len(delete_scores)
        suggested  = int((avg_best + avg_delete) / 2)
        self._title_label.configure(
            text=f"💡 Your Best avg: {avg_best:.0f}  Delete avg: {avg_delete:.0f}"
                 f"  →  Suggested threshold: {suggested}")
        self.after(5000, lambda: self._title_label.configure(
            text=Path(self.files[self.index]).name))

    # ── Multi-pass culling ───────────────────────────────────────────────────

    def _cycle_pass(self) -> None:
        """Cycle through pass modes: All → Not-deleted → Best only → All."""
        if not self._multi_pass_on:
            self._multi_pass_on = True
            self._pass_num = 1
        else:
            self._pass_num = (self._pass_num % 3) + 1
            if self._pass_num == 1:
                self._multi_pass_on = False
        self._update_pass_display()
        # Jump to first valid image in this pass
        for idx in range(self.total):
            if self._pass_allows(idx):
                self._jump_to_index(idx)
                return

    def _pass_allows(self, idx: int) -> bool:
        """Return True if the image at idx is shown in current pass."""
        if not self._multi_pass_on:
            return True
        tags = self.status.get(self.files[idx], {})
        if self._pass_num == 2:   # survivors: not marked delete
            return not tags.get("delete", False)
        if self._pass_num == 3:   # best only
            return bool(tags.get("best", False))
        return True

    def _update_pass_display(self) -> None:
        if not hasattr(self, "_pass_btn"):
            return
        if not self._multi_pass_on:
            self._pass_btn.configure(
                text="⊏ Pass", fg_color=C_BORDER, text_color=C_MUTED)
            return
        count = sum(1 for i in range(self.total) if self._pass_allows(i))
        labels = {1: ("P1·All", C_MUTED),
                  2: ("P2·Surv", C_ID),
                  3: ("P3·Best", C_BEST)}
        txt, col = labels.get(self._pass_num, ("Pass?", C_MUTED))
        self._pass_btn.configure(
            text=f"{txt} {count}", fg_color=col, text_color="white",
            width=80)

    def _toggle_auto_advance(self) -> None:
        """Toggle auto-advance on delete."""
        self._auto_advance = not self._auto_advance
        if hasattr(self, "_adv_btn_lbl"):
            if self._auto_advance:
                self._adv_btn_lbl.configure(
                    text="Auto→ ON", bg="#166534", fg="white")
            else:
                self._adv_btn_lbl.configure(
                    text="Auto→ OFF", bg="#3a1a1a", fg="#ef4444")

    def _next_pass_image(self, direction: int = 1) -> None:
        """Navigate to next/prev image allowed by current pass."""
        idx = self.index + direction
        while 0 <= idx < self.total:
            if self._pass_allows(idx):
                self._jump_to_index(idx)
                return
            idx += direction

    def _jump_to_next_unreviewed(self) -> None:
        """Jump to first image with no tags of any kind."""
        for idx in range(self.index + 1, self.total):
            path = self.files[idx]
            tags = self.status.get(path, {})
            if not any(tags.values()):
                self._jump_to_index(idx)
                return
        # Wrap around from start
        for idx in range(0, self.index):
            path = self.files[idx]
            tags = self.status.get(path, {})
            if not any(tags.values()):
                self._jump_to_index(idx)
                return
        self._title_label.configure(text="✓ All images have been reviewed")
        self.after(2500, lambda: self._title_label.configure(
            text=Path(self.files[self.index]).name))

    def _send_to_topaz(self) -> None:
        """Open current image (or all Best-tagged) in Topaz Photo AI."""
        topaz_exe = self._app_settings.get("topaz_path", TOPAZ_DEFAULT_PATH)
        if not os.path.exists(topaz_exe):
            # Let user locate Topaz
            path = fd.askopenfilename(
                title="Locate Topaz Photo AI executable",
                filetypes=[("Executable", "*.exe"), ("All files", "*.*")])
            if not path:
                return
            topaz_exe = path
            self._app_settings["topaz_path"] = topaz_exe
            save_app_settings(self._app_settings)

        # Ask: current image or all Best?
        # Ask: current image or all Best?
        choice = mb.askyesnocancel(
            "Send to Topaz",
            "Yes = send current image\n"
            "No  = send all Best-tagged images\n"
            "Cancel = abort")
        if choice is None:
            return
        if choice:
            files = [self.files[self.index]]
        else:
            files = [p for p, t in self.status.items() if t.get("best")]
        if not files:
            mb.showinfo("No files", "No Best-tagged images found.")
            return
        import subprocess
        try:
            subprocess.Popen([topaz_exe] + files)
        except Exception as e:
            mb.showerror("Topaz Error", f"Could not launch Topaz:\n{e}")
    def _open_general_compare(self) -> None:
        """Enter inline compare mode — splits the canvas into side-by-side panels."""
        self._enter_compare_mode()

    def _toggle_compare(self) -> None:
        """
        C key — one function only: open or close the compare view.
        Never adds images to the set (that is X's job).
        Shows a brief hint if fewer than 2 images are in the set.
        """
        if self._compare_mode:
            # C exits compare mode
            self._exit_compare_mode()
        elif len(self._compare_set) >= 2:
            # C opens compare mode
            self._enter_compare_mode()
        else:
            # Not enough images — tell the user what to do
            n = len(self._compare_set)
            need = 2 - n
            msg = (f"Add {need} more image{'s' if need > 1 else ''} to compare set first  "
                   f"(press X on any image to add it)")
            self._title_label.configure(text=msg)
            self.after(3000, lambda: self._title_label.configure(
                text=Path(self.files[self.index]).name))

    def _enter_compare_mode(self) -> None:
        """Replace the canvas with an inline split view of compared images."""
        if not self._compare_set:
            return
        self._compare_mode = True
        # Hide the image canvas, show compare frame in its place
        self._canvas.grid_remove()
        self._compare_frame.grid(row=0, column=0, sticky="nsew")
        self._build_compare_panels()
        # Update header to show compare mode
        self._title_label.configure(
            text=f"COMPARE  ({len(self._compare_set)} images)  —  X to add/remove  ·  C to close")

    def _exit_compare_mode(self) -> None:
        """Return to single-image view."""
        self._compare_mode = False
        self._compare_frame.grid_remove()
        self._canvas.grid(row=0, column=0, sticky="nsew")
        for w in self._compare_frame.winfo_children():
            w.destroy()
        self._compare_tk_images.clear()
        self._show_current()

    def _build_compare_panels(self) -> None:
        """Build the inline split view panels."""
        for w in self._compare_frame.winfo_children():
            w.destroy()
        self._compare_tk_images.clear()

        n = len(self._compare_set)
        cols = min(n, 2) if n <= 2 else (3 if n == 3 else 2)
        rows = 1 if n <= 3 else 2

        self._compare_frame.columnconfigure(list(range(cols)), weight=1)
        self._compare_frame.rowconfigure(list(range(rows)), weight=1)

        for idx, path in enumerate(self._compare_set):
            col = idx % cols
            row = idx // cols
            panel = self._make_compare_panel(path)
            panel.grid(row=row, column=col, sticky="nsew", padx=2, pady=2)


    def _make_compare_panel(self, path: str) -> tk.Frame:
        """Build one panel in the inline compare view."""
        panel = tk.Frame(self._compare_frame, bg=C_BG)

        # Info bar at top
        bar = tk.Frame(panel, bg=C_SURFACE, height=36)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        tags   = self.status.get(path, {})
        scores = self._scores.get(path)
        bs     = composite_score(scores)

        # Tag border colour
        border = (C_BEST   if tags.get("best")   else
                  C_DELETE if tags.get("delete") else
                  C_ID     if tags.get("id")     else C_BORDER)
        panel.configure(highlightthickness=2, highlightbackground=border)

        # Filename
        tk.Label(bar, text=Path(path).name,
                 bg=C_SURFACE, fg=C_TEXT,
                 font=("Helvetica", 9, "bold"),
                 anchor="w").pack(side="left", padx=8, pady=8)

        # Score
        if bs > 0:
            tk.Label(bar, text=f"▣ {bs:.0f}",
                     bg=C_SURFACE, fg=_score_color(int(bs)),
                     font=("Helvetica", 9, "bold")).pack(side="left", padx=4)

        # Tag buttons
        btn_row = tk.Frame(bar, bg=C_SURFACE)
        btn_row.pack(side="right", padx=6)

        for sym, tag, col in [("★","best",C_BEST),("✕","delete",C_DELETE),("⚑","id",C_ID)]:
            tk.Button(btn_row, text=sym, bg=col, fg="white",
                      font=("Helvetica", 9, "bold"), relief="flat", bd=0, padx=6,
                      command=lambda p=path, t=tag: self._compare_toggle_tag(p, t)
                      ).pack(side="left", padx=2, pady=6)

        # Remove from compare
        tk.Button(btn_row, text="⊠", bg=C_COMPARE, fg="white",
                  font=("Helvetica", 9, "bold"), relief="flat", bd=0, padx=6,
                  command=lambda p=path: self._compare_remove(p)
                  ).pack(side="left", padx=2, pady=6)

        # Image canvas
        canvas = tk.Canvas(panel, bg="#050505", highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        canvas.bind("<Configure>", lambda e, p=path, c=canvas: self._render_compare_image(p, c))

        # Click to jump to image
        canvas.bind("<Button-1>", lambda e, p=path: self._jump_and_exit_compare(p))

        return panel

    def _render_compare_image(self, path: str, canvas: tk.Canvas) -> None:
        """Render image into a compare panel canvas."""
        canvas.unbind("<Configure>")
        try:
            img = self._preload_cache.get(path) or _extract_thumb_img(path)
            cw  = canvas.winfo_width()
            ch  = canvas.winfo_height()
            if cw < 10 or ch < 10:
                return
            iw, ih = img.size
            scale  = min(cw / iw, ch / ih)
            tw, th = max(1, int(iw * scale)), max(1, int(ih * scale))
            resized = img.resize((tw, th), Image.LANCZOS)
            tk_img  = ImageTk.PhotoImage(resized)
            self._compare_tk_images.append(tk_img)
            canvas.create_image(cw // 2, ch // 2, image=tk_img, anchor="center")
        except Exception as e:
            log.warning("Compare render failed %s: %s", path, e)

    def _compare_toggle_tag(self, path: str, tag: str) -> None:
        """Tag an image from within compare mode, rebuild panels to reflect."""
        self.status[path][tag] = not self.status[path][tag]
        save_status(self.status_file, self.status)
        self._update_counts()
        if path in self.files:
            self._update_filmstrip_slot(self.files.index(path))
        # Rebuild panels to show updated border colour
        self._build_compare_panels()

    def _compare_remove(self, path: str) -> None:
        """Remove image from compare set and rebuild."""
        if path in self._compare_set:
            self._compare_set.remove(path)
        if len(self._compare_set) < 2:
            self._exit_compare_mode()
        else:
            self._build_compare_panels()
        self._refresh_filmstrip(self.index)

    def _jump_and_exit_compare(self, path: str) -> None:
        """Click on a compare panel image — jump to it and exit compare mode."""
        self._exit_compare_mode()
        if path in self.files:
            self._jump_to_index(self.files.index(path))

    # ── Search / browse ───────────────────────────────────────────────────────

    def _perform_search(self) -> None:
        query = self._search_entry.get().strip().lower()
        if not query:
            return
        for i, p in enumerate(self.files):
            if query in Path(p).name.lower():
                self.index = i
                self._search_entry.delete(0, "end")
                self._show_current()
                return
        mb.showinfo("Not Found", f"No filename containing '{query}'")

    def _browse_images(self) -> None:
        win = ctk.CTkToplevel(self)
        win.title("Browse Images")
        win.geometry("480x600")
        win.configure(fg_color=C_SURFACE)
        win.grab_set()

        ctk.CTkLabel(win, text="Jump to image",
                     font=ctk.CTkFont("Helvetica", 13, "bold"),
                     text_color=C_TEXT).pack(padx=16, pady=(16, 4), anchor="w")

        filter_var = tk.StringVar()
        ctk.CTkEntry(win, textvariable=filter_var,
                     placeholder_text="Filter filenames…",
                     font=ctk.CTkFont("Helvetica", 11)).pack(fill="x", padx=16, pady=(0, 8))

        list_frame = tk.Frame(win, bg=C_BG)
        list_frame.pack(fill="both", expand=True, padx=16, pady=(0, 8))
        sb2 = tk.Scrollbar(list_frame)
        sb2.pack(side="right", fill="y")
        listbox = tk.Listbox(list_frame, yscrollcommand=sb2.set,
                             bg=C_BG, fg=C_TEXT, selectbackground=C_ACCENT,
                             font=("Helvetica", 10), bd=0, highlightthickness=0,
                             activestyle="none")
        listbox.pack(side="left", fill="both", expand=True)
        sb2.config(command=listbox.yview)

        names  = [Path(p).name for p in self.files]
        shown: list[int] = []

        def populate(q: str = "") -> None:
            listbox.delete(0, "end")
            shown.clear()
            for idx, name in enumerate(names):
                if q.lower() in name.lower():
                    t       = self.status[self.files[idx]]
                    tags    = ("★" if t["best"] else "") + ("✕" if t["delete"] else "") + ("⚑" if t["id"] else "")
                    marker  = " ◀" if idx == self.index else ""
                    listbox.insert("end", f"  {idx+1:>4}.  {name}  {tags}{marker}")
                    shown.append(idx)

        populate()
        filter_var.trace_add("write", lambda *_: populate(filter_var.get()))

        for li, fi in enumerate(shown):
            if fi == self.index:
                listbox.selection_set(li); listbox.see(li); break

        def jump(_event=None) -> None:
            sel = listbox.curselection()
            if not sel:
                return
            self.index = shown[sel[0]]
            win.destroy()
            self._show_current()

        listbox.bind("<Double-Button-1>", jump)
        listbox.bind("<Return>", jump)
        ctk.CTkButton(win, text="Jump to selected",
                      fg_color=C_ACCENT, hover_color="#7C3AED",
                      font=ctk.CTkFont("Helvetica", 12, "bold"),
                      command=jump).pack(fill="x", padx=16, pady=(0, 16))

    # ── Processing ────────────────────────────────────────────────────────────

    def _process(self) -> None:
        to_best   = [p for p, t in self.status.items() if t["best"]   and os.path.exists(p)]
        to_id     = [p for p, t in self.status.items() if t["id"]     and os.path.exists(p)]
        to_delete = [p for p, t in self.status.items() if t["delete"] and os.path.exists(p)]

        if not any([to_best, to_id, to_delete]):
            mb.showinfo("Nothing to do", "No images are tagged for processing.")
            return
        if not mb.askyesno("Confirm Process",
                           f"Ready to process:\n\n"
                           f"  Copy to Best/    : {len(to_best)} file(s)\n"
                           f"  Convert to ID/   : {len(to_id)} file(s)  (JPEG)\n"
                           f"  Send to trash    : {len(to_delete)} file(s)\n\nContinue?"):
            return

        best_dir = os.path.join(self.base_dir, "Best")
        id_dir   = os.path.join(self.base_dir, "ID")
        os.makedirs(best_dir, exist_ok=True)
        os.makedirs(id_dir,   exist_ok=True)

        copied, converted, trashed, errors = [], [], [], []

        for p in to_best:
            try:
                shutil.copy2(p, os.path.join(best_dir, Path(p).name))
                copied.append(Path(p).name)
            except Exception as e:
                log.error("Copy failed %s: %s", p, e)
                errors.append(Path(p).name)

        for p in to_id:
            out = convert_to_jpeg(p, id_dir)
            (converted if out else errors).append(Path(out or p).name)

        for p in to_delete:
            try:
                send2trash(p)
                trashed.append(Path(p).name)
            except Exception as e:
                log.error("Trash failed %s: %s", p, e)
                errors.append(Path(p).name)

        for p in set(to_best + to_id + to_delete):
            self.status.pop(p, None)
            self._scores.pop(p, None)
            self._histograms.pop(p, None)
        save_status(self.status_file, self.status)
        save_score_cache(self.score_file, self._scores)

        report = "Process complete.\n\n"
        if copied:    report += f"Copied to Best/    : {len(copied)}\n"
        if converted: report += f"Converted to ID/   : {len(converted)}\n"
        if trashed:   report += f"Sent to trash      : {len(trashed)}\n"
        if errors:    report += "\nErrors:\n" + "\n".join(f"  {e}" for e in errors)
        mb.showinfo("Done", report)
        self.destroy()

    # ── Fullscreen ────────────────────────────────────────────────────────────

    def _go_fullscreen(self) -> None:
        self._is_fullscreen = True
        self.deiconify()          # ensure window is visible
        self.update_idletasks()   # flush pending geometry events
        if platform.system() == "Windows":
            self.state("zoomed")
        else:
            self.attributes("-fullscreen", True)
        # Show first image after fullscreen geometry is applied
        self.after(100, self._show_current)

    def _toggle_fullscreen(self) -> None:
        if getattr(self, "_is_fullscreen", False):
            self._is_fullscreen = False
            sys = platform.system()
            if sys == "Windows":
                self.state("normal")
                sw = self.winfo_screenwidth()
                sh = self.winfo_screenheight()
                w, h = int(sw * 0.85), int(sh * 0.85)
                self.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
            else:
                self.attributes("-fullscreen", False)
        else:
            self._go_fullscreen()

    def _init_session(self, base_dir: str, files: list[str]) -> None:
        """Reinitialise session with a new folder — used by folder tree."""
        self.base_dir    = base_dir
        self.files       = files
        self.total       = len(files)
        self.index       = 0
        self.status_file = str(Path(base_dir) / "photo_reviewer_status.json")
        self.status      = {
            p: dict(best=False, delete=False, id=False, star=0,
                    keywords=[], species="", caption="",
                    copyright=self._app_settings.get("default_copyright",""),
                    location="")
            for p in files}
        loaded = load_status(self.status_file, set(files))
        for path, tags in loaded.items():
            if path in self.status:
                self.status[path].update(tags)
        self._scores.clear()
        self._histograms.clear()
        self._exif_cache.clear()
        self._preload_cache.clear()
        self._hashes.clear()
        self._undo_stack.clear()
        self._compare_set.clear()
        self._hash_ready = False
        self._hide_overlay()
        self._start_hash_workers()
        self._show_current()
        self._update_counts()
        self.title(f"📷 Photo Reviewer — {Path(base_dir).name}")

    def _quit(self) -> None:
        self.destroy()

    # ── Shortcuts overlay ─────────────────────────────────────────────────────

    def _toggle_shortcuts_overlay(self) -> None:
        """Show or hide the keyboard shortcuts reference overlay."""
        if hasattr(self, "_shortcuts_overlay") and self._shortcuts_overlay:
            self._shortcuts_overlay.destroy()
            self._shortcuts_overlay = None
        else:
            self._shortcuts_overlay = self._build_shortcuts_overlay()

    def _build_shortcuts_overlay(self) -> tk.Toplevel:
        """
        Modal overlay listing all keyboard shortcuts.
        Semi-transparent dark panel centred on the main window.
        Dismissed by Escape, ?, or clicking outside.
        """
        ov = tk.Toplevel(self)
        ov.title("")
        ov.configure(bg="#050505")
        ov.resizable(False, False)
        ov.transient(self)
        ov.grab_set()

        # Centre on main window
        self.update_idletasks()
        mw = self.winfo_width()
        mh = self.winfo_height()
        mx = self.winfo_rootx()
        my = self.winfo_rooty()
        w, h = 680, 540
        ov.geometry(f"{w}x{h}+{mx + (mw-w)//2}+{my + (mh-h)//2}")

        ov.bind("<Escape>",    lambda e: ov.destroy())
        ov.bind("<question>",  lambda e: ov.destroy())
        ov.bind("<Button-1>",  lambda e: None)  # absorb clicks
        self.bind("<Escape>",  lambda e: ov.destroy(), add="+")

        # Header
        hdr = tk.Frame(ov, bg="#111111", height=44)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Label(hdr, text="⌨  Keyboard Shortcuts",
                 bg="#111111", fg="#E8EAF0",
                 font=("Helvetica", 13, "bold")).pack(side="left", padx=16, pady=10)
        tk.Button(hdr, text="✕", bg="#111111", fg="#6B7280",
                  font=("Helvetica", 12), relief="flat", bd=0,
                  command=ov.destroy).pack(side="right", padx=12)

        # Shortcut data — (key, description, category)
        shortcuts = [
            # Navigation
            ("← / →",           "Previous / Next image",                 "Navigation"),
            ("Numpad 4 / 6",    "Previous / Next image",                 "Navigation"),
            ("Tab",             "Jump to next unreviewed image",          "Navigation"),
            ("G  / Gallery ⊞",  "Open/close gallery",                    "Navigation"),
            ("Z  / 1:1 button", "Toggle 1:1 zoom",                       "Navigation"),
            ("Scroll wheel",    "Zoom in / out",                         "Navigation"),
            ("Drag",            "Pan when zoomed",                       "Navigation"),
            # Tagging
            ("B / Enter",       "Tag as Best",                           "Tagging"),
            ("D / Backspace",   "Tag as Delete (+ auto-advance if on)",  "Tagging"),
            ("I / Space / +",   "Tag as ID",                            "Tagging"),
            ("Ctrl + Z",        "Undo last tag",                         "Tagging"),
            ("1 – 5",           "Set star rating",                       "Tagging"),
            ("0",               "Clear star rating",                     "Tagging"),
            # Compare
            ("X  / Compare ⊞", "Add/remove from compare set",           "Compare"),
            ("C  / Compare ⊟", "Open/close compare view (2+ images)",   "Compare"),
            # Views
            ("⚡ Clean Load",   "Score images, show low scorers",        "Views"),
            ("🔬 Detail Clean", "Full RAW score + results",              "Views"),
            ("⊹ Bursts",        "Step through burst groups",             "Views"),
            ("🔍 Species",      "Batch iNaturalist species ID",           "Views"),
            ("← Back button",  "Close any open view",                   "Views"),
            # Display tools
            ("F  / ⬡ Peak",     "Toggle focus peaking overlay",          "Display"),
            ("H  / ◑ Expose",   "Cycle: normal / shadows / highlights",  "Display"),
            ("L  / ⊙ Loupe",    "Toggle loupe magnifier (move cursor)",  "Display"),
            ("N  / ≋ Noise",    "Toggle noise heat map",                 "Display"),
            ("?",               "Show / hide this shortcuts panel",      "Display"),
            ("Escape",          "Reset zoom to fit",                     "Display"),
            ("⛶ button",        "Toggle fullscreen",                     "Display"),
            # Multi-pass
            ("Pass button",     "Cycle pass: All→Survivors→Best",        "Multi-pass"),
            ("Auto→ button",    "Toggle auto-advance on Delete",         "Multi-pass"),
            # Folder tree
            ("◀/▶ button",      "Collapse/expand folder tree",           "Folders"),
            # Rename
            ("Name field",      "Edit filename (Enter to confirm)",      "Rename"),
            ("Escape",          "Cancel rename",                         "Rename"),
            # Send to Topaz
            ("→ Topaz",         "Send current/Best images to Topaz AI", "Export"),
            ("Q",               "Quit application",                      "App"),
        ]

        # Group by category
        from collections import OrderedDict
        cats: dict = OrderedDict()
        for key, desc, cat in shortcuts:
            cats.setdefault(cat, []).append((key, desc))

        body = tk.Frame(ov, bg="#050505")
        body.pack(fill="both", expand=True, padx=16, pady=12)

        # Two-column layout of categories
        cat_list = list(cats.items())
        left_cats  = cat_list[:len(cat_list)//2 + len(cat_list)%2]
        right_cats = cat_list[len(cat_list)//2 + len(cat_list)%2:]

        left_frame  = tk.Frame(body, bg="#050505")
        right_frame = tk.Frame(body, bg="#050505")
        left_frame.pack(side="left", fill="both", expand=True, padx=(0, 12))
        right_frame.pack(side="left", fill="both", expand=True)

        def render_category(parent: tk.Frame, cat: str, items: list) -> None:
            tk.Label(parent, text=cat.upper(),
                     bg="#050505", fg=C_ACCENT,
                     font=("Helvetica", 9, "bold")).pack(anchor="w", pady=(10, 4))

            for key, desc in items:
                row = tk.Frame(parent, bg="#111111")
                row.pack(fill="x", pady=1)
                tk.Label(row, text=key, width=18,
                         bg="#111111", fg="#E8EAF0",
                         font=("Courier", 9, "bold"),
                         anchor="w").pack(side="left", padx=(8, 4), pady=3)
                tk.Label(row, text=desc,
                         bg="#111111", fg="#6B7280",
                         font=("Helvetica", 9),
                         anchor="w").pack(side="left", padx=(0, 8), pady=3)

        for cat, items in left_cats:
            render_category(left_frame, cat, items)
        for cat, items in right_cats:
            render_category(right_frame, cat, items)

        # Footer hint
        tk.Label(ov, text="Press  ?  or  Esc  to close",
                 bg="#050505", fg="#444860",
                 font=("Helvetica", 8)).pack(pady=(0, 8))

        return ov

    # ── Workflow menu ─────────────────────────────────────────────────────────

    def _show_workflow_menu(self) -> None:
        """Show a small popup menu below the Workflows button."""
        menu = tk.Menu(self, tearoff=0,
                       bg=C_SURFACE, fg=C_TEXT,
                       activebackground=C_ACCENT, activeforeground="white",
                       relief="flat", bd=0,
                       font=("Helvetica", 10))
        menu.add_command(label="⚡  Clean Photo Load",
                         command=self._open_scoring_quick)
        menu.add_command(label="🔬  Detail Photo Clean",
                         command=self._open_scoring_detail)
        menu.add_separator()
        menu.add_command(label="🔗  Analyse Bursts",
                         command=self._analyse_bursts)
        menu.add_command(label="⊹  Review Bursts",
                         command=self._open_burst_triage)

        # Position below the button
        x = self.winfo_rootx() + 200
        y = self.winfo_rooty() + 80
        menu.tk_popup(x, y)

    # ── Rename ───────────────────────────────────────────────────────────────

    def _populate_rename_entry(self, path: str) -> None:
        """Fill rename entry with current filename stem and extension."""
        p    = Path(path)
        stem = p.stem
        ext  = p.suffix
        self._rename_var.set(stem)
        self._rename_original = stem
        self._ext_label.configure(text=ext.upper())
        self._rename_status.configure(text="")
        # Hide confirm button since name matches original
        if hasattr(self, "_rename_confirm_btn"):
            self._rename_confirm_btn.pack_forget()

    def _rename_focus_in(self) -> None:
        self._rename_active = True

    def _rename_focus_out(self) -> None:
        self._rename_active = False

    def _cancel_rename(self) -> None:
        """Reset entry to original name and defocus."""
        self._rename_var.set(self._rename_original)
        self._rename_status.configure(text="")
        self._canvas.focus_set()

    def _apply_rename(self) -> None:
        """
        Rename the current file on disk to the value in the entry field.
        Updates all internal state (files list, status dict, caches).
        Shows brief status feedback in the rename bar.
        """
        path     = self.files[self.index]
        old_stem = Path(path).stem
        new_stem = self._rename_var.get().strip()

        # Validation
        if not new_stem:
            self._rename_status.configure(text="⚠ Name cannot be empty", fg=C_DELETE)
            return
        if new_stem == old_stem:
            self._canvas.focus_set()
            return

        # Reject characters that are invalid on Windows/macOS/Linux
        invalid = set('\\/: *?\"<>|')
        bad = [c for c in new_stem if c in invalid]
        if bad:
            self._rename_status.configure(
                text=f"⚠ Invalid characters: {''.join(bad)}", fg=C_DELETE)
            return

        old_path = Path(path)
        new_path = old_path.parent / (new_stem + old_path.suffix)

        if new_path.exists():
            self._rename_status.configure(
                text=f"⚠ File already exists", fg=C_DELETE)
            return

        try:
            old_path.rename(new_path)
        except Exception as e:
            self._rename_status.configure(text=f"⚠ {e}", fg=C_DELETE)
            log.error("Rename failed: %s", e)
            return

        new_path_str = str(new_path)

        # ── Update all internal state ─────────────────────────────────────────
        # files list
        self.files[self.index] = new_path_str

        # status dict
        if path in self.status:
            self.status[new_path_str] = self.status.pop(path)

        # caches
        for cache in (self._scores, self._histograms,
                      self._exif_cache, self._preload_cache, self._hashes):
            if path in cache:
                cache[new_path_str] = cache.pop(path)

        # hash file→burst mapping
        if path in self._file_burst:
            grp = self._file_burst.pop(path)
            self._file_burst[new_path_str] = grp
            # Update within the group list
            for g in self._bursts:
                for i, f in enumerate(g):
                    if f == path:
                        g[i] = new_path_str

        # undo stack
        self._undo_stack = [
            (new_path_str if p == path else p, t, v)
            for p, t, v in self._undo_stack
        ]

        # status file
        save_status(self.status_file, self.status)

        # Update UI
        self._rename_original = new_stem
        self._title_label.configure(text=new_path.name)
        self._rename_status.configure(text=f"✓ Renamed", fg=C_BEST)
        self.after(2000, lambda: self._rename_status.configure(text=""))
        if hasattr(self, "_rename_confirm_btn"):
            self._rename_confirm_btn.pack_forget()
        self._canvas.focus_set()
        log.info("Renamed %s → %s", path, new_path_str)

    def _open_species_suggest(self) -> None:
        """Toggle inline species suggestion overlay."""
        self._show_overlay("species", self._build_species_overlay)

    def _build_species_overlay(self, frame: tk.Frame) -> None:
        view = InlineSpeciesView(frame, self)
        view.pack(fill="both", expand=True)

    # ── Workflow launchers ────────────────────────────────────────────────────

    def _open_scoring_quick(self) -> None:
        """Toggle inline scoring overlay — fast thumbnail scoring."""
        self._show_overlay("scoring_quick", lambda f: self._build_scoring_overlay(f, False))

    def _open_scoring_detail(self) -> None:
        """Toggle inline scoring overlay — full-preview scoring."""
        self._show_overlay("scoring_detail", lambda f: self._build_scoring_overlay(f, True))

    def _build_scoring_overlay(self, frame: tk.Frame, detail_mode: bool) -> None:
        view = InlineScoringView(frame, self, detail_mode)
        view.pack(fill="both", expand=True)

    def _open_burst_triage(self) -> None:
        """Toggle inline burst triage overlay."""
        # Toggle off if already active
        if self._active_overlay == "burst_triage":
            self._hide_overlay()
            return
        if not self._hash_ready:
            if not self._hashes:
                self._title_label.configure(
                    text="Still analysing — please wait then try again")
                self.after(3000, lambda: self._title_label.configure(
                    text=Path(self.files[self.index]).name))
                return
            # Need to analyse first — show loading screen then analyse
            self._show_overlay("burst_triage", self._build_burst_loading)
            self.after(80, self._analyse_bursts)
            return
        # Bursts ready — open directly
        self._show_overlay("burst_triage", self._build_burst_overlay)

    def _build_burst_loading(self, frame: tk.Frame) -> None:
        """Show spinner while burst view initialises."""
        f = tk.Frame(frame, bg=C_BG)
        f.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(f, text="Loading burst groups…",
                 bg=C_BG, fg=C_TEXT,
                 font=("Helvetica", 14, "bold")).pack(pady=(0, 12))
        prog = ctk.CTkProgressBar(f, mode="indeterminate", width=300,
                                   height=8, fg_color=C_BORDER,
                                   progress_color=C_ACCENT)
        prog.pack()
        prog.start()
        frame._prog = prog

    def _finish_burst_overlay(self) -> None:
        """Replace loading content with the actual burst view."""
        if self._active_overlay != "burst_triage":
            return
        for w in self._overlay_frame.winfo_children():
            w.destroy()
        view = InlineBurstTriageView(self._overlay_frame, self)
        view.pack(fill="both", expand=True)
        self._update_header_buttons()

    def _build_burst_overlay(self, frame: tk.Frame) -> None:
        view = InlineBurstTriageView(frame, self)
        view.pack(fill="both", expand=True)


# ── Launcher window ──────────────────────────────────────────────────────────

def _load_recent_folders() -> list[str]:
    """Load recently opened folders from disk."""
    try:
        if RECENT_FILE.exists():
            data = json.loads(RECENT_FILE.read_text(encoding="utf-8"))
            return [f for f in data if os.path.isdir(f)][:RECENT_MAX]
    except Exception:
        pass
    return []


def _save_recent_folder(folder: str) -> None:
    """Prepend folder to recent list and save."""
    recent = _load_recent_folders()
    if folder in recent:
        recent.remove(folder)
    recent.insert(0, folder)
    try:
        RECENT_FILE.write_text(
            json.dumps(recent[:RECENT_MAX], indent=2), encoding="utf-8")
    except Exception:
        pass


class LauncherWindow(ctk.CTk):
    """
    The ONE true Tk root — lives for the entire process lifetime.
    Never destroyed until the user quits, so the Tk interpreter and its
    image registry stay valid for every ImageTk.PhotoImage created anywhere.

    The reviewer opens as a CTkToplevel child of this window, sharing the
    same interpreter. The launcher hides itself while the reviewer is open
    and reappears if the reviewer is closed without quitting.
    """

    def __init__(self):
        super().__init__()
        self.title("📷 Photo Reviewer")
        self.resizable(False, False)

        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        w, h = 560, 400
        self.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
        self.configure(fg_color=C_BG)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._build_splash()

    def _build_splash(self) -> None:
        recent = _load_recent_folders()
        h = 420 + len(recent) * 40
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"580x{h}+{(sw-580)//2}+{(sh-h)//2}")

        # Use plain tk.Frame for logo — avoids CTkFrame internal canvas issues
        # that cause tk.Label children with images to not render correctly
        logo = tk.Frame(self, bg=C_SURFACE, height=190)
        logo.pack(fill="x")
        logo.pack_propagate(False)

        # Build icon with Pillow
        from PIL import Image as _PI, ImageDraw as _PD, ImageTk as _PT
        _ico = _PI.new("RGBA", (80, 80), (0, 0, 0, 0))
        _d   = _PD.Draw(_ico)
        _d.rounded_rectangle([0, 0, 79, 79], radius=16, fill="#0a0f0b")
        _d.rounded_rectangle([6, 14, 74, 66], radius=6,
                              outline="#2D7A4F", width=3, fill="#0d1a0e")
        for _y in (20, 30, 40, 50, 60):
            _d.rectangle([8,  _y, 14, _y+6], fill="#2D7A4F")
            _d.rectangle([66, _y, 72, _y+6], fill="#2D7A4F")
        _d.line([(22, 46), (34, 58), (58, 26)], fill="#2D7A4F", width=6)
        _d.line([(22, 46), (34, 58), (58, 26)], fill="#22C55E", width=2)
        self._splash_icon = _PT.PhotoImage(_ico)

        icon_lbl = tk.Label(logo, image=self._splash_icon, bg=C_SURFACE)
        icon_lbl.pack(pady=(22, 6))

        # Wordmark
        wordmark = tk.Frame(logo, bg=C_SURFACE)
        wordmark.pack()
        tk.Label(wordmark, text="Photo", bg=C_SURFACE, fg=C_ACCENT,
                 font=("Helvetica", 22, "bold")).pack(side="left")
        tk.Label(wordmark, text=" Reviewer", bg=C_SURFACE, fg=C_TEXT,
                 font=("Helvetica", 22)).pack(side="left")

        tk.Label(logo, text="Sort  ·  Score  ·  Select",
                 bg=C_SURFACE, fg=C_MUTED,
                 font=("Helvetica", 10)).pack(pady=(4, 0))

        action = tk.Frame(self, bg=C_BG)
        action.pack(fill="both", expand=True, padx=32, pady=16)

        ctk.CTkButton(
            action,
            text="📂   Open Image Folder…",
            font=ctk.CTkFont("Helvetica", 13, "bold"),
            height=44, corner_radius=10,
            fg_color=C_ACCENT, hover_color=_darken(C_ACCENT),
            command=self._pick_folder,
        ).pack(fill="x", pady=(0, 6))

        ctk.CTkLabel(action,
                     text="NEF · CR2 · CR3 · ARW · RAF · DNG · JPG · TIFF and more",
                     font=ctk.CTkFont("Helvetica", 8),
                     text_color=C_MUTED).pack()

        if recent:
            ctk.CTkLabel(action, text="RECENT FOLDERS",
                         font=ctk.CTkFont("Helvetica", 8, "bold"),
                         text_color=C_MUTED).pack(anchor="w", pady=(14, 4))
            for folder in recent:
                name   = Path(folder).name or folder
                parent = str(Path(folder).parent)
                row = ctk.CTkFrame(action, fg_color=C_BORDER, corner_radius=6)
                row.pack(fill="x", pady=2)
                ctk.CTkButton(
                    row, text=f"  {name}",
                    anchor="w",
                    font=ctk.CTkFont("Helvetica", 10, "bold"),
                    fg_color="transparent", hover_color=C_SURFACE,
                    text_color=C_TEXT, height=32,
                    command=lambda f=folder: self._launch_folder(f),
                ).pack(side="left", fill="x", expand=True, padx=4)
                ctk.CTkLabel(row, text=parent,
                             font=ctk.CTkFont("Helvetica", 8),
                             text_color=C_MUTED).pack(side="left", padx=(0, 8))

        self._status_lbl = ctk.CTkLabel(action, text="",
                                         font=ctk.CTkFont("Helvetica", 10),
                                         text_color=C_WARN)
        self._status_lbl.pack(pady=(10, 0))
        self._progress = ctk.CTkProgressBar(action, mode="indeterminate",
                                             height=6, corner_radius=3,
                                             fg_color=C_BORDER,
                                             progress_color=C_ACCENT)

    def _pick_folder(self) -> None:
        base_dir = fd.askdirectory(title="Select Image Folder", parent=self)
        if not base_dir:
            return
        self._launch_folder(base_dir)

    def _launch_folder(self, base_dir: str) -> None:
        if not os.path.isdir(base_dir):
            self._status_lbl.configure(
                text="Folder no longer exists.", text_color=C_DELETE)
            return

        _save_recent_folder(base_dir)

        self._status_lbl.configure(text="Scanning folder…", text_color=C_WARN)
        self._progress.pack(fill="x", pady=(6, 0))
        self._progress.start()
        self.update_idletasks()

        files = sorted(
            str(Path(base_dir) / f)
            for f in os.listdir(base_dir)
            if Path(f).suffix.lower() in SUPPORTED_EXTS
        )

        if not files:
            self._progress.stop()
            self._progress.pack_forget()
            self._status_lbl.configure(
                text="No supported images found in that folder.",
                text_color=C_DELETE)
            return

        self._status_lbl.configure(
            text=f"Found {len(files)} image(s) — launching…",
            text_color=C_BEST)
        self.update_idletasks()
        self.after(300, lambda: self._open_reviewer(base_dir, files))

    def _open_reviewer(self, base_dir: str, files: list[str]) -> None:
        """
        Open the reviewer as a CTkToplevel child of this window.
        Hide the launcher while the reviewer is open.
        Both windows share the same Tk interpreter — no image GC issues.
        """
        self.withdraw()   # hide launcher (keep interpreter alive)

        app = PhotoReviewer(self, base_dir, files)

        def on_close():
            if mb.askyesno("Quit", "Exit the reviewer?\n(Tags are already saved.)"):
                app.destroy()
                self.destroy()   # destroy root last — exits mainloop cleanly

        app.protocol("WM_DELETE_WINDOW", on_close)

        # Force geometry flush before fullscreen — without this the
        # CTkToplevel canvas winfo_width returns 1 and images place wrong
        app.update_idletasks()
        app.lift()
        app.focus_force()
        app.update()   # process all pending events including geometry


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    LauncherWindow().mainloop()


if __name__ == "__main__":
    main()