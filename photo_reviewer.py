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

Modules:
    photo_reviewer.py    — the UI (this file; run it to start the app)
    reviewer_imaging.py  — EXIF, preview/thumbnail loading, scoring
    reviewer_storage.py  — session files, caches, XMP sidecars, settings

Keyboard shortcuts: press ? in the app (or see the user guide).
"""

from __future__ import annotations

import bisect
import datetime
import logging
import math
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import customtkinter as ctk
import tkinter as tk
import tkinter.filedialog as fd
import tkinter.messagebox as mb
import tkinter.simpledialog as sd
import tkinter.ttk as ttk

from PIL import Image, ImageDraw, ImageTk
import imagehash
from send2trash import send2trash

# Image access (no Tkinter) and persistence (no Tkinter) live in their own modules
from reviewer_imaging import (
    CLIP_B, CLIP_G, CLIP_R, RAW_EXTS, SCORE_VERSION, SUBJECT_BLOWN, SUPPORTED_EXTS,
    _capture_timestamp, apply_raw_clipping, raw_clip_mask, raw_clip_text, raw_clipping, _extract_thumb_img,
    _open_thumb_unoriented, auto_orient, build_histogram, composite_score,
    convert_to_jpeg, format_exif_display, load_preview, load_small_thumb, np,
    read_exif, read_focus_area, score_image)
import reviewer_accel
from reviewer_detect import (
    MODEL_ID, SUGGESTION_SHORT, SUGGESTION_TEXT, detector_status, get_detector,
    main_animal, refine, suggestions)
from reviewer_regions import (
    RegionLists, by_iso, by_name, countries, guess_country, region_weight)
from reviewer_sightings import DEFAULT_GAP_SECS, GAP_CHOICES, build_sightings
from reviewer_search import (
    OfflineIndex, get_image_encoder, get_text_encoder, is_external, library_folders,
    load_index, make_root, resolve_root, root_name, save_index, search_status, stale_files)
from reviewer_species import (
    HISTORY_FILE as SPECIES_HISTORY_FILE, SpeciesHistory, get_species_model,
    load_predictions, save_predictions, species_status)
from reviewer_import import ImportDialog
from reviewer_eyes import (
    burst_eye_note, eye_status, get_eye_finder, load_eyes, measure as measure_eye, save_eyes)
from reviewer_predict import (
    HISTORY_FILE, MIN_BURSTS, MODEL_FILE, History, KeeperModel, build_features,
    embedder_status, folder_samples, get_embedder, group_bursts, labels_for,
    load_features, save_features, train)
from reviewer_storage import (
    DETECT_CACHE_FILE, HASH_CACHE_FILE, SCORE_CACHE_FILE, STATUS_FILENAME,
    _MISSING, _user_data_dir, load_detections, save_detections, session_status,
    _load_recent_folders, _resource_path, _save_recent_folder, _xmp_path_for,
    remap_path, rename_recent_folder,
    is_removable_drive, is_reviewed, load_app_settings, load_hash_cache,
    load_score_cache, save_app_settings, save_hash_cache,
    save_score_cache, save_status, write_xmp_sidecar)

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

# Average hash threshold for burst grouping
AHASH_BURST      = 6    # tighter — reduces false positives from similar scenes
BURST_MAX_SECS   = 30   # images >30s apart cannot be same burst
ZOOM_STEP        = 1.15
ZOOM_MIN         = 0.1
ZOOM_MAX         = 10.0
FILMSTRIP_H      = 110
PRELOAD_RADIUS   = 3    # decode ±N images ahead/behind
FILMSTRIP_WINDOW = 20   # images shown each side of current in filmstrip
THUMB_CACHE_MAX  = 1500 # small thumbnails kept in memory (≈300 MB at most)
THUMB_WORKERS    = 3
DETAIL_DELAY_MS  = 250  # load the full-res preview once you settle on an image
SCORE_MIN_SIDE   = 800  # scoring works on an ≤800 px image, so decode no larger
STATUS_SAVE_DELAY_MS = 400   # write the session file this long after a change
SEQUENCE_GAP_SECS = 300 # gallery divider when shots are >5 min apart
ANALYSIS_WORKERS = 2    # background scoring / animal detection threads
AUTO_ADVANCE_LABELS = {0: ("Auto→ OFF", "#3a1a1a", "#ef4444"),
                       1: ("Auto→ Delete", "#166534", "white"),
                       2: ("Auto→ Best+Del", "#166534", "white")}

# App settings
TOPAZ_DEFAULT_PATH = r"C:\Program Files\Topaz Labs LLC\Topaz Photo AI\Topaz Photo AI.exe"


# Hashing — worker threads and polling interval
HASH_WORKERS     = 4    # parallel threads for file I/O + hashing
HASH_POLL_MS     = 50   # ms between result-queue drain ticks
# Palette
C_BG       = "#0a0a0a"   # near-black canvas
C_SURFACE  = "#141414"   # surface / header / sidebar
C_BORDER   = "#222222"   # borders / separators
C_TEXT     = "#E8EAF0"   # primary text
C_MUTED    = "#6B7280"   # muted text / inactive
C_BEST     = "#22C55E"   # green — Best tag
C_DELETE   = "#EF4444"   # red — Delete tag
C_ID       = "#3B82F6"   # blue — ID tag (clearly different from Best green)
C_ACCENT   = "#2D7A4F"   # forest green — primary accent
C_CURRENT  = "#E8EAF0"   # white — outline of the image currently shown
C_SELECT   = "#A855F7"   # purple — selected cells in grid views
C_FOCUS    = "#FACC15"   # yellow — keyboard cursor in grid views / AF box
C_ANIMAL   = "#22D3EE"   # cyan — detected animal boxes
C_PICK     = "#38BDF8"   # sky blue — predicted keeper of a burst
DEFAULT_BEST_TEMPLATE = "{species}_{date}_{location}_{n}"
C_WARN     = "#F59E0B"
C_COMPARE  = "#F97316"   # orange — compare tag
C_STAR     = "#FBBF24"   # amber — star ratings

STAR_COLORS = {1: "#ef4444", 2: "#f97316", 3: "#eab308",
               4: "#84cc16", 5: "#22c55e"}  # red→green gradient

# Tag → (symbol, word, colour), in display order
TAG_STYLE = {"best":   ("★", "Best",   C_BEST),
             "delete": ("✕", "Delete", C_DELETE),
             "id":     ("⚑", "ID",     C_ID)}

_SKIPPED = object()   # worker result: job skipped because it went stale

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"{max(s, 1)} s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} min {s:02d} s" if m < 10 else f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min"


class Eta:
    """Estimates the time left for a job from its progress so far."""

    def __init__(self, total: int):
        self.total = total
        self.start = time.monotonic()

    def text(self, done: int) -> str:
        if self.total <= 0 or done >= self.total:
            return ""
        elapsed = time.monotonic() - self.start
        if done <= 0 or elapsed < 1.5:
            return "estimating time left…"
        return f"about {_fmt_duration(elapsed / done * (self.total - done))} left"


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


# ── Shared thumbnail grid helpers ────────────────────────────────────────────

def tag_color(tags: dict, in_compare: bool = False,
              default: Optional[str] = None) -> Optional[str]:
    """Colour representing an image's tags (compare > best > delete > id)."""
    if in_compare:
        return C_COMPARE
    for tag, (_sym, _word, col) in TAG_STYLE.items():
        if tags.get(tag):
            return col
    return default


def make_tag_buttons(parent, app: "PhotoReviewer", path: str, on_toggle,
                     words: bool = False, size: int = 9) -> tk.Frame:
    """Best / Delete / ID toggle buttons; active tags are filled in colour."""
    row  = tk.Frame(parent, bg=C_SURFACE)
    tags = app.status.get(path, {})
    for tag, (sym, word, col) in TAG_STYLE.items():
        tk.Button(row, text=f"{sym} {word}" if words else sym,
                  bg=col if tags.get(tag) else C_BORDER, fg="white",
                  activebackground=_darken(col), activeforeground="white",
                  font=("Helvetica", size, "bold"), relief="flat", bd=0,
                  command=lambda t=tag: on_toggle(path, t)
                  ).pack(side="left", fill="x", expand=True, padx=1, pady=2)
    return row


def make_thumb_cell(parent, path: str, app, cell_w: int, cell_h: int,
                    on_click=None, show_scores: bool = True,
                    is_current: bool = False,
                    border: Optional[str] = None,
                    on_double=None, thickness: int = 2,
                    show_pick: bool = True) -> tk.Frame:
    """
    Build a thumbnail cell frame for gallery/scoring/burst views.
    show_pick=False hides the predicted-pick marker and keeper chance.
    The thumbnail itself is drawn later into cell._canvas by the grid.
    """
    scores  = app._scores.get(path)
    bs      = composite_score(scores)
    tags    = app.status.get(path, {})
    star    = tags.get("star", 0)
    tag_str = "".join(sym for t, (sym, _w, _c) in TAG_STYLE.items() if tags.get(t)) + \
              (f' {"★" * star}' if star else "")

    if border is None:
        border = C_CURRENT if is_current else tag_color(tags, default="#1c1c1c")

    cell = tk.Frame(parent, bg=C_SURFACE,
                    highlightthickness=thickness, highlightbackground=border)

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
            for key, short in [("sharpness", "S"), ("exposure", "E")]:
                v = scores.get(key, 0)
                tk.Label(info, text=f"{short}:{v}",
                         bg=C_SURFACE, fg=_score_color(v),
                         font=("Helvetica", 7)).pack(side="left", padx=1)
        tk.Label(info, text=tag_str, bg=C_SURFACE, fg=C_TEXT,
                 font=("Helvetica", 8)).pack(side="right", padx=2)
        keep = app._keeper.get(path) if show_pick else None
        if keep is not None:
            tk.Label(info, text=f"◆{keep:.0%}" if path in app._picks else f"K{keep:.0%}",
                     bg=C_SURFACE, fg=C_PICK if path in app._picks else C_MUTED,
                     font=("Helvetica", 7, "bold")).pack(side="right", padx=2)

    if show_pick and path in app._picks:
        tk.Label(cell, text="◆ Predicted pick", bg=C_SURFACE, fg=C_PICK,
                 font=("Helvetica", 7, "bold")).pack(padx=4)
    note = app._suggest_note(path)
    if note:
        tk.Label(cell, text=f"⚠ {note}", bg=C_SURFACE, fg=C_WARN,
                 font=("Helvetica", 7, "bold")).pack(padx=4)
    elif app._eye_note(path) == "best":
        tk.Label(cell, text="◉ Sharpest eye in burst", bg=C_SURFACE, fg=C_BEST,
                 font=("Helvetica", 7, "bold")).pack(padx=4)
    tk.Label(cell, text=Path(path).name, bg=C_SURFACE, fg=C_MUTED,
             font=("Helvetica", 7), wraplength=cell_w - 6).pack(
        padx=4, pady=(0, 4))

    if on_click:
        canvas.bind("<Button-1>", lambda e: on_click(path, e))
    if on_double:
        canvas.bind("<Double-Button-1>", lambda e: on_double(path))

    cell._canvas = canvas   # type: ignore[attr-defined]
    cell._path   = path     # type: ignore[attr-defined]
    return cell


# ── ThumbnailGrid base class ──────────────────────────────────────────────────

class ThumbnailGrid(tk.Frame):
    """
    Scrollable, virtualised thumbnail grid used by the gallery, scoring and
    burst views. Only rows on screen (plus a small margin) exist as widgets,
    so large folders open quickly and memory stays bounded. Thumbnails come
    from the app's shared small-thumbnail cache, decoded on worker threads.

    Subclasses implement _make_toolbar(bar) and _make_cell(parent, path),
    may add a left panel in _build_side_panel(body), and call set_paths().
    """
    MIN_CELL    = 160
    MAX_CELL    = 300
    CELL_PAD    = 6
    CELL_CHROME = 10     # cell border + padding around the thumbnail canvas
    SEP_H       = 32
    MARGIN_ROWS = 2

    def __init__(self, parent: tk.Frame, app: "PhotoReviewer",
                 toolbar_title: str = ""):
        super().__init__(parent, bg=C_BG)
        self._app        = app
        self._paths: list[str] = []
        self.COLS        = 4
        self.THUMB_W     = 200
        self.THUMB_H     = 140
        self._col_w      = 0
        self._cell_h     = 0
        self._rows: list[tuple[str, object]] = []     # ("cells", [paths]) / ("sep", ts)
        self._row_y: list[int] = []
        self._row_of: dict[str, int] = {}
        self._live: dict[int, list] = {}               # row → [(canvas item, widget)]
        self._item_of: dict[str, int] = {}             # on-screen path → canvas item
        self._cell_of: dict[str, tk.Frame] = {}        # on-screen path → cell
        self._tk_images: dict[str, Optional[ImageTk.PhotoImage]] = {}
        self._width      = 0
        self._dividers   = True
        self._alive      = True
        self._visible_job: Optional[str] = None
        self._layout_job:  Optional[str] = None
        self._pending_scroll: Optional[str] = None
        self._sel: set[str] = set()                    # selected images
        self._cursor: Optional[str] = None             # keyboard cursor
        self._anchor: Optional[str] = None             # shift-select anchor
        self._sel_bar: Optional[tk.Frame] = None
        # first path of each group → separator text (None = time-gap dividers)
        self._group_starts: Optional[dict[str, str]] = None
        self._show_pick  = True

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

        # Extra rows (progress bars, messages) sit between toolbar and grid
        self._extra = tk.Frame(self, bg=C_BG)
        self._extra.pack(fill="x")

        # ── Scrollable grid ───────────────────────────────────────────────────
        body = tk.Frame(self, bg=C_BG)
        body.pack(fill="both", expand=True)
        self._build_side_panel(body)
        self._vsb = tk.Scrollbar(body, orient="vertical", command=self._yview)
        self._vsb.pack(side="right", fill="y")
        self._canvas = tk.Canvas(body, bg=C_BG, highlightthickness=0,
                                 yscrollincrement=20)
        self._canvas.configure(yscrollcommand=self._on_yscroll)
        self._canvas.pack(side="left", fill="both", expand=True)
        self._canvas.bind("<Configure>", self._on_configure)
        self._bind_wheel(self._canvas)
        self.bind("<Destroy>", self._on_destroy_grid, add="+")
        self.after(80, self._poll_thumbs)

    # ── Subclass interface ────────────────────────────────────────────────────

    def _make_toolbar(self, toolbar: tk.Frame) -> None:
        """Override to add toolbar widgets (right-aligned)."""

    def _build_side_panel(self, body: tk.Frame) -> None:
        """Override to add a panel left of the grid."""

    def _make_cell(self, parent: tk.Frame, path: str) -> tk.Frame:
        return self._cell(parent, path)

    def _cell(self, parent: tk.Frame, path: str, **kw) -> tk.Frame:
        """Thumbnail cell with selection / cursor outline and click handling."""
        default = kw.pop("border", None)
        if path in self._sel:
            kw["border"], kw["thickness"] = C_SELECT, 4
        elif path == self._cursor:
            kw["border"], kw["thickness"] = C_FOCUS, 3
        elif default:
            kw["border"] = default
        kw.setdefault("on_click", self._cell_click)
        kw.setdefault("on_double", self._jump_and_close)
        kw.setdefault("show_pick", self._show_pick)
        return make_thumb_cell(parent, path, self._app,
                               self.THUMB_W, self.THUMB_H, **kw)

    # ── Selection & keyboard ─────────────────────────────────────────────────

    def _cell_click(self, path: str, event=None) -> None:
        """Click selects; Ctrl+click toggles; Shift+click selects a range."""
        state = getattr(event, "state", 0)
        changed = set(self._sel) | {path, self._cursor}
        if state & 0x1 and self._anchor in self._paths:          # Shift
            a, b = sorted((self._paths.index(self._anchor), self._paths.index(path)))
            self._sel = set(self._paths[a:b + 1])
        elif state & 0x4:                                         # Ctrl
            self._sel ^= {path}
            self._anchor = path
        else:
            self._sel = {path}
            self._anchor = path
        self._cursor = path
        self._refresh_cells(changed | self._sel)

    def _refresh_cells(self, paths) -> None:
        for p in paths:
            if p:
                self.refresh_cell(p)
        self._update_sel_bar()

    def _targets(self) -> list[str]:
        """Images a key / button acts on: the selection, else the cursor."""
        if self._sel:
            return [p for p in self._paths if p in self._sel]
        return [self._cursor] if self._cursor in self._paths else []

    def handle_key(self, ks: str, ctrl: bool, shift: bool) -> bool:
        """Keyboard control for grid views. True if the key was handled."""
        if not self._paths:
            return False
        if ctrl and ks in ("a", "A"):
            self._sel = set(self._paths)
            self._refresh_cells(list(self._cell_of))
            return True
        moves = {"Left": -1, "Right": 1, "Up": -self.COLS, "Down": self.COLS,
                 "Prior": -self.COLS * 4, "Next": self.COLS * 4,
                 "Home": -len(self._paths), "End": len(self._paths)}
        if ks in moves:
            old = self._cursor
            if old in self._paths:
                i = max(0, min(len(self._paths) - 1, self._paths.index(old) + moves[ks]))
            else:
                i = 0
            self._cursor = self._paths[i]
            changed = {old, self._cursor} | set(self._sel)
            if shift:
                if self._anchor not in self._paths:
                    self._anchor = old if old in self._paths else self._cursor
                a, b = sorted((self._paths.index(self._anchor), i))
                self._sel = set(self._paths[a:b + 1])
            else:
                self._anchor = self._cursor
            self._refresh_cells(changed | self._sel)
            self._ensure_visible(self._cursor)
            return True
        if ks in ("Return", "KP_Enter") and self._cursor in self._paths:
            self._jump_and_close(self._cursor)
            return True
        if ks == "Escape" and self._sel:
            changed = set(self._sel)
            self._sel.clear()
            self._refresh_cells(changed)
            return True
        tag = {"b": "best", "B": "best", "d": "delete", "D": "delete",
               "BackSpace": "delete", "i": "id", "I": "id", "space": "id"}.get(ks)
        if tag or ks in "012345" and len(ks) == 1:
            targets = self._targets()
            if not targets:
                self._app._flash_title("Click an image or use the arrow keys first", 2000)
                return True
            if tag:
                self._app._apply_tag_many(targets, tag)
            else:
                self._app._apply_star_many(targets, int(ks))
            self._after_batch(targets)
            return True
        return False

    def _after_batch(self, paths: list[str]) -> None:
        """Refresh after a change to several images (subclasses may re-filter)."""
        self._refresh_cells(paths)

    def _ensure_visible(self, path: str) -> None:
        r = self._row_of.get(path)
        if r is None:
            return
        total = (self._row_y[-1] + self._cell_h) if self._row_y else 1
        top = self._canvas.canvasy(0)
        bottom = top + self._canvas.winfo_height()
        y = self._row_y[r]
        if y < top:
            self._canvas.yview_moveto(max(0.0, y / total))
        elif y + self._cell_h > bottom:
            self._canvas.yview_moveto(
                max(0.0, (y + self._cell_h - self._canvas.winfo_height()) / total))

    def _update_sel_bar(self) -> None:
        """Action bar for the selection (shown while images are selected)."""
        n = len(self._sel)
        if not n:
            if self._sel_bar is not None:
                self._sel_bar.pack_forget()
            return
        if self._sel_bar is None:
            bar = self._sel_bar = tk.Frame(self._extra, bg="#2a1f3d")
            self._sel_lbl = tk.Label(bar, bg="#2a1f3d", fg="white",
                                     font=("Helvetica", 9, "bold"))
            self._sel_lbl.pack(side="left", padx=10, pady=4)
            for tag, (sym, word, col) in TAG_STYLE.items():
                tk.Button(bar, text=f"{sym} {word}", bg=col, fg="white", bd=0,
                          relief="flat", font=("Helvetica", 9, "bold"), padx=8,
                          command=lambda t=tag: self._bar_tag(t)).pack(side="left", padx=2)
            stars = tk.Menubutton(bar, text="★ Rate ▾", bg=C_BORDER, fg=C_STAR,
                                  relief="flat", font=("Helvetica", 9, "bold"))
            menu = tk.Menu(stars, tearoff=0)
            for n_ in range(6):
                menu.add_command(label="Clear rating" if n_ == 0 else "★" * n_,
                                 command=lambda v=n_: self._bar_star(v))
            stars.configure(menu=menu)
            stars.pack(side="left", padx=6)
            self._kw_btn = tk.Menubutton(bar, text="Keyword ▾", bg=C_BORDER, fg=C_TEXT,
                                         relief="flat", font=("Helvetica", 9))
            self._kw_btn.pack(side="left", padx=2)
            for label, key in (("Species…", "species"), ("Location…", "location")):
                tk.Button(bar, text=label, bg=C_BORDER, fg=C_TEXT, bd=0, relief="flat",
                          font=("Helvetica", 9), padx=8,
                          command=lambda k=key: self._bar_field(k)).pack(side="left", padx=2)
            tk.Button(bar, text="Clear selection", bg=C_BORDER, fg=C_TEXT, bd=0,
                      relief="flat", font=("Helvetica", 9), padx=8,
                      command=lambda: self.handle_key("Escape", False, False)
                      ).pack(side="right", padx=8)
        menu = tk.Menu(self._kw_btn, tearoff=0)
        for kw in self._app._keywords or []:
            menu.add_command(label=kw, command=lambda k=kw: self._bar_keyword(k))
        if not self._app._keywords:
            menu.add_command(label="(add keywords in the sidebar first)", state="disabled")
        self._kw_btn.configure(menu=menu)
        self._sel_lbl.configure(text=f"{n} selected")
        self._sel_bar.pack(fill="x", before=self._extra.winfo_children()[0]
                           if self._extra.winfo_children()[0] is not self._sel_bar else None)

    def _bar_tag(self, tag: str) -> None:
        t = self._targets()
        self._app._apply_tag_many(t, tag)
        self._after_batch(t)

    def _bar_star(self, n: int) -> None:
        t = self._targets()
        self._app._apply_star_many(t, n)
        self._after_batch(t)

    def _bar_field(self, key: str) -> None:
        t = self._targets()
        current = {self._app.status.get(p, {}).get(key, "") for p in t}
        value = sd.askstring(key.capitalize(), f"{key.capitalize()} for {len(t)} image(s):",
                             initialvalue=current.pop() if len(current) == 1 else "",
                             parent=self)
        if value is not None:
            self._app._apply_field_many(t, key, value.strip())
            self._after_batch(t)

    def _bar_keyword(self, kw: str) -> None:
        t = self._targets()
        self._app._apply_keyword_many(t, kw)
        self._after_batch(t)

    # ── Layout ────────────────────────────────────────────────────────────────

    def set_paths(self, paths: list[str], keep_scroll: bool = False,
                  scroll_to: Optional[str] = None) -> None:
        """Show these images (re-lays out; only visible rows are built)."""
        self._paths = list(paths)
        if scroll_to:
            self._pending_scroll = scroll_to
        self._relayout(keep_scroll)

    def _on_configure(self, event) -> None:
        if abs(event.width - self._width) > 4:
            first = self._width == 0
            self._width = event.width
            if first:                       # first layout: no delay
                self._relayout(keep_scroll=True)
                return
            if self._layout_job:
                self.after_cancel(self._layout_job)
            self._layout_job = self.after(60, lambda: self._relayout(keep_scroll=True))
        else:
            self._schedule_visible()

    def _relayout(self, keep_scroll: bool = False) -> None:
        self._layout_job = None
        if not self._alive:
            return
        w = self._width or self._canvas.winfo_width()
        if w < 50:
            return   # not mapped yet — <Configure> will lay out
        target  = max(self.MIN_CELL, min(self.MAX_CELL, w // 4))
        cols    = max(1, w // (target + 2 * self.CELL_PAD + self.CELL_CHROME))
        self.COLS    = cols
        self._col_w  = w // cols
        self.THUMB_W = max(80, self._col_w - 2 * self.CELL_PAD - self.CELL_CHROME)
        self.THUMB_H = int(self.THUMB_W * 0.70)
        frac = self._canvas.yview()[0] if keep_scroll else 0.0

        self._clear_live()
        self._tk_images.clear()
        if self._paths:
            sample = self._make_cell(self._canvas, self._paths[0])
            sample.update_idletasks()
            self._cell_h = sample.winfo_reqheight() + 2 * self.CELL_PAD
            sample.destroy()

        # Rows, with a divider wherever the capture-time gap exceeds the limit
        rows: list[tuple[str, object]] = []
        row_of: dict[str, int] = {}
        stamps  = self._app._timestamps
        prev_ts: Optional[float] = None
        current: list[str] = []
        starts = self._group_starts
        for p in self._paths:
            ts = stamps.get(p)
            if starts is not None:
                if p in starts:
                    if current:
                        rows.append(("cells", current))
                        current = []
                    rows.append(("sep", starts[p]))
            elif self._dividers and ts is not None and prev_ts is not None \
                    and ts - prev_ts > SEQUENCE_GAP_SECS:
                if current:
                    rows.append(("cells", current))
                    current = []
                rows.append(("sep", ts))
            if ts is not None:
                prev_ts = ts
            current.append(p)
            if len(current) == cols:
                rows.append(("cells", current))
                current = []
        if current:
            rows.append(("cells", current))

        row_y: list[int] = []
        y = 0
        for i, (kind, data) in enumerate(rows):
            row_y.append(y)
            if kind == "cells":
                for p in data:          # type: ignore[union-attr]
                    row_of[p] = i
                y += self._cell_h
            else:
                y += self.SEP_H
        self._rows, self._row_y, self._row_of = rows, row_y, row_of
        self._canvas.configure(scrollregion=(0, 0, w, max(y, 1)))

        target = self._pending_scroll
        self._pending_scroll = None
        if target and target in row_of:
            self._canvas.yview_moveto(
                max(0.0, (row_y[row_of[target]] - self._cell_h) / max(y, 1)))
        else:
            self._canvas.yview_moveto(frac)
        self._update_visible()

    # ── Scrolling / visible rows ─────────────────────────────────────────────

    def _bind_wheel(self, widget) -> None:
        widget.bind("<MouseWheel>", self._on_wheel)
        widget.bind("<Button-4>", self._on_wheel)
        widget.bind("<Button-5>", self._on_wheel)
        for child in widget.winfo_children():
            self._bind_wheel(child)

    def _on_wheel(self, event):
        up = getattr(event, "num", 0) == 4 or getattr(event, "delta", 0) > 0
        self._canvas.yview_scroll(-3 if up else 3, "units")
        return "break"

    def _yview(self, *args) -> None:
        self._canvas.yview(*args)

    def _on_yscroll(self, first, last) -> None:
        self._vsb.set(first, last)
        self._schedule_visible()

    def _schedule_visible(self) -> None:
        if self._visible_job is None and self._alive:
            self._visible_job = self.after_idle(self._update_visible)

    def _update_visible(self) -> None:
        self._visible_job = None
        if not self._alive:
            return
        if not self._rows:
            self._app._thumb_wanted_grid = set()
            return
        top    = self._canvas.canvasy(0)
        bottom = top + self._canvas.winfo_height()
        margin = self._cell_h * self.MARGIN_ROWS
        first  = max(0, bisect.bisect_right(self._row_y, top - margin) - 1)
        last   = bisect.bisect_left(self._row_y, bottom + margin)
        want   = set(range(first, last))
        for r in list(self._live):
            if r not in want:
                self._drop_row(r)
        for r in sorted(want - set(self._live)):
            self._build_row(r)
        visible = list(self._cell_of)
        self._app._thumb_wanted_grid = set(visible)
        self._app._request_thumbs(visible)
        self._draw_ready_thumbs()

    def _build_row(self, r: int) -> None:
        kind, data = self._rows[r]
        y = self._row_y[r]
        items = []
        if kind == "sep":
            if isinstance(data, str):
                text = data
            else:
                stamp = datetime.datetime.fromtimestamp(data).strftime("%Y-%m-%d  %H:%M")  # type: ignore[arg-type]
                text = f"  ▸ New sequence  {stamp}"
            sep = tk.Frame(self._canvas, bg="#1a2a1a")
            tk.Label(sep, text=text,
                     bg="#1a2a1a", fg="#66aa66",
                     font=("Helvetica", 8, "italic")).pack(side="left", padx=4, pady=4)
            item = self._canvas.create_window(
                self.CELL_PAD, y + 4, window=sep, anchor="nw",
                width=max(10, self._col_w * self.COLS - 2 * self.CELL_PAD),
                height=self.SEP_H - 8)
            self._bind_wheel(sep)
            items.append((item, sep))
        else:
            for col, path in enumerate(data):   # type: ignore[arg-type]
                cell = self._make_cell(self._canvas, path)
                item = self._canvas.create_window(
                    col * self._col_w + self.CELL_PAD, y + self.CELL_PAD,
                    window=cell, anchor="nw")
                self._bind_wheel(cell)
                self._cell_of[path] = cell
                self._item_of[path] = item
                items.append((item, cell))
        self._live[r] = items

    def _drop_row(self, r: int) -> None:
        for item, widget in self._live.pop(r, []):
            self._canvas.delete(item)
            widget.destroy()
        if r < len(self._rows) and self._rows[r][0] == "cells":
            for p in self._rows[r][1]:          # type: ignore[union-attr]
                self._cell_of.pop(p, None)
                self._item_of.pop(p, None)
                self._tk_images.pop(p, None)

    def _clear_live(self) -> None:
        for r in list(self._live):
            self._drop_row(r)

    def refresh_cell(self, path: str) -> None:
        """Rebuild one image's cell in place (e.g. after a tag change)."""
        old, item = self._cell_of.get(path), self._item_of.get(path)
        if old is None or item is None:
            return
        cell = self._make_cell(self._canvas, path)
        self._canvas.itemconfigure(item, window=cell)
        r = self._row_of[path]
        self._live[r] = [(i, cell if w is old else w) for i, w in self._live[r]]
        old.destroy()
        self._bind_wheel(cell)
        self._cell_of[path] = cell
        photo = self._tk_images.get(path)
        if photo is not None:                    # reuse the drawn thumbnail
            cell._canvas.delete("all")
            cell._canvas.create_image(self.THUMB_W // 2, self.THUMB_H // 2,
                                      image=photo, anchor="center")
        else:
            self._tk_images.pop(path, None)
            self._draw_ready_thumbs()

    # ── Thumbnails ────────────────────────────────────────────────────────────

    def _draw_ready_thumbs(self) -> None:
        cache, failed = self._app._thumb_cache, self._app._thumb_failed
        for path, cell in self._cell_of.items():
            if path in self._tk_images:
                continue
            c = cell._canvas
            if path in failed:
                c.delete("all")
                c.create_text(self.THUMB_W // 2, self.THUMB_H // 2,
                              text="Can't load", fill=C_MUTED, font=("Helvetica", 8))
                self._tk_images[path] = None
                continue
            img = cache.get(path)
            if img is None:
                continue
            t = img.copy()
            t.thumbnail((self.THUMB_W, self.THUMB_H), Image.BILINEAR)
            tk_img = ImageTk.PhotoImage(t)
            self._tk_images[path] = tk_img
            c.delete("all")
            c.create_image(self.THUMB_W // 2, self.THUMB_H // 2,
                           image=tk_img, anchor="center")

    def _poll_thumbs(self) -> None:
        if not self._alive:
            return
        try:
            self._draw_ready_thumbs()
        except tk.TclError:
            return
        self.after(80, self._poll_thumbs)

    def _on_destroy_grid(self, event) -> None:
        if event.widget is not self:
            return
        self._alive = False
        self._app._thumb_wanted_grid = set()

    # ── Navigation helpers ────────────────────────────────────────────────────

    def _jump_and_close(self, path: str) -> None:
        """Jump to image in main viewer and close overlay."""
        self._app._hide_overlay()
        if path in self._app.files:
            self._app._jump_to_index(self._app.files.index(path))

    def _after_tag(self, path: str, relayout: bool = False) -> None:
        """Refresh after a tag change from a cell's buttons."""
        self._app._after_tag_change(path)
        if relayout:
            self._relayout(keep_scroll=True)
        else:
            self.refresh_cell(path)


class InlineGalleryView(ThumbnailGrid):
    """Gallery view — all images, filterable by tag and filename."""

    def __init__(self, parent: tk.Frame, app: "PhotoReviewer"):
        self._filter_var = tk.StringVar()
        self._tag_filter = tk.StringVar(value="all")
        self._by_keeper  = False
        super().__init__(parent, app, f"Gallery  —  {app.total} images")
        self._apply_filter(scroll_to=app.files[app.index])

    def _make_toolbar(self, bar: tk.Frame) -> None:
        # Tag filter pills (right side)
        for label, value, color in [
            ("⚠ Check", "check", C_WARN),
            ("Untagged", "none", C_MUTED), ("⚑ ID", "id", C_ID),
            ("✕ Delete", "delete", C_DELETE), ("★ Best", "best", C_BEST),
            ("All", "all", C_BORDER),
        ]:
            tk.Button(bar, text=label, bg=color, fg="white",
                      font=("Helvetica", 9), relief="flat", bd=0,
                      padx=8, pady=2,
                      command=lambda v=value: self._set_tag(v)
                      ).pack(side="right", padx=2, pady=10)

        # Search bar below toolbar
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
        self._sort_btn = tk.Button(search, text="Sort: time", bg=C_BG, fg=C_TEXT,
                                   relief="flat", bd=0, padx=8, font=("Helvetica", 9),
                                   command=self._toggle_sort)
        self._sort_btn.pack(side="right", padx=8, pady=4)

    def _toggle_sort(self) -> None:
        if not self._app._keeper:
            self._app._flash_title("Keeper prediction isn't ready yet", 2000)
            return
        self._by_keeper = not self._by_keeper
        self._sort_btn.configure(text="Sort: keeper chance" if self._by_keeper else "Sort: time")
        self._apply_filter()

    def _set_tag(self, v: str) -> None:
        self._tag_filter.set(v)
        self._apply_filter()

    def _matches(self, path: str) -> bool:
        query = self._filter_var.get().strip().lower()
        if query and query not in Path(path).name.lower():
            return False
        mode = self._tag_filter.get()
        tags = self._app.status.get(path, {})
        if mode in ("best", "delete", "id"):
            return bool(tags.get(mode))
        if mode == "none":
            return not is_reviewed(tags)
        if mode == "check":
            return bool(self._app._suggestions(path))
        return True

    def _apply_filter(self, keep_scroll: bool = False,
                      scroll_to: Optional[str] = None) -> None:
        paths = [p for p in self._app.files if self._matches(p)]
        if self._by_keeper:
            paths.sort(key=lambda p: -self._app._keeper.get(p, -1))
        self._dividers = not self._by_keeper
        self._count_lbl.configure(text=f"{len(paths)} images")
        self.set_paths(paths, keep_scroll=keep_scroll, scroll_to=scroll_to)

    def _make_cell(self, parent: tk.Frame, path: str) -> tk.Frame:
        cell = self._cell(parent, path,
                          is_current=(path == self._app.files[self._app.index]))
        make_tag_buttons(cell, self._app, path, self._tag).pack(
            fill="x", padx=4, pady=(0, 4))
        return cell

    def _after_batch(self, paths: list[str]) -> None:
        if any(not self._matches(p) for p in paths):
            self._apply_filter(keep_scroll=True)     # some left the filter
            self._update_sel_bar()
        else:
            self._refresh_cells(paths)

    def _tag(self, path: str, tag: str) -> None:
        """Toggle a tag; only re-filter if the image left/entered the filter."""
        before = self._matches(path)
        self._app._toggle_tag_for(path, tag)
        changed = before != self._matches(path)
        self._after_tag(path, relayout=False)
        if changed:
            self._apply_filter(keep_scroll=True)


class InlineScoringView(ThumbnailGrid):
    """Scoring view — progress bar then filterable scored grid with tag buttons."""

    def __init__(self, parent: tk.Frame, app: "PhotoReviewer"):
        self._use_sharp    = tk.BooleanVar(value=True)
        self._use_exposure = tk.BooleanVar(value=True)
        self._threshold    = tk.IntVar(value=40)
        self._scored       = False
        super().__init__(parent, app, "Clean Photo Load")
        self._prog_lbl = tk.Label(self._extra, text="Scoring images…",
                                  bg=C_BG, fg=C_MUTED, font=("Helvetica", 9))
        self._prog_lbl.pack(fill="x", padx=12, pady=2)
        self._prog_bar = ctk.CTkProgressBar(self._extra, mode="determinate", height=6,
                                            fg_color=C_BORDER, progress_color=C_BEST)
        self._prog_bar.set(0)
        self._prog_bar.pack(fill="x", padx=12, pady=(0, 4))
        self._threshold.trace_add("write", lambda *_: self._restyle())
        self.after(100, self._run_scoring)

    def _make_toolbar(self, bar: tk.Frame) -> None:
        tk.Label(bar, text="Threshold:", bg=C_SURFACE, fg=C_MUTED,
                 font=("Helvetica", 9)).pack(side="right", padx=(8, 2))
        tk.Entry(bar, textvariable=self._threshold, width=4,
                 bg=C_BG, fg=C_TEXT, relief="flat",
                 font=("Helvetica", 9)).pack(side="right", pady=8)
        for label, var, col in [
            ("Sharpness", self._use_sharp,    C_BEST),
            ("Exposure",  self._use_exposure, C_WARN),
        ]:
            tk.Checkbutton(bar, text=label, variable=var, bg=C_SURFACE, fg=col,
                           activebackground=C_SURFACE, selectcolor=C_BG,
                           font=("Helvetica", 9), command=self._restyle
                           ).pack(side="right", padx=6, pady=8)

    def _restyle(self) -> None:
        if self._scored:
            self._relayout(keep_scroll=True)

    def _run_scoring(self) -> None:
        """Images are scored by the app's background analysis; show its progress."""
        self._app._ensure_analysis()
        self._poll_scoring()

    def _poll_scoring(self) -> None:
        if not self._alive:
            return
        app   = self._app
        total = len(app.files)
        need  = app._unscored_count()
        done  = total - need
        self._prog_bar.set(done / total if total else 1.0)
        if need:
            eta = app._analysis_eta_text()
            waiting = "" if app._analysis_running() else "   ·   waiting for folder analysis…"
            self._prog_lbl.configure(
                text=f"Scoring… {done} / {total}" + (f"   ·   {eta}" if eta else "") + waiting)
            self.after(300, self._poll_scoring)
            return
        self._prog_lbl.configure(text=f"✓ Complete — {total} images scored")
        self._scored = True
        self.set_paths(app.files)

    def _make_cell(self, parent: tk.Frame, path: str) -> tk.Frame:
        try:
            thresh = int(self._threshold.get())
        except Exception:
            thresh = 40
        bs   = composite_score(self._app._scores.get(path), self._use_sharp.get(),
                               self._use_exposure.get())
        cell = self._cell(parent, path, border=C_DELETE if bs < thresh else C_BEST)
        make_tag_buttons(cell, self._app, path, self._tag, words=True, size=8).pack(
            fill="x", padx=4, pady=(0, 4))
        return cell

    def _tag(self, path: str, tag: str) -> None:
        self._app._toggle_tag_for(path, tag)
        self._after_tag(path)


class InlineBurstTriageView(ThumbnailGrid):
    """Burst triage — step through burst groups, select keepers."""

    def __init__(self, parent: tk.Frame, app: "PhotoReviewer"):
        self._bursts     = [g for g in app._bursts if len(g) >= 2]
        self._burst_idx  = 0
        self._selected: set[str] = set()
        self._listbox: Optional[tk.Listbox] = None
        super().__init__(parent, app, f"Review Bursts  —  {len(self._bursts)} groups")
        if not self._bursts:
            tk.Label(self._extra, text="No burst groups found.",
                     bg=C_BG, fg=C_MUTED, font=("Helvetica", 11)).pack(pady=40)
            return
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

    def _build_side_panel(self, body: tk.Frame) -> None:
        """Burst list on the left of the grid."""
        if not self._bursts:
            return
        lf = tk.Frame(body, bg=C_SURFACE, width=180)
        lf.pack(side="left", fill="y")
        lf.pack_propagate(False)
        tk.Label(lf, text="BURSTS", bg=C_SURFACE, fg=C_MUTED,
                 font=("Helvetica", 8, "bold")).pack(anchor="w", padx=10, pady=(8, 2))
        nav = tk.Frame(lf, bg=C_SURFACE)
        nav.pack(side="bottom", fill="x", pady=4)
        tk.Button(nav, text="◀ Prev", bg=C_BORDER, fg=C_TEXT, relief="flat", bd=0,
                  command=lambda: self._load_burst(self._burst_idx - 1)
                  ).pack(side="left", padx=3, fill="x", expand=True)
        tk.Button(nav, text="Next ▶", bg=C_BORDER, fg=C_TEXT, relief="flat", bd=0,
                  command=lambda: self._load_burst(self._burst_idx + 1)
                  ).pack(side="right", padx=3, fill="x", expand=True)
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

    def _load_burst(self, idx: int) -> None:
        idx = max(0, min(len(self._bursts) - 1, idx))
        self._burst_idx = idx
        self._selected.clear()
        group = self._bursts[idx]
        self._hdr_lbl.configure(
            text=f"Burst {idx+1}/{len(self._bursts)}  —  {len(group)} images")
        if self._listbox:
            self._listbox.selection_clear(0, "end")
            self._listbox.selection_set(idx)
            self._listbox.see(idx)
        self.set_paths(group)

    def _make_cell(self, parent: tk.Frame, path: str) -> tk.Frame:
        cell = self._cell(parent, path, show_scores=True)
        make_tag_buttons(cell, self._app, path, self._tag_burst, words=True, size=8).pack(
            fill="x", padx=4, pady=(0, 2))
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
        self._app._toggle_tag_for(path, tag)
        self._after_tag(path)

    def _on_list_select(self, _=None) -> None:
        sel = self._listbox.curselection()
        if sel and sel[0] != self._burst_idx:
            self._load_burst(sel[0])

    def _skip(self) -> None:
        self._load_burst(self._burst_idx + 1)

    def _confirm_next(self) -> None:
        for path in self._selected:
            self._app._set_tag(path, "best", True)
            self._app._after_tag_change(path)
        if self._listbox:
            self._listbox.itemconfig(
                self._burst_idx, fg=C_BEST if self._selected else C_MUTED)
        if self._burst_idx < len(self._bursts) - 1:
            self._load_burst(self._burst_idx + 1)
        else:
            mb.showinfo("Complete", f"All {len(self._bursts)} bursts reviewed.")
            self._app._hide_overlay()

# ── Sightings ────────────────────────────────────────────────────────────────

def _gap_text(secs: int) -> str:
    return f"{secs // 60} min"


class InlineSightingsView(ThumbnailGrid):
    """
    Review by sighting: bursts of the same animal close together in time.
    Every frame is shown (blurred and unusual ones included) and nothing is
    tagged for you; you tag with B / D / I as anywhere else, confirm the
    species once for the whole sighting, then Tab to the next one.
    """

    def __init__(self, parent: tk.Frame, app: "PhotoReviewer"):
        self._sightings: list[dict] = []
        self._i = 0
        self._list: Optional[tk.Listbox] = None
        self._pick_var = tk.BooleanVar(value=bool(app._app_settings.get("sightings_show_pick", True)))
        gap = int(app._app_settings.get("sighting_gap", DEFAULT_GAP_SECS))
        self._gap_var = tk.StringVar(value=_gap_text(gap))
        super().__init__(parent, app, "Sightings")
        self._show_pick = self._pick_var.get()
        self._build_species_bar()
        self._reload(keep=app._current_path)
        self.after(1500, self._poll_pending)

    # ── Layout ───────────────────────────────────────────────────────────────

    def _make_toolbar(self, bar: tk.Frame) -> None:
        tk.Button(bar, text="Done → Next  Tab", bg=C_BEST, fg="white",
                  font=("Helvetica", 9, "bold"), relief="flat", bd=0, padx=10,
                  command=self._done_next).pack(side="right", padx=4, pady=8)
        tk.Button(bar, text="Next ]", bg=C_BORDER, fg=C_TEXT, font=("Helvetica", 9),
                  relief="flat", bd=0, padx=8,
                  command=lambda: self._load(self._i + 1)).pack(side="right", padx=2, pady=8)
        tk.Button(bar, text="[ Prev", bg=C_BORDER, fg=C_TEXT, font=("Helvetica", 9),
                  relief="flat", bd=0, padx=8,
                  command=lambda: self._load(self._i - 1)).pack(side="right", padx=2, pady=8)
        tk.Checkbutton(bar, text="Show app's pick ◆", variable=self._pick_var,
                       command=self._toggle_pick, bg=C_SURFACE, fg=C_MUTED,
                       selectcolor=C_BG, activebackground=C_SURFACE,
                       font=("Helvetica", 9)).pack(side="right", padx=8)
        cb = ttk.Combobox(bar, textvariable=self._gap_var, width=7, state="readonly",
                          style="Name.TCombobox",
                          values=[_gap_text(g) for g in GAP_CHOICES])
        cb.pack(side="right", pady=10)
        cb.bind("<<ComboboxSelected>>", self._change_gap)
        tk.Label(bar, text="New sighting after a gap of", bg=C_SURFACE, fg=C_MUTED,
                 font=("Helvetica", 9)).pack(side="right", padx=(10, 4))
        self._hdr_lbl = tk.Label(bar, text="", bg=C_SURFACE, fg=C_MUTED,
                                 font=("Helvetica", 9))
        self._hdr_lbl.pack(side="left", padx=10)

    def _build_side_panel(self, body: tk.Frame) -> None:
        lf = tk.Frame(body, bg=C_SURFACE, width=270)
        lf.pack(side="left", fill="y")
        lf.pack_propagate(False)
        tk.Label(lf, text="SIGHTINGS", bg=C_SURFACE, fg=C_MUTED,
                 font=("Helvetica", 8, "bold")).pack(anchor="w", padx=10, pady=(8, 2))
        self._pending_lbl = tk.Label(lf, text="", bg=C_SURFACE, fg=C_MUTED, anchor="w",
                                     justify="left", wraplength=250, font=("Helvetica", 8))
        self._pending_lbl.pack(side="bottom", fill="x", padx=8, pady=4)
        sb = tk.Scrollbar(lf)
        sb.pack(side="right", fill="y")
        self._list = tk.Listbox(lf, yscrollcommand=sb.set, bg=C_BG, fg=C_TEXT,
                                selectbackground=C_ACCENT, font=("Helvetica", 9), bd=0,
                                highlightthickness=0, activestyle="none")
        self._list.pack(fill="both", expand=True, padx=4)
        sb.config(command=self._list.yview)
        self._list.bind("<<ListboxSelect>>", self._on_list_select)

    def _build_species_bar(self) -> None:
        bar = tk.Frame(self._extra, bg=C_BORDER)
        bar.pack(fill="x")
        tk.Label(bar, text="SPECIES FOR THIS SIGHTING", bg=C_BORDER, fg=C_MUTED,
                 font=("Helvetica", 8, "bold")).pack(side="left", padx=(10, 6), pady=6)
        self._species = ttk.Combobox(bar, style="Name.TCombobox", width=34,
                                     font=("Helvetica", 10))
        self._species.pack(side="left", pady=6)
        self._species.bind("<<ComboboxSelected>>", lambda e: self._confirm())
        self._species.bind("<Return>", lambda e: (self._confirm(), "break")[1])
        tk.Button(bar, text="Confirm for whole sighting  N", bg=C_ACCENT, fg="white",
                  font=("Helvetica", 9, "bold"), relief="flat", bd=0, padx=10,
                  command=self._confirm).pack(side="left", padx=8)
        self._sp_state = tk.Label(bar, text="", bg=C_BORDER, fg=C_MUTED,
                                  font=("Helvetica", 9))
        self._sp_state.pack(side="left", padx=6)

    # ── Data ─────────────────────────────────────────────────────────────────

    def _reload(self, keep: Optional[str] = None) -> None:
        """Regroup (after a species confirmation, gap change or new suggestions)."""
        self._sightings = self._app._build_sightings()
        idx = 0
        if keep:
            idx = next((i for i, s in enumerate(self._sightings) if keep in s["paths"]), 0)
        self._fill_list()
        if self._sightings:
            self._load(idx)
        else:
            self._hdr_lbl.configure(text="No photos to group yet")
            self.set_paths([])

    def _line(self, s: dict) -> str:
        when = datetime.datetime.fromtimestamp(s["start"]).strftime("%H:%M") \
            if s["start"] is not None else "--:--"
        name = s["name"] or "Unidentified"
        best = sum(1 for p in s["paths"] if self._app.status.get(p, {}).get("best"))
        done = "✔ " if self._app._sighting_done(s) else "   "
        return (f"{done}{when}  {name}{' ✓' if s['confirmed'] else ''}"
                f"  ·  {len(s['paths'])}" + (f"  · ★{best}" if best else ""))

    def _fill_list(self) -> None:
        if not self._list:
            return
        self._list.delete(0, "end")
        for i, s in enumerate(self._sightings):
            self._list.insert("end", self._line(s))
            self._colour_row(i)

    def _colour_row(self, i: int) -> None:
        s = self._sightings[i]
        self._list.itemconfig(i, fg=C_MUTED if self._app._sighting_done(s)
                              else (C_BEST if s["confirmed"] else C_TEXT))

    def _update_row(self, i: int) -> None:
        if not self._list or not (0 <= i < len(self._sightings)):
            return
        sel = self._list.curselection()
        self._list.delete(i)
        self._list.insert(i, self._line(self._sightings[i]))
        self._colour_row(i)
        if sel:
            self._list.selection_set(sel[0])

    def _load(self, i: int) -> None:
        if not self._sightings:
            return
        i = max(0, min(len(self._sightings) - 1, i))
        self._i = i
        s = self._sightings[i]
        starts = {}
        for k, g in enumerate(s["groups"]):
            ts = [self._app._timestamps.get(p) for p in g if self._app._timestamps.get(p)]
            when = datetime.datetime.fromtimestamp(min(ts)).strftime("%H:%M:%S") if ts else ""
            what = "burst" if len(g) > 1 else "single frame"
            starts[g[0]] = (f"  ▸ {what.capitalize()} {k + 1} of {len(s['groups'])}"
                            f"  ·  {len(g)} frame{'s' if len(g) > 1 else ''}  ·  {when}")
        self._group_starts = starts
        self._cursor = s["paths"][0]
        self._sel.clear()
        self.set_paths(list(s["paths"]))
        if self._list:
            self._list.selection_clear(0, "end")
            self._list.selection_set(i)
            self._list.see(i)
        span = ""
        if s["start"] is not None and s["end"] is not None:
            a = datetime.datetime.fromtimestamp(s["start"])
            b = datetime.datetime.fromtimestamp(s["end"])
            span = f"  ·  {a:%Y-%m-%d %H:%M}–{b:%H:%M}"
        self._hdr_lbl.configure(
            text=f"Sighting {i + 1} of {len(self._sightings)}  ·  {len(s['paths'])} frames"
                 f" in {len(s['groups'])} burst(s){span}")
        self._update_species()

    def _update_species(self) -> None:
        if not self._sightings:
            return
        s = self._sightings[self._i]
        app = self._app
        tops = [app._species_top(g[0]) for g in s["groups"]]
        best = max((t for t in tops if t), key=lambda t: t[0][2], default=None)
        choices = [f"{n} — {p:.0%}" for n, _s, p in (best or [])]
        choices += [n for n in app._species_hist.recent()
                    if n not in [c.split(" — ")[0] for c in choices]]
        self._species.configure(values=choices)
        if self.focus_get() is self._species:
            return
        if s["confirmed"]:
            self._species.set(s["name"])
            n = sum(1 for p in s["paths"] if app.status.get(p, {}).get("species"))
            self._sp_state.configure(text=f"✓ Confirmed on {n} of {len(s['paths'])} frames",
                                     fg=C_BEST)
        elif s["name"]:
            self._species.set(s["name"])
            self._sp_state.configure(text="Suggested — N to confirm, or pick / type another",
                                     fg=C_WARN)
        else:
            self._species.set("")
            self._sp_state.configure(
                text="Identifying…" if any(t is None for t in tops) and app._species_model
                else "No suggestion — type a name, or tag I to identify later", fg=C_MUTED)

    # ── Actions ──────────────────────────────────────────────────────────────

    def _confirm(self) -> None:
        if not self._sightings:
            return
        name = self._species.get().strip()
        if not name:
            self._app._flash_title("Type or pick a species first", 1800)
            return
        s = self._sightings[self._i]
        self._app._confirm_species_for(s["paths"], name)
        self._canvas.focus_set()
        self._reload(keep=s["paths"][0])

    def _done_next(self) -> None:
        if not self._sightings:
            return
        s = self._sightings[self._i]
        self._app._mark_seen(s["paths"])
        self._update_row(self._i)
        nxt = next((j for j in range(self._i + 1, len(self._sightings))
                    if not self._app._sighting_done(self._sightings[j])), None)
        if nxt is None:
            nxt = next((j for j in range(len(self._sightings))
                        if not self._app._sighting_done(self._sightings[j])), None)
        if nxt is None:
            self._app._flash_title(f"All {len(self._sightings)} sightings done", 2500)
            return
        self._load(nxt)

    def _toggle_pick(self) -> None:
        self._show_pick = self._pick_var.get()
        self._app._app_settings["sightings_show_pick"] = self._show_pick
        save_app_settings(self._app._app_settings)
        self._relayout(keep_scroll=True)

    def _change_gap(self, _e=None) -> None:
        secs = next((g for g in GAP_CHOICES if _gap_text(g) == self._gap_var.get()),
                    DEFAULT_GAP_SECS)
        self._app._app_settings["sighting_gap"] = secs
        save_app_settings(self._app._app_settings)
        keep = self._sightings[self._i]["paths"][0] if self._sightings else None
        self._canvas.focus_set()
        self._reload(keep=keep)

    def _on_list_select(self, _e=None) -> None:
        sel = self._list.curselection()
        if sel and sel[0] != self._i:
            self._load(sel[0])

    def _after_batch(self, paths: list[str]) -> None:
        super()._after_batch(paths)
        self._update_row(self._i)

    def _after_tag(self, path: str, relayout: bool = False) -> None:
        super()._after_tag(path, relayout)
        self._update_row(self._i)

    def _poll_pending(self) -> None:
        """Say when species suggestions are still coming (they refine the grouping)."""
        if not self._alive:
            return
        n = len(self._app._species_pending)
        analysing = self._app._analysis_running()
        msg = []
        if n:
            msg.append(f"Identifying {n} more burst(s)…")
        if analysing:
            msg.append("Analysis still running…")
        if msg:
            msg.append("Press R to regroup with the latest results.")
        self._pending_lbl.configure(text="  ".join(msg))
        if self._sightings:
            self._update_species()
        self.after(1500, self._poll_pending)

    def handle_key(self, ks: str, ctrl: bool, shift: bool) -> bool:
        if not ctrl:
            if ks in ("n", "N"):
                self._confirm()
                return True
            if ks == "bracketright":
                self._load(self._i + 1)
                return True
            if ks == "bracketleft":
                self._load(self._i - 1)
                return True
            if ks in ("r", "R"):
                keep = self._sightings[self._i]["paths"][0] if self._sightings else None
                self._reload(keep=keep)
                return True
        return super().handle_key(ks, ctrl, shift)

    def handle_tab(self) -> None:
        self._done_next()



# ── ID review ────────────────────────────────────────────────────────────────

ID_STATES = {"confirmed": ("✓", C_BEST), "unsure": ("?", C_WARN),
             "unidentifiable": ("✕", C_MUTED), None: ("•", C_TEXT)}


class InlineIdReviewView(tk.Frame):
    """
    Work through ID-tagged sightings (an ID-tagged burst counts as one):
    see the bird large, the species suggestions, and confirm / mark unsure /
    not identifiable. Confirmed species are written to every frame of the
    sighting (and to the XMP).
    """

    def __init__(self, parent: tk.Frame, app: "PhotoReviewer"):
        super().__init__(parent, bg=C_BG)
        self._app = app
        self._pending_only = tk.BooleanVar(value=True)
        self._sightings: list[list[str]] = []
        self._i = 0
        self._frame = 0
        self._tk_img = None
        self._build()
        self._reload()

    # ── layout ────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        bar = tk.Frame(self, bg=C_SURFACE, height=44)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        tk.Button(bar, text="← Back", bg=C_BORDER, fg=C_TEXT, font=("Helvetica", 10, "bold"),
                  relief="flat", bd=0, padx=12, command=self._app._hide_overlay
                  ).pack(side="left", padx=8, pady=8)
        self._title = tk.Label(bar, text="", bg=C_SURFACE, fg=C_TEXT, font=("Helvetica", 12, "bold"))
        self._title.pack(side="left", padx=8)
        tk.Button(bar, text="ID inbox (all folders)…", bg=C_BORDER, fg=C_TEXT, relief="flat",
                  bd=0, padx=8, font=("Helvetica", 9), command=self._app._id_inbox
                  ).pack(side="right", padx=8)
        tk.Checkbutton(bar, text="Only still to identify", variable=self._pending_only,
                       command=self._reload, bg=C_SURFACE, fg=C_MUTED, selectcolor=C_BG,
                       activebackground=C_SURFACE, font=("Helvetica", 9)).pack(side="right", padx=8)

        body = tk.Frame(self, bg=C_BG)
        body.pack(fill="both", expand=True)
        left = tk.Frame(body, bg=C_SURFACE, width=220)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        self._list = tk.Listbox(left, bg=C_BG, fg=C_TEXT, selectbackground=C_ACCENT,
                                font=("Helvetica", 9), bd=0, highlightthickness=0,
                                activestyle="none")
        self._list.pack(fill="both", expand=True, padx=4, pady=4)
        self._list.bind("<<ListboxSelect>>", lambda e: self._select(self._list.curselection()))

        right = tk.Frame(body, bg=C_SURFACE, width=300)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)
        self._canvas = tk.Canvas(body, bg="#050505", highlightthickness=0)
        self._canvas.pack(side="left", fill="both", expand=True)
        self._canvas.bind("<Configure>", lambda e: self._draw())
        self._canvas.bind("<Double-Button-1>", lambda e: self._open_in_viewer())

        tk.Label(right, text="SUGGESTIONS  (press 1–5)", bg=C_SURFACE, fg=C_MUTED,
                 font=("Helvetica", 8, "bold")).pack(anchor="w", padx=12, pady=(12, 4))
        self._sugg = tk.Frame(right, bg=C_SURFACE)
        self._sugg.pack(fill="x", padx=8)
        tk.Label(right, text="Species", bg=C_SURFACE, fg=C_MUTED,
                 font=("Helvetica", 8)).pack(anchor="w", padx=12, pady=(12, 0))
        self._species = ttk.Combobox(right, style="Name.TCombobox", font=("Helvetica", 11))
        self._species.pack(fill="x", padx=12)
        self._species.bind("<Return>", lambda e: (self._confirm(), "break")[1])
        tk.Label(right, text="Note (optional)", bg=C_SURFACE, fg=C_MUTED,
                 font=("Helvetica", 8)).pack(anchor="w", padx=12, pady=(8, 0))
        self._note = tk.Entry(right, bg=C_BG, fg=C_TEXT, insertbackground=C_TEXT,
                              relief="flat", font=("Helvetica", 10))
        self._note.pack(fill="x", padx=12)
        for text, cmd, col in (("✓ Confirm  (Enter)", self._confirm, C_BEST),
                               ("?  Unsure  (U)", lambda: self._mark("unsure"), C_WARN),
                               ("✕  Not identifiable  (X)", lambda: self._mark("unidentifiable"), C_BORDER),
                               ("Open in viewer — zoom / loupe  (V)", self._open_in_viewer, C_BORDER)):
            tk.Button(right, text=text, bg=col, fg="white", relief="flat", bd=0,
                      font=("Helvetica", 10, "bold"), command=cmd
                      ).pack(fill="x", padx=12, pady=(10, 0), ipady=4)
        self._state = tk.Label(right, text="", bg=C_SURFACE, fg=C_MUTED, justify="left",
                               wraplength=270, font=("Helvetica", 9))
        self._state.pack(fill="x", padx=12, pady=12)
        tk.Label(right, text="← → sighting · ↑ ↓ frame · Esc closes", bg=C_SURFACE, fg=C_MUTED,
                 font=("Helvetica", 8)).pack(side="bottom", pady=8)

    # ── data ──────────────────────────────────────────────────────────────────

    def _reload(self, keep: Optional[str] = None) -> None:
        app = self._app
        every = app._id_sightings()
        state = lambda g: app.status.get(g[0], {}).get("id_status")   # noqa: E731
        done = sum(1 for g in every if state(g) in ("confirmed", "unidentifiable"))
        self._title.configure(text=f"ID Review — {done} of {len(every)} sightings done")
        self._sightings = [g for g in every if not self._pending_only.get()
                           or state(g) not in ("confirmed", "unidentifiable")]
        self._list.delete(0, "end")
        for g in self._sightings:
            t = app.status.get(g[0], {})
            sym, col = ID_STATES.get(t.get("id_status"), ID_STATES[None])
            name = t.get("species") or (app._species_top(g[0]) or [["?"]])[0][0] + " ?"
            self._list.insert("end", f" {sym}  {Path(g[0]).stem}  {name}"
                                     + (f"  ×{len(g)}" if len(g) > 1 else ""))
            self._list.itemconfig("end", fg=col)
        if not self._sightings:
            self._canvas.delete("all")
            self._canvas.create_text(300, 200, fill=C_MUTED, font=("Helvetica", 12),
                                     text="Nothing left to identify here 🎉\n\n"
                                          "Tag frames with I (ID) while culling to add them.")
            self._state.configure(text="")
            return
        idx = next((i for i, g in enumerate(self._sightings) if keep in g), None)
        self._show(min(self._i, len(self._sightings) - 1) if idx is None else idx)

    def _select(self, sel) -> None:
        if sel:
            self._show(sel[0])

    def _show(self, i: int) -> None:
        self._i = max(0, min(len(self._sightings) - 1, i))
        self._frame = 0
        self._list.selection_clear(0, "end")
        self._list.selection_set(self._i)
        self._list.see(self._i)
        g = self._sightings[self._i]
        app = self._app
        t = app.status.get(g[0], {})
        top = app._species_top(g[0])
        if top is None:
            app._request_species(g[0], urgent=True)
            self.after(800, lambda: self._refresh_suggestions(g))
        self._fill_suggestions(top)
        self._species.configure(values=[n for n, _s, _p in (top or [])] +
                                [n for n in app._species_hist.recent()
                                 if n not in [x[0] for x in (top or [])]])
        self._species.set(t.get("species") or (top[0][0] if top else ""))
        self._note.delete(0, "end")
        self._note.insert(0, t.get("id_note", ""))
        state = t.get("id_status")
        self._state.configure(text={"confirmed": f"✓ Confirmed: {t.get('species', '')}",
                                    "unsure": "? Marked unsure",
                                    "unidentifiable": "✕ Marked not identifiable"}.get(
            state, "Not identified yet"), fg=ID_STATES.get(state, ID_STATES[None])[1])
        self._draw()

    def _refresh_suggestions(self, g: list[str]) -> None:
        if self._sightings and self._sightings[self._i] is g:
            top = self._app._species_top(g[0])
            if top is None:
                self.after(800, lambda: self._refresh_suggestions(g))
            else:
                self._fill_suggestions(top)
                if not self._app.status.get(g[0], {}).get("species"):
                    self._species.set(top[0][0])

    def _fill_suggestions(self, top: Optional[list]) -> None:
        for w in self._sugg.winfo_children():
            w.destroy()
        if not top:
            tk.Label(self._sugg, text="Identifying…" if self._app._species_model else
                     "Species suggestions unavailable", bg=C_SURFACE, fg=C_MUTED,
                     font=("Helvetica", 9)).pack(anchor="w", padx=4)
            return
        for k, (name, sci, p) in enumerate(top[:5], 1):
            unusual = self._app._not_local(sci)
            tk.Button(self._sugg, text=f"{k}  {name}  {p:.0%}" + ("  ⚠" if unusual else ""),
                      anchor="w", bg=C_BG,
                      fg=C_BEST if p >= 0.6 else (C_WARN if p >= 0.25 else C_TEXT),
                      relief="flat", bd=0, font=("Helvetica", 10), padx=6,
                      command=lambda n=name: self._confirm(n)).pack(fill="x", pady=1)
            tk.Label(self._sugg, text=f"     {sci}" + (f" · {unusual}" if unusual else ""),
                     anchor="w", bg=C_SURFACE, fg=C_MUTED,
                     font=("Helvetica", 7, "italic")).pack(fill="x")

    def _draw(self) -> None:
        self._canvas.delete("all")
        if not self._sightings:
            return
        g = self._sightings[self._i]
        path = g[self._frame % len(g)]
        cw, ch = max(50, self._canvas.winfo_width()), max(50, self._canvas.winfo_height())
        try:
            img = load_preview(path, max(cw, ch))
            img.thumbnail((cw, ch), Image.LANCZOS)
        except Exception as e:
            self._canvas.create_text(cw // 2, ch // 2, text=f"Cannot load\n{e}", fill=C_MUTED)
            return
        self._tk_img = ImageTk.PhotoImage(img)
        self._canvas.create_image(cw // 2, ch // 2, image=self._tk_img)
        self._canvas.create_text(10, 10, anchor="nw", fill=C_TEXT, font=("Helvetica", 9, "bold"),
                                 text=f"{Path(path).name}" + (f"   frame {self._frame % len(g) + 1}"
                                                              f" of {len(g)} (↑ ↓)" if len(g) > 1 else ""))

    # ── actions ───────────────────────────────────────────────────────────────

    def _confirm(self, name: Optional[str] = None) -> None:
        if not self._sightings:
            return
        name = (name or self._species.get()).strip()
        if not name:
            self._app._flash_title("Type or pick a species first", 1500)
            return
        g = self._sightings[self._i]
        self._app._set_id_result(g, "confirmed", name, self._note.get().strip())
        self._next_after_change(g)

    def _mark(self, state: str) -> None:
        if self._sightings:
            g = self._sightings[self._i]
            self._app._set_id_result(g, state, None, self._note.get().strip())
            self._next_after_change(g)

    def _next_after_change(self, g: list[str]) -> None:
        nxt = self._sightings[self._i + 1][0] if self._i + 1 < len(self._sightings) else None
        self._reload(keep=nxt if self._pending_only.get() or nxt else g[0])
        if not self._pending_only.get() and nxt:
            self._show(self._i)

    def _open_in_viewer(self) -> None:
        if self._sightings:
            g = self._sightings[self._i]
            path = g[self._frame % len(g)]
            self._app._hide_overlay()
            self._app._jump_to_index(self._app.files.index(path))
            self._app._flash_title("Press the ID Review button to go back to identifying", 3000)

    def handle_key(self, ks: str, ctrl: bool, shift: bool) -> bool:
        if not self._sightings:
            return False
        if ks in ("Right", "Next"):
            self._show(self._i + 1)
        elif ks in ("Left", "Prior"):
            self._show(self._i - 1)
        elif ks in ("Down", "Up"):
            self._frame += 1 if ks == "Down" else -1
            self._draw()
        elif ks in ("Return", "KP_Enter"):
            self._confirm()
        elif ks in "12345" and len(ks) == 1:
            top = self._app._species_top(self._sightings[self._i][0]) or []
            if int(ks) <= len(top):
                self._confirm(top[int(ks) - 1][0])
        elif ks in ("u", "U"):
            self._mark("unsure")
        elif ks in ("x", "X"):
            self._mark("unidentifiable")
        elif ks in ("v", "V"):
            self._open_in_viewer()
        else:
            return False
        return True


# ── AI search ────────────────────────────────────────────────────────────────

class InlineSearchView(ThumbnailGrid):
    """Search the photo library by description; results in a grid."""

    def __init__(self, parent: tk.Frame, app: "PhotoReviewer"):
        self._query = tk.StringVar(value=app._app_settings.get("last_search", ""))
        self._best_only = tk.BooleanVar(value=False)
        self._hide_deleted = tk.BooleanVar(value=True)
        self._min_stars = tk.StringVar(value="Any")
        self._species_f = tk.StringVar()
        self._year_f = tk.StringVar()
        self._sim: dict[str, float] = {}
        super().__init__(parent, app, "Search")
        self._build_filters()
        self._dividers = False
        self.after(300, self._poll_status)
        if self._query.get():
            self.after(400, self._run)

    def _make_toolbar(self, bar: tk.Frame) -> None:
        e = ctk.CTkEntry(bar, textvariable=self._query, width=420,
                         placeholder_text="Describe the photo, e.g. robin in the snow",
                         font=ctk.CTkFont("Helvetica", 12))
        e.pack(side="left", padx=8, pady=7)
        e.bind("<Return>", lambda _e: self._run())
        tk.Button(bar, text="Search", bg=C_ACCENT, fg="white", relief="flat", bd=0, padx=12,
                  font=("Helvetica", 10, "bold"), command=self._run).pack(side="left", pady=8)
        self.after(100, e.focus_set)

    def _build_filters(self) -> None:
        f = tk.Frame(self._extra, bg=C_BORDER)
        f.pack(fill="x")
        tk.Checkbutton(f, text="Best only", variable=self._best_only, command=self._run,
                       bg=C_BORDER, fg=C_TEXT, selectcolor=C_BG, activebackground=C_BORDER,
                       font=("Helvetica", 9)).pack(side="left", padx=6, pady=3)
        tk.Checkbutton(f, text="Hide Delete-tagged", variable=self._hide_deleted, command=self._run,
                       bg=C_BORDER, fg=C_TEXT, selectcolor=C_BG, activebackground=C_BORDER,
                       font=("Helvetica", 9)).pack(side="left", padx=6)
        tk.Label(f, text="Stars ≥", bg=C_BORDER, fg=C_MUTED, font=("Helvetica", 9)).pack(side="left", padx=(10, 2))
        cb = ttk.Combobox(f, textvariable=self._min_stars, values=["Any", "1", "2", "3", "4", "5"],
                          width=4, state="readonly", style="Name.TCombobox")
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self._run())
        for label, var, w in (("Species", self._species_f, 14), ("Year", self._year_f, 6)):
            tk.Label(f, text=label, bg=C_BORDER, fg=C_MUTED, font=("Helvetica", 9)).pack(side="left", padx=(10, 2))
            ent = tk.Entry(f, textvariable=var, width=w, bg=C_BG, fg=C_TEXT, relief="flat",
                           insertbackground=C_TEXT)
            ent.pack(side="left")
            ent.bind("<Return>", lambda e: self._run())
        tk.Button(f, text="Library folders…", bg=C_BG, fg=C_TEXT, relief="flat", bd=0, padx=8,
                  font=("Helvetica", 9), command=self._change_library).pack(side="right", padx=6)
        self._status = tk.Label(self._extra, text="", bg=C_BG, fg=C_MUTED, anchor="w",
                                font=("Helvetica", 9))
        self._status.pack(fill="x", padx=10, pady=2)

    def _change_library(self) -> None:
        self._app._manage_library()

    def _poll_status(self) -> None:
        if not self._alive:
            return
        self._status.configure(text=self._app._search_status_text()
                               + (f"   ·   {len(self._paths)} results" if self._paths else ""))
        self.after(500, self._poll_status)

    def _run(self) -> None:
        q = self._query.get().strip()
        if not q:
            return
        self._app._app_settings["last_search"] = q
        try:
            stars = 0 if self._min_stars.get() == "Any" else int(self._min_stars.get())
        except ValueError:
            stars = 0
        results = self._app._search(q, best_only=self._best_only.get(),
                                    hide_deleted=self._hide_deleted.get(), min_stars=stars,
                                    species=self._species_f.get().strip(),
                                    year=self._year_f.get().strip())
        if results is None:
            return
        self._sim = dict(results)
        self.set_paths([p for p, _s in results])

    def _make_cell(self, parent: tk.Frame, path: str) -> tk.Frame:
        cell = self._cell(parent, path, show_scores=False)
        tk.Label(cell, text=f"{Path(path).parent.name}  ·  match {self._sim.get(path, 0):.2f}",
                 bg=C_SURFACE, fg=C_MUTED, font=("Helvetica", 7)).pack(padx=4, pady=(0, 4))
        off = self._app._offline.get(path)
        if off and not os.path.exists(path):
            tk.Label(cell, text=f"⏏ On {off[1]} — plug in to open", bg=C_SURFACE, fg=C_WARN,
                     font=("Helvetica", 7, "bold")).pack(padx=4, pady=(0, 4))
        return cell

    def _jump_and_close(self, path: str) -> None:
        self._app._open_photo_anywhere(path)

    def handle_key(self, ks: str, ctrl: bool, shift: bool) -> bool:
        if not ctrl and ks in ("b", "B", "d", "D", "i", "I", "space", "BackSpace") or \
                (len(ks) == 1 and ks in "012345"):
            self._app._flash_title("Open a photo (double-click) to tag it in its folder", 2000)
            return True
        return super().handle_key(ks, ctrl, shift)

    def _update_sel_bar(self) -> None:
        n = len(self._sel)
        if not n:
            if self._sel_bar is not None:
                self._sel_bar.pack_forget()
            return
        if self._sel_bar is None:
            bar = self._sel_bar = tk.Frame(self._extra, bg="#2a1f3d")
            self._sel_lbl = tk.Label(bar, bg="#2a1f3d", fg="white", font=("Helvetica", 9, "bold"))
            self._sel_lbl.pack(side="left", padx=10, pady=4)
            tk.Button(bar, text="Copy to folder…", bg=C_ACCENT, fg="white", relief="flat", bd=0,
                      padx=8, font=("Helvetica", 9, "bold"),
                      command=lambda: self._app._copy_photos(self._targets())).pack(side="left", padx=2)
            tk.Button(bar, text="Show in Explorer", bg=C_BORDER, fg=C_TEXT, relief="flat", bd=0,
                      padx=8, font=("Helvetica", 9),
                      command=lambda: self._app._reveal(self._targets()[0])).pack(side="left", padx=2)
            tk.Button(bar, text="Clear selection", bg=C_BORDER, fg=C_TEXT, relief="flat", bd=0,
                      padx=8, font=("Helvetica", 9),
                      command=lambda: self.handle_key("Escape", False, False)).pack(side="right", padx=8)
        self._sel_lbl.configure(text=f"{n} selected")
        self._sel_bar.pack(fill="x")


# ── Main application ──────────────────────────────────────────────────────────

class PhotoReviewer(ctk.CTkToplevel):

    def __init__(self, master, base_dir: str, files: list[str]):
        super().__init__(master)
        self.base_dir    = str(Path(base_dir))
        self.files       = files
        self.total       = len(files)
        self.index       = 0
        self.status_file = os.path.join(self.base_dir, STATUS_FILENAME)

        # Display state
        self._pil_image: Optional[Image.Image]        = None
        self._tk_thumb:  Optional[ImageTk.PhotoImage] = None
        self._zoom    = 1.0
        self._pan_x   = 0.0
        self._pan_y   = 0.0
        self._pan_start: Optional[tuple[int, int]]    = None

        # Persistent cache file paths
        self.score_file  = os.path.join(self.base_dir, SCORE_CACHE_FILE)

        # Analysis caches — pre-populated from disk cache where available
        self._bursts:     list[list[str]]                = []
        self._file_burst: dict[str, list[str]]           = {}
        self._histograms: dict[str, list[list[int]]]     = {}
        self._hash_ready  = False

        # Persistent cache paths
        self.hash_file  = os.path.join(self.base_dir, HASH_CACHE_FILE)

        # Bumped on folder switch / close so stale background work is ignored
        self._session_gen = 0
        self._executor: Optional[ThreadPoolExecutor] = None
        self._load_caches()

        # Filmstrip GC anchor
        # Filmstrip: drawn thumbnails (reused) and per-image canvas items
        self._fs_photos: "OrderedDict[tuple[str, int], ImageTk.PhotoImage]" = OrderedDict()
        self._fs_tag_items: dict[int, int] = {}      # file index → tag text item
        self._fs_stripe_items: dict[int, int] = {}   # file index → stripe item
        # Undo stack: (path, {field: previous value or _MISSING})
        self._undo_stack: list[tuple[str, dict]] = []
        # Compare set — purely in-memory, never saved
        self._compare_set: list[str] = []   # ordered, max 4
        self._compare_mode: bool = False
        self._compare_tk_images: list[ImageTk.PhotoImage] = []
        self._shortcuts_overlay = None
        self._focus_peaking: bool = False
        self._clip_view: bool = False                     # raw highlight blinkies (H)
        self._clip_masks: OrderedDict = OrderedDict()     # path → oriented clip mask
        self._clip_mask_busy: set = set()
        self._clip_mask_q: queue.Queue = queue.Queue()
        self._loupe_active: bool = False
        self._loupe_tk_img       = None
        self._multi_pass_on: bool = False   # multi-pass culling mode toggle
        self._pass_num: int       = 1       # 1=all, 2=not-deleted, 3=best-only
        self._app_settings: dict = load_app_settings()
        # 0 = off, 1 = after Delete, 2 = after Best and Delete
        self._auto_mode: int = int(self._app_settings.get("auto_advance", 1))
        self._af_grid = (self._app_settings.get("af_grid_width"),
                         self._app_settings.get("af_grid_height"))
        self._detect_on = bool(self._app_settings.get("detect_animals", True))
        # Graphics chip (OpenVINO) for the AI models when available and checked
        reviewer_accel.configure(self._app_settings.get("graphics_acceleration", True),
                                 _user_data_dir())
        self._show_boxes = False                  # P: focus point / animal boxes
        self._overlay_view = None                 # grid view shown in the overlay
        self._batch: Optional[list] = None        # collects one undo step
        self._review_times: list[float] = []      # when images became reviewed
        self._analysis_executor: Optional[ThreadPoolExecutor] = None
        self._analysis_q: queue.Queue = queue.Queue()
        self._analysis_total = self._analysis_done = 0
        self._analysis_eta: Optional[Eta] = None
        self._detector = None
        # Keeper prediction
        self._embedder = None
        self._keeper: dict[str, float] = {}       # path → chance you keep it
        self._picks: set[str] = set()             # predicted best frame per burst
        self._keeper_info: dict = {}
        self._model: Optional[KeeperModel] = KeeperModel.load(_user_data_dir() / MODEL_FILE)
        self._history: Optional[History] = None
        self._history_dirty = False
        self._train_job: Optional[str] = None
        self._training = False
        self._train_q: queue.Queue = queue.Queue()
        self._learn_q: queue.Queue = queue.Queue()
        self._learn_stop = threading.Event()
        self._learning: Optional[dict] = None
        threading.Thread(target=self._load_history, daemon=True).start()
        # Species suggestions (one per burst) for naming
        self._species_model = None
        self._species_hist = SpeciesHistory(_user_data_dir() / SPECIES_HISTORY_FILE)
        self._species_q: queue.Queue = queue.Queue()
        self._species_pending: set[str] = set()
        self._species_bg: Optional[ThreadPoolExecutor] = None
        self._species_now = ThreadPoolExecutor(max_workers=1)    # current burst first
        # Eye check: finds the animal's eye and compares its sharpness within the burst
        self._eye_on = bool(self._app_settings.get("eye_check", True))
        self._eye_q: queue.Queue = queue.Queue()
        self._eye_pending: set[str] = set()
        self._eye_urgent: set[str] = set()
        self._eye_bg: Optional[ThreadPoolExecutor] = None
        self._eye_now = ThreadPoolExecutor(max_workers=1)        # the photo on screen
        self._eye_view = False                                   # E zoomed onto the eye
        # Raw highlight clipping: its own pass after the analysis (unpacking a
        # NEF takes about a second), the photo on screen first
        self._rawclip_q: queue.Queue = queue.Queue()
        self._rawclip_pending: set[str] = set()
        self._rawclip_urgent: set[str] = set()
        self._rawclip_bg: Optional[ThreadPoolExecutor] = None
        self._rawclip_now = ThreadPoolExecutor(max_workers=1)
        self._name_burst = bool(self._app_settings.get("name_whole_burst", True))
        # Region hint: species recorded in the folder's country rank higher
        self._regions = RegionLists(_user_data_dir() / "regions")
        self._region_q: queue.Queue = queue.Queue()
        self._region_error: dict[str, str] = {}
        # AI search: per-folder CLIP indexes for the photo library
        self._clip_encoder = None
        self._library: dict[str, tuple] = {}          # folder → (names, mtimes, matrix)
        self._index_q: queue.Queue = queue.Queue()
        self._index_stop = threading.Event()
        self._indexing: Optional[dict] = None
        self._index_restart = False
        self._offline: dict[str, tuple[str, str]] = {}   # result path → (thumbnail, drive)
        self._offline_cache: dict[str, tuple] = {}       # offline file → (mtime, data)
        self._status_cache: dict[str, tuple] = {}     # folder → (mtime, status)
        self._keywords: list[str] = self._app_settings.get("keywords", [])
        self._folder_tree_visible: bool = True

        # Preload cache: path → pre-sized PIL Image ready for canvas
        self._preload_cache: dict[str, Image.Image] = {}
        # EXIF cache — read once per file, never again
        self._exif_cache: dict[str, dict[str, str]] = {}
        self._current_path: Optional[str] = None  # image on screen (read by workers)
        self._init_background()
        self._meta_path: Optional[str] = None     # image the metadata fields show
        self._score_save_job: Optional[str] = None
        self._status_save_job: Optional[str] = None
        self._status_backed_up = False            # .bak made once per session
        self._title_job: Optional[str] = None
        self._busy = False                        # True while Process runs
        self._lanczos_job: Optional[str] = None  # after() job id
        # Canvas size hint for preload workers (safe to read from threads)
        self._canvas_w: int = 1200
        self._canvas_h: int = 800

        # Fullscreen state
        self._is_fullscreen     = False

        self._build_status()
        self._build_ui()
        self.bind_all("<KeyPress>", self._on_key)
        self.bind_all("<Tab>", self._on_tab)
        self.after(500, self._poll_background)

        # _show_current is called after fullscreen is applied (in _go_fullscreen)
        # so canvas dimensions are real when we first render
        self.after(300, self._start_hash_workers)

    # ── Status ────────────────────────────────────────────────────────────────

    def _build_status(self) -> None:
        """Tags for this folder (see reviewer_storage.session_status)."""
        self.status: dict[str, dict] = session_status(self.base_dir, self.files)

    def _load_caches(self) -> None:
        """Load score / hash / timestamp caches for the current folder."""
        valid = set(self.files)
        self._scores: dict[str, dict[str, int]] = load_score_cache(self.score_file, valid)
        self._hashes, self._timestamps, self._focus = load_hash_cache(self.hash_file, valid)
        self.detect_file = os.path.join(self.base_dir, DETECT_CACHE_FILE)
        self._detections: dict[str, list] = load_detections(self.detect_file, valid, MODEL_ID)
        self._emb = load_features(self.base_dir, valid)
        self._species_pred: dict[str, list] = load_predictions(self.base_dir, valid)
        self._species_dirty = False
        self._eyes: dict[str, dict] = load_eyes(self.base_dir, valid)
        self._eyes_dirty = False
        self._clip = load_index(self.base_dir)          # file name → (mtime, embedding)
        self._clip_dirty = False
        self._keeper, self._picks = {}, set()
        self._scores_dirty = False
        self._dets_dirty = False
        self._emb_dirty = False
        # Work still needed: no hash, capture time or focus point read yet
        self._hash_queue: list[str] = [
            f for f in self.files
            if f not in self._hashes or f not in self._timestamps or f not in self._focus]
        # Workers post results here; the main thread drains it via after()
        self._hash_result_q: queue.Queue = queue.Queue()
        self._hash_total_needed = len(self._hash_queue)
        self._hash_done = 0
        log.info("Cache hit: %d/%d scores, %d/%d hashes pre-loaded",
                 len(self._scores), len(self.files),
                 len(self._hashes), len(self.files))

    # ── Incremental hashing ───────────────────────────────────────────────────

    def _start_hash_workers(self) -> None:
        """
        Hash files and read capture times on worker threads. Workers post
        (path, hash, timestamp) into a queue that the main thread drains
        with after() — workers never touch Tkinter.
        """
        self._check_folder_writable()
        if not self._hash_queue:
            log.info("All hashes cached — skipping hash workers.")
            self.after(50, self._on_hashing_complete)
            return
        # Don't block: you can start culling while the folder is prepared;
        # progress is shown in the header
        self._hide_loading_overlay()
        self._hash_last_save = time.monotonic()

        n_workers = min(HASH_WORKERS, len(self._hash_queue))
        log.info("Hashing %d files with %d workers…",
                 len(self._hash_queue), n_workers)

        gen    = self._session_gen
        result = self._hash_result_q
        cached = dict(self._hashes)
        grid   = self._af_grid
        self._hash_eta = Eta(len(self._hash_queue))
        self._executor = ThreadPoolExecutor(max_workers=n_workers)

        def _worker(path: str) -> None:
            """Runs in a thread — NO Tkinter calls."""
            if self._session_gen != gen:
                return
            h = cached.get(path)
            ts: Optional[float] = None
            try:
                if h is None:
                    img = _open_thumb_unoriented(path)
                    ts  = _capture_timestamp(path, img)
                    if getattr(img, "format", None) == "JPEG":
                        img.draft("L", (64, 64))      # 1/8-scale decode is plenty
                    h   = imagehash.average_hash(auto_orient(img).convert("L"))
                else:
                    ts = _capture_timestamp(path)
            except Exception as e:
                log.warning("Hashing failed for %s: %s", path, e)
            focus = read_focus_area(path, *grid)
            result.put((path, h, ts, focus))

        for path in self._hash_queue:
            self._executor.submit(_worker, path)
        self.after(HASH_POLL_MS, lambda: self._poll_hash_results(gen))

    def _poll_hash_results(self, gen: int) -> None:
        """Drain worker results on the main thread; finish when all are in."""
        if gen != self._session_gen:
            return   # folder switched — this run is obsolete
        while True:
            try:
                path, h, ts, focus = self._hash_result_q.get_nowait()
            except queue.Empty:
                break
            if h is not None:
                self._hashes[path] = h
            self._timestamps[path] = ts
            self._focus[path] = focus
            self._hash_done += 1

        needed = self._hash_total_needed
        self._update_analysis_label()

        # Count results (failures included) so one bad file can't stall this
        if self._hash_done < needed:
            # Save progress regularly, so closing the app mid-way loses little
            if time.monotonic() - self._hash_last_save > 10:
                self._save_hash_progress()
            self.after(HASH_POLL_MS, lambda: self._poll_hash_results(gen))
        else:
            if self._executor:
                self._executor.shutdown(wait=False)
            self._save_hash_progress()
            self.after(50, self._on_hashing_complete)

    def _save_hash_progress(self) -> None:
        self._hash_last_save = time.monotonic()
        save_hash_cache(self.hash_file, self._hashes, self._timestamps, self._focus)

    def _check_folder_writable(self) -> None:
        """Warn once if results can't be saved (read-only folder, locked card)."""
        probe = os.path.join(self.base_dir, ".photo_reviewer_write_test")
        try:
            with open(probe, "w") as f:
                f.write("x")
            os.remove(probe)
        except OSError:
            mb.showwarning(
                "Folder is read-only",
                "This folder can't be written to (read-only folder or locked card).\n\n"
                "Tags, ratings and analysis results can't be saved, so the analysis "
                "will run again next time. Copy the images to your computer first "
                "(Import from card does this).", parent=self)

    def _run_burst_analysis(self) -> None:
        """Build burst groups, then open the burst triage view."""
        self._compute_bursts()
        n_bursts = sum(1 for g in self._bursts if len(g) > 1)
        self._hide_loading_overlay()
        self._refresh_sidebar(self.files[self.index])
        self._refresh_filmstrip(self.index)
        self._refresh_burst_ui(self.files[self.index])
        self._flash_title(f"Found {n_bursts} burst groups")
        self.after(50, self._open_burst_after_analysis)

    def _compute_bursts(self) -> None:
        """Group similar frames shot close together (see reviewer_predict.group_bursts)."""
        self._bursts = group_bursts(self.files, self._hashes, self._timestamps,
                                    AHASH_BURST, BURST_MAX_SECS)
        self._file_burst = {f: grp for grp in self._bursts for f in grp}
        self._hash_ready = True
        self._update_picks()
        log.info("Burst analysis complete — %d burst groups.",
                 sum(1 for g in self._bursts if len(g) > 1))

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
        view = self._overlay_view = InlineBurstTriageView(self._overlay_frame, self)
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
        """Hashing done: build bursts and start scoring / animal detection."""
        self._hide_loading_overlay()
        self._compute_bursts()
        self._refresh_sidebar(self.files[self.index])
        self._refresh_filmstrip(self.index)
        self._refresh_burst_ui(self.files[self.index])
        self._update_counts()
        self._start_analysis()
        self._ensure_region_list()

    # ── Background analysis (scores + animal detection, whole folder) ─────────

    def _sharpness_region(self, focus, dets) -> tuple:
        """Where to measure sharpness: focus point > animal > frame centre."""
        if focus:
            return focus[:4], "af"
        animal = main_animal(dets)
        if animal:
            return tuple(animal[2:6]), "animal"
        return None, "centre"

    def _needs_analysis(self, path: str) -> bool:
        sc = self._scores.get(path)
        if not sc or sc.get("v") != SCORE_VERSION:
            return True
        if self._detector is not None and path not in self._detections:
            return True
        if self._embedder is not None and path not in self._emb:
            return True
        if self._clip_encoder is not None and not self._clip_fresh(path):
            return True
        return (sc.get("sharp_at") == "centre" and not self._focus.get(path)
                and main_animal(self._detections.get(path)) is not None)

    def _start_analysis(self) -> None:
        """Score (and detect animals in) every image, nearest images first."""
        if self._analysis_executor:
            self._analysis_executor.shutdown(wait=False, cancel_futures=True)
            self._analysis_executor = None
        self._detector = get_detector() if self._detect_on else None
        self._embedder = get_embedder()
        self._species_model = get_species_model()
        self._clip_encoder = get_image_encoder()
        order = self.files[self.index:] + self.files[:self.index]
        todo  = [p for p in order if self._needs_analysis(p)]
        self._analysis_total, self._analysis_done = len(todo), 0
        self._analysis_eta = Eta(len(todo))
        if todo:
            gen = self._session_gen
            # the graphics chip takes the model work off the CPU → one more worker
            workers = ANALYSIS_WORKERS + (1 if reviewer_accel.gpu_found() else 0)
            self._analysis_executor = ThreadPoolExecutor(max_workers=workers)
            for p in todo:
                self._analysis_executor.submit(self._analysis_worker, p, gen)
        else:                       # all analysed before: the later passes start now
            self._start_species()
            self._start_eyes()
            self._start_rawclip()
        self._update_analysis_label()

    def _ensure_analysis(self) -> None:
        """Start analysis now if hashing is finished and nothing is running."""
        if self._hash_done >= self._hash_total_needed and not self._analysis_running() \
                and any(self._needs_analysis(p) for p in self.files):
            self._start_analysis()

    def _clip_fresh(self, path: str) -> bool:
        e = self._clip.get(os.path.basename(path))
        try:
            return e is not None and abs(e[0] - os.path.getmtime(path)) <= 1
        except OSError:
            return True

    def _analysis_running(self) -> bool:
        return self._analysis_done < self._analysis_total

    def _analysis_eta_text(self) -> str:
        return self._analysis_eta.text(self._analysis_done) if self._analysis_eta else ""

    def _unscored_count(self) -> int:
        return sum(1 for p in self.files
                   if (self._scores.get(p) or {}).get("v") != SCORE_VERSION)

    def _analysis_worker(self, path: str, gen: int) -> None:
        """Thread: one decode → animal detection + focus-aware score."""
        result = (gen, path, None, _SKIPPED, _SKIPPED, _SKIPPED, _SKIPPED)
        if gen == self._session_gen:
            try:
                img  = load_preview(path, SCORE_MIN_SIDE)
                dets = self._detections.get(path)
                if dets is None and self._detector is not None:
                    dets = refine(self._detector.detect(img), img, self._species_model)
                focus = (self._focus[path] if path in self._focus
                         else read_focus_area(path, *self._af_grid))
                region, kind = self._sharpness_region(focus, dets)
                emb = _SKIPPED
                if self._embedder is not None and path not in self._emb:
                    animal = main_animal(dets)
                    emb = self._embedder.embed(img, animal[2:6] if animal else None)
                clip = _SKIPPED
                if self._clip_encoder is not None and not self._clip_fresh(path):
                    clip = (os.path.getmtime(path), self._clip_encoder.embed([img])[0])
                score = score_image(img, region, kind)
                result = (gen, path, score,
                          dets if dets is not None else _SKIPPED, focus, emb, clip)
            except Exception as e:
                log.debug("Analysis failed %s: %s", path, e)
        self._analysis_q.put(result)

    def _update_analysis_label(self) -> None:
        if not hasattr(self, "_analysis_lbl"):
            return
        if self._hash_done < self._hash_total_needed:
            eta = self._hash_eta.text(self._hash_done) if getattr(self, "_hash_eta", None) else ""
            self._analysis_lbl.configure(
                text=f"⟳ Preparing folder {self._hash_done}/{self._hash_total_needed}"
                     + (f" · {eta}" if eta else ""))
        elif self._learning is not None:
            L = self._learning
            self._analysis_lbl.configure(
                text=f"⟳ Learning from past folders {L['folders']}/{L['total']} · "
                     f"{L['frames']:,} frames")
        elif self._analysis_running():
            eta = self._analysis_eta_text()
            what = "Analysing" + (" (animals + focus)" if self._detector else "")
            accel = reviewer_accel.status()
            self._analysis_lbl.configure(
                text=f"⟳ {what} {self._analysis_done}/{self._analysis_total}"
                     + (f" · {eta}" if eta else "") + (f" · {accel}" if accel else ""))
        else:
            self._analysis_lbl.configure(text="")

    def _suggestions(self, path: str) -> list[str]:
        codes = suggestions(self._detections.get(path), self._focus.get(path))
        rc = (self._scores.get(path) or {}).get("raw")
        if rc and (rc.get("s") or 0) >= SUBJECT_BLOWN:
            codes.append("blown")
        if self._eye_note(path) == "softer":
            codes.append("eye_soft")
        return codes

    # ── Eye check ─────────────────────────────────────────────────────────────

    def _eye_group(self, path: str) -> list[str]:
        return self._file_burst.get(path, [path])

    def _eye_note(self, path: str) -> Optional[str]:
        if not getattr(self, "_eye_on", False) or path not in getattr(self, "_eyes", {}):
            return None
        return burst_eye_note(path, self._eye_group(path), self._eyes)

    def _request_eye(self, path: Optional[str], urgent: bool = False) -> None:
        """Queue the eye check for a frame with an animal (the on-screen one first)."""
        if not self._eye_on or not path or path in self._eyes or path not in self.status:
            return
        # Loading the eye model (and preparing it for the graphics chip) is heavy:
        # wait until the main analysis — which the keeper and species need — is done
        if not self._hash_ready or self._analysis_running():
            return
        if path in (self._eye_urgent if urgent else self._eye_pending):
            return
        a = main_animal(self._detections.get(path))
        if not a or len(a) < 6:
            return
        pool = self._eye_now if urgent else self._eye_bg
        if pool is None:
            return
        (self._eye_urgent if urgent else self._eye_pending).add(path)
        pool.submit(self._eye_worker, path, tuple(a[2:6]), self._session_gen, urgent)

    def _start_eyes(self) -> None:
        """Check every frame's eye in the background, nearest the current photo first."""
        if not self._eye_on:
            return
        if self._eye_bg is None:
            self._eye_bg = ThreadPoolExecutor(max_workers=1)
        for p in self.files[self.index:] + self.files[:self.index]:
            self._request_eye(p)

    def _eye_worker(self, path: str, box: tuple, gen: int, urgent: bool) -> None:
        """Thread: find the eye and measure its sharpness."""
        res = _SKIPPED
        # skip photos already done, from an old folder, or left behind on screen
        if gen == self._session_gen and path not in self._eyes \
                and not (urgent and path != self._current_path):
            try:
                finder = get_eye_finder()
                if finder is not None:
                    res = measure_eye(path, box, finder) or {}
            except Exception as e:
                log.debug("Eye check failed %s: %s", path, e)
        self._eye_q.put((gen, path, res, urgent))

    def _drain_eyes(self) -> None:
        changed = False
        while True:
            try:
                gen, path, res, urgent = self._eye_q.get_nowait()
            except queue.Empty:
                break
            (self._eye_urgent if urgent else self._eye_pending).discard(path)
            if gen != self._session_gen or res is _SKIPPED or path not in self.status:
                continue
            self._eyes[path] = res
            self._eyes_dirty = True
            if self._current_path in self._eye_group(path):
                changed = True
        if changed and self._current_path:
            self._update_suggestion_panel(self._current_path, self._scores.get(self._current_path))
            if self._show_boxes:
                self._render_image()

    def _eye_line(self, path: str) -> str:
        """Sidebar line about the eye ('' when there is nothing to say)."""
        if not self._eye_on:
            return ""
        e = self._eyes.get(path)
        if e is None:
            if eye_status():
                return f"Eye check unavailable — {eye_status()}"
            a = main_animal(self._detections.get(path))
            if not a or len(a) < 6:
                return ""
            if not self._hash_ready or self._analysis_running():
                return "Eye: after the analysis…"
            return "Eye: checking…" if path in self._eye_urgent | self._eye_pending else "Eye: waiting…"
        if not e:
            return ""                                   # animal too small to look for the eye
        note = self._eye_note(path)
        if note == "hidden":
            return "Eye: not found (head turned, hidden or too small)"
        if note == "best":
            return "Eye: sharpest in this burst — E to look"
        if note == "tie":
            return "Eye: as sharp as the best in this burst — E to look"
        if note == "softer":
            g = self._eye_group(path)
            best = max((q for q in g if (self._eyes.get(q) or {}).get("s") is not None),
                       key=lambda q: self._eyes[q]["s"])
            return f"Eye: softer than in {Path(best).name} — E to look"
        return "Eye: found — E to look"

    def _zoom_to_eye(self) -> None:
        """E: 100 % on the animal's eye (again: back to the whole frame)."""
        path = self._current_path
        if self._pil_image is None or not path:
            return
        if self._eye_view and self._zoom > 1.01:
            self._eye_view = False
            self._zoom, self._pan_x, self._pan_y = 1.0, 0.0, 0.0
            self._render_image()
            return
        e = self._eyes.get(path)
        b = (e or {}).get("b")
        if not b:
            msg = ("Eye check is off in the settings" if not self._eye_on else
                   f"Eye check unavailable — {eye_status()}" if eye_status() else
                   "No animal to look for an eye on" if e == {} else
                   "No eye visible in this frame" if e else "Eye not checked yet — a moment…")
            self._flash_title(msg, 2000)
            self._request_eye(path, urgent=True)
            return
        self._ensure_detail_now()
        cw = self._canvas_w or 1200
        ch = self._canvas_h or 800
        iw, ih = self._full_res_size()
        fit = min(cw / iw, ch / ih)
        zoom = max(2.0, 1.0 / fit)                       # 100 %, or 2× for small images
        u, v = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        self._zoom = zoom
        self._pan_x = -(u - 0.5) * iw * fit * zoom
        self._pan_y = -(v - 0.5) * ih * fit * zoom
        self._eye_view = True
        self._render_image()

    def _suggest_note(self, path: str) -> str:
        codes = self._suggestions(path)
        return ", ".join(SUGGESTION_SHORT[c] for c in codes[:2])

    # ── Keeper prediction ─────────────────────────────────────────────────────

    def _load_history(self) -> None:
        """Thread: load labelled frames from past folders."""
        self._history = History(_user_data_dir() / HISTORY_FILE)

    def _schedule_retrain(self, delay_ms: int = 8000) -> None:
        """Retrain a few seconds after you stop tagging (and on new features)."""
        if not self._emb and self._history is None:
            return
        if self._train_job:
            self.after_cancel(self._train_job)
        self._train_job = self.after(delay_ms, self._start_training)

    def _snapshot(self) -> dict:
        """Copies of the data the training thread reads (UI keeps changing it)."""
        return dict(paths=list(self.files), folder=self.base_dir, gen=self._session_gen,
                    status={p: dict(v) for p, v in self.status.items()},
                    bursts=[list(g) for g in self._bursts], scores=dict(self._scores),
                    dets=dict(self._detections), focus=dict(self._focus), emb=dict(self._emb))

    def _start_training(self) -> None:
        self._train_job = None
        if self._training:
            self._schedule_retrain(3000)
            return
        self._training = True
        threading.Thread(target=self._train_worker, args=(self._snapshot(),),
                         daemon=True).start()

    def _train_worker(self, snap: dict) -> None:
        """Thread: train on past folders + this folder, then score this folder."""
        result = None
        try:
            cur = folder_samples(snap["paths"], snap["status"], snap["bursts"],
                                 snap["scores"], snap["dets"], snap["focus"], snap["emb"])
            parts = []
            hist = self._history
            if hist is not None:
                past = hist.arrays(exclude=snap["folder"])
                if past is not None:
                    parts.append(past)
            if len(cur[3]):
                parts.append(cur[:4] + (cur[4] + 10 ** 9,) + cur[5:])   # own group ids
            model, info = self._model, dict(self._model.meta) if self._model else {}
            if parts:
                H, E, R, y, g, s = (np.concatenate([p[i] for p in parts]) for i in range(6))
                uniq, counts = np.unique(g, return_counts=True)
                n_bursts = int((counts > 1).sum())
                info = {"bursts": n_bursts, "frames": int(len(y))}
                if n_bursts >= MIN_BURSTS:
                    new, info = train(H, E, R, y, g, s)
                    if new is not None:
                        model = new
                        model.save(_user_data_dir() / MODEL_FILE)
            info["folders"] = (hist.folders if hist else 0) + (1 if len(cur[3]) else 0)
            probs: dict[str, float] = {}
            if model is not None:
                H2, E2, R2, kept = build_features(snap["paths"], snap["bursts"], snap["scores"],
                                                  snap["dets"], snap["focus"], snap["emb"])
                if kept:
                    probs = dict(zip(kept, model.predict(H2, E2, R2).tolist()))
            result = (snap["gen"], model, probs, info)
        except Exception as e:
            log.exception("Keeper training failed: %s", e)
        self._train_q.put(result)

    def _drain_training(self) -> None:
        while True:
            try:
                res = self._train_q.get_nowait()
            except queue.Empty:
                break
            self._training = False
            if not res or res[0] != self._session_gen:
                continue
            _gen, model, probs, info = res
            self._model, self._keeper, self._keeper_info = model, probs, info
            self._update_picks()
            if self._current_path:
                self._update_keeper_panel(self._current_path)
            self._refresh_filmstrip(self.index)
            view = self._overlay_view
            if view is not None and hasattr(view, "_relayout"):
                view._relayout(keep_scroll=True)
        while True:
            try:
                msg = self._learn_q.get_nowait()
            except queue.Empty:
                break
            if msg[0] == "progress":
                self._learning = msg[1]
            elif msg[0] == "done":
                self._learning = None
                self._learn_btn.configure(text="Learn from past folders…")
                self._flash_title(msg[1], 4000)
                self._schedule_retrain(200)
            self._update_analysis_label()

    # ── Species suggestions ───────────────────────────────────────────────────

    def _burst_rep(self, path: str) -> str:
        """Frame used to identify a burst's species: predicted pick, else the
        frame with the most prominent animal, else the first frame."""
        g = self._file_burst.get(path, [path])
        pick = next((p for p in g if p in self._picks), None)
        if pick:
            return pick
        def prominence(p: str) -> float:
            a = main_animal(self._detections.get(p))
            return (a[4] - a[2]) * (a[5] - a[3]) * a[1] if a else 0.0
        return max(g, key=prominence) if len(g) > 1 else g[0]

    def _species_top(self, path: str) -> Optional[list]:
        """
        Species suggestions for the burst containing path (None = not yet),
        re-ranked so species you've confirmed before count for more.
        """
        g = self._file_burst.get(path, [path])
        raw = next((self._species_pred[p] for p in [self._burst_rep(path)] + g
                    if p in self._species_pred), None)
        if raw is None:
            return None
        prior = self._species_hist.prior()
        iso = self._folder_region()
        region = self._regions.get(iso) if iso else None
        groups = self._species_model.group if self._species_model else {}
        weighted = [[n, s, p * prior.get(s, 1.0) * region_weight(s, groups.get(s), region)]
                    for n, s, p in raw]
        # Renormalise including the probability left outside these candidates,
        # so if every candidate is unlikely here, their confidence drops too
        rest = max(0.0, 1.0 - sum(p for _n, _s, p in raw))
        total = (sum(w for _n, _s, w in weighted) + rest) or 1.0
        ranked = sorted(([n, s, w / total] for n, s, w in weighted), key=lambda r: -r[2])
        return ranked[:5]

    def _not_local(self, scientific: str) -> str:
        """'not recorded in Kenya' if the folder's country list lacks the species."""
        iso = self._folder_region()
        region = self._regions.get(iso) if iso else None
        groups = self._species_model.group if self._species_model else {}
        if region and region_weight(scientific, groups.get(scientific), region) < 1.0:
            return f"not recorded in {region.get('name', iso)}"
        return ""

    def _request_species(self, path: str, urgent: bool = False) -> None:
        if self._species_model is None:
            return
        rep = self._burst_rep(path)
        if rep in self._species_pred or rep in self._species_pending:
            return
        self._species_pending.add(rep)
        pool = self._species_now if urgent else self._species_bg
        if pool is not None:
            pool.submit(self._species_worker, rep, self._session_gen)

    def _start_species(self) -> None:
        """Predict the species of every burst / single frame (background)."""
        self._species_model = self._species_model or get_species_model()
        if self._species_model is None:
            return
        if self._species_bg is None:
            self._species_bg = ThreadPoolExecutor(max_workers=1)
        if self._current_path:
            self._request_species(self._current_path, urgent=True)
        for g in self._bursts:
            self._request_species(g[0])

    def _species_worker(self, rep: str, gen: int) -> None:
        """Thread: species suggestions for one burst's representative frame."""
        top = None
        if gen == self._session_gen:
            try:
                img = load_preview(rep, 1600)          # enough detail for small birds
                a = main_animal(self._detections.get(rep))
                # raw top 25; your confirmed species and the region are weighted in
                # at display time
                top = self._species_model.predict(img, a[2:6] if a else None, k=25)
            except Exception as e:
                log.debug("Species prediction failed %s: %s", rep, e)
        self._species_q.put((gen, rep, top))

    def _drain_species(self) -> None:
        changed = False
        while True:
            try:
                gen, rep, top = self._species_q.get_nowait()
            except queue.Empty:
                break
            self._species_pending.discard(rep)
            if gen != self._session_gen or top is None:
                continue
            self._species_pred[rep] = top
            self._species_dirty = True
            changed = changed or (self._current_path in self._file_burst.get(rep, [rep]))
        if changed:
            self._update_name_panel(self._current_path)

    # ── Region hint ───────────────────────────────────────────────────────────

    def _folder_region(self) -> Optional[str]:
        """Country (ISO code) for this folder: your choice, else from its name."""
        chosen = self._app_settings.get("folder_regions", {}).get(self.base_dir)
        if chosen is not None:
            return chosen or None                     # "" = you chose Worldwide
        return guess_country(Path(self.base_dir).name)

    def _set_folder_region(self, iso: Optional[str]) -> None:
        regions = self._app_settings.setdefault("folder_regions", {})
        regions[self.base_dir] = iso or ""
        save_app_settings(self._app_settings)
        self._ensure_region_list()
        self._region_changed()

    def _ensure_region_list(self) -> None:
        """Download the folder country's species list if needed (background)."""
        iso = self._folder_region()
        if iso and self._regions.stale(iso) and not self._regions.fetching(iso):
            self._region_error.pop(iso, None)
            self._regions.fetch(iso, lambda i, err: self._region_q.put((i, err)))

    def _drain_regions(self) -> None:
        changed = False
        while True:
            try:
                iso, err = self._region_q.get_nowait()
            except queue.Empty:
                break
            if err:
                self._region_error[iso] = err
            changed = True
        if changed:
            self._region_changed()

    def _region_changed(self) -> None:
        """Re-rank shown suggestions after the region (or its list) changed."""
        if self._current_path:
            self._update_name_panel(self._current_path)
        view = self._overlay_view
        if isinstance(view, InlineIdReviewView) and view._sightings:
            view._show(view._i)

    def _region_text(self) -> str:
        iso = self._folder_region()
        if not iso:
            return "Worldwide — pick the country to rank local species first"
        c = by_iso(iso)
        name = c["name"] if c else iso
        data = self._regions.get(iso)
        if self._regions.fetching(iso):
            return f"{name}: downloading its species list…"
        if data:
            how = "" if self._app_settings.get("folder_regions", {}).get(self.base_dir) \
                else "  (from the folder name)"
            return f"{name}: {len(data['species']):,} species recorded{how}"
        err = self._region_error.get(iso)
        return f"{name}: couldn't download its species list ({err}) — using worldwide" if err \
            else f"{name}: species list not downloaded yet"

    # ── Naming panel (species / date / location for the Best name) ───────────

    def _name_targets(self) -> list[str]:
        path = self._current_path
        return list(self._file_burst.get(path, [path])) if self._name_burst else [path]

    def _confirm_species(self, name: str) -> None:
        """Set the species for the current image (or its burst) and remember it."""
        self._confirm_species_for(self._name_targets(), name)

    def _confirm_species_for(self, paths: list[str], name: str) -> None:
        """Set the species on these frames (one undo step) and remember it."""
        name = re.sub(r"\s+—\s+\d+%.*$", "", name).strip()    # "Grey Heron — 55% · …"
        if not name or not paths:
            return
        top = []
        for p in paths:                       # suggestions from any burst of these frames
            top += [t for t in (self._species_top(p) or []) if t not in top]
            if len(top) > 40:
                break
        sci = next((s for n, s, _p in top if n.lower() == name.lower()), "") \
            or self._species_hist.scientific_for(name)
        self._run_batch(paths, lambda p: self._set_fields(p, {"species": name,
                                                              "species_sci": sci}))
        self._species_hist.confirm(name, sci)
        self._species_hist.save()
        if self._current_path:
            self._load_metadata_fields(self._current_path)
            self._update_name_panel(self._current_path)
        self._flash_title(f"Species: {name}" + (f" · {len(paths)} frames" if len(paths) > 1 else ""),
                          1500)

    # ── Sightings ─────────────────────────────────────────────────────────────

    def _build_sightings(self) -> list[dict]:
        """Bursts grouped into sightings (reviewer_sightings.build_sightings)."""
        gap = int(self._app_settings.get("sighting_gap", DEFAULT_GAP_SECS))

        def species_of(g: list[str]):
            for p in g:
                t = self.status.get(p, {})
                if t.get("species"):
                    return (t.get("species_sci") or t["species"], t["species"], True, 1.0)
            top = self._species_top(g[0])
            if top and not top[0][1].startswith("Homo "):
                return (top[0][1], top[0][0], False, top[0][2])
            return None

        def emb_of(g: list[str]):
            v = [self._emb[p] for p in g if p in self._emb]
            return np.mean(v, axis=0) if v else None

        groups = self._bursts or [[p] for p in self.files]
        return build_sightings(groups, self._timestamps, species_of, emb_of, gap)

    def _sighting_done(self, s: dict) -> bool:
        return all(self.status.get(p, {}).get("seen") for p in s["paths"])

    def _mark_seen(self, paths: list[str]) -> None:
        """Remember a sighting was gone through (tags are left exactly as they are)."""
        for p in paths:
            if p in self.status:
                self.status[p]["seen"] = True
        self._schedule_status_save()

    def _open_sightings(self) -> None:
        if self._active_overlay == "sightings":
            self._hide_overlay()
            return
        if not self._hash_ready:
            self._flash_title(
                f"Still preparing the folder ({self._hash_done}/{self._hash_total_needed}) "
                "— sightings are available when it finishes", 3000)
            return
        self._show_overlay("sightings", self._build_sightings_view)

    def _build_sightings_view(self, frame: tk.Frame) -> None:
        view = self._overlay_view = InlineSightingsView(frame, self)
        view.pack(fill="both", expand=True)

    def _confirm_suggested_species(self) -> None:
        """N: accept the suggested species for this burst."""
        top = self._species_top(self._current_path)
        if self.status.get(self._current_path, {}).get("species"):
            self._flash_title("Species already set — edit it in the NAME panel", 1800)
        elif top:
            self._confirm_species(top[0][0])
        else:
            self._flash_title("No species suggestion yet for this burst", 1800)

    def _confirm_location(self, value: str) -> None:
        value = value.strip()
        paths = self._name_targets()
        self._run_batch(paths, lambda p: self._set_value(p, "location", value))
        if value:
            recent = [v for v in self._app_settings.get("recent_locations", []) if v != value]
            self._app_settings["recent_locations"] = [value] + recent[:14]
            save_app_settings(self._app_settings)
        self._load_metadata_fields(self._current_path)
        self._update_name_panel(self._current_path)

    def _confirm_date(self, value: str) -> None:
        value = value.strip()
        if value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            self._flash_title("Date must look like 2026-05-12", 2000)
            self._update_name_panel(self._current_path)
            return
        exif_date = self._name_parts(self._current_path, exif_only=True)["date"]
        keep = "" if value == exif_date else value
        self._run_batch(self._name_targets(), lambda p: self._set_value(p, "name_date", keep))
        self._update_name_panel(self._current_path)

    def _update_name_panel(self, path: Optional[str]) -> None:
        if not path or not hasattr(self, "_name_species"):
            return
        t = self.status.get(path, {})
        top = self._species_top(path)
        if path in self.status and top is None and t.get("species") is None:
            self._request_species(path, urgent=True)
        choices = [f"{n} — {p:.0%}" + (f" · {self._not_local(s)}" if self._not_local(s) else "")
                   for n, s, p in (top or [])]
        choices += [n for n in self._species_hist.recent() if n not in [c.split(" — ")[0] for c in choices]]
        self._name_species.configure(values=choices)
        confirmed = t.get("species")
        self._name_species.set(confirmed or (top[0][0] if top else ""))
        if confirmed:
            self._name_state.configure(text="✓ Confirmed", fg=C_BEST)
        elif top:
            self._name_state.configure(
                text=f"Suggested ({top[0][2]:.0%}) — N or Enter to confirm, or pick/type"
                     + ("\n⚠ " + self._not_local(top[0][1])[:1].upper()
                        + self._not_local(top[0][1])[1:] + " — check carefully"
                        if self._not_local(top[0][1]) else ""), fg=C_WARN)
        elif self._species_model is None and species_status():
            self._name_state.configure(text="Species suggestions unavailable — type a name",
                                       fg=C_MUTED)
        else:
            self._name_state.configure(text="Identifying…", fg=C_MUTED)
        parts = self._name_parts(path)
        self._name_date.delete(0, "end")
        self._name_date.insert(0, parts["date"])
        locs = [parts["location"]] + [v for v in self._app_settings.get("recent_locations", [])
                                      if v != parts["location"]]
        self._name_location.configure(values=[v for v in locs if v])
        self._name_location.set(parts["location"])
        iso = self._folder_region()
        if self.focus_get() is not self._name_region:
            c = by_iso(iso)
            self._name_region.set(c["name"] if c else "Worldwide")
        self._region_lbl.configure(text=self._region_text())
        template = self._app_settings.get("best_template", DEFAULT_BEST_TEMPLATE)
        try:
            preview = self._render_name(template, parts, 1).replace("001", "###")
        except (KeyError, ValueError, IndexError):
            preview = "(check the name pattern in the Process dialog)"
        self._name_preview.configure(text=f"→ {preview}")

    # ── ID review ─────────────────────────────────────────────────────────────

    def _id_sightings(self) -> list[list[str]]:
        """ID-tagged frames, one group per sighting (bursts of the same animal)."""
        groups = ([s["paths"] for s in self._build_sightings()] if self._hash_ready
                  else [[p] for p in self.files])
        out = []
        for g in groups:
            ids = [p for p in g if self.status.get(p, {}).get("id")]
            if ids:
                out.append(ids)
        return out

    def _set_id_result(self, group: list[str], state: str, species: Optional[str],
                       note: str) -> None:
        """Record an ID decision (and species) on every frame of a sighting."""
        changes: dict = {"id_status": state, "id_note": note}
        if species:
            top = self._species_top(group[0]) or []
            sci = next((s for n, s, _p in top if n.lower() == species.lower()), "") \
                or self._species_hist.scientific_for(species)
            changes.update(species=species, species_sci=sci)
            self._species_hist.confirm(species, sci)
            self._species_hist.save()
        self._run_batch(group, lambda p: self._set_fields(p, dict(changes)))
        if self._current_path in group:
            self._load_metadata_fields(self._current_path)
        label = {"confirmed": f"✓ {species}", "unsure": "Marked unsure",
                 "unidentifiable": "Marked not identifiable"}[state]
        self._flash_title(f"{label}" + (f" · {len(group)} frames" if len(group) > 1 else ""), 1500)

    def _open_id_review(self) -> None:
        self._show_overlay("id_review", self._build_id_review)

    def _build_id_review(self, frame: tk.Frame) -> None:
        view = self._overlay_view = InlineIdReviewView(frame, self)
        view.pack(fill="both", expand=True)

    def _id_inbox(self) -> None:
        """Folders under the library that still have photos to identify."""
        root = self._library_root(ask=True)
        if not root:
            return
        self.configure(cursor="watch")
        self.update_idletasks()
        rows = []
        try:
            for folder in [f for _r, now in self._online_roots()
                           for f in library_folders(now, SUPPORTED_EXTS)]:
                if not os.path.exists(os.path.join(folder, STATUS_FILENAME)):
                    continue
                files = sorted(os.path.join(folder, f) for f in os.listdir(folder)
                               if Path(f).suffix.lower() in SUPPORTED_EXTS)
                st = self.status if str(Path(folder)) == self.base_dir else session_status(folder, files)
                todo = sum(1 for t in st.values() if t.get("id")
                           and t.get("id_status") not in ("confirmed", "unidentifiable"))
                if todo:
                    rows.append((folder, files, todo))
        finally:
            self.configure(cursor="")
        win = ctk.CTkToplevel(self)
        win.title("ID inbox")
        win.geometry("560x420")
        win.configure(fg_color=C_SURFACE)
        win.transient(self)
        ctk.CTkLabel(win, text="Photos still to identify in your library folders",
                     font=ctk.CTkFont("Helvetica", 12, "bold")).pack(padx=16, pady=(14, 6), anchor="w")
        lb = tk.Listbox(win, bg=C_BG, fg=C_TEXT, selectbackground=C_ACCENT, font=("Helvetica", 10),
                        bd=0, highlightthickness=0, activestyle="none")
        lb.pack(fill="both", expand=True, padx=16, pady=4)
        for folder, _f, n in rows:
            lb.insert("end", f"  {Path(folder).name}   —   {n} to identify")
        if not rows:
            lb.insert("end", "  Nothing waiting — all ID photos are identified 🎉")

        def open_sel(_e=None):
            sel = lb.curselection()
            if not sel or not rows:
                return
            folder, files, _n = rows[sel[0]]
            win.destroy()
            if str(Path(folder)) != self.base_dir:
                self._init_session(folder, files)
            self.after(300, self._open_id_review)
        lb.bind("<Double-Button-1>", open_sel)
        lb.bind("<Return>", open_sel)
        ctk.CTkButton(win, text="Open in ID Review", command=open_sel).pack(pady=10)

    # ── AI search ─────────────────────────────────────────────────────────────

    def _library_roots(self) -> list[dict]:
        """Library folders searched (several drives allowed)."""
        roots = self._app_settings.get("library_roots")
        if not isinstance(roots, list):
            old = self._app_settings.get("library_root")        # one folder, before
            roots = [make_root(old)] if isinstance(old, str) and old else []
            self._app_settings["library_roots"] = roots
        return [r for r in roots if isinstance(r, dict) and r.get("path")]

    def _save_library_roots(self, roots: list[dict]) -> None:
        self._app_settings["library_roots"] = roots
        # older versions read one folder
        self._app_settings["library_root"] = roots[0]["path"] if roots else ""
        save_app_settings(self._app_settings)
        self._library.clear()
        self._offline_cache.clear()
        self._status_cache.clear()
        self._start_indexing()

    def _online_roots(self) -> list[tuple[dict, str]]:
        """(library folder, where it is now) for folders that are reachable."""
        out = []
        for r in self._library_roots():
            now = resolve_root(r)
            if now:
                out.append((r, now))
        return out

    def _library_root(self, ask: bool = False) -> Optional[str]:
        """First reachable library folder (asks for one if there are none)."""
        online = self._online_roots()
        if not online and ask and not self._library_roots():
            root = fd.askdirectory(title="Folder that holds your photos (e.g. the year)",
                                   parent=self, initialdir=str(Path(self.base_dir).parent))
            if root:
                self._set_library_root(str(Path(root)))
                online = self._online_roots()
        return online[0][1] if online else None

    def _set_library_root(self, root: str) -> None:
        """Add a library folder (kept for older callers)."""
        roots = self._library_roots()
        if not any(os.path.normcase(r["path"]) == os.path.normcase(root) for r in roots):
            roots.append(make_root(root))
        self._save_library_roots(roots)

    def _manage_library(self) -> None:
        """Add / remove the folders AI search and the ID inbox look in."""
        win = ctk.CTkToplevel(self)
        win.title("Library folders")
        win.geometry("640x380")
        win.configure(fg_color=C_SURFACE)
        win.transient(self)
        ctk.CTkLabel(win, text="Folders searched (e.g. your year folder on the laptop and "
                     "your holidays folder on the external drive)",
                     font=ctk.CTkFont("Helvetica", 11, "bold"), wraplength=600,
                     justify="left").pack(padx=16, pady=(14, 6), anchor="w")
        lb = tk.Listbox(win, bg=C_BG, fg=C_TEXT, selectbackground=C_ACCENT,
                        font=("Helvetica", 10), bd=0, highlightthickness=0, activestyle="none")
        lb.pack(fill="both", expand=True, padx=16, pady=4)

        def fill():
            lb.delete(0, "end")
            for r in self._library_roots():
                now = resolve_root(r)
                where = now if now else f"{r['path']}  — not connected"
                extra = "  (shown offline when unplugged)" if is_external(now or r["path"]) else ""
                lb.insert("end", f"  {root_name(r)}   ·   {where}{extra}")
                lb.itemconfig("end", fg=C_TEXT if now else C_MUTED)

        def add():
            d = fd.askdirectory(parent=win, title="Add a folder of photos")
            if d:
                self._set_library_root(str(Path(d)))
                fill()

        def remove():
            sel = lb.curselection()
            if not sel:
                return
            roots = self._library_roots()
            roots.pop(sel[0])
            self._save_library_roots(roots)
            fill()
        bar = ctk.CTkFrame(win, fg_color="transparent")
        bar.pack(fill="x", padx=16, pady=10)
        ctk.CTkButton(bar, text="Add folder…", command=add, width=120).pack(side="left")
        ctk.CTkButton(bar, text="Remove", command=remove, width=100,
                      fg_color=C_BORDER).pack(side="left", padx=8)
        ctk.CTkButton(bar, text="Close", command=win.destroy, width=90,
                      fg_color=C_BORDER).pack(side="right")
        fill()

    def _open_search(self) -> None:
        if search_status():
            mb.showinfo("AI search", f"AI search is unavailable: {search_status()}", parent=self)
            return
        if self._library_root(ask=True):
            self._start_indexing()
        self._show_overlay("search", self._build_search)

    def _build_search(self, frame: tk.Frame) -> None:
        view = self._overlay_view = InlineSearchView(frame, self)
        view.pack(fill="both", expand=True)

    def _start_indexing(self) -> None:
        """Index (or update the index of) every library folder — background."""
        if self._indexing is not None:
            self._index_restart = True          # folders changed: go again when done
            self._index_stop.set()
            return
        roots = self._online_roots()
        if not roots:
            return
        self._index_stop.clear()
        self._index_restart = False
        self._indexing = {"folders": 0, "total": 0, "photos": 0, "left": 0}
        threading.Thread(target=self._index_worker, args=(roots,), daemon=True).start()

    def _index_worker(self, roots: list[tuple[dict, str]]) -> None:
        enc = get_image_encoder()
        try:
            work = []
            offline: dict[str, tuple] = {}          # root → (OfflineIndex, entries)
            for entry, root in roots:
                if is_external(root):
                    off = OfflineIndex(_user_data_dir() / "library", entry)
                    offline[root] = (off, off.load())
                for folder in (library_folders(root, SUPPORTED_EXTS) if enc else []):
                    idx = load_index(folder)
                    todo = [] if str(Path(folder)) == self.base_dir \
                        else stale_files(folder, SUPPORTED_EXTS, idx)   # open folder: analysis
                    work.append((folder, idx, todo, root))
            state = {"folders": 0, "total": len(work), "photos": 0,
                     "left": sum(len(w[2]) for w in work)}
            self._index_q.put(("progress", dict(state), None))
            for folder, idx, todo, root in work:
                for i in range(0, len(todo), 8):
                    if self._index_stop.is_set():
                        break
                    batch, imgs = [], []
                    for name in todo[i:i + 8]:
                        p = os.path.join(folder, name)
                        try:
                            imgs.append(load_small_thumb(p, 384))
                            batch.append((name, os.path.getmtime(p)))
                        except Exception as e:
                            log.debug("Index: skipped %s: %s", p, e)
                    if imgs:
                        for (name, mt), e in zip(batch, enc.embed(imgs)):
                            idx[name] = (mt, e)
                    state["left"] -= len(todo[i:i + 8])
                    state["photos"] += len(imgs)
                    if i % 200 == 0:
                        self._index_q.put(("progress", dict(state), None))
                    if i and i % 400 == 0:
                        save_index(folder, idx)
                if todo:
                    save_index(folder, idx)
                if root in offline and not self._index_stop.is_set():
                    self._update_offline(folder, root, idx, *offline[root])
                state["folders"] += 1
                if str(Path(folder)) != self.base_dir:
                    self._index_q.put(("folder", dict(state), (folder, idx)))
                if self._index_stop.is_set():
                    break
            for off, entries in offline.values():
                off.save(entries)
        except Exception as e:
            log.exception("Indexing failed: %s", e)
        self._index_q.put(("done", None, None))

    def _update_offline(self, folder: str, root: str, idx: dict, off, entries: dict) -> None:
        """Thread: offline copy of one folder (index, thumbnails, tags) for unplugged drives."""
        files = [os.path.join(folder, n) for n in idx]
        st = session_status(folder, files)
        prefix = os.path.relpath(folder, root)
        prefix = "" if prefix == "." else prefix
        for rel in [r for r in entries if os.path.dirname(r) == prefix]:
            if os.path.basename(rel) not in idx:
                entries.pop(rel)                         # deleted from the drive
        for name, (mt, emb) in idx.items():
            p = os.path.join(folder, name)
            rel = os.path.join(prefix, name) if prefix else name
            t = st.get(p, {})
            entries[rel] = {"mtime": mt, "emb": emb, "best": bool(t.get("best")),
                            "delete": bool(t.get("delete")), "star": int(t.get("star") or 0),
                            "species": t.get("species") or ""}
            if not off.thumb_path(rel).exists():
                if self._index_stop.is_set():
                    return
                try:
                    off.save_thumb(rel, load_small_thumb(p))
                except Exception as e:
                    log.debug("Offline thumbnail failed %s: %s", p, e)

    def _drain_index(self) -> None:
        while True:
            try:
                kind, state, data = self._index_q.get_nowait()
            except queue.Empty:
                break
            if kind == "done":
                self._indexing = None
                if self._index_restart:
                    self._index_restart = False
                    self._start_indexing()
            else:
                self._indexing = state
            if kind == "folder" and data:
                folder, idx = data
                if idx:
                    names = list(idx)
                    self._library[folder] = (names, np.array([idx[n][0] for n in names]),
                                             np.stack([idx[n][1] for n in names]).astype(np.float32))

    def _search_status_text(self) -> str:
        roots = self._library_roots()
        if not roots:
            return "Add your library folders (Library folders…) to search them"
        online = self._online_roots()
        n = sum(len(v[0]) for v in self._library.values()) + len(self._clip)
        names = ", ".join(root_name(r) for r, _p in online) or "none connected"
        text = f"Library: {names}  ·  {n:,} photos indexed"
        off = len(roots) - len(online)
        if off:
            text += f"  ·  {off} folder(s) unplugged — searched from the offline copy"
        s = self._indexing
        if s is not None:
            text += f"  ·  indexing {s['folders']}/{s['total']} folders, {s['left']:,} photos left"
        return text

    def _folder_status(self, folder: str) -> dict:
        if folder == self.base_dir:
            return self.status
        sf = os.path.join(folder, STATUS_FILENAME)
        mt = os.path.getmtime(sf) if os.path.exists(sf) else 0
        cached = self._status_cache.get(folder)
        if cached and cached[0] == mt:
            return cached[1]
        files = [os.path.join(folder, n) for n in self._library.get(folder, ([],))[0]]
        st = session_status(folder, files) if mt else {}
        self._status_cache[folder] = (mt, st)
        return st

    def _search(self, query: str, best_only=False, hide_deleted=True, min_stars=0,
                species="", year="", limit=300) -> Optional[list]:
        enc = get_text_encoder()
        if enc is None:
            self._flash_title("AI search text model is unavailable", 2500)
            return None
        v = enc.embed([query, f"a photo of {query}"]).mean(0)
        v /= np.linalg.norm(v)
        sources = dict(self._library)
        if self._clip:
            names = list(self._clip)
            sources[self.base_dir] = (names, np.array([self._clip[n][0] for n in names]),
                                      np.stack([self._clip[n][1] for n in names]).astype(np.float32))
        species = species.lower()
        hits = self._search_offline(v, best_only, hide_deleted, min_stars, species, year, limit)
        for folder, (names, mtimes, M) in sources.items():
            sims = M @ v
            keep = np.argsort(-sims)[:limit * 2]
            st = self._folder_status(folder) if (best_only or hide_deleted or min_stars or species) else {}
            for i in keep:
                path = os.path.join(folder, names[i])
                t = st.get(path, {})
                if best_only and not t.get("best"):
                    continue
                if hide_deleted and t.get("delete"):
                    continue
                if min_stars and (t.get("star") or 0) < min_stars:
                    continue
                if species and species not in (t.get("species") or "").lower():
                    continue
                if year and datetime.datetime.fromtimestamp(mtimes[i]).strftime("%Y") != year:
                    continue
                if os.path.exists(path):
                    hits.append((path, float(sims[i])))
        hits.sort(key=lambda h: -h[1])
        return hits[:limit]

    def _search_offline(self, v, best_only, hide_deleted, min_stars, species, year,
                        limit) -> list:
        """Results from library folders whose drive is unplugged (offline copy)."""
        hits = []
        for entry in self._library_roots():
            if resolve_root(entry):
                continue
            off = OfflineIndex(_user_data_dir() / "library", entry)
            try:
                mt = off.file.stat().st_mtime
            except OSError:
                continue
            cached = self._offline_cache.get(str(off.file))
            if not cached or cached[0] != mt:
                data = off.load()
                rels = list(data)
                cached = (mt, rels, data,
                          np.stack([data[r]["emb"] for r in rels]).astype(np.float32) if rels else None)
                self._offline_cache[str(off.file)] = cached
            _mt, rels, data, M = cached
            if M is None:
                continue
            sims = M @ v
            drive = entry.get("label") or os.path.splitdrive(entry["path"])[0]
            for i in np.argsort(-sims)[:limit * 2]:
                t = data[rels[i]]
                if best_only and not t["best"] or hide_deleted and t["delete"] \
                        or min_stars and t["star"] < min_stars \
                        or species and species not in t["species"].lower() \
                        or year and datetime.datetime.fromtimestamp(t["mtime"]).strftime("%Y") != year:
                    continue
                path = os.path.join(entry["path"], rels[i])
                self._offline[path] = (str(off.thumb_path(rels[i])), drive)
                hits.append((path, float(sims[i])))
        return hits

    def _open_photo_anywhere(self, path: str) -> None:
        """Open a search result — switching folder if it lives elsewhere."""
        off = self._offline.get(path)
        if off and not os.path.exists(path):
            mb.showinfo("Drive not connected", f"This photo is on “{off[1]}”.\n"
                        "Plug the drive in, then open it again.", parent=self)
            return
        if path in self.files:
            self._hide_overlay()
            self._jump_to_index(self.files.index(path))
            return
        folder = str(Path(path).parent)
        if not mb.askyesno("Open folder", f"Open “{Path(folder).name}” to view this photo?\n"
                           "(Your current folder is saved first.)", parent=self):
            return
        files = sorted(os.path.join(folder, f) for f in os.listdir(folder)
                       if Path(f).suffix.lower() in SUPPORTED_EXTS)
        self._init_session(folder, files)
        if path in self.files:
            self._jump_to_index(self.files.index(path))

    def _copy_photos(self, paths: list[str]) -> None:
        """Copy photos (with their XMP sidecars) to a folder you choose."""
        dest = fd.askdirectory(title=f"Copy {len(paths)} photo(s) to…", parent=self)
        if not dest:
            return
        done = 0
        for p in paths:
            try:
                shutil.copy2(p, os.path.join(dest, os.path.basename(p)))
                xmp = _xmp_path_for(p)
                if xmp is not None and xmp.exists():
                    shutil.copy2(xmp, os.path.join(dest, xmp.name))
                done += 1
            except OSError as e:
                log.error("Copy failed %s: %s", p, e)
        self._flash_title(f"Copied {done} photo(s) to {dest}", 2500)

    @staticmethod
    def _reveal(path: str) -> None:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])

    def _update_picks(self) -> None:
        """The frame with the highest keeper chance in each burst."""
        picks = set()
        if self._keeper:
            for g in self._bursts:
                scored = [p for p in g if p in self._keeper]
                if len(g) > 1 and scored:
                    picks.add(max(scored, key=self._keeper.get))
        self._picks = picks

    def _jump_to_pick(self) -> None:
        """K: go to the predicted keeper of the current burst."""
        g = self._file_burst.get(self.files[self.index], [])
        pick = next((p for p in g if p in self._picks), None)
        if pick is None:
            self._flash_title("No predicted pick for this frame's burst yet", 2000)
        elif pick != self.files[self.index]:
            self._jump_to_index(self.files.index(pick))

    def _labelled_bursts(self) -> int:
        labels, group = labels_for(self.files, self.status, self._bursts)
        counts: dict[int, int] = defaultdict(int)
        for p in labels:
            counts[group[p]] += 1
        return sum(1 for c in counts.values() if c > 1)

    def _update_keeper_panel(self, path: str) -> None:
        if not hasattr(self, "_keeper_lbl"):
            return
        lines, colour = [], C_TEXT
        status = embedder_status()
        info = self._keeper_info or (self._model.meta if self._model else {})
        if status:
            lines.append(f"Unavailable — {status}")
            colour = C_MUTED
        elif not self._keeper:
            have = self._labelled_bursts() + (info.get("bursts", 0) if self._model else 0)
            if self._model is None:
                lines.append(f"Learning your choices — {min(have, MIN_BURSTS)} of "
                             f"{MIN_BURSTS} bursts with a Best so far")
                lines.append("Tag a Best in more bursts, or learn from past folders.")
            else:
                lines.append("Preparing predictions for this folder…")
            colour = C_MUTED
        else:
            p = self._keeper.get(path)
            if p is not None:
                lines.append(f"Keeper chance: {p:.0%}")
            g = self._file_burst.get(path, [path])
            if len(g) > 1:
                pick = next((q for q in g if q in self._picks), None)
                if pick == path:
                    lines.append("◆ Predicted pick of this burst")
                    colour = C_PICK
                elif pick:
                    lines.append(f"Predicted pick: frame {g.index(pick) + 1} of {len(g)} (K)")
        if info.get("frames"):
            lines.append(f"Learned from {info['frames']:,} frames · {info.get('bursts', 0):,} "
                         f"bursts · {info.get('folders', 1)} folder(s)")
        if info.get("agree") is not None:
            base = info.get("agree_sharp")
            lines.append(f"Picks your choice in {info['agree']:.0%} of test bursts"
                         + (f" (sharpness alone: {base:.0%})" if base is not None else ""))
        self._keeper_lbl.configure(text="\n".join(lines), fg=colour)

    def _save_folder_history(self) -> None:
        """Add this folder's choices to the learning history (on close/switch)."""
        if not self._history_dirty or not self._emb:
            return
        try:
            if self._history is None:
                self._load_history()
            samples = folder_samples(self.files, self.status, self._bursts, self._scores,
                                     self._detections, self._focus, self._emb)
            self._history.set_folder(self.base_dir, *samples)
            self._history.save()
            self._history_dirty = False
        except Exception as e:
            log.error("Could not save learning history: %s", e)

    def _learn_from_folders(self) -> None:
        """Button: learn from past folders under a chosen root (background)."""
        if self._learning is not None:
            self._learn_stop.set()
            self._learn_btn.configure(text="Stopping…")
            return
        root = fd.askdirectory(title="Folder containing your past shoots", parent=self,
                               initialdir=str(Path(self.base_dir).parent))
        if not root:
            return
        self._learn_stop.clear()
        self._learning = {"folders": 0, "total": 0, "frames": 0}
        self._learn_btn.configure(text="Stop learning")
        threading.Thread(target=self._learn_worker, args=(str(Path(root)),),
                         daemon=True).start()
        self._update_analysis_label()

    def _learn_worker(self, root: str) -> None:
        """Thread: features + labels for every past folder with a session file."""
        try:
            if self._history is None:
                self._load_history()
            folders = [d for d, _sub, fs in os.walk(root)
                       if STATUS_FILENAME in fs and str(Path(d)) != self.base_dir
                       and Path(d).name not in ("Best", "ID")]
            prog = {"folders": 0, "total": len(folders), "frames": 0}
            self._learn_q.put(("progress", dict(prog)))
            det = get_detector() if self._detect_on else None
            emb_model = get_embedder()
            for folder in folders:
                if self._learn_stop.is_set():
                    break
                prog["frames"] += self._learn_one(folder, det, emb_model)
                prog["folders"] += 1
                self._learn_q.put(("progress", dict(prog)))
            self._history.save()
            msg = (f"Learned from {prog['folders']} past folder(s), {prog['frames']:,} frames"
                   + (" (stopped)" if self._learn_stop.is_set() else ""))
        except Exception as e:
            log.exception("Learning from past folders failed: %s", e)
            msg = f"Learning from past folders failed: {e}"
        self._learn_q.put(("done", msg))

    def _learn_one(self, folder: str, det, emb_model) -> int:
        files = sorted(os.path.join(folder, f) for f in os.listdir(folder)
                       if Path(f).suffix.lower() in SUPPORTED_EXTS)
        if not files or emb_model is None:
            return 0
        valid = set(files)
        status = session_status(folder, files)
        hash_file = os.path.join(folder, HASH_CACHE_FILE)
        hashes, stamps, focus = load_hash_cache(hash_file, valid)
        changed = False
        for p in files:                                    # bursts need hashes
            if self._learn_stop.is_set():
                return 0
            if p in hashes and p in stamps:
                continue
            try:
                img = _open_thumb_unoriented(p)
                stamps[p] = _capture_timestamp(p, img)
                if getattr(img, "format", None) == "JPEG":
                    img.draft("L", (64, 64))
                hashes[p] = imagehash.average_hash(auto_orient(img).convert("L"))
                changed = True
            except Exception:
                stamps[p] = None
        bursts = group_bursts(files, hashes, stamps, AHASH_BURST, BURST_MAX_SECS)
        labels, _ = labels_for(files, status, bursts)
        score_file = os.path.join(folder, SCORE_CACHE_FILE)
        detect_file = os.path.join(folder, DETECT_CACHE_FILE)
        scores = load_score_cache(score_file, valid)
        dets = load_detections(detect_file, valid, MODEL_ID)
        emb = load_features(folder, valid)
        for p in labels:                                   # only frames with a label
            if self._learn_stop.is_set():
                return 0
            if (scores.get(p) or {}).get("v") == SCORE_VERSION and p in emb \
                    and (det is None or p in dets):
                continue
            try:
                img = load_preview(p, SCORE_MIN_SIDE)
                if det is not None and p not in dets:
                    dets[p] = refine(det.detect(img), img, get_species_model())
                if p not in focus:
                    focus[p] = read_focus_area(p, *self._af_grid)
                region, kind = self._sharpness_region(focus.get(p), dets.get(p))
                scores[p] = score_image(img, region, kind)
                a = main_animal(dets.get(p))
                emb[p] = emb_model.embed(img, a[2:6] if a else None)
                changed = True
            except Exception as e:
                log.debug("Learning: skipped %s: %s", p, e)
        if changed:                                        # cache for next time
            save_hash_cache(hash_file, hashes, stamps, focus)
            save_score_cache(score_file, scores)
            save_detections(detect_file, dets, MODEL_ID)
            save_features(folder, emb)
        samples = folder_samples(files, status, bursts, scores, dets, focus, emb)
        self._history.set_folder(str(Path(folder)), *samples)
        return int(len(samples[3]))

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

    # ── Background image loading ─────────────────────────────────────────────
    #
    # Worker threads never touch Tkinter; results come back through queues
    # drained on the main thread by _poll_background():
    #   _preload_cache  ±PRELOAD_RADIUS images pre-sized to the canvas, plus
    #                   EXIF / score / histogram computed from the same decode
    #   _thumb_cache    small thumbnails for gallery views and the filmstrip
    #                   (LRU-bounded, THUMB_CACHE_MAX entries)
    #   _detail_image   full-resolution preview of the current image only,
    #                   used for 1:1 zoom and the loupe

    def _init_background(self) -> None:
        self._preload_executor = ThreadPoolExecutor(max_workers=3)
        self._preload_wanted: set[str] = set()
        self._preload_inflight: set[str] = set()
        self._preload_q: queue.Queue = queue.Queue()
        self._thumb_executor = ThreadPoolExecutor(max_workers=THUMB_WORKERS)
        self._thumb_cache: "OrderedDict[str, Image.Image]" = OrderedDict()
        self._thumb_failed: set[str] = set()
        self._thumb_inflight: set[str] = set()
        self._thumb_q: queue.Queue = queue.Queue()
        self._thumb_wanted_grid: set[str] = set()
        self._thumb_wanted_strip: set[str] = set()
        self._detail_executor = ThreadPoolExecutor(max_workers=1)
        self._detail_image: Optional[Image.Image] = None
        self._detail_path: Optional[str] = None
        self._detail_q: queue.Queue = queue.Queue()
        self._detail_job: Optional[str] = None

    def _reset_background(self) -> None:
        """Forget per-folder image caches (folder switch)."""
        self._preload_cache.clear()
        self._preload_wanted = set()
        self._thumb_cache.clear()
        self._thumb_failed.clear()
        self._thumb_wanted_grid = set()
        self._thumb_wanted_strip = set()
        self._fs_photos.clear()
        self._detail_image = None
        self._detail_path = None

    def _display_min_side(self) -> int:
        """Decode size that still fills the canvas (either orientation)."""
        return max(self._canvas_w or 1200, self._canvas_h or 800)

    def _fit_to_canvas(self, img: Image.Image, in_place: bool = False) -> Image.Image:
        """Scale an image to fit the canvas (high quality, fast for large images)."""
        cw = self._canvas_w or 1200
        ch = self._canvas_h or 800
        iw, ih = img.size
        scale = min(cw / iw, ch / ih)
        size = (max(1, int(iw * scale)), max(1, int(ih * scale)))
        if scale < 1:
            out = img if in_place else img.copy()
            out.thumbnail(size, Image.LANCZOS, reducing_gap=3.0)
            return out
        return img.resize(size, Image.BILINEAR)

    def _analyse(self, path: str, img: Image.Image) -> tuple:
        """EXIF, quality score and histogram — whichever are still missing."""
        exif = None if path in self._exif_cache else read_exif(path)
        score = hist = None
        current = self._scores.get(path)
        if not current or current.get("v") != SCORE_VERSION:
            focus = (self._focus[path] if path in self._focus
                     else read_focus_area(path, *self._af_grid))
            score = score_image(img, *self._sharpness_region(
                focus, self._detections.get(path)))
        if path not in self._histograms:
            hist = build_histogram(img)
        return exif, score, hist

    def _schedule_preload(self) -> None:
        """Queue decodes for ±PRELOAD_RADIUS images (nearest first); evict the rest."""
        targets = []
        for offset in range(1, PRELOAD_RADIUS + 1):
            for direction in (offset, -offset):
                idx = self.index + direction
                if 0 <= idx < self.total:
                    targets.append(self.files[idx])
        keep = set(targets) | {self.files[self.index]}
        self._preload_wanted = keep
        for path in list(self._preload_cache):
            if path not in keep:
                del self._preload_cache[path]
        gen = self._session_gen
        for path in targets:
            if path not in self._preload_cache and path not in self._preload_inflight:
                self._preload_inflight.add(path)
                self._preload_executor.submit(self._preload_worker, path, gen)

    def _preload_worker(self, path: str, gen: int) -> None:
        """Thread: decode once → info (EXIF/score/histogram) + canvas-sized image."""
        result = (path, None, None, None, None)
        try:
            # Skip work that went stale while queued (user moved on)
            if gen == self._session_gen and path in self._preload_wanted:
                img = load_preview(path, self._display_min_side())
                info = self._analyse(path, img)
                result = (path, self._fit_to_canvas(img, in_place=True), *info)
        except Exception as e:
            log.debug("Preload failed %s: %s", path, e)
        self._preload_q.put(result)

    def _analyse_worker(self, path: str, img: Optional[Image.Image], gen: int) -> None:
        """Thread: EXIF/score/histogram for the current image."""
        result = (path, None, None, None, None)
        try:
            if gen == self._session_gen:
                if img is None:
                    img = load_preview(path, SCORE_MIN_SIDE)
                result = (path, None, *self._analyse(path, img))
        except Exception as e:
            log.debug("Analysis failed %s: %s", path, e)
        self._preload_q.put(result)

    def _request_info(self, path: str, img: Optional[Image.Image] = None) -> None:
        if path not in self._preload_inflight:
            self._preload_inflight.add(path)
            self._preload_executor.submit(self._analyse_worker, path, img, self._session_gen)

    def _request_thumbs(self, paths) -> None:
        """Queue small-thumbnail decodes for paths not cached yet."""
        gen = self._session_gen
        for p in paths:
            if p in self._thumb_cache or p in self._thumb_failed or p in self._thumb_inflight:
                continue
            self._thumb_inflight.add(p)
            self._thumb_executor.submit(self._thumb_worker, p, gen)

    def _thumb_worker(self, path: str, gen: int) -> None:
        img: object = _SKIPPED
        if gen == self._session_gen and (path in self._thumb_wanted_grid
                                         or path in self._thumb_wanted_strip):
            try:
                off = self._offline.get(path)
                if off and not os.path.exists(path):
                    with Image.open(off[0]) as im:
                        img = im.convert("RGB")
                else:
                    img = load_small_thumb(path)
            except Exception as e:
                log.debug("Thumbnail failed %s: %s", path, e)
                img = None
        self._thumb_q.put((path, img))

    def _schedule_detail(self) -> None:
        """Load the full-resolution preview once the user settles on an image."""
        if self._detail_job:
            self.after_cancel(self._detail_job)
            self._detail_job = None
        path = self.files[self.index]
        if self._detail_path == path:
            return
        self._detail_image = None
        self._detail_path = None
        self._detail_job = self.after(DETAIL_DELAY_MS, lambda p=path: self._start_detail(p))

    def _start_detail(self, path: str) -> None:
        self._detail_job = None
        if self._current_path == path:
            self._detail_executor.submit(self._detail_worker, path, self._session_gen)

    def _detail_worker(self, path: str, gen: int) -> None:
        img = None
        if gen == self._session_gen and self._current_path == path:
            try:
                img = _extract_thumb_img(path)
                img.load()
            except Exception as e:
                log.debug("Detail load failed %s: %s", path, e)
                img = None
        self._detail_q.put((path, img))

    def _ensure_detail_now(self) -> None:
        """Load the full-resolution preview immediately (1:1 key, loupe)."""
        path = self._current_path
        if path and self._detail_path != path:
            try:
                self._detail_image = _extract_thumb_img(path)
                self._detail_path = path
            except Exception as e:
                log.debug("Detail load failed %s: %s", path, e)

    def _poll_background(self) -> None:
        """Main-thread poll: hand worker results to the UI."""
        try:
            if not self.winfo_exists():
                return
            self._drain_background()
        except tk.TclError:
            return
        self.after(60, self._poll_background)

    def _drain_background(self) -> None:
        current = self._current_path
        strip_dirty = sidebar_dirty = False

        while True:
            try:
                path, presized, exif, score, hist = self._preload_q.get_nowait()
            except queue.Empty:
                break
            self._preload_inflight.discard(path)
            if path not in self.status:
                continue                      # result from a previous folder
            if presized is not None and path in self._preload_wanted:
                self._preload_cache[path] = presized
                strip_dirty = True
            if exif is not None:
                self._exif_cache[path] = exif
            if score is not None and (self._scores.get(path) or {}).get("v") != SCORE_VERSION:
                self._scores[path] = score
                self._scores_dirty = True
            if hist is not None:
                self._histograms[path] = hist
            if path == current:
                sidebar_dirty = True

        while True:
            try:
                path, img = self._thumb_q.get_nowait()
            except queue.Empty:
                break
            self._thumb_inflight.discard(path)
            if img is _SKIPPED:
                continue
            if img is None:
                self._thumb_failed.add(path)
            else:
                self._thumb_cache[path] = img
                self._thumb_cache.move_to_end(path)
                while len(self._thumb_cache) > THUMB_CACHE_MAX:
                    self._thumb_cache.popitem(last=False)
            if path in self._thumb_wanted_strip:
                strip_dirty = True

        while True:
            try:
                path, img = self._detail_q.get_nowait()
            except queue.Empty:
                break
            if path == current and img is not None:
                self._detail_image, self._detail_path = img, path
                if self._zoom > 1.01:
                    self._render_image()

        analysed = False
        while True:
            try:
                gen, path, score, dets, focus, emb, clip = self._analysis_q.get_nowait()
            except queue.Empty:
                break
            if gen != self._session_gen:
                continue
            self._analysis_done += 1
            analysed = True
            if path not in self.status:
                continue
            if score is not None:
                self._scores[path] = score
                self._scores_dirty = True
            if dets is not _SKIPPED:
                self._detections[path] = dets
                self._dets_dirty = True
            if focus is not _SKIPPED:
                self._focus[path] = focus
            if emb is not _SKIPPED:
                self._emb[path] = emb
                self._emb_dirty = True
            if path == current:
                if dets is not _SKIPPED:
                    self._request_eye(path, urgent=True)
                self._request_rawclip(path, urgent=True)
            if clip is not _SKIPPED:
                self._clip[os.path.basename(path)] = clip
                self._clip_dirty = True
            if path == current:
                sidebar_dirty = True
            if path in self._thumb_wanted_strip:
                strip_dirty = True
        if analysed:
            self._update_analysis_label()
            if not self._analysis_running():
                self._save_scores_if_dirty()
                self._schedule_retrain(300)          # predict with the new features
                self._start_species()
                self._start_eyes()
                self._start_rawclip()
                if self._overlay_view is not None and hasattr(self._overlay_view, "_relayout"):
                    self._overlay_view._relayout(keep_scroll=True)   # show new notes

        self._drain_clip_masks()
        self._drain_eyes()
        self._drain_rawclip()
        self._drain_training()
        self._drain_species()
        self._drain_regions()

        self._drain_index()
        if (self._scores_dirty or self._dets_dirty or self._emb_dirty or self._species_dirty
                or self._clip_dirty or self._eyes_dirty) and not self._score_save_job:
            self._schedule_score_save()
        if strip_dirty and self.files:
            self._refresh_filmstrip(self.index)
        if sidebar_dirty and current:
            self._refresh_sidebar_data(current)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.title("Photo Reviewer")
        # Set taskbar icon
        try:
            _li = Image.open(str(_resource_path("photo_reviewer_icon.ico")))
            self._taskbar_icon = ImageTk.PhotoImage(_li)
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
        tk.Button(hdr, text="✎ Rename", bg="#111111", fg=C_MUTED,
                  font=("Helvetica", 8), relief="flat", bd=0, cursor="hand2",
                  command=self._rename_selected_folder).pack(side="right", padx=4)

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
        self._folder_listbox.bind("<Button-3>", self._folder_menu)
        self._populate_folder_tree()

    def _folder_menu(self, event) -> None:
        """Right-click a folder: Rename…"""
        i = self._folder_listbox.nearest(event.y)
        dirs = getattr(self, "_tree_dirs", [])
        if not (0 <= i < len(dirs)):
            return
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label=f"Rename “{dirs[i].name}”…",
                         command=lambda d=str(dirs[i]): self._rename_folder(d))
        menu.tk_popup(event.x_root, event.y_root)

    def _rename_selected_folder(self) -> None:
        """Header button: rename the folder picked in the list (else the open one)."""
        sel = self._folder_listbox.curselection()
        dirs = getattr(self, "_tree_dirs", [])
        self._rename_folder(str(dirs[sel[0]]) if sel and sel[0] < len(dirs) else self.base_dir)

    def _rename_folder(self, folder: str, new_name: Optional[str] = None) -> Optional[str]:
        """
        Rename a shoot folder. Tags, caches, Best/ and ID/ move with it (they
        are matched by file name); the region choice, recent list, learning
        history and search library are pointed at the new name. Returns the
        new path, or None if nothing was renamed.
        """
        folder = str(Path(folder))
        old_name = Path(folder).name
        if new_name is None:
            new_name = sd.askstring("Rename folder", "New folder name:",
                                    initialvalue=old_name, parent=self)
        if new_name is None:
            return None
        new_name = new_name.strip().rstrip(". ")
        if not new_name or new_name == old_name:
            return None
        if re.search(r'[<>:"/\\|?*]', new_name):
            mb.showerror("Rename folder", 'A folder name can\'t contain < > : " / \\ | ? *',
                         parent=self)
            return None
        new = str(Path(folder).parent / new_name)
        if os.path.exists(new) and os.path.normcase(new) != os.path.normcase(folder):
            mb.showerror("Rename folder", f"“{new_name}” already exists here.", parent=self)
            return None
        if ImportDialog.is_importing(folder):
            mb.showerror("Rename folder", "This folder is still being imported into — "
                         "rename it when the import has finished.", parent=self)
            return None
        is_open = os.path.normcase(folder) == os.path.normcase(self.base_dir) or \
            os.path.normcase(self.base_dir).startswith(os.path.normcase(folder) + os.sep)
        if is_open:
            self._suspend_session()
        err = None
        for attempt in range(12):                 # files may be closing (thumbnails…)
            try:
                os.rename(folder, new)
                err = None
                break
            except OSError as e:
                err = e
                time.sleep(0.25)
                self.update()
        if err is not None:
            mb.showerror("Rename folder", f"Couldn't rename “{old_name}”:\n{err}\n\n"
                         "Close any program using photos in it (e.g. darktable) and try again.",
                         parent=self)
            if is_open:
                self._init_session(self.base_dir, self.files, save_first=False)
            return None
        self._remap_folder_refs(folder, new)
        if is_open:
            nd = remap_path(self.base_dir, folder, new)
            files = sorted(str(Path(nd) / f) for f in os.listdir(nd)
                           if Path(f).suffix.lower() in SUPPORTED_EXTS)
            keep = Path(self._current_path).name if self._current_path else None
            self._init_session(nd, files, save_first=False)
            if keep:
                p = str(Path(nd) / keep)
                if p in self.files:
                    self._jump_to_index(self.files.index(p))
        self._populate_folder_tree()
        self._flash_title(f"Renamed to “{new_name}”", 2000)
        log.info("Renamed folder %s → %s", folder, new)
        return new

    def _suspend_session(self) -> None:
        """Save everything and stop background work (before renaming the open folder)."""
        self._flush_metadata()
        self._save_status_now()
        self._save_folder_history()
        self._save_scores_if_dirty()
        if self._hash_done < self._hash_total_needed:
            self._save_hash_progress()
        if self._species_dirty:
            save_predictions(self.base_dir, self._species_pred)
            self._species_dirty = False
        if self._compare_mode:
            self._exit_compare_mode()
        self._hide_overlay()
        self._session_gen += 1
        for attr in ("_executor", "_analysis_executor", "_species_bg", "_eye_bg", "_rawclip_bg"):
            ex = getattr(self, attr)
            if ex:
                ex.shutdown(wait=True, cancel_futures=True)
                setattr(self, attr, None)
        self._eye_pending.clear()
        self._eye_urgent.clear()
        self._rawclip_pending.clear()
        self._rawclip_urgent.clear()
        self._preload_cache.clear()
        self._thumb_cache.clear()

    def _remap_folder_refs(self, old: str, new: str) -> None:
        """Settings, history and caches that remember folder paths."""
        s = self._app_settings
        if isinstance(s.get("folder_regions"), dict):
            s["folder_regions"] = {remap_path(k, old, new): v for k, v in s["folder_regions"].items()}
        if isinstance(s.get("library_root"), str):
            s["library_root"] = remap_path(s["library_root"], old, new)
        roots = s.get("library_roots")
        if isinstance(roots, list):
            for r in roots:
                if isinstance(r, dict) and isinstance(r.get("path"), str):
                    r["path"] = remap_path(r["path"], old, new)
        save_app_settings(s)
        rename_recent_folder(old, new)
        try:
            if self._history is None:
                self._load_history()
            if self._history.rename_folder(old, new):
                self._history.save()
        except Exception as e:
            log.error("Could not update learning history: %s", e)
        self._library = {remap_path(k, old, new): v for k, v in self._library.items()}
        self._status_cache.clear()

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
        self._rename_entry.bind("<Tab>",       lambda _: (self._apply_rename(), "break")[1])
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


        # Status feedback label (right-aligned)
        self._rename_status = tk.Label(
            bar, text="", bg="#111111", fg=C_MUTED,
            font=("Helvetica", 9))
        self._rename_status.pack(side="right", padx=12)

        self._rename_original: str = ""   # track original name for cancel
        self._rename_active   = False      # True while entry has focus

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
            _ico = Image.new("RGBA", (22, 22), (0, 0, 0, 0))
            _d   = ImageDraw.Draw(_ico)
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
            self._header_icon_img = ImageTk.PhotoImage(_ico)
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
        txt, bg, fg = AUTO_ADVANCE_LABELS[self._auto_mode]
        self._adv_btn_lbl = tk.Button(
            hdr, text=txt, bg=bg, fg=fg,
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
        tk.Frame(hdr, bg=C_BORDER, width=1).pack(side="right", fill="y", pady=8, padx=2)

        # Display tools — labelled buttons showing active state
        for label, active_label, cmd, attr in [
            ("1:1",     "1:1",      self._zoom_to_100,            None),
            ("Loupe",   "Loupe ON", self._toggle_loupe,           "_loupe_btn"),
            ("Peak",    "Peak ON",  self._toggle_focus_peaking,   "_peak_btn"),
            ("Clip",    "Clip ON",  self._toggle_clip_view,       "_clip_btn"),
            ("AF+Animal", "AF ON",  self._toggle_boxes,           "_boxes_btn"),
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
        self._analysis_lbl = ctk.CTkLabel(hdr, text="",
                                          font=ctk.CTkFont("Helvetica", 9),
                                          text_color=C_WARN)
        self._progress_label.pack(side="right", padx=(4,2))
        self._analysis_lbl.pack(side="right", padx=(4, 6))

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

        _wf_btn("⊞ Gallery  G",    self._open_gallery,         "gallery",        96)
        _wf_btn("⊟ Compare  C",    self._open_comparison,      None,             96)
        tk.Frame(wf, bg=C_BORDER, width=1).pack(side="left", fill="y", pady=6)
        _wf_btn("⚡ Clean Load",    self._open_scoring_quick,   "scoring_quick",  92)
        _wf_btn("◎ Sightings  V",   self._open_sightings,       "sightings",      100)
        _wf_btn("⊹ Bursts",         self._open_burst_triage,    "burst_triage",   72)
        _wf_btn("🔎 ID Review",      self._open_id_review,       "id_review",      92)
        _wf_btn("🔍 Search",         self._open_search,          "search",         80)
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
                              corner_radius=16, width=380, height=200)
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
        self._loading_bar.pack(pady=(0, 6))
        self._loading_eta = ctk.CTkLabel(inner, text="",
                                         font=ctk.CTkFont("Helvetica", 10),
                                         text_color=C_MUTED)
        self._loading_eta.pack(pady=(0, 16))

    def _update_loading_overlay(self, message: str, progress: float = -1,
                                eta: str = "") -> None:
        """Update status text, progress bar (0.0–1.0) and time-left estimate."""
        if hasattr(self, "_loading_label"):
            self._loading_label.configure(text=message)
        if progress >= 0 and hasattr(self, "_loading_bar"):
            self._loading_bar.set(min(1.0, progress))
        if hasattr(self, "_loading_eta"):
            self._loading_eta.configure(text=eta)
        self.update_idletasks()

    def _show_loading(self, message: str) -> None:
        """Cover the window with the progress panel."""
        self._update_loading_overlay(message, 0, "")
        self._overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._overlay.tkraise()

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
        focus = self._focus.get(self._current_path) if self._current_path else None
        if focus:
            parts.append(f"AF {focus[4]}")
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
        self._progress_info = tk.Label(body, text="", bg=C_SURFACE, fg=C_TEXT,
                                       font=("Helvetica", 9), justify="left",
                                       anchor="w", wraplength=230)
        self._progress_info.pack(fill="x", padx=20, pady=(6, 8))

        # ── Name for Best: species / date / location ─────────────────────────
        body = accordion("NAME FOR BEST", start_open=True)
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Name.TCombobox", fieldbackground=C_BG, background=C_BORDER,
                        foreground=C_TEXT, arrowcolor=C_TEXT, bordercolor=C_BORDER)
        style.map("Name.TCombobox", fieldbackground=[("readonly", C_BG)])

        def label(text):
            tk.Label(body, text=text, bg=C_SURFACE, fg=C_MUTED,
                     font=("Helvetica", 8)).pack(anchor="w", padx=12, pady=(4, 0))
        label("Species")
        self._name_species = ttk.Combobox(body, style="Name.TCombobox", font=("Helvetica", 10))
        self._name_species.pack(fill="x", padx=12)
        self._name_state = tk.Label(body, text="", bg=C_SURFACE, fg=C_MUTED,
                                    font=("Helvetica", 8), anchor="w", justify="left",
                                    wraplength=230)
        self._name_state.pack(fill="x", padx=12)
        label("Date")
        self._name_date = tk.Entry(body, bg=C_BG, fg=C_TEXT, insertbackground=C_TEXT,
                                   relief="flat", font=("Helvetica", 10))
        self._name_date.pack(fill="x", padx=12)
        label("Location")
        self._name_location = ttk.Combobox(body, style="Name.TCombobox", font=("Helvetica", 10))
        self._name_location.pack(fill="x", padx=12)
        label("Region (ranks local species first)")
        self._name_region = ttk.Combobox(body, style="Name.TCombobox", font=("Helvetica", 10),
                                         values=["Worldwide"] + [c["name"] for c in countries()])
        self._name_region.pack(fill="x", padx=12)
        self._region_lbl = tk.Label(body, text="", bg=C_SURFACE, fg=C_MUTED,
                                    font=("Helvetica", 8), anchor="w", justify="left",
                                    wraplength=230)
        self._region_lbl.pack(fill="x", padx=12)

        def commit_region(_e=None):
            value = self._name_region.get().strip()
            if not value or value.lower() == "worldwide":
                self._set_folder_region(None)
            else:
                c = by_name(value)
                if c is None:
                    self._flash_title(f"Unknown country: {value}", 2000)
                else:
                    self._set_folder_region(c["iso"])
            self._canvas.focus_set()
            return "break"
        self._name_region.bind("<<ComboboxSelected>>", commit_region)
        self._name_region.bind("<Return>", commit_region)
        self._name_burst_var = tk.BooleanVar(value=self._name_burst)

        def burst_toggle():
            self._name_burst = bool(self._name_burst_var.get())
            self._app_settings["name_whole_burst"] = self._name_burst
            save_app_settings(self._app_settings)
        tk.Checkbutton(body, text="Apply to the whole burst", variable=self._name_burst_var,
                       command=burst_toggle, bg=C_SURFACE, fg=C_MUTED,
                       activebackground=C_SURFACE, selectcolor=C_BG,
                       font=("Helvetica", 8)).pack(anchor="w", padx=8)
        self._name_preview = tk.Label(body, text="", bg=C_SURFACE, fg=C_PICK,
                                      font=("Courier", 8), anchor="w", justify="left",
                                      wraplength=240)
        self._name_preview.pack(fill="x", padx=12, pady=(2, 8))

        def commit_species(_e=None):
            value = self._name_species.get()
            if value and value != self.status.get(self._current_path, {}).get("species"):
                self._confirm_species(value)
            self._canvas.focus_set()
            return "break"
        self._name_species.bind("<<ComboboxSelected>>", commit_species)
        self._name_species.bind("<Return>", commit_species)
        self._name_species.bind("<Escape>", lambda e: (self._update_name_panel(self._current_path),
                                                       self._canvas.focus_set()))

        def commit_location(_e=None):
            value = self._name_location.get()
            if value != self._name_parts(self._current_path)["location"]:
                self._confirm_location(value)
            self._canvas.focus_set()
            return "break"
        self._name_location.bind("<<ComboboxSelected>>", commit_location)
        self._name_location.bind("<Return>", commit_location)
        self._name_location.bind("<FocusOut>", lambda e: commit_location())

        def commit_date(_e=None):
            if self._name_date.get().strip() != self._name_parts(self._current_path)["date"]:
                self._confirm_date(self._name_date.get())
            return "break"
        self._name_date.bind("<Return>", lambda e: (commit_date(), self._canvas.focus_set()))
        self._name_date.bind("<FocusOut>", commit_date)

        # ── Suggestions (from animal detection + focus point) ─────────────────
        body = accordion("SUGGESTIONS", start_open=True)
        self._suggest_lbl = tk.Label(body, text="", bg=C_SURFACE, fg=C_TEXT,
                                     font=("Helvetica", 9), justify="left",
                                     anchor="w", wraplength=230)
        self._suggest_lbl.pack(fill="x", padx=16, pady=(6, 8))

        # ── Keeper prediction ─────────────────────────────────────────────────
        body = accordion("KEEPER PREDICTION", start_open=True)
        self._keeper_lbl = tk.Label(body, text="", bg=C_SURFACE, fg=C_TEXT,
                                    font=("Helvetica", 9), justify="left",
                                    anchor="w", wraplength=230)
        self._keeper_lbl.pack(fill="x", padx=16, pady=(6, 4))
        self._learn_btn = tk.Button(body, text="Learn from past folders…",
                                    bg=C_BORDER, fg=C_TEXT, relief="flat", bd=0,
                                    font=("Helvetica", 9), padx=8,
                                    command=self._learn_from_folders)
        self._learn_btn.pack(anchor="w", padx=16, pady=(0, 8))

        # ── Quality ───────────────────────────────────────────────────────────
        body = accordion("QUALITY  (0–100)", start_open=True)
        self._score_rows: dict[str, ScoreRow] = {}
        for metric, label in [
            ("sharpness",   "Sharpness"),
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
            ("⚑ ID",      "I / Space", self._mark_id,       C_ID,       "white"),
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
        self._current_path = path
        pct         = int((self.index + 1) / self.total * 100)
        self._title_label.configure(text=Path(path).name)
        self._progress_label.configure(text=f"{self.index + 1} / {self.total}  ({pct}%)")
        if hasattr(self, "_session_prog_bar") and self.total > 0:
            self._session_prog_bar.place(relwidth=(self.index + 1) / self.total)
        if not self._rename_active:
            self._populate_rename_entry(path)

        # ── Image: pre-sized copy from the preload cache, else decode now ─────
        if self._detail_path != path:
            self._detail_image = None
            self._detail_path = None
        cached = self._preload_cache.get(path)
        if cached is not None:
            self._pil_image = cached
        else:
            try:
                small = load_preview(path, self._display_min_side())
                self._pil_image = self._fit_to_canvas(small)
                self._preload_cache[path] = self._pil_image
                if path not in self._exif_cache or path not in self._scores \
                        or path not in self._histograms:
                    self._request_info(path, small)
            except Exception as e:
                self._pil_image = None
                self._canvas.delete("all")
                cw = self._canvas_w or 800
                ch = self._canvas_h or 600
                self._canvas.create_text(cw / 2, ch / 2,
                                         text=f"Cannot load image\n{e}",
                                         fill=C_MUTED, font=("Helvetica", 11),
                                         justify="center")

        self._zoom = 1.0; self._pan_x = 0.0; self._pan_y = 0.0
        if self._lanczos_job:
            self.after_cancel(self._lanczos_job)
            self._lanczos_job = None
        self._render_image()

        self._schedule_preload()
        self._schedule_detail()
        self._eye_view = False
        self._request_eye(path, urgent=True)
        self._request_rawclip(path, urgent=True)

        self._update_counts()
        self._update_star_display()
        self._refresh_burst_ui(path)
        self._refresh_sidebar(path)
        self._refresh_filmstrip(self.index)

    def _display_source(self) -> Image.Image:
        """Full-resolution preview when zoomed in (if loaded), else the fit copy."""
        if (self._zoom > 1.01 and self._detail_image is not None
                and self._detail_path == self._current_path):
            return self._detail_image
        return self._pil_image

    def _render_image(self, quality: int = Image.BILINEAR) -> None:
        """
        Draw the current image. Only the part visible on the canvas is
        resized, so deep zoom stays fast.
        """
        if self._pil_image is None:
            return
        self._canvas.delete("all")
        cw = self._canvas_w or self._canvas.winfo_width()  or 1200
        ch = self._canvas_h or self._canvas.winfo_height() or 800
        src = self._display_source()
        iw, ih = src.size
        s  = min(cw / iw, ch / ih) * self._zoom          # screen px per source px
        cx = cw / 2 + self._pan_x                         # image centre on canvas
        cy = ch / 2 + self._pan_y
        x0 = max(0, int((0 - cx) / s + iw / 2))
        y0 = max(0, int((0 - cy) / s + ih / 2))
        x1 = min(iw, int(math.ceil((cw - cx) / s + iw / 2)))
        y1 = min(ih, int(math.ceil((ch - cy) / s + ih / 2)))
        self._update_zoom_label()
        if x1 <= x0 or y1 <= y0:
            return
        tw = max(1, round((x1 - x0) * s))
        th = max(1, round((y1 - y0) * s))
        if (x0, y0, x1, y1) == (0, 0, iw, ih) and abs(tw - iw) <= 1 and abs(th - ih) <= 1:
            img = src
        else:
            img = src.resize((tw, th), quality, box=(x0, y0, x1, y1))

        if self._focus_peaking:
            try:
                img = self._apply_focus_peaking(img)
            except Exception as e:
                log.debug("Focus peaking failed: %s", e)
        if self._clip_view:
            try:
                img = self._apply_clip_view(img, (x0 / iw, y0 / ih, x1 / iw, y1 / ih))
            except Exception as e:
                log.debug("Clipping overlay failed: %s", e)

        self._tk_thumb = ImageTk.PhotoImage(img)
        self._canvas.create_image(cx + (x0 - iw / 2) * s, cy + (y0 - ih / 2) * s,
                                  image=self._tk_thumb, anchor="nw")
        if self._show_boxes:
            self._draw_boxes(cx, cy, iw * s, ih * s)

    def _draw_boxes(self, cx: float, cy: float, dw: float, dh: float) -> None:
        """Overlay focus point (yellow) and detections on the displayed image."""
        path = self._current_path

        def rect(box, colour, text, width=2, dash=None):
            x0 = cx + (box[0] - 0.5) * dw
            y0 = cy + (box[1] - 0.5) * dh
            x1 = cx + (box[2] - 0.5) * dw
            y1 = cy + (box[3] - 0.5) * dh
            self._canvas.create_rectangle(x0, y0, x1, y1, outline=colour,
                                          width=width, dash=dash)
            self._canvas.create_text(x0 + 3, y0 - 2, text=text, fill=colour,
                                     anchor="sw", font=("Helvetica", 9, "bold"))

        for d in self._detections.get(path) or []:
            if d[1] < 0.2:
                continue
            colour = (C_ANIMAL, C_COMPARE, C_DELETE)[d[0]]
            rect(d[2:6], colour, f"{('animal', 'person', 'vehicle')[d[0]]} {d[1]:.0%}", dash=(4, 2))
        focus = self._focus.get(path)
        if focus:
            rect(focus, C_FOCUS, f"AF {focus[4]}", width=3)
        eye = (self._eyes.get(path) or {}) if self._eye_on else {}
        if eye.get("b"):
            rect(eye["b"], C_BEST, "eye", width=2)

    def _full_res_size(self) -> tuple[int, int]:
        if self._detail_image is not None and self._detail_path == self._current_path:
            return self._detail_image.size
        return self._pil_image.size if self._pil_image is not None else (1, 1)

    def _update_zoom_label(self) -> None:
        if self._zoom <= 1.001:
            text = "Fit"
        else:
            cw = self._canvas_w or 1200
            ch = self._canvas_h or 800
            iw, ih = self._full_res_size()
            text = f"{min(cw / iw, ch / ih) * self._zoom * 100:.0f}%"
        self._zoom_label.configure(text=text)

    # ── Burst UI ──────────────────────────────────────────────────────────────

    def _refresh_burst_ui(self, path: str) -> None:
        """Show 'Burst n/m' in the header when the image is part of a burst."""
        if self._hash_ready:
            group = self._file_burst.get(path, [path])
            if len(group) > 1:
                pos = (group.index(path) + 1) if path in group else "?"
                self._burst_label.configure(text=f"Burst {pos}/{len(group)}")
                return
        self._burst_label.configure(text="")

    def _refresh_filmstrip(self, current_idx: int) -> None:
        """
        Filmstrip of ±FILMSTRIP_WINDOW images centred on the current one.
        Thumbnails come from the small-thumbnail cache (requested here if
        missing); drawn thumbnails are reused between redraws.
        """
        fc = self._filmstrip_canvas
        fc.delete("all")
        self._filmstrip_outer.grid()
        if not self.files:
            return
        lo = max(0, current_idx - FILMSTRIP_WINDOW)
        hi = min(self.total - 1, current_idx + FILMSTRIP_WINDOW)
        nearest = sorted(range(lo, hi + 1), key=lambda i: abs(i - current_idx))
        self._thumb_wanted_strip = {self.files[i] for i in nearest}
        self._request_thumbs([self.files[i] for i in nearest])

        W  = fc.winfo_width()
        if W < 50:
            W = self._canvas_w or 1200
        th = FILMSTRIP_H - 6
        MIN_CW, MAX_CW = 60, 200

        def source(i: int) -> Optional[Image.Image]:
            p = self.files[i]
            img = self._thumb_cache.get(p) or self._preload_cache.get(p)
            if img is None and i == current_idx:
                img = self._pil_image
            return img

        def width_of(img: Optional[Image.Image]) -> int:
            if img is None:
                return int(th * 1.5)
            return max(MIN_CW, min(MAX_CW, int(img.width / img.height * th)))

        # Lay out lo..hi side by side, centre the current image, but don't
        # leave empty space at the start / end of the folder
        widths = {i: width_of(source(i)) for i in range(lo, hi + 1)}
        xs, x = {}, 0
        for i in range(lo, hi + 1):
            xs[i] = x
            x += widths[i] + 2
        total_w = x - 2
        offset = W // 2 - (xs[current_idx] + widths[current_idx] // 2)
        offset = max(min(offset, 0), min(0, W - total_w))
        placed = {i: (xs[i] + offset, widths[i]) for i in range(lo, hi + 1)
                  if xs[i] + offset < W and xs[i] + offset + widths[i] > 0}

        self._fs_tag_items.clear()
        self._fs_stripe_items.clear()
        for idx, (x0, cw) in placed.items():
            p          = self.files[idx]
            x1         = x0 + cw
            is_current = idx == current_idx
            tags       = self.status.get(p, {})
            in_compare = p in self._compare_set

            fc.create_rectangle(x0, 0, x1, FILMSTRIP_H,
                                fill="#1e2130" if is_current else C_BG, outline="")
            photo = self._fs_photos.get((p, cw))
            if photo is None:
                img = source(idx)
                if img is not None:
                    photo = ImageTk.PhotoImage(img.resize((cw, th), Image.BILINEAR))
                    self._fs_photos[(p, cw)] = photo
                    while len(self._fs_photos) > 200:
                        self._fs_photos.popitem(last=False)
            if photo is not None:
                fc.create_image(x0, 0, image=photo, anchor="nw")
            if is_current:
                fc.create_rectangle(x0 + 1, 1, x1 - 1, th - 1,
                                    fill="", outline=C_CURRENT, width=3)

            if p in self._picks:
                fc.create_text(x1 - 4, 3, text="◆", fill=C_PICK,
                               font=("Helvetica", 9, "bold"), anchor="ne")
            bs = composite_score(self._scores.get(p))
            if bs > 0:
                fc.create_rectangle(x0 + 2, 2, x0 + 22, 13,
                                    fill="#000000", outline="", stipple="gray50")
                fc.create_text(x0 + 3, 3, text=f"{bs:.0f}",
                               fill=_score_color(int(bs)),
                               font=("Helvetica", 7, "bold"), anchor="nw")

            self._fs_stripe_items[idx] = fc.create_rectangle(
                x0, FILMSTRIP_H - 6, x1, FILMSTRIP_H,
                fill=tag_color(tags, in_compare, default="#1a1a1a"), outline="")
            self._fs_tag_items[idx] = fc.create_text(
                x0 + cw // 2, FILMSTRIP_H - 3, text=self._tag_symbols(p),
                fill="white", font=("Helvetica", 6, "bold"), anchor="center")
            if idx != lo:
                fc.create_line(x0, 0, x0, FILMSTRIP_H, fill="#2a2d38", width=1)
            hit = fc.create_rectangle(x0, 0, x1, FILMSTRIP_H, fill="", outline="")
            fc.tag_bind(hit, "<Button-1>", lambda e, _i=idx: self._jump_to_index(_i))

    def _tag_symbols(self, path: str) -> str:
        tags = self.status.get(path, {})
        return (("⊞" if path in self._compare_set else "")
                + "".join(sym for t, (sym, _w, _c) in TAG_STYLE.items() if tags.get(t)))

    def _update_filmstrip_slot(self, file_idx: int) -> None:
        """Partial update — only tag stripe + text, no thumbnail redraw."""
        stripe = self._fs_stripe_items.get(file_idx)
        if stripe is None:
            return
        p = self.files[file_idx]
        self._filmstrip_canvas.itemconfig(
            stripe, fill=tag_color(self.status.get(p, {}), p in self._compare_set,
                                   default="#1a1a1a"))
        self._filmstrip_canvas.itemconfig(self._fs_tag_items[file_idx],
                                          text=self._tag_symbols(p))

    def _jump_to_index(self, idx: int) -> None:
        self.index = idx
        self._show_current()


    # ── Gallery / comparison ──────────────────────────────────────────────────

    def _open_gallery(self) -> None:
        """Toggle inline gallery overlay — available immediately, no waiting."""
        self._show_overlay("gallery", self._build_gallery_overlay)

    def _build_gallery_overlay(self, frame: tk.Frame) -> None:
        view = self._overlay_view = InlineGalleryView(frame, self)
        view.pack(fill="both", expand=True)
        if self._hash_done < self._hash_total_needed:
            # Show subtle banner that scoring is still in progress
            banner = tk.Frame(frame, bg="#1a3a1a", height=24)
            banner.place(relx=0, rely=1.0, relwidth=1.0, anchor="sw")
            tk.Label(banner, text="⟳  Still analysing images — sequence dividers appear when done",
                     bg="#1a3a1a", fg="#88cc88",
                     font=("Helvetica", 8)).pack(side="left", padx=8)

    def _open_comparison(self) -> None:
        """Header button — same as C key."""
        self._toggle_compare()

    # ── Zoom / pan ────────────────────────────────────────────────────────────

    def _toggle_focus_peaking(self) -> None:
        """Toggle focus peaking overlay — highlights sharpest edges in red."""
        self._focus_peaking = not self._focus_peaking
        self._set_tool_btn("_peak_btn", self._focus_peaking)
        self._render_image()

    def _apply_focus_peaking(self, img: Image.Image) -> Image.Image:
        """Overlay focus peaking edges on the image."""
        gray = np.asarray(img.convert("L"), dtype=np.float32)
        lap = np.abs(
            4 * gray[1:-1, 1:-1]
            - gray[:-2, 1:-1] - gray[2:, 1:-1]
            - gray[1:-1, :-2] - gray[1:-1, 2:])
        mask = lap > np.percentile(lap, 95)        # top 5% of edges
        out = np.asarray(img.convert("RGB")).copy()
        padded = np.zeros(out.shape[:2], dtype=bool)
        padded[1:-1, 1:-1] = mask
        if padded.any():
            out[padded, 0] = 255
            out[padded, 1] = int(out[padded, 1].mean() * 0.3)
            out[padded, 2] = int(out[padded, 2].mean() * 0.3)
        return Image.fromarray(out)

    def _request_rawclip(self, path: Optional[str], urgent: bool = False) -> None:
        """Queue reading a RAW file's highlight clipping (once it is scored and,
        with animal detection on, its animal is known)."""
        if not path or path not in self.status or os.path.splitext(path)[1].lower() not in RAW_EXTS:
            return
        sc = self._scores.get(path)
        if not sc or "raw" in sc:
            return
        if self._detect_on and not detector_status() and path not in self._detections:
            return                     # wait for the animal box (unless detection can't run)
        if path in (self._rawclip_urgent if urgent else self._rawclip_pending):
            return
        pool = self._rawclip_now if urgent else self._rawclip_bg
        if pool is None:
            return
        a = main_animal(self._detections.get(path))
        box = tuple(a[2:6]) if a and len(a) >= 6 else None
        (self._rawclip_urgent if urgent else self._rawclip_pending).add(path)
        pool.submit(self._rawclip_worker, path, box, self._session_gen, urgent)

    def _start_rawclip(self) -> None:
        if self._rawclip_bg is None:
            self._rawclip_bg = ThreadPoolExecutor(max_workers=2)
        for p in self.files[self.index:] + self.files[:self.index]:
            self._request_rawclip(p)

    def _rawclip_worker(self, path: str, box, gen: int, urgent: bool) -> None:
        rc = _SKIPPED
        if gen == self._session_gen and "raw" not in (self._scores.get(path) or {}) \
                and not (urgent and path != self._current_path):
            try:
                rc = raw_clipping(path, box)
            except Exception as e:
                log.debug("Raw clipping failed %s: %s", path, e)
                rc = None
        self._rawclip_q.put((gen, path, rc, urgent))

    def _drain_rawclip(self) -> None:
        changed = False
        while True:
            try:
                gen, path, rc, urgent = self._rawclip_q.get_nowait()
            except queue.Empty:
                break
            (self._rawclip_urgent if urgent else self._rawclip_pending).discard(path)
            sc = self._scores.get(path)
            if gen != self._session_gen or rc is _SKIPPED or not sc or "raw" in sc:
                continue
            apply_raw_clipping(sc, rc)
            self._scores_dirty = True
            changed = changed or path == self._current_path
        if changed:
            self._refresh_sidebar_data(self._current_path)

    def _toggle_clip_view(self) -> None:
        """Toggle raw highlight blinkies — where the sensor itself clipped."""
        self._clip_view = not self._clip_view
        self._set_tool_btn("_clip_btn", self._clip_view)
        path = self._current_path
        if self._clip_view and path and os.path.splitext(path)[1].lower() not in RAW_EXTS:
            self._flash_title("Raw clipping needs a RAW file")
        self._render_image()

    def _apply_clip_view(self, img: Image.Image, view: tuple) -> Image.Image:
        """Paint clipped photosites on the visible part of the image:
        red where every channel clipped, magenta where one or two did."""
        path = self._current_path
        if path not in self._clip_masks:
            self._request_clip_mask(path)
            return img
        mask = self._clip_masks[path]
        if mask is None or not mask.any():
            return img
        mh, mw = mask.shape
        r0 = min(mh - 1, int(view[1] * mh))
        c0 = min(mw - 1, int(view[0] * mw))
        r1 = max(r0 + 1, int(math.ceil(view[3] * mh)))
        c1 = max(c0 + 1, int(math.ceil(view[2] * mw)))
        m = np.asarray(Image.fromarray(mask[r0:r1, c0:c1]).resize(img.size, Image.NEAREST))
        if not m.any():
            return img
        out = np.asarray(img.convert("RGB")).copy()
        allc = CLIP_R | CLIP_G | CLIP_B
        out[(m > 0) & (m != allc)] = (255, 0, 255)
        out[m == allc] = (255, 0, 0)
        return Image.fromarray(out)

    def _request_clip_mask(self, path: Optional[str]) -> None:
        if not path or path in self._clip_mask_busy or path in self._clip_masks:
            return
        if os.path.splitext(path)[1].lower() not in RAW_EXTS:
            self._clip_masks[path] = None
            return
        self._clip_mask_busy.add(path)
        gen = self._session_gen

        def work():
            got = None
            try:
                got = raw_clip_mask(path)
            except Exception as e:
                log.debug("Clip mask failed %s: %s", path, e)
            self._clip_mask_q.put((gen, path, got[0] if got else None))
        threading.Thread(target=work, daemon=True).start()

    def _drain_clip_masks(self) -> None:
        while True:
            try:
                gen, path, mask = self._clip_mask_q.get_nowait()
            except queue.Empty:
                return
            self._clip_mask_busy.discard(path)
            if gen != self._session_gen:
                continue
            self._clip_masks[path] = mask
            while len(self._clip_masks) > 6:
                self._clip_masks.popitem(last=False)
            if self._clip_view and path == self._current_path:
                self._render_image()

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
        """Toggle loupe — shows the area under the cursor at ≥100 %."""
        self._loupe_active = not self._loupe_active
        if not self._loupe_active:
            self._canvas.delete("loupe")
            self._canvas.unbind("<Motion>")
        else:
            self._ensure_detail_now()
            self._canvas.bind("<Motion>", self._draw_loupe)
        self._set_tool_btn("_loupe_btn", self._loupe_active)

    def _draw_loupe(self, event) -> None:
        """
        Magnified view of the area under the cursor, drawn bottom-right.
        Uses the full-resolution preview: magnification is 3× the current
        view, and never less than 100 % (one image pixel per screen pixel).
        """
        if self._pil_image is None or not self._loupe_active:
            return
        self._canvas.delete("loupe")
        R  = 120                                   # loupe radius on screen
        cw = self._canvas_w or 1200
        ch = self._canvas_h or 800
        fit_src = self._pil_image
        src = (self._detail_image
               if self._detail_image is not None and self._detail_path == self._current_path
               else fit_src)
        iw, ih = src.size
        s  = min(cw / iw, ch / ih) * self._zoom   # current screen px per source px
        sx = (event.x - cw / 2 - self._pan_x) / s + iw / 2
        sy = (event.y - ch / 2 - self._pan_y) / s + ih / 2
        mag  = max(3 * s, 1.0)
        half = R / mag
        box  = (max(0, int(sx - half)), max(0, int(sy - half)),
                min(iw, int(sx + half)), min(ih, int(sy + half)))
        if box[2] <= box[0] or box[3] <= box[1]:
            return
        size = (max(1, round((box[2] - box[0]) * mag)), max(1, round((box[3] - box[1]) * mag)))
        crop = src.resize(size, Image.BILINEAR, box=box)
        self._loupe_tk_img = ImageTk.PhotoImage(crop)
        lx, ly = cw - R - 20, ch - R - 20
        self._canvas.create_image(lx, ly, image=self._loupe_tk_img,
                                  anchor="center", tags="loupe")
        self._canvas.create_oval(lx - R, ly - R, lx + R, ly + R,
                                 outline="white", width=2, tags="loupe")
        self._canvas.create_line(event.x - 10, event.y, event.x + 10, event.y,
                                 fill="white", width=1, tags="loupe")
        self._canvas.create_line(event.x, event.y - 10, event.x, event.y + 10,
                                 fill="white", width=1, tags="loupe")

    def _max_zoom(self) -> float:
        """Allow zooming to at least 4× the 1:1 level."""
        cw = self._canvas_w or 1200
        ch = self._canvas_h or 800
        iw, ih = self._full_res_size()
        return max(ZOOM_MAX, 4.0 / min(cw / iw, ch / ih))

    def _zoom_to_100(self) -> None:
        """
        Z / 1:1: toggle between fit-to-window and 100 % (one pixel of the
        full-resolution preview per screen pixel), centred on the mouse
        pointer when it is over the image.
        """
        if self._pil_image is None:
            return
        if self._zoom > 1.01:
            self._zoom = 1.0
            self._pan_x = self._pan_y = 0.0
            self._render_image()
            return
        self._ensure_detail_now()
        cw = self._canvas_w or 1200
        ch = self._canvas_h or 800
        iw, ih = self._full_res_size()
        new = 1.0 / min(cw / iw, ch / ih)
        if new <= 1.01:
            self._flash_title("This image already fits at 100 %", 2000)
            return
        # Keep the point under the mouse fixed (else zoom on the centre)
        px = self._canvas.winfo_pointerx() - self._canvas.winfo_rootx()
        py = self._canvas.winfo_pointery() - self._canvas.winfo_rooty()
        if 0 <= px < cw and 0 <= py < ch:
            mx, my = px - cw / 2, py - ch / 2
            ratio = new / self._zoom
            self._pan_x = mx + (self._pan_x - mx) * ratio
            self._pan_y = my + (self._pan_y - my) * ratio
        else:
            self._pan_x = self._pan_y = 0.0
        self._zoom = new
        self._render_image()

    def _on_scroll(self, event) -> None:
        up  = event.num == 4 or (hasattr(event, "delta") and event.delta > 0)
        fac = ZOOM_STEP if up else 1 / ZOOM_STEP
        new = max(ZOOM_MIN, min(self._max_zoom(), self._zoom * fac))
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
        self._flush_metadata()   # save edits made to the previous image
        self._refresh_sidebar_data(path)
        if not self._rename_active:
            self._load_metadata_fields(path)

    def _refresh_sidebar_data(self, path: str) -> None:
        """
        Scores, histogram, EXIF and camera bar from what is known so far.
        Missing pieces are computed on a worker thread and filled in when
        they arrive (see _drain_background).
        """
        scores = self._scores.get(path)
        for metric, row in self._score_rows.items():
            row.set_score(scores.get(metric) if scores else None)
        self._histogram.set_data(self._histograms.get(path))
        exif = self._exif_cache.get(path)
        self._draw_exif(exif or {})
        self._update_camera_bar(exif or {})
        self._update_suggestion_panel(path, scores)
        self._update_keeper_panel(path)
        if self.focus_get() not in (getattr(self, "_name_species", None),
                                    getattr(self, "_name_date", None),
                                    getattr(self, "_name_location", None),
                                    getattr(self, "_name_region", None)):
            self._update_name_panel(path)
        if (scores is None or exif is None or path not in self._histograms) \
                and path not in self._preload_inflight:
            self._request_info(path)

    def _update_suggestion_panel(self, path: str, scores: Optional[dict]) -> None:
        lines = []
        focus = self._focus.get(path)
        if path in self._focus:
            lines.append(f"Focus point: {focus[4]}" if focus else "Focus point: not recorded")
        where = {"af": "at the focus point", "animal": "on the detected animal",
                 "centre": "at the frame centre"}.get((scores or {}).get("sharp_at"))
        if where:
            lines.append(f"Sharpness measured {where}")
        raw_line = raw_clip_text((scores or {}).get("raw"))
        if raw_line:
            lines.append(raw_line)
        eye_line = self._eye_line(path)
        if eye_line:
            lines.append(eye_line)
        dets = self._detections.get(path)
        if dets is None:
            status = detector_status() if self._detect_on else "turned off in settings"
            if status:
                lines.append(f"Animal detection unavailable — {status}")
            else:
                lines.append("Animal detection: waiting…")
            colour = C_MUTED
        else:
            codes = self._suggestions(path)
            for c in codes:
                tip = "  — consider Delete" if c in ("empty", "missed") else ""
                if c == "blown":
                    tip = "  — press H to see where"
                elif c == "eye_soft":
                    tip = "  — press E to look"
                lines.append(f"⚠ {SUGGESTION_TEXT[c]}{tip}")
            if not codes:
                lines.append("✓ Animal found — no concerns")
            colour = C_WARN if codes else C_BEST
        self._suggest_lbl.configure(text="\n".join(lines), fg=colour)

    _META_KEYS = ("species", "caption", "copyright", "location")

    def _save_metadata(self, key: str) -> None:
        """Save one metadata field to the image it was loaded for."""
        path = self._meta_path
        if path is None or path not in self.status:
            return
        w = getattr(self, f"_meta_{key}", None)
        if w is None:
            return
        val = (w.get("1.0", "end-1c") if isinstance(w, tk.Text) else w.get()).strip()
        if val == self.status[path].get(key, ""):
            return
        self._set_value(path, key, val)

    def _flush_metadata(self) -> None:
        """Save any unsaved metadata edits (before navigating or closing)."""
        for key in self._META_KEYS:
            self._save_metadata(key)

    def _load_metadata_fields(self, path: str) -> None:
        """Populate metadata fields from status for the given image."""
        tags = self.status.get(path, {})
        for key in self._META_KEYS:
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
        self._meta_path = path

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
        self._set_value(path, "keywords", kws)
        self._flash_title(f"Keyword {'added' if kw in kws else 'removed'}: {kw}", 1500)

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

    def _schedule_score_save(self) -> None:
        """Save the score cache a few seconds after scoring goes quiet."""
        if self._score_save_job:
            self.after_cancel(self._score_save_job)
        self._score_save_job = self.after(5000, self._save_scores_if_dirty)

    def _save_scores_if_dirty(self) -> None:
        self._score_save_job = None
        if self._scores_dirty:
            save_score_cache(self.score_file, self._scores)
            self._scores_dirty = False
        if self._dets_dirty:
            save_detections(self.detect_file, self._detections, MODEL_ID)
            self._dets_dirty = False
        if self._emb_dirty:
            save_features(self.base_dir, self._emb)
            self._emb_dirty = False
        if self._species_dirty:
            save_predictions(self.base_dir, self._species_pred)
            self._species_dirty = False
        if self._clip_dirty:
            save_index(self.base_dir, self._clip)
            self._clip_dirty = False
        if self._eyes_dirty:
            save_eyes(self.base_dir, self._eyes)
            self._eyes_dirty = False

    def _update_counts(self) -> None:
        self._count_best.set_value(sum(v["best"]   for v in self.status.values()))
        self._count_delete.set_value(sum(v["delete"] for v in self.status.values()))
        self._count_id.set_value(sum(v["id"]     for v in self.status.values()))
        self._update_progress_info()

    def _update_progress_info(self) -> None:
        """Reviewed images / bursts, and time left at the current pace."""
        if not hasattr(self, "_progress_info"):
            return
        reviewed = sum(1 for v in self.status.values() if is_reviewed(v))
        total = self.total
        lines = [f"Reviewed {reviewed:,} / {total:,}  ({reviewed / total:.0%})"
                 if total else "No images"]
        bursts = [g for g in self._bursts if len(g) > 1]
        if bursts:
            done = sum(1 for g in bursts
                       if all(is_reviewed(self.status.get(p, {})) for p in g))
            lines.append(f"Bursts done {done} / {len(bursts)}")
        now = time.monotonic()
        recent = [t for t in self._review_times if now - t <= 900]   # last 15 min
        if len(recent) >= 5 and recent[-1] > recent[0] and reviewed < total:
            per_sec = (len(recent) - 1) / (recent[-1] - recent[0])
            lines.append(f"Pace {per_sec * 60:.0f}/min · about "
                         f"{_fmt_duration((total - reviewed) / per_sec)} left")
        self._progress_info.configure(text="\n".join(lines))

    # ── Keyboard ──────────────────────────────────────────────────────────────

    def _typing_in_field(self) -> bool:
        """True while a text field (filename, metadata, search…) has focus."""
        try:
            w = self.focus_get()
            return w is not None and w.winfo_class() in ("Entry", "Text", "TEntry", "TCombobox")
        except Exception:
            return False

    def _on_tab(self, _event=None):
        """Tab: jump to the next unreviewed image (fields keep normal Tab)."""
        if _event is not None and self._from_import_window(_event):
            return None
        if self._busy:
            return "break"
        if self._typing_in_field():
            return None
        view = self._overlay_view if self._active_overlay else None
        if view is not None and hasattr(view, "handle_tab"):
            view.handle_tab()
            return "break"
        self._jump_to_next_unreviewed()
        return "break"

    @staticmethod
    def _from_import_window(event) -> bool:
        """Keys typed in the import window must not tag photos here."""
        try:
            return isinstance(event.widget.winfo_toplevel(), ImportDialog)
        except Exception:
            return False

    def _on_key(self, event) -> None:
        if self._busy or self._typing_in_field() or self._from_import_window(event):
            return
        ks    = event.keysym
        ctrl  = bool(event.state & 0x4)
        shift = bool(event.state & 0x1)
        view  = self._overlay_view if self._active_overlay else None
        if ctrl and ks in ("z", "Z"):
            self._undo_tag()
            return
        if view is not None and hasattr(view, "handle_key"):
            # Grid views (gallery, scoring, bursts) handle arrows, tags, stars
            if view.handle_key(ks, ctrl, shift):
                return
            if ks == "Escape":
                self._hide_overlay()
            elif ks in ("g", "G") and self._active_overlay == "gallery":
                self._hide_overlay()
            elif ks in ("v", "V") and self._active_overlay == "sightings":
                self._hide_overlay()
            elif ks in ("question", "slash"):
                self._toggle_shortcuts_overlay()
            return               # don't act on the hidden viewer image
        if ctrl:
            return
        # One key, one function — no context-dependent behaviour
        if   ks in ("Left", "KP_Left", "KP_4"):           self._prev_image()
        elif ks in ("Right", "KP_Right", "KP_6"):         self._next_image()
        elif ks in ("Return", "b", "B"):                  self._mark_best()
        elif ks in ("BackSpace", "d", "D"):               self._mark_delete()
        elif ks in ("i", "I", "plus", "KP_Add", "space"): self._mark_id()
        elif ks in ("x", "X"):                            self._mark_compare()
        elif ks in ("c", "C"):                            self._toggle_compare()
        elif ks in ("1", "2", "3", "4", "5"):             self._set_star(int(ks))
        elif ks == "0":                                   self._set_star(0)
        elif ks in ("z", "Z"):                            self._zoom_to_100()
        elif ks in ("g", "G"):                            self._open_gallery()
        elif ks in ("v", "V"):                            self._open_sightings()
        elif ks in ("f", "F"):                            self._toggle_focus_peaking()
        elif ks in ("h", "H"):                            self._toggle_clip_view()
        elif ks in ("e", "E"):                            self._zoom_to_eye()
        elif ks in ("l", "L"):                            self._toggle_loupe()
        elif ks in ("p", "P"):                            self._toggle_boxes()
        elif ks in ("k", "K"):                            self._jump_to_pick()
        elif ks in ("n", "N"):                            self._confirm_suggested_species()
        elif ks in ("question", "slash"):                 self._toggle_shortcuts_overlay()
        elif ks == "Escape":
            # Escape only resets zoom — use ⛶ button for fullscreen
            if self._zoom > 1.01:
                self._zoom = 1.0; self._pan_x = 0.0; self._pan_y = 0.0
                self._render_image()
        elif ks in ("q", "Q"):                            self._quit()

    # ── Navigation ────────────────────────────────────────────────────────────

    def _prev_image(self) -> None:
        self._next_pass_image(-1)

    def _next_image(self) -> None:
        self._next_pass_image(1)

    # ── Tagging ───────────────────────────────────────────────────────────────

    def _set_fields(self, path: str, changes: dict, undo: bool = True) -> None:
        """
        Change fields of an image's session entry and persist them (status
        JSON, debounced + XMP sidecar). Every tag / rating / keyword /
        metadata edit goes through here, so all of them save, sync to XMP
        and undo as one step.
        """
        entry = self.status.setdefault(
            path, {"best": False, "delete": False, "id": False})
        was_reviewed = is_reviewed(entry)
        old: dict = {}
        for key, value in changes.items():
            prev = entry.get(key, _MISSING)
            if prev is _MISSING or prev != value:
                old[key] = prev
                entry[key] = value
        if not old:
            return
        if undo:
            if self._batch is not None:
                self._batch.append((path, old))
            else:
                self._undo_stack.append([(path, old)])
                del self._undo_stack[:-50]
        if not was_reviewed and is_reviewed(entry):
            self._review_times.append(time.monotonic())
            del self._review_times[:-500]
        if "best" in old or "delete" in old:
            self._history_dirty = True
            self._schedule_retrain()
        self._schedule_status_save()
        write_xmp_sidecar(path, entry)

    def _run_batch(self, paths: list[str], change) -> None:
        """Apply change(path) to several images as ONE undo step."""
        self._batch = []
        try:
            for p in paths:
                change(p)
        finally:
            step, self._batch = self._batch, None
        if step:
            self._undo_stack.append(step)
            del self._undo_stack[:-50]
        self._after_many(paths)

    def _after_many(self, paths: list[str]) -> None:
        self._update_counts()
        self._update_pass_display()
        index = {p: i for i, p in enumerate(self.files)}
        for p in paths:
            if p in index:
                self._update_filmstrip_slot(index[p])
        self._update_star_display()

    def _apply_tag_many(self, paths: list[str], tag: str) -> None:
        """Toggle a tag on several images: on for all, or off if all have it."""
        on = not all(self.status.get(p, {}).get(tag) for p in paths)
        self._run_batch(paths, lambda p: self._set_tag(p, tag, on))
        self._flash_title(f"{TAG_STYLE[tag][1]} {'set on' if on else 'removed from'} "
                          f"{len(paths)} image{'s' if len(paths) != 1 else ''}", 1800)

    def _apply_field_many(self, paths: list[str], key: str, value: str) -> None:
        """Set a metadata field (species, location, …) on several images."""
        self._run_batch(paths, lambda p: self._set_value(p, key, value))
        if self._current_path in paths:
            self._load_metadata_fields(self._current_path)
        self._flash_title(f"{key.capitalize()} set on {len(paths)} image(s)", 1800)

    def _apply_star_many(self, paths: list[str], n: int) -> None:
        self._run_batch(paths, lambda p: self._set_value(p, "star", n))

    def _apply_keyword_many(self, paths: list[str], kw: str) -> None:
        """Add a keyword to several images (removed if all already have it)."""
        add = not all(kw in self.status.get(p, {}).get("keywords", []) for p in paths)

        def change(p: str) -> None:
            kws = [k for k in self.status.get(p, {}).get("keywords", []) if k != kw]
            self._set_value(p, "keywords", kws + [kw] if add else kws)
        self._run_batch(paths, change)
        self._flash_title(f"Keyword '{kw}' {'added to' if add else 'removed from'} "
                          f"{len(paths)} image(s)", 1800)

    def _set_value(self, path: str, key: str, value, undo: bool = True) -> None:
        self._set_fields(path, {key: value}, undo)

    def _set_tag(self, path: str, tag: str, on: bool) -> None:
        """Set a tag. Best and Delete exclude each other; ID combines with either."""
        changes = {tag: on}
        if on and tag == "best":
            changes["delete"] = False
        elif on and tag == "delete":
            changes["best"] = False
        self._set_fields(path, changes)

    def _toggle_tag_for(self, path: str, tag: str) -> None:
        self._set_tag(path, tag, not self.status.get(path, {}).get(tag, False))

    def _toggle_tag(self, tag: str) -> None:
        self._toggle_tag_for(self.files[self.index], tag)
        self._update_tags_only()

    def _undo_tag(self) -> None:
        """Undo the last change (tag, rating, keyword or metadata field)."""
        if not self._undo_stack:
            return
        step = self._undo_stack.pop()
        paths = []
        for path, old in reversed(step):
            entry = self.status.get(path)
            if entry is None:
                continue
            for key, value in old.items():
                if value is _MISSING:
                    entry.pop(key, None)
                else:
                    entry[key] = value
            write_xmp_sidecar(path, entry)
            paths.append(path)
        self._schedule_status_save()
        self._after_many(paths)
        if self.files[self.index] in paths:
            self._update_tags_only()
            self._load_metadata_fields(self.files[self.index])
        view = self._overlay_view
        if view is not None and hasattr(view, "_after_batch"):
            view._after_batch(paths)
        if len(paths) > 1:
            self._flash_title(f"Undid change to {len(paths)} images", 1500)

    def _schedule_status_save(self) -> None:
        """Write the session file shortly after the last change."""
        if self._status_save_job:
            self.after_cancel(self._status_save_job)
        self._status_save_job = self.after(STATUS_SAVE_DELAY_MS, self._save_status_now)

    def _save_status_now(self) -> None:
        """Write the session file now (also used before switching or closing)."""
        if self._status_save_job:
            self.after_cancel(self._status_save_job)
            self._status_save_job = None
        save_status(self.status_file, self.status, backup=not self._status_backed_up)
        self._status_backed_up = True

    def _after_tag_change(self, path: str) -> None:
        """Refresh everything that displays tags after `path` changed."""
        self._update_counts()
        self._update_pass_display()
        try:
            idx = self.files.index(path)
        except ValueError:
            return
        self._update_filmstrip_slot(idx)
        if idx == self.index:
            self._update_star_display()

    def _update_tags_only(self) -> None:
        """Fast path after a tag change on the current image."""
        path = self.files[self.index]
        self._after_tag_change(path)
        self._flash_canvas(tag_color(self.status[path], default=C_BG))

    def _flash_title(self, msg: str, ms: int = 2500) -> None:
        """Show a message in the title bar, then restore the filename."""
        if self._title_job:
            self.after_cancel(self._title_job)
        self._title_label.configure(text=msg)

        def restore() -> None:
            self._title_job = None
            if self.files:
                self._title_label.configure(text=Path(self.files[self.index]).name)
        self._title_job = self.after(ms, restore)

    def _flash_canvas(self, color: str) -> None:
        """Briefly tint the canvas border to confirm a tag action."""
        self._canvas.configure(highlightthickness=4, highlightbackground=color)
        self.after(220, lambda: self._canvas.configure(highlightthickness=0))

    def _mark_best(self) -> None:
        self._toggle_tag("best")
        if self._auto_mode == 2 and self.status[self.files[self.index]].get("best"):
            self.after(120, lambda: self._next_pass_image(1))

    def _mark_delete(self) -> None:
        self._toggle_tag("delete")
        if self._auto_mode >= 1 and self.status[self.files[self.index]].get("delete"):
            self._flash_canvas(C_DELETE)
            self.after(120, lambda: self._next_pass_image(1))
    def _mark_id(self)     -> None: self._toggle_tag("id")

    def _set_star(self, n: int) -> None:
        """Set star rating 1-5 (0 clears); repeating the current rating clears it."""
        path = self.files[self.index]
        cur  = self.status[path].get("star", 0)
        self._set_value(path, "star", 0 if (n == 0 or cur == n) else n)
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
            self._flash_title(f"{n} image{'s' if n>1 else ''} in compare set  —  C to view", 2500)
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
        self._overlay_view = None
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
        self._overlay_view = None
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

    # ── Multi-pass culling ───────────────────────────────────────────────────

    def _cycle_pass(self) -> None:
        """Cycle through pass modes: All → Not-deleted → Best only → off."""
        if not self._multi_pass_on:
            self._multi_pass_on = True
            self._pass_num = 1
        else:
            self._pass_num = (self._pass_num % 3) + 1
            if self._pass_num == 1:
                self._multi_pass_on = False
        self._update_pass_display()
        # Stay on the current image if it is in this pass, else move to the
        # nearest one that is
        if not self._pass_allows(self.index):
            if not self._next_pass_image(1, quiet=True):
                self._next_pass_image(-1)

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
        """Cycle auto-advance: off → after Delete → after Best and Delete."""
        self._auto_mode = (self._auto_mode + 1) % 3
        txt, bg, fg = AUTO_ADVANCE_LABELS[self._auto_mode]
        self._adv_btn_lbl.configure(text=txt, bg=bg, fg=fg)
        self._app_settings["auto_advance"] = self._auto_mode
        save_app_settings(self._app_settings)

    def _toggle_boxes(self) -> None:
        """P: show the camera's focus point and detected animals on the image."""
        self._show_boxes = not self._show_boxes
        self._set_tool_btn("_boxes_btn", self._show_boxes)
        self._render_image()

    def _next_pass_image(self, direction: int = 1, quiet: bool = False) -> bool:
        """Move to the next/previous image allowed by the current pass."""
        idx = self.index + direction
        while 0 <= idx < self.total:
            if self._pass_allows(idx):
                self._jump_to_index(idx)
                return True
            idx += direction
        if self._multi_pass_on and not quiet:
            self._flash_title("No more images in this pass", 1500)
        return False

    def _jump_to_next_unreviewed(self) -> None:
        """Jump to the next image with no tag and no star rating (wraps)."""
        order = list(range(self.index + 1, self.total)) + list(range(0, self.index))
        for idx in order:
            if not is_reviewed(self.status.get(self.files[idx], {})):
                self._jump_to_index(idx)
                return
        self._flash_title("✓ All images have been reviewed")

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
        # Windows limits a command line to ~32k characters — split if needed
        batches: list[list[str]] = [[]]
        length = len(topaz_exe)
        for f in files:
            if batches[-1] and length + len(f) + 3 > 30000:
                batches.append([])
                length = len(topaz_exe)
            batches[-1].append(f)
            length += len(f) + 3
        if len(batches) > 1 and not mb.askyesno(
                "Send to Topaz",
                f"{len(files)} images are too many to open in one go.\n"
                f"They will be sent in {len(batches)} batches "
                f"(Topaz may open once per batch). Continue?"):
            return
        try:
            for batch in batches:
                subprocess.Popen([topaz_exe] + batch)
        except Exception as e:
            mb.showerror("Topaz Error", f"Could not launch Topaz:\n{e}")
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
            self._flash_title(msg, 3000)

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
        if self._title_job:                 # stays up while compare mode is open
            self.after_cancel(self._title_job)
            self._title_job = None
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

        border = tag_color(tags, default=C_BORDER)
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

        make_tag_buttons(btn_row, self, path, self._compare_toggle_tag).pack(
            side="left", padx=2, pady=4)

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
        self._toggle_tag_for(path, tag)
        self._after_tag_change(path)
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
        """Copy Best → Best/, convert ID → ID/ (JPEG), send Delete to the Recycle Bin."""
        self._flush_metadata()
        self._save_status_now()
        st = self.status
        conflicted = [p for p, t in st.items() if t.get("best") and t.get("delete")]
        to_best   = [p for p, t in st.items()
                     if t.get("best") and not t.get("delete") and os.path.exists(p)]
        to_id     = [p for p, t in st.items() if t.get("id") and os.path.exists(p)]
        to_delete = [p for p, t in st.items()
                     if t.get("delete") and not t.get("best") and os.path.exists(p)]

        warn = ""
        if conflicted:
            names = "\n".join(f"    {Path(p).name}" for p in conflicted[:8])
            more  = f"\n    … and {len(conflicted) - 8} more" if len(conflicted) > 8 else ""
            warn  = (f"\n\n⚠ {len(conflicted)} image(s) are tagged both Best and "
                     f"Delete and will be skipped:\n{names}{more}")
        if not any([to_best, to_id, to_delete]):
            mb.showinfo("Nothing to do", "No images are tagged for processing." + warn)
            return
        summary = (f"Ready to process:\n\n"
                   f"  Copy to Best/    : {len(to_best)} file(s)\n"
                   f"  Convert to ID/   : {len(to_id)} file(s)  (JPEG)\n"
                   f"  Send to trash    : {len(to_delete)} file(s){warn}")
        choice = self._process_dialog(summary, to_best)
        if not choice:
            return
        template = choice.get("template") if choice.get("rename") else None

        best_dir = os.path.join(self.base_dir, "Best")
        best_names = self._plan_best_names(to_best, template)
        id_dir   = os.path.join(self.base_dir, "ID")
        os.makedirs(best_dir, exist_ok=True)
        os.makedirs(id_dir,   exist_ok=True)

        # IMG_1.NEF and IMG_1.JPG both tagged ID would both become ID/IMG_1.jpg
        stems = defaultdict(int)
        for p in to_id:
            stems[Path(p).stem.lower()] += 1

        def id_name(p: str) -> str:
            q = Path(p)
            t = self.status.get(p, {})
            prefix = ""
            if t.get("id_status") == "confirmed" and t.get("species"):
                prefix = re.sub(r"\s+", "-", re.sub(r'[\\/:*?"<>|]+', "", t["species"])) + "_"
            if stems[q.stem.lower()] > 1:
                return f"{prefix}{q.stem}_{q.suffix[1:].upper()}.jpg"
            return f"{prefix}{q.stem}.jpg"

        def same_file(a: str, b: str) -> bool:
            try:
                sa, sb = os.stat(a), os.stat(b)
                return sa.st_size == sb.st_size and abs(sa.st_mtime - sb.st_mtime) < 2
            except OSError:
                return False

        report = {"copied": 0, "converted": 0, "trashed": 0, "errors": []}
        jobs   = [("best", p) for p in to_best] + [("id", p) for p in to_id]
        total  = len(jobs) + len(to_delete)
        eta    = Eta(total)
        done_q: queue.Queue = queue.Queue()

        def work() -> None:                     # thread: copies + JPEG conversion
            for kind, p in jobs:
                name = Path(p).name
                try:
                    if kind == "best":
                        stem = best_names[p]
                        dest = os.path.join(best_dir, stem + Path(p).suffix)
                        if not same_file(p, dest):
                            shutil.copy2(p, dest)
                        xmp = _xmp_path_for(p)
                        if xmp is not None and xmp.exists():
                            shutil.copy2(xmp, os.path.join(best_dir, stem + ".xmp"))
                        done_q.put(("copied", name, None))
                    else:
                        out = convert_to_jpeg(p, id_dir, id_name(p))
                        done_q.put(("converted" if out else "error", name, None))
                except Exception as e:
                    log.error("Process failed %s: %s", p, e)
                    done_q.put(("error", name, e))
            done_q.put(("end", "", None))

        self._busy = True
        self._show_loading("Processing…")
        threading.Thread(target=work, daemon=True).start()
        state = {"done": 0, "copying": True, "trash_i": 0}

        def progress(label: str) -> None:
            self._update_loading_overlay(
                f"{label}  {state['done']} / {total}",
                state["done"] / total if total else 1.0, eta.text(state["done"]))

        def poll() -> None:
            while state["copying"]:
                try:
                    kind, name, _err = done_q.get_nowait()
                except queue.Empty:
                    break
                if kind == "end":
                    state["copying"] = False
                    break
                state["done"] += 1
                if kind == "error":
                    report["errors"].append(name)
                else:
                    report[kind] += 1
            if state["copying"]:
                progress("Copying and converting…")
                self.after(100, poll)
                return
            # Recycle Bin moves run here on the main thread, a few per tick
            i = state["trash_i"]
            for p in to_delete[i:i + 5]:
                try:
                    xmp = _xmp_path_for(p)
                    send2trash(p)
                    report["trashed"] += 1
                    if xmp is not None and xmp.exists():
                        send2trash(str(xmp))     # don't leave orphaned sidecars
                except Exception as e:
                    log.error("Trash failed %s: %s", p, e)
                    report["errors"].append(Path(p).name)
                state["done"] += 1
            state["trash_i"] = i + 5
            if state["trash_i"] < len(to_delete):
                progress("Moving to Recycle Bin…")
                self.after(10, poll)
                return
            finish()

        def finish() -> None:
            # Remember the Best names so later runs reuse (not duplicate) them
            for p in to_best:
                if p in self.status and best_names[p] != Path(p).stem:
                    self.status[p]["best_name"] = best_names[p]
            self._history_dirty = True
            self._save_folder_history()
            # Trashed files leave the session; Best/ID files keep their data
            for p in to_delete:
                if not os.path.exists(p):
                    self.status.pop(p, None)
                    self._scores.pop(p, None)
                    self._histograms.pop(p, None)
            self._save_status_now()
            save_score_cache(self.score_file, self._scores)
            self._busy = False
            text = "Process complete.\n\n"
            if report["copied"]:    text += f"Copied to Best/    : {report['copied']}\n"
            if report["converted"]: text += f"Converted to ID/   : {report['converted']}\n"
            if report["trashed"]:   text += f"Sent to trash      : {report['trashed']}\n"
            if conflicted:          text += f"Skipped (Best + Delete): {len(conflicted)}\n"
            if report["errors"]:
                text += "\nErrors:\n" + "\n".join(f"  {e}" for e in report["errors"])
            mb.showinfo("Done", text)
            self._close_to_launcher()

        poll()

    # ── Fullscreen ────────────────────────────────────────────────────────────

    # ── Naming the Best copies (before Topaz / darktable) ─────────────────────

    def _name_parts(self, path: str, exif_only: bool = False) -> dict:
        """
        Species / date / location for a Best copy's name: what you confirmed,
        else the species suggestion for its burst; the date you set, else
        the capture date; the location you set, else the folder label.
        """
        t = self.status.get(path, {})
        try:
            ts = self._timestamps.get(path) or os.path.getmtime(path)
        except OSError:
            ts = time.time()
        folder_label = re.sub(r"^\d{4}-\d{2}-\d{2}\s*", "", Path(self.base_dir).name)
        top = self._species_top(path)
        date = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        return {"species": t.get("species") or (top[0][0] if top else "Unidentified"),
                "location": t.get("location") or folder_label,
                "date": date if exif_only else (t.get("name_date") or date),
                "time": datetime.datetime.fromtimestamp(ts).strftime("%H%M%S"),
                "orig": Path(path).stem}

    @staticmethod
    def _render_name(template: str, parts: dict, n: int) -> str:
        def safe(v: str) -> str:
            v = re.sub(r'[\\/:*?"<>|]+', "", str(v).strip())
            return re.sub(r"\s+", "-", v)
        name = template.format(**{k: safe(v) for k, v in parts.items()}, n=f"{n:03d}")
        name = re.sub(r"([_\-])[_\-]+", r"\1", name).strip("_-. ")
        return name or parts["orig"]

    def _plan_best_names(self, paths: list[str], template: Optional[str]) -> dict[str, str]:
        """
        File stem for each Best copy. Without a template the original name is
        kept. RAW+JPEG pairs share a name; numbers run in capture order per
        species/date/location and never reuse a name already in Best/.
        """
        if not template:
            return {p: Path(p).stem for p in paths}
        best_dir = os.path.join(self.base_dir, "Best")
        taken = ({Path(f).stem.lower() for f in os.listdir(best_dir)}
                 if os.path.isdir(best_dir) else set())
        pairs: dict[str, list[str]] = defaultdict(list)
        for p in paths:
            pairs[Path(p).stem.lower()].append(p)
        plan: dict[str, str] = {}
        groups: dict[tuple, list[list[str]]] = defaultdict(list)
        for ps in pairs.values():
            earlier = next((self.status[p].get("best_name") for p in ps
                            if self.status.get(p, {}).get("best_name")), None)
            if earlier:                          # named by an earlier Process
                for p in ps:
                    plan[p] = earlier
                taken.add(earlier.lower())
                continue
            parts = self._name_parts(ps[0])
            groups[(parts["species"], parts["date"], parts["location"])].append(ps)
        for items in groups.values():
            items.sort(key=lambda ps: (self._timestamps.get(ps[0]) or 0, ps[0]))
            n = 1
            for ps in items:
                parts = self._name_parts(ps[0])
                while True:
                    stem = self._render_name(template, parts, n)
                    if "{n}" not in template and stem.lower() in taken:
                        stem = f"{stem}_{n:03d}"
                    if stem.lower() not in taken:
                        break
                    n += 1
                taken.add(stem.lower())
                n += 1
                for p in ps:
                    plan[p] = stem
        return plan

    def _process_dialog(self, summary: str, to_best: list[str]) -> Optional[dict]:
        """Confirm Process; optionally rename the Best copies. None = cancel."""
        dlg = ctk.CTkToplevel(self)
        dlg.title("Process")
        dlg.geometry("620x520")
        dlg.configure(fg_color=C_SURFACE)
        dlg.transient(self)
        dlg.grab_set()
        result: dict = {}
        rename = tk.BooleanVar(value=bool(self._app_settings.get("best_rename", False)))
        template = tk.StringVar(value=self._app_settings.get("best_template",
                                                             DEFAULT_BEST_TEMPLATE))
        ctk.CTkLabel(dlg, text=summary, justify="left", anchor="w",
                     font=ctk.CTkFont("Courier", 11)).pack(fill="x", padx=20, pady=(16, 8))
        ctk.CTkCheckBox(dlg, text="Rename the copies in Best/ (originals keep their names)",
                        variable=rename).pack(anchor="w", padx=20, pady=(8, 4))
        ctk.CTkEntry(dlg, textvariable=template).pack(fill="x", padx=20)
        ctk.CTkLabel(dlg, text="Fields: {species} {date} {location} {n} {time} {orig}.  "
                     "Location defaults to the folder label; set species/location for "
                     "many images with the gallery selection bar.",
                     wraplength=570, justify="left", text_color=C_MUTED,
                     font=ctk.CTkFont("Helvetica", 10)).pack(fill="x", padx=20, pady=4)
        preview = ctk.CTkLabel(dlg, text="", justify="left", anchor="w",
                               font=ctk.CTkFont("Courier", 10))
        preview.pack(fill="x", padx=20, pady=6)

        def update(*_):
            if not rename.get():
                preview.configure(text="Copies keep their original names.")
                return
            try:
                plan = self._plan_best_names(to_best[:200], template.get())
            except (KeyError, ValueError, IndexError) as e:
                preview.configure(text=f"⚠ Template problem: {e}")
                return
            lines = [f"{Path(p).name}  →  {plan[p]}{Path(p).suffix}" for p in to_best[:5]]
            unconfirmed = [p for p in to_best if not self.status.get(p, {}).get("species")]
            guessed = sum(1 for p in unconfirmed if self._species_top(p))
            if guessed:
                lines.append(f"⚠ {guessed} image(s) named from an unconfirmed species suggestion")
            if len(unconfirmed) - guessed:
                lines.append(f"⚠ {len(unconfirmed) - guessed} image(s) have no species → 'Unidentified'")
            preview.configure(text="\n".join(lines) or "Nothing to copy to Best/.")
        rename.trace_add("write", update)
        template.trace_add("write", update)
        update()

        def ok():
            result.update(rename=bool(rename.get()), template=template.get())
            self._app_settings.update(best_rename=result["rename"], best_template=result["template"])
            save_app_settings(self._app_settings)
            dlg.destroy()
        row = ctk.CTkFrame(dlg, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=16, side="bottom")
        ctk.CTkButton(row, text="Process", command=ok).pack(side="right")
        ctk.CTkButton(row, text="Cancel", fg_color=C_BORDER,
                      command=dlg.destroy).pack(side="right", padx=8)
        self.wait_window(dlg)
        return result or None

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
            system = platform.system()
            if system == "Windows":
                self.state("normal")
                sw = self.winfo_screenwidth()
                sh = self.winfo_screenheight()
                w, h = int(sw * 0.85), int(sh * 0.85)
                self.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")
            else:
                self.attributes("-fullscreen", False)
        else:
            self._go_fullscreen()

    def _init_session(self, base_dir: str, files: list[str], save_first: bool = True) -> None:
        """Switch the reviewer to another folder (used by the folder tree)."""
        if save_first:                    # (already saved when renaming the folder)
            self._flush_metadata()
            self._save_status_now()
            self._save_folder_history()
            self._save_scores_if_dirty()
            if self._hash_done < self._hash_total_needed:
                self._save_hash_progress()
        if self._compare_mode:
            self._exit_compare_mode()
        self._hide_overlay()
        self._session_gen += 1            # stale hash workers/polls stop
        for attr in ("_executor", "_analysis_executor", "_species_bg", "_eye_bg", "_rawclip_bg"):
            ex = getattr(self, attr)
            if ex:
                ex.shutdown(wait=False, cancel_futures=True)
                setattr(self, attr, None)
        self._species_pending.clear()
        self._eye_pending.clear()
        self._eye_urgent.clear()
        self._rawclip_pending.clear()
        self._rawclip_urgent.clear()
        self._analysis_total = self._analysis_done = 0
        self._update_analysis_label()

        self.base_dir    = str(Path(base_dir))
        self.files       = files
        self.total       = len(files)
        self.index       = 0
        self.status_file = os.path.join(self.base_dir, STATUS_FILENAME)
        self.score_file  = os.path.join(self.base_dir, SCORE_CACHE_FILE)
        self.hash_file   = os.path.join(self.base_dir, HASH_CACHE_FILE)
        self._meta_path  = None
        self._status_backed_up = False

        self._build_status()
        self._load_caches()
        self._histograms.clear()
        self._exif_cache.clear()
        self._reset_background()
        self._undo_stack.clear()
        self._compare_set.clear()
        self._bursts     = []
        self._file_burst = {}
        self._hash_ready = False
        _save_recent_folder(self.base_dir)

        self._start_hash_workers()
        self._show_current()
        self._update_counts()
        self.title(f"📷 Photo Reviewer — {Path(self.base_dir).name}")

    def _shutdown(self) -> None:
        """Save pending edits and stop background work before closing."""
        try:
            self._flush_metadata()
            self._save_status_now()
            self._save_scores_if_dirty()
            if self._hash_done < self._hash_total_needed:
                self._save_hash_progress()
            self._save_folder_history()
        except Exception as e:
            log.error("Saving on close failed: %s", e)
        self._learn_stop.set()
        self._index_stop.set()
        self._session_gen += 1
        for ex in (self._executor, self._preload_executor, self._analysis_executor,
                   self._thumb_executor, self._detail_executor, self._species_bg,
                   self._species_now, self._eye_bg, self._eye_now,
                   self._rawclip_bg, self._rawclip_now):
            if ex:
                ex.shutdown(wait=False, cancel_futures=True)

    def _quit(self) -> None:
        if self._busy:
            return
        if not mb.askyesno("Quit", "Exit Photo Reviewer?\n(Tags are already saved.)",
                           parent=self):
            return
        self._shutdown()
        self.master.destroy()            # ends the app (launcher is the Tk root)

    def _close_to_launcher(self) -> None:
        """Close the reviewer and show the start screen again."""
        self._shutdown()
        launcher = self.master
        self.destroy()
        if hasattr(launcher, "show_again"):
            launcher.show_again()
        else:
            launcher.destroy()

    # ── Shortcuts overlay ─────────────────────────────────────────────────────

    def _toggle_shortcuts_overlay(self) -> None:
        """Show or hide the keyboard shortcuts reference overlay."""
        ov = self._shortcuts_overlay
        if ov is not None and ov.winfo_exists():
            ov.destroy()
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
        w, h = 760, 640
        ov.geometry(f"{w}x{h}+{mx + (mw-w)//2}+{my + (mh-h)//2}")

        ov.bind("<Escape>",    lambda e: ov.destroy())
        ov.bind("<question>",  lambda e: ov.destroy())
        ov.bind("<Button-1>",  lambda e: None)  # absorb clicks
        ov.focus_set()

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
            ("Z  / 1:1 button", "Toggle 100 % zoom (at the mouse)",      "Navigation"),
            ("Scroll wheel",    "Zoom in / out",                         "Navigation"),
            ("Drag",            "Pan when zoomed",                       "Navigation"),
            # Tagging
            ("B / Enter",       "Tag as Best",                           "Tagging"),
            ("D / Backspace",   "Tag as Delete (+ auto-advance if on)",  "Tagging"),
            ("I / Space / +",   "Tag as ID",                            "Tagging"),
            ("Ctrl + Z",        "Undo last change",                      "Tagging"),
            ("1 – 5",           "Set star rating",                       "Tagging"),
            ("0",               "Clear star rating",                     "Tagging"),
            # Compare
            ("X  / Compare ⊞", "Add/remove from compare set",           "Compare"),
            ("C  / Compare ⊟", "Open/close compare view (2+ images)",   "Compare"),
            # Views
            ("⚡ Clean Load",   "Score images, show low scorers",        "Views"),
            ("V  / ◎ Sightings", "Review by sighting: Tab done → next · [ ] prev/next · N species · R regroup", "Views"),
            ("⊹ Bursts",        "Step through burst groups",             "Views"),
            ("← Back button",  "Close any open view",                   "Views"),
            # Display tools
            ("F  / ⬡ Peak",     "Toggle focus peaking overlay",          "Display"),
            ("H  / Clip",       "Blown highlights from the raw data",    "Display"),
            ("E",               "100 % on the animal's eye (again: whole frame)", "Display"),
            ("L  / ⊙ Loupe",    "Toggle loupe (≥100 %, follows cursor)", "Display"),
            ("P  / AF+Animal",  "Show focus point + detected animals",    "Display"),
            ("K",               "Jump to the predicted keeper of the burst", "Navigation"),
            ("N",               "Confirm the suggested species (whole burst)", "Tagging"),
            ("🔎 ID Review",    "1–5 pick · Enter confirm · U unsure · X not identifiable", "Views"),
            ("🔍 Search",       "Find photos by description across your library", "Views"),
            ("?",               "Show / hide this shortcuts panel",      "Display"),
            ("Escape",          "Reset zoom to fit",                     "Display"),
            ("⛶ button",        "Toggle fullscreen",                     "Display"),
            # Multi-pass
            ("Pass button",     "Cycle pass: All→Survivors→Best",        "Multi-pass"),
            ("Auto→ button",    "Auto-advance: off / Delete / Best+Delete", "Multi-pass"),
            # Grid views
            ("Arrows / Enter",  "Gallery: move cursor / open image",     "Grid views"),
            ("Shift / Ctrl+click", "Select a range / toggle selection",   "Grid views"),
            ("Ctrl + A",        "Select all shown",                      "Grid views"),
            ("B D I  0–5",      "Tag / rate the selection (or cursor)",  "Grid views"),
            ("Double-click",    "Open image in viewer",                  "Grid views"),
            # Folder tree
            ("◀/▶ button",      "Collapse/expand folder tree",           "Folders"),
            ("✎ Rename / right-click", "Rename a folder (tags and caches move with it)", "Folders"),
            # Rename
            ("Name field",      "Edit filename (Enter to confirm)",      "Rename"),
            ("Escape",          "Cancel rename",                         "Rename"),
            # Send to Topaz
            ("→ Topaz",         "Send current/Best images to Topaz AI", "Export"),
            ("Q",               "Quit application",                      "App"),
        ]

        # Group by category
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
                text="⚠ File already exists", fg=C_DELETE)
            return

        # The .xmp sidecar (ratings, keywords) is renamed with the image
        old_xmp = _xmp_path_for(path)
        old_xmp = old_xmp if (old_xmp is not None and old_xmp.exists()) else None
        new_xmp = new_path.with_suffix(".xmp")
        if old_xmp is not None and new_xmp.exists():
            self._rename_status.configure(
                text=f"⚠ {new_xmp.name} already exists", fg=C_DELETE)
            return

        try:
            old_path.rename(new_path)
        except Exception as e:
            self._rename_status.configure(text=f"⚠ {e}", fg=C_DELETE)
            log.error("Rename failed: %s", e)
            return
        if old_xmp is not None:
            try:
                old_xmp.rename(new_xmp)
            except Exception as e:
                log.error("XMP rename failed %s: %s", old_xmp, e)

        new_path_str = str(new_path)

        # ── Update all internal state ─────────────────────────────────────────
        # files list
        self.files[self.index] = new_path_str

        # status dict
        if path in self.status:
            self.status[new_path_str] = self.status.pop(path)

        # caches
        if self._meta_path == path:
            self._meta_path = new_path_str
        if path in self._timestamps:
            self._timestamps[new_path_str] = self._timestamps.pop(path)
        for cache in (self._scores, self._histograms, self._exif_cache,
                      self._preload_cache, self._hashes, self._thumb_cache):
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
        self._undo_stack = [[(new_path_str if p == path else p, old) for p, old in step]
                            for step in self._undo_stack]
        for d in (self._focus, self._detections):
            if path in d:
                d[new_path_str] = d.pop(path)
        for attr in ("_current_path", "_detail_path"):
            if getattr(self, attr) == path:
                setattr(self, attr, new_path_str)

        # status file + caches (so the new name keeps its scores/hash)
        self._save_status_now()
        save_hash_cache(self.hash_file, self._hashes, self._timestamps, self._focus)
        save_score_cache(self.score_file, self._scores)
        save_detections(self.detect_file, self._detections, MODEL_ID)

        # Update UI
        self._rename_original = new_stem
        self._title_label.configure(text=new_path.name)
        self._rename_status.configure(text=f"✓ Renamed", fg=C_BEST)
        self.after(2000, lambda: self._rename_status.configure(text=""))
        if hasattr(self, "_rename_confirm_btn"):
            self._rename_confirm_btn.pack_forget()
        self._canvas.focus_set()
        log.info("Renamed %s → %s", path, new_path_str)

    # ── Workflow launchers ────────────────────────────────────────────────────

    def _open_scoring_quick(self) -> None:
        """Toggle inline scoring overlay — fast thumbnail scoring."""
        self._show_overlay("scoring_quick", self._build_scoring_overlay)

    def _build_scoring_overlay(self, frame: tk.Frame) -> None:
        view = self._overlay_view = InlineScoringView(frame, self)
        view.pack(fill="both", expand=True)

    def _open_burst_triage(self) -> None:
        """Toggle inline burst triage overlay."""
        # Toggle off if already active
        if self._active_overlay == "burst_triage":
            self._hide_overlay()
            return
        if not self._hash_ready:
            if self._hash_done < self._hash_total_needed:
                self._flash_title(
                    f"Still preparing the folder ({self._hash_done}/{self._hash_total_needed}) "
                    "— bursts are available when it finishes", 3000)
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

    def _build_burst_overlay(self, frame: tk.Frame) -> None:
        view = self._overlay_view = InlineBurstTriageView(frame, self)
        view.pack(fill="both", expand=True)


# ── Launcher window ──────────────────────────────────────────────────────────

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
        h = 470 + len(recent) * 40
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"580x{h}+{(sw-580)//2}+{(sh-h)//2}")

        # Use plain tk.Frame for logo — avoids CTkFrame internal canvas issues
        # that cause tk.Label children with images to not render correctly
        logo = tk.Frame(self, bg=C_SURFACE, height=190)
        logo.pack(fill="x")
        logo.pack_propagate(False)

        # Build icon with Pillow
        _ico = Image.new("RGBA", (80, 80), (0, 0, 0, 0))
        _d   = ImageDraw.Draw(_ico)
        _d.rounded_rectangle([0, 0, 79, 79], radius=16, fill="#0a0f0b")
        _d.rounded_rectangle([6, 14, 74, 66], radius=6,
                              outline="#2D7A4F", width=3, fill="#0d1a0e")
        for _y in (20, 30, 40, 50, 60):
            _d.rectangle([8,  _y, 14, _y+6], fill="#2D7A4F")
            _d.rectangle([66, _y, 72, _y+6], fill="#2D7A4F")
        _d.line([(22, 46), (34, 58), (58, 26)], fill="#2D7A4F", width=6)
        _d.line([(22, 46), (34, 58), (58, 26)], fill="#22C55E", width=2)
        self._splash_icon = ImageTk.PhotoImage(_ico)

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

        ctk.CTkButton(
            action,
            text="📥   Import from card…",
            font=ctk.CTkFont("Helvetica", 12),
            height=36, corner_radius=10,
            fg_color=C_BORDER, hover_color=_darken(C_ACCENT),
            command=self._import_card,
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

    def show_again(self) -> None:
        """Re-show the start screen after the reviewer closes (e.g. after Process)."""
        for w in self.winfo_children():
            w.destroy()
        self._build_splash()
        self.deiconify()
        self.lift()

    def _import_card(self) -> None:
        """Copy a memory card into dated folders; open the first one as soon as
        it has finished copying (the rest carries on in the import window)."""
        settings = load_app_settings()          # read only (older import settings)

        def done(folder: Optional[str]) -> None:
            if folder:
                self._launch_folder(folder)
        ImportDialog(self, settings, done, on_folder_ready=self._launch_folder)

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
        app.protocol("WM_DELETE_WINDOW", app._quit)

        # Force geometry flush before fullscreen — without this the
        # CTkToplevel canvas winfo_width returns 1 and images place wrong
        app.update_idletasks()
        app.lift()
        app.focus_force()
        app.update()   # process all pending events including geometry


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    if len(sys.argv) == 4 and sys.argv[1] == reviewer_accel.DEQUANTIZE_FLAG:
        # helper process: one-off graphics-chip copy of a model (reviewer_accel)
        sys.exit(reviewer_accel.main(sys.argv[1:]))
    LauncherWindow().mainloop()


if __name__ == "__main__":
    main()