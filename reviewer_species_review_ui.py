"""
Review a species list (opened from the Species list window after Identify).

Laid out like the main selector: the photo large, a filmstrip underneath, the
sidebar on the right. Flick back and forth with ← →: every photo stays in the
strip with what you decided (✓ a name, ✕ removed, ? still to decide), and any
of them can be decided again. Pick a guess (1–3), type a name, or remove it
(Del); Auto→ moves on after each decision (as in the main selector).

Nothing in the list folder changes while you review (so each decision is
instant): like the main selector's tags, decisions are resolved at the end
(✔ Resolve, or on closing), with a summary first. Naming a species that's
already in that country's list keeps the photo there and drops this one
(Shift: use this photo instead). Removing a confident
species' photo keeps the species if it was seen elsewhere in that country (the
next photo joins the strip to check). Ctrl+Z undoes. The copies in the
Species list folder follow every decision, and every decision is remembered
for the next Identify (reviewer_species_list.ListEditor).

Options: G shows the photos filed under the same guess side by side; I puts
an iNaturalist photo of the species next to yours (click it for the next one,
right-click for its iNaturalist page).
"""

from __future__ import annotations

import hashlib
import math
import os
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from tkinter import messagebox as mb, ttk
from typing import Callable, Optional

import customtkinter as ctk
from PIL import Image, ImageTk

import reviewer_species_list as sl
from reviewer_imaging import RAW_EXTS, load_preview, load_small_thumb, slow_drive
from reviewer_storage import _user_data_dir, load_app_settings, save_app_settings

# (the main selector's colours)
C_BG, C_SURFACE, C_BORDER = "#0a0a0a", "#141414", "#222222"
C_TEXT, C_MUTED, C_ACCENT = "#E8EAF0", "#6B7280", "#2D7A4F"
C_BEST, C_DELETE, C_ID = "#22C55E", "#EF4444", "#3B82F6"
C_WARN, C_CURRENT, C_FOCUS = "#F59E0B", "#E8EAF0", "#FACC15"
C_SELECT = "#A855F7"                             # (selected in a grid, as in the main selector)
PREVIEW_SIDE = 1800       # previews kept on the laptop drive (photos on D: / RAW read once)
PREVIEW_MAX = 6000        # previews kept at most (the oldest go)
CELL_W, CELL_H = 210, 192
C_BAD = C_DELETE
FILMSTRIP_H = 110
FILMSTRIP_WINDOW = 20
SIDEBAR_W = 300

SHOW = ["Everything to check", "Maybe no animal", "To clarify", "To research", "Certain (AI, not checked)",
        "Name check ⚠", "All species", "Best photo per species"]
ALL_COUNTRIES = "All countries"
SORTS = ["Date taken", "Most confident first", "Least confident first", "As found"]
NOTES = ["", "juvenile", "female", "male", "eclipse / winter plumage", "heard / call",
         "duplicate", "no animal", "too far / blurred to tell", "not wild (zoo, farm, pet)"]
DIGIT_KEYCODES = {49: 1, 50: 2, 51: 3}           # (Windows: Shift+1 is "exclam")
# remove with a reason: one key each (B is left alone: it's Best in the main selector)
REASONS = [("d", "Duplicate", "duplicate"), ("n", "No animal", "no animal"),
           ("f", "Too far / blurred", "too far / blurred to tell"), ("u", "Can't identify", "can't identify"),
           ("w", "Not wild", "not wild (zoo, farm, pet)")]
REASON_KEYS = {k: note for k, _label, note in REASONS}
KEYS_HELP = """G   gallery: select many (click, Ctrl, Shift, drag, Ctrl+A), then Del / D N F U W / Enter for all
← →   previous / next photo
Tab   next one still to decide
↑ ↓   the burst's other frames
1–3   pick that guess   (Shift: and keep this photo instead of the one in the list)
Enter   confirm the name in the box   (Shift+Enter: keep this photo)
Del / X   remove this photo
K   don't know what it is: keep it to research (the note box is your hint: "terrapin")
D N F U W   remove it, with a reason: duplicate · no animal · too far / blurred · can't identify · not wild
Ctrl+Z   undo
wheel / Z   zoom in on the photo (drag to move round it; Z again: the whole photo)
S   the photos under this guess side by side      I   compare with iNaturalist
V   open the original      O   show the original in its folder
Esc   close"""


def _darken(hex_color: str, factor: float = 0.7) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return "#{:02x}{:02x}{:02x}".format(int(r * factor), int(g * factor), int(b * factor))


class SpeciesReviewWindow(ctk.CTkToplevel):
    def __init__(self, master, editor: sl.ListEditor,
                 resolve: Callable[[str, Optional[str]], tuple],
                 names: Callable[[Optional[str]], list[str]],
                 on_close: Optional[Callable[[], None]] = None, rescreen: Optional[Callable] = None,
                 records: Optional[Callable] = None, frame_score: Optional[Callable] = None):
        super().__init__(master)
        self.title("Species list — Review")
        self.geometry("1400x860")
        self.configure(fg_color=C_BG)
        self.transient(master)
        self._ed, self._resolve, self._names_of, self._on_close = editor, resolve, names, on_close
        self._rescreen = rescreen                # (the model asked again, within your hint)
        self._records = records                  # (scientific, iso) → iNaturalist records there (offline)
        self._frame_score = frame_score          # (photo, box) → how good a frame is (Best photo)
        self._ref_choice: Optional[str] = None   # (the iNaturalist tab chosen)
        self._ref_mode = "photos"
        self._ref_tabs: list = []
        self._zoom = 1.0                         # (1: the whole photo; more: zoomed in)
        self._zc = (0.5, 0.5)
        self._zgeom = None
        self._fitgeom = None
        self._pan = None
        self._full: dict = {}                    # (full resolution, for zooming)
        self._scores: dict = {}                  # (frame key → score, for Best photo)
        self._scoring = False
        # (the Species list window's settings: saving replaces the file, so one copy)
        self._settings = getattr(master, "_settings", None)
        if not isinstance(self._settings, dict):
            self._settings = load_app_settings()
        self._show = tk.StringVar(value=SHOW[0])
        self._country = tk.StringVar(value=ALL_COUNTRIES)
        self._sort = tk.StringVar(value=self._settings.get("species_review_order", SORTS[0]))
        if self._sort.get() not in SORTS:
            self._sort.set(SORTS[0])
        self._side = tk.BooleanVar(value=bool(self._settings.get("species_review_side_by_side", False)))
        self._compare = tk.BooleanVar(value=bool(self._settings.get("species_review_inat", False)))
        self._auto = bool(self._settings.get("species_review_auto", True))
        # the photos, in order: each stays put with what you decided about it
        self._views: dict[tuple, list[dict]] = {}       # (show, country) → [{"s": sighting, "level"}]
        self._at: dict[tuple, int] = {}                  # (show, country) → where you were
        self._slots: list[dict] = []
        self._i = 0
        self._frame = 0
        self._guesses: list = []
        self._imgs: dict[str, Image.Image] = {}          # large previews (a few)
        self._thumbs: dict[str, Image.Image] = {}        # small thumbnails
        self._fs_photos: dict[tuple, ImageTk.PhotoImage] = {}
        self._pool = ThreadPoolExecutor(max_workers=2)
        self._loading: set = set()
        self._paths: dict[int, str] = {}
        self._area = (0, 0)                              # (the photo's space on screen)                 # (a photo's place → the file shown: worked out once)
        self._strip_missing = self._grid_missing = False
        # gallery (G): select many, decide them at once
        self._gallery = False
        self._sel: set[int] = set()
        self._anchor = 0
        self._g_top = 0
        self._g_photos: dict = {}
        self._press = None
        # previews prepared in the background, kept on the laptop drive
        self._closing = False
        self._warm_order: list[str] = []
        self._warmed = 0
        self._polls = 0
        self._cache_dir = os.path.join(str(_user_data_dir()), "review_previews")
        threading.Thread(target=self._warm, daemon=True).start()
        self._tk_img = self._tk_thumb = self._tk_ref = None
        self._cells: list = []
        self._tk_cells: list = []
        self._all_names: dict = {}
        self._ref = None                         # reviewer_inat.ReferencePhotos (made when first used)
        self._ref_got: dict = {}                 # scientific → result / error
        self._ref_i = 0
        self._ref_box = None
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Key>", self._key)
        self.after(20, self._maximise)
        self.after(60, self._reload)
        self.after(150, self._poll_thumbs)
        self.grab_set()

    def _maximise(self) -> None:
        """Fill the screen above the taskbar. (A window tied to its parent can't be
        maximised on Windows, so it's sized to the work area instead.)"""
        try:
            left, top, right, bottom = 0, 0, self.winfo_screenwidth(), self.winfo_screenheight() - 48
            try:
                import ctypes

                class _Rect(ctypes.Structure):
                    _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                                ("r", ctypes.c_long), ("b", ctypes.c_long)]
                r = _Rect()
                if ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0):  # work area
                    left, top, right, bottom = r.l, r.t, r.r, r.b
            except (AttributeError, OSError):
                pass
            scale = ctk.ScalingTracker.get_window_scaling(self) or 1.0
            title_bar = 32                    # (outside the size given)
            w = int((right - left - 16) / scale)
            h = int((bottom - top - title_bar - 8) / scale)
            self.geometry(f"{w}x{h}+{left}+{top}")
        except (tk.TclError, ValueError):
            pass

    # ── layout (as the main selector) ────────────────────────────────────────

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # row 0: name · where you are · what's this one · messages · Auto→ / Undo
        hdr = tk.Frame(self, bg=C_SURFACE, height=46)
        hdr.grid(row=0, column=0, columnspan=2, sticky="ew")
        hdr.grid_propagate(False)
        tk.Label(hdr, text="🦜", bg=C_SURFACE, fg=C_ACCENT, font=("Segoe UI Emoji", 13)
                 ).pack(side="left", padx=(10, 4))
        tk.Label(hdr, text="Species", bg=C_SURFACE, fg=C_ACCENT,
                 font=("Helvetica", 11, "bold")).pack(side="left", padx=(0, 2), pady=8)
        tk.Label(hdr, text="Review", bg=C_SURFACE, fg=C_MUTED,
                 font=("Helvetica", 11)).pack(side="left", padx=(0, 8), pady=8)
        tk.Frame(hdr, bg=C_BORDER, width=1).pack(side="left", fill="y", pady=8)
        self._pos = tk.Label(hdr, text="", bg=C_SURFACE, fg=C_TEXT, font=("Helvetica", 11, "bold"))
        self._place = tk.Label(hdr, text="", bg=C_SURFACE, fg=C_TEXT, font=("Helvetica", 15, "bold"))
        self._pos.pack(side="left", padx=(10, 6))
        self._place.pack(side="left", padx=(4, 12))
        self._title = tk.Label(hdr, text="", bg=C_SURFACE, fg=C_MUTED, font=("Helvetica", 10))
        self._title.pack(side="left", padx=6)
        self._resolve_btn = ctk.CTkButton(hdr, text="✔ Resolve", width=110, height=26, fg_color=C_BORDER,
                                          hover_color=_darken(C_ACCENT), text_color=C_MUTED,
                                          font=ctk.CTkFont("Helvetica", 9, "bold"), command=self._resolve_decisions)
        self._resolve_btn.pack(side="right", padx=(4, 10), pady=10)
        self._undo_btn = ctk.CTkButton(hdr, text="↶ Undo", width=70, height=26, fg_color=C_BORDER,
                                       hover_color="#333333", text_color=C_TEXT,
                                       font=ctk.CTkFont("Helvetica", 9), command=self._undo)
        self._undo_btn.pack(side="right", padx=4, pady=10)
        self._auto_btn = ctk.CTkButton(hdr, text="", width=74, height=26, command=self._toggle_auto,
                                       font=ctk.CTkFont("Helvetica", 9, "bold"))
        self._auto_btn.pack(side="right", padx=4, pady=10)
        self._paint_auto()
        self._state = tk.Label(hdr, text="", bg=C_SURFACE, fg=C_MUTED, font=("Helvetica", 9))
        self._state.pack(side="right", padx=12)

        # row 1: workflow bar
        wf = tk.Frame(self, bg="#111111", height=34)
        wf.grid(row=1, column=0, columnspan=2, sticky="ew")
        wf.grid_propagate(False)

        def wf_btn(text, cmd, width=96):
            b = ctk.CTkButton(wf, text=text, width=width, height=26, fg_color="transparent",
                              hover_color=C_BORDER, text_color=C_MUTED,
                              font=ctk.CTkFont("Helvetica", 9), command=cmd)
            b.pack(side="left", padx=2, pady=4)
            return b
        wf_btn("◀ Previous  ←", lambda: self._go(self._i - 1), 100)
        wf_btn("Next  →", lambda: self._go(self._i + 1), 70)
        wf_btn("⇥ Next to decide  Tab", self._next_undecided, 140)
        tk.Frame(wf, bg=C_BORDER, width=1).pack(side="left", fill="y", pady=6)
        wf_btn("🔁 Re-check to research", self._recheck, 150)
        self._gallery_btn = wf_btn("⊞ Gallery  G", self._toggle_gallery, 92)
        self._side_btn = wf_btn("▦ Group  S", lambda: self._flip(self._side), 80)
        self._compare_btn = wf_btn("🌐 iNaturalist  I", lambda: self._flip(self._compare), 110)
        tk.Frame(wf, bg=C_BORDER, width=1).pack(side="left", fill="y", pady=6)
        tk.Label(wf, text="Show", bg="#111111", fg=C_MUTED, font=("Helvetica", 9)).pack(side="left", padx=(8, 4))
        ttk.Combobox(wf, textvariable=self._show, values=SHOW, width=22, state="readonly"
                     ).pack(side="left", padx=2)
        tk.Label(wf, text="Country", bg="#111111", fg=C_MUTED, font=("Helvetica", 9)).pack(side="left", padx=(10, 4))
        self._countries = ttk.Combobox(wf, textvariable=self._country, width=20, state="readonly")
        self._countries.pack(side="left", padx=2)
        tk.Label(wf, text="Sort", bg="#111111", fg=C_MUTED, font=("Helvetica", 9)).pack(side="left", padx=(10, 4))
        ttk.Combobox(wf, textvariable=self._sort, values=SORTS, width=19, state="readonly").pack(side="left", padx=2)
        self._sort.trace_add("write", lambda *_a: self._resort())
        self._show.trace_add("write", lambda *_a: self._reload())
        self._country.trace_add("write", lambda *_a: self._reload())
        tk.Frame(wf, bg=C_BORDER, width=1).pack(side="left", fill="y", pady=6, padx=(8, 0))
        ctk.CTkButton(wf, text="?", width=28, height=26, fg_color="transparent", hover_color=C_BORDER,
                      text_color=C_MUTED, font=ctk.CTkFont("Helvetica", 10, "bold"),
                      command=lambda: mb.showinfo("Review: keys", KEYS_HELP, parent=self)
                      ).pack(side="left", padx=2, pady=4)
        self._paint_options()

        # row 2: the photo (and the filmstrip under it) | the sidebar
        area = tk.Frame(self, bg=C_BG)
        area.grid(row=2, column=0, sticky="nsew")
        area.grid_columnconfigure(0, weight=1)
        area.grid_rowconfigure(0, weight=1)
        self._canvas = tk.Canvas(area, bg=C_BG, highlightthickness=0)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        self._canvas.bind("<Configure>", lambda e: self._draw())
        self._canvas.bind("<Double-Button-1>", self._double)
        self._canvas.bind("<Button-1>", self._click)
        self._canvas.bind("<B1-Motion>", self._drag)
        self._canvas.bind("<ButtonRelease-1>", self._release)
        self._canvas.bind("<Button-3>", self._right_click)
        self._canvas.bind("<MouseWheel>", self._wheel)
        strip = tk.Frame(area, bg="#111111")
        strip.grid(row=1, column=0, sticky="ew")
        strip.grid_columnconfigure(0, weight=1)
        prog = tk.Frame(strip, bg="#1a1a1a", height=4)
        prog.grid(row=0, column=0, sticky="ew")
        self._prog = tk.Frame(prog, bg=C_ACCENT, height=4)
        self._prog.place(relx=0, rely=0, relheight=1, relwidth=0)
        self._strip_frame = strip
        self._strip = tk.Canvas(strip, bg="#080808", height=FILMSTRIP_H, highlightthickness=0)
        self._strip.grid(row=1, column=0, sticky="ew")
        self._strip.bind("<Configure>", lambda e: self._draw_strip())
        self._strip.bind("<MouseWheel>", lambda e: self._go(self._i + (-1 if e.delta > 0 else 1)))

        self._build_sidebar()

    def _build_sidebar(self) -> None:
        outer = tk.Frame(self, bg="#2a2d38", width=SIDEBAR_W + 1)
        outer.grid(row=2, column=1, sticky="ns")
        outer.grid_propagate(False)
        inner = tk.Frame(outer, bg=C_SURFACE, width=SIDEBAR_W)
        inner.pack(side="right", fill="both", expand=True)
        cv = tk.Canvas(inner, bg=C_SURFACE, highlightthickness=0, width=SIDEBAR_W - 16)
        bar = tk.Scrollbar(inner, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=bar.set)
        bar.grid(row=0, column=1, sticky="ns")
        cv.grid(row=0, column=0, sticky="nsew")
        inner.grid_columnconfigure(0, weight=1)
        inner.grid_rowconfigure(0, weight=1)
        sb = tk.Frame(cv, bg=C_SURFACE)
        win = cv.create_window((0, 0), window=sb, anchor="nw")
        sb.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.bind("<Configure>", lambda e: cv.itemconfig(win, width=e.width))
        cv.bind("<MouseWheel>", lambda e: cv.yview_scroll(int(-e.delta / 120), "units"))
        sb.grid_columnconfigure(0, weight=1)
        row = [0]

        def section(title: str) -> tk.Frame:
            """A collapsible section, as in the main selector's sidebar."""
            state = {"open": True}
            hdr = tk.Frame(sb, bg=C_BORDER, height=28)
            hdr.grid(row=row[0], column=0, sticky="ew", pady=(6, 0))
            hdr.grid_propagate(False)
            arrow = tk.Label(hdr, text="▾", bg=C_BORDER, fg=C_MUTED, font=("Helvetica", 9))
            arrow.pack(side="left", padx=(10, 4))
            lbl = tk.Label(hdr, text=title, bg=C_BORDER, fg=C_MUTED, font=("Helvetica", 9, "bold"))
            lbl.pack(side="left")
            body = tk.Frame(sb, bg=C_SURFACE)
            body.grid(row=row[0] + 1, column=0, sticky="ew")
            row[0] += 2

            def toggle(_e=None):
                state["open"] = not state["open"]
                (body.grid if state["open"] else body.grid_remove)()
                arrow.configure(text="▾" if state["open"] else "▸")
            for w in (hdr, arrow, lbl):
                w.bind("<Button-1>", toggle)
            return body

        body = section("THIS PHOTO")
        self._badge = tk.Label(body, text="", bg=C_SURFACE, fg=C_WARN, font=("Helvetica", 11, "bold"),
                               anchor="w", justify="left", wraplength=SIDEBAR_W - 40)
        self._badge.pack(fill="x", padx=14, pady=(8, 0))
        self._info = tk.Label(body, text="", bg=C_SURFACE, fg=C_TEXT, justify="left",
                              wraplength=SIDEBAR_W - 40, font=("Helvetica", 9), anchor="w")
        self._info.pack(fill="x", padx=14, pady=(4, 8))

        body = section("GUESSES   1–3  ·  Shift: keep this photo")
        self._sugg = tk.Frame(body, bg=C_SURFACE)
        self._sugg.pack(fill="x", padx=8, pady=(6, 6))

        body = section("SPECIES")
        tk.Label(body, text="Name (type to search: common or scientific)", bg=C_SURFACE, fg=C_MUTED,
                 font=("Helvetica", 8)).pack(anchor="w", padx=14, pady=(8, 0))
        self._species = ttk.Combobox(body, font=("Helvetica", 11))
        self._species.pack(fill="x", padx=14)
        self._species.bind("<KeyRelease>", self._search)
        self._species.bind("<<ComboboxSelected>>", lambda e: (self._show_existing(),
                                                              self._compare.get() and self._draw()))
        self._species.bind("<Return>", lambda e: (self._confirm(use_this=bool(e.state & 1)), "break")[1])
        tk.Label(body, text="Reason / note (optional)", bg=C_SURFACE, fg=C_MUTED,
                 font=("Helvetica", 8)).pack(anchor="w", padx=14, pady=(8, 0))
        self._note = ttk.Combobox(body, font=("Helvetica", 10), values=NOTES)
        self._note.pack(fill="x", padx=14)
        self._note.bind("<Return>", lambda e: (self._confirm(use_this=bool(e.state & 1)), "break")[1])
        for text, cmd, col in (("✓  Confirm   Enter", lambda: self._confirm(), C_ACCENT),
                               ("✓  Confirm, keep this photo   Shift+Enter", lambda: self._confirm(True),
                                C_BORDER),
                               ("✕  Remove   Del", self._reject, C_DELETE)):
            ctk.CTkButton(body, text=text, height=30, fg_color=col, hover_color=_darken(col),
                          text_color="white", font=ctk.CTkFont("Helvetica", 10, "bold"),
                          command=cmd).pack(fill="x", padx=14, pady=(8, 0))
        ctk.CTkButton(body, text="❔  Don't know: keep to research   K", height=28, fg_color="#4c1d95",
                      hover_color="#3b0f78", text_color="white", font=ctk.CTkFont("Helvetica", 10, "bold"),
                      command=self._dont_know).pack(fill="x", padx=14, pady=(8, 0))
        tk.Label(body, text="(the note box is your hint: terrapin, warbler, frog…)", bg=C_SURFACE, fg=C_MUTED,
                 font=("Helvetica", 8)).pack(anchor="w", padx=14)
        tk.Label(body, text="Remove, with a reason (one key)", bg=C_SURFACE, fg=C_MUTED,
                 font=("Helvetica", 8)).pack(anchor="w", padx=14, pady=(8, 2))
        grid = tk.Frame(body, bg=C_SURFACE)
        grid.pack(fill="x", padx=14)
        grid.grid_columnconfigure((0, 1), weight=1)
        for n, (key, label, note) in enumerate(REASONS):
            b = tk.Button(grid, text=f"{key.upper()}   {label}", anchor="w", bg=C_BG, fg="#fca5a5",
                          activebackground=C_BORDER, activeforeground="white", relief="flat", bd=0,
                          font=("Helvetica", 9), padx=8, pady=3,
                          command=lambda note=note: self._reject(note))
            b.grid(row=n // 2, column=n % 2, sticky="ew", padx=(0, 4) if n % 2 == 0 else 0, pady=2)
        two = tk.Frame(body, bg=C_SURFACE)
        two.pack(fill="x", padx=14, pady=(8, 10))
        for text, cmd in (("Original  V", self._open_original), ("In folder  O", self._show_folder)):
            ctk.CTkButton(two, text=text, height=26, width=120, fg_color="transparent", border_width=1,
                          border_color=C_BORDER, hover_color=C_BORDER, text_color=C_MUTED,
                          font=ctk.CTkFont("Helvetica", 9), command=cmd).pack(side="left", expand=True,
                                                                              fill="x", padx=(0, 4))

        body = section("ALREADY IN THE LIST")
        self._existing = tk.Label(body, text="", bg=C_SURFACE, fg=C_MUTED, justify="left",
                                  wraplength=SIDEBAR_W - 40, font=("Helvetica", 9), anchor="w")
        self._existing.pack(fill="x", padx=14, pady=(8, 4))
        self._thumb = tk.Label(body, bg=C_SURFACE)
        self._thumb.pack(padx=14, pady=(0, 10))

    def _paint_auto(self) -> None:
        on = self._auto
        self._auto_btn.configure(text="Auto →" if on else "Auto off",
                                 fg_color=C_ACCENT if on else C_BORDER,
                                 hover_color=_darken(C_ACCENT) if on else "#333333",
                                 text_color="white" if on else C_MUTED)

    def _paint_options(self) -> None:
        for btn, on in ((self._side_btn, self._side.get()), (self._compare_btn, self._compare.get()),
                        (self._gallery_btn, self._gallery)):
            btn.configure(text_color=C_TEXT if on else C_MUTED, fg_color=C_BORDER if on else "transparent")

    # ── the photos and what's decided about each ─────────────────────────────

    def _view(self) -> tuple:
        return self._show.get(), self._country.get()

    def _wanted(self) -> list[dict]:
        show, country = self._view()
        unsure = [(c, "photo") for c in self._ed.clarify]
        ai = [(e, "species") for e in self._ed.entries if e.how.startswith("AI")]   # (filed by the model alone)
        if show == SHOW[0]:
            # (everything not checked by you, the unsure first; kept to research: their own view)
            items = [(c, lv) for c, lv in unsure if not c.unknown] + ai
        elif show == SHOW[1]:                    # (only faintly an animal: clear them in one go)
            items = [(i, lv) for i, lv in unsure + ai if self._ed.pick(i).weak
                     and self._ed.pick(i).how == "AI"]
        elif show == SHOW[2]:
            items = [(c, lv) for c, lv in unsure if not c.unknown]
        elif show == SHOW[3]:                    # (you didn't know: re-check them, or To identify)
            items = [(c, lv) for c, lv in unsure if c.unknown]
        elif show == SHOW[4]:
            items = ai
        elif show == SHOW[5]:
            items = [(e, "species") for e in self._ed.entries if e.name_note.startswith("⚠")]
        elif show == SHOW[7]:                    # (each species' photo: you choose, best first)
            items = [(e, "best") for e in self._ed.entries]
        else:
            items = [(e, "species") for e in self._ed.entries]
        if country != ALL_COUNTRIES:
            items = [(i, lv) for i, lv in items if sl.country_name(self._ed.iso(i)) == country]
        return [dict({"s": self._ed.pick(i), "level": lv}, **({"e": i} if lv == "best" else {}))
                for i, lv in items]

    def _reload(self) -> None:
        """The current view's photos: those already here keep their places (and
        what you decided); new ones join at the end."""
        names = sorted({sl.country_name(self._ed.iso(i)) for i in self._ed.entries + self._ed.clarify})
        self._countries.configure(values=[ALL_COUNTRIES] + names)
        key = self._view()
        if self._slots is not self._views.get(key):
            self._at[self._views_key_of(self._slots)] = self._i
        slots = self._views.setdefault(key, [])
        have = {id(x["s"]) for x in slots}
        new = [x for x in self._wanted() if id(x["s"]) not in have]
        slots += self._sorted(new) if not slots else new      # (a new view in your order; later ones at the end)
        self._slots = slots
        self._go(self._at.get(key, 0))

    def _confidence(self, slot: dict) -> float:
        """How sure the AI was about this photo (its best guess, as it first said:
        deciding doesn't move a photo)."""
        s = slot["s"]
        kind, it = self._ed.locate(s)
        if kind == "clarify" and it.guesses:
            return it.guesses[0][2]
        return s.guesses[0][2] if s.guesses else s.p

    def _sorted(self, slots: list[dict]) -> list[dict]:
        mode = self._sort.get()
        if mode == SORTS[3]:
            return list(slots)
        if mode == SORTS[0]:                     # (date taken: a burst's frames and a day's photos together)
            return sorted(slots, key=lambda x: (x["s"].pick.ts or 0, x["s"].pick.path.lower()))
        return sorted(slots, key=lambda x: self._confidence(x), reverse=(mode == SORTS[1]))

    def _resort(self) -> None:
        """Sort the photos in view again (staying on the one you're on)."""
        self._settings["species_review_order"] = self._sort.get()
        save_app_settings(self._settings)
        if not self._slots:
            return
        here = self._slots[self._i]["s"]
        if self._sort.get() == SORTS[3]:              # (as found: the order Identify gave)
            order = {id(x["s"]): n for n, x in enumerate(self._wanted())}
            self._slots.sort(key=lambda x: order.get(id(x["s"]), len(order)))
        else:
            self._slots[:] = self._sorted(self._slots)
        self._fs_photos.clear()
        self._go(next((j for j, x in enumerate(self._slots) if x["s"] is here), 0))

    def _views_key_of(self, slots) -> tuple:
        return next((k for k, v in self._views.items() if v is slots), ("", ""))

    def _item(self, slot: dict):
        """What a photo is part of now: its Clarify, the species it's in, or None (removed)."""
        return self._ed.locate(slot["s"])[1]

    def _status(self, slot: dict) -> tuple[str, str, str]:
        """(symbol, colour, words) for a photo: ✓ named by you, AI (filed by the
        model), ? still to decide, ✕ removed."""
        s = slot["s"]
        if slot["level"] == "best":
            e = slot["e"]
            if e not in self._ed.entries:
                return "✕", C_DELETE, f"{e.species}: no longer in the list"
            if self._ed.data.get("best", {}).get(sl.entry_key(e)):
                return "✓", C_BEST, f"{e.species}: its best photo chosen"
            return "?", C_WARN, f"{e.species}: choose its best photo"
        kind, it = self._ed.locate(s)
        if kind == "removed":
            why = self._ed.data["decisions"].get(sl.sighting_key(s), {}).get("note", "")
            return "✕", C_DELETE, f"Removed: {why}" if why else "Removed"
        if kind == "clarify":
            if it.unknown:
                return "❔", C_SELECT, (f"To research: {s.unknown}" if s.unknown else "To research")
            return "?", C_WARN, f"To decide: {it.name}?" + ("  (no animal?)" if s.weak else "")
        mine = s.how in ("yours", "file name") or sl.sighting_key(s) in self._ed.data["decisions"]
        name = it.species
        if slot["level"] == "photo" and it.pick is not s:
            name += "  (the list keeps its other photo)"
        return ("✓", C_BEST, name) if mine else (
            "AI", C_ID, f"{name}  (AI {s.p:.0%}" + (" · no animal?)" if s.weak else ")"))

    def _go(self, i: int) -> None:
        if not self._slots:
            self._i = 0
            self._show_nothing()
            return
        self._i = max(0, min(len(self._slots) - 1, i))
        self._frame = 0
        self._ref_i = 0
        self._ref_choice = None
        self._zoom, self._zc, self._zgeom, self._pan = 1.0, (0.5, 0.5), None, None
        self._show_slot()

    def _next_undecided(self) -> None:
        n = len(self._slots)
        for k in range(1, n + 1):
            j = (self._i + k) % n
            if self._status(self._slots[j])[0] in ("?", "AI"):           # (❔: you've decided: research)
                self._go(j)
                return
        self._state.configure(text="Every photo here is decided ✓", fg=C_BEST)

    def _show_nothing(self) -> None:
        self._canvas.delete("all")
        self._canvas.create_text(max(300, self._canvas.winfo_width() // 2), 200, fill=C_MUTED,
                                 font=("Helvetica", 12), text="Nothing to review here 🎉")
        for w in self._sugg.winfo_children():
            w.destroy()
        for lbl in (self._badge, self._info, self._existing, self._pos, self._title, self._place):
            lbl.configure(text="")
        self._thumb.configure(image="")
        self._draw_strip()

    def _show_slot(self) -> None:
        slot = self._slots[self._i]
        s = slot["s"]
        it = self._item(slot)
        sym, col, words = self._status(slot)
        decided = sum(1 for x in self._slots if self._status(x)[0] in ("✓", "✕"))
        self._pos.configure(text=f"{self._i + 1} / {len(self._slots)}")
        ready = self._warmed if self._warm_order else 0
        self._place.configure(text=sl.country_name(s.iso))
        self._title.configure(text=f"{s.date} · {decided} decided, "
                                   f"{len(self._slots) - decided} to go"
                                   + (f" · previews ready {ready} / {len(self._slots)}"
                                      if self._warm_order and ready < len(self._warm_order) else ""))
        self._prog.place_configure(relwidth=decided / max(1, len(self._slots)))
        self._undo_btn.configure(state="normal" if self._ed.can_undo else "disabled")
        self._paint_resolve()
        self._badge.configure(text=f"{sym}  {words}", fg=col)
        # guesses: the model's for this photo (for a species, its name first)
        guesses = self._ed.guesses_for(s)
        if isinstance(it, sl.Clarify) and it.guesses:
            guesses = list(it.guesses)
        if slot["level"] == "species" and isinstance(it, sl.Entry):
            guesses = [(it.species, it.scientific, max(x.p for x in it.sightings))] + \
                [g for g in guesses if g[1] != it.scientific]
        self._guesses = guesses[:3]
        lines = [f"{sl.country_name(s.iso)} · {s.date}" + (f" · AI {s.p:.0%}" if s.how == "AI" else "")]
        if isinstance(it, sl.Entry) and slot["level"] == "species":
            lines.append(f"{len(it.sightings)} sighting(s) of {it.species} here · {it.how}"
                         + (f"\n{it.name_note}" if it.name_note else ""))
        if s.learned:
            lines.append(f"With your examples: {s.learned}")
        if getattr(s, "part", 0):
            lines.append("Another animal in the same photo (a mixed flock): the one outlined.")
        if s.weak and s.how == "AI":
            lines.append("No animal? The detector only saw one faintly (even looking closer): "
                         "N removes it as no animal.")
        if isinstance(it, sl.Clarify) and it.unknown:
            lines.append(f"Kept to research{f' (your hint: {s.unknown})' if s.unknown else ''}. "
                         + (f"These guesses are a re-check within '{it.rechecked or 'its type'}'. "
                            if it.rechecked is not None else "🔁 Re-check asks the AI again within your hint. ")
                         + "Still unknown when you resolve: To identify.")
        lines.append(s.pick.path)
        if it is not None and not self._ed.copy_of(s):
            lines.append("(no copy of it in the list folder yet: Copy list copies it)")
        self._info.configure(text="\n".join(lines))
        self._note.set(s.note)
        for w in self._sugg.winfo_children():
            w.destroy()
        for k, (name, sci, p) in enumerate(self._guesses, 1):
            have = self._ed.entry_for(s.iso, sci, name, besides=it if isinstance(it, sl.Entry) else None)
            b = tk.Button(self._sugg, text=f"{k}   {name}   {p:.0%}" + ("    ✓ in list" if have else ""),
                          anchor="w", bg=C_BG, fg=C_BEST if p >= 0.6 else (C_WARN if p >= 0.25 else C_TEXT),
                          activebackground=C_BORDER, relief="flat", bd=0, font=("Helvetica", 10), padx=8)
            b.configure(command=lambda k=k: self._pick(k, False))
            b.bind("<Shift-Button-1>", lambda e, k=k: (self._pick(k, True), "break")[1])
            b.pack(fill="x", pady=1)
            tk.Label(self._sugg, text=f"       {sci}", anchor="w", bg=C_SURFACE, fg=C_MUTED,
                     font=("Helvetica", 7, "italic")).pack(fill="x")
        if s.iso not in self._all_names:
            self._all_names[s.iso] = self._names_of(s.iso)
        if isinstance(it, sl.Entry) and sym == "✓":
            current = (it.species, it.scientific)                # (what you said)
        else:
            current = self._guesses[0][:2] if self._guesses else ("", "")
        self._species.set(f"{current[0]} ({current[1]})" if current[1] else current[0])
        self._species.configure(values=[f"{n} ({sc})" for n, sc, _p in self._guesses if sc])
        self._show_existing()
        self._draw()
        self._draw_strip()
        self._prefetch()

    def _search(self, e) -> None:
        if e.keysym in ("Return", "Up", "Down", "Escape", "Tab"):
            return
        text = self._species.get().strip().lower()
        if len(text) < 2 or not self._slots:
            return
        names = self._all_names.get(self._slots[self._i]["s"].iso, [])
        starts = [n for n in names if n.lower().startswith(text)]
        within = [n for n in names if text in n.lower() and n not in starts] if len(starts) < 40 else []
        self._species.configure(values=(starts + within)[:60])
        self._show_existing()

    def _show_existing(self) -> None:
        """What's already in the list under the name in the box (to compare with)."""
        if not self._slots:
            return
        slot = self._slots[self._i]
        it = self._item(slot)
        text = self._species.get().strip()
        e = None
        if text:
            name, sci, _g = self._resolve(text, slot["s"].iso)
            e = self._ed.entry_for(slot["s"].iso, sci, name)
            if e is not None and (e is it or e.pick is slot["s"]):
                e = None                                       # (this photo is the one there)
        if e is None:
            self._existing.configure(text="Not in this country's list yet: Confirm adds it." if text else "")
            self._thumb.configure(image="")
            self._tk_thumb = None
            return
        self._existing.configure(text=f"{e.species} ({e.pick.date}) is in the list.\nConfirm keeps that "
                                      "photo and removes this copy; Shift+Enter keeps this one instead.")
        img = self._thumb_of(self._ed.on_disk(e) or e.pick.pick.path, wait=True)
        if img is None:
            self._thumb.configure(image="")
            self._tk_thumb = None
            return
        img = img.copy()
        img.thumbnail((SIDEBAR_W - 40, 200))
        self._tk_thumb = ImageTk.PhotoImage(img)
        self._thumb.configure(image=self._tk_thumb)

    def _path_of(self, slot: dict) -> str:
        """The photo to show: the list's copy if this is the one it keeps, else the original."""
        got = self._paths.get(id(slot))
        if got is None:
            s = slot["s"]
            got = self._ed.copy_of(s) or sl.find_original(s.pick.path) or s.pick.path
            self._paths[id(slot)] = got
        return got

    def _frames(self, slot: dict) -> list[str]:
        s = slot["s"]
        return [self._path_of(slot)] + [p.path for p in s.photos if p is not s.pick]

    # ── images: large previews and thumbnails, read in the background ────────

    def _thumb_of(self, path: str, wait: bool = False) -> Optional[Image.Image]:
        img = self._thumbs.get(path)
        if img is None and wait:
            try:
                img = load_small_thumb(path, 400)
                self._thumbs[path] = img
            except Exception:
                return None
        elif img is None and path not in self._loading:
            self._loading.add(path)
            self._pool.submit(self._load_thumb, path)
        return img

    def _cache_file(self, path: str) -> Optional[str]:
        try:
            st = os.stat(path)
        except OSError:
            return None
        h = hashlib.sha1(f"{sl._key(path)}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()
        return os.path.join(self._cache_dir, h[:2], h + ".jpg")

    @staticmethod
    def _worth_caching(path: str) -> bool:
        return os.path.splitext(path)[1].lower() in RAW_EXTS or slow_drive(path)

    def _cached(self, path: str) -> Optional[Image.Image]:
        f = self._cache_file(path) if self._worth_caching(path) else None
        if f and os.path.exists(f):
            try:
                img = Image.open(f)
                img.load()
                return img
            except OSError:
                pass
        return None

    def _make_cached(self, path: str) -> Image.Image:
        """Read the photo's preview once (from the RAW, or the USB drive) and keep a
        screen-size JPEG of it on the laptop drive."""
        img = load_preview(path, PREVIEW_SIDE)
        img.load()
        img = img.convert("RGB")
        img.thumbnail((PREVIEW_SIDE, PREVIEW_SIDE), Image.LANCZOS)
        f = self._cache_file(path)
        if f:
            try:
                os.makedirs(os.path.dirname(f), exist_ok=True)
                img.save(f + ".tmp", "JPEG", quality=85)
                os.replace(f + ".tmp", f)
            except OSError:
                pass
        return img

    def _warm(self) -> None:
        """Background: previews for the photos ahead of you, so flicking never waits."""
        self._trim_cache()
        done: set = set()
        while not self._closing:
            todo = [p for p in self._warm_order if p not in done]
            if not todo:
                time.sleep(0.5)
                continue
            for p in todo[:4]:                   # (a few, then the order again: you may have moved on)
                if self._closing:
                    return
                try:
                    if self._worth_caching(p):
                        f = self._cache_file(p)
                        if f and not os.path.exists(f):
                            self._make_cached(p)
                except Exception:
                    pass
                done.add(p)
                self._warmed = sum(1 for x in self._warm_order if x in done)

    def _trim_cache(self) -> None:
        try:
            files = [e for d in os.scandir(self._cache_dir) if d.is_dir() for e in os.scandir(d.path)]
        except OSError:
            return
        if len(files) > PREVIEW_MAX:
            for e in sorted(files, key=lambda e: e.stat().st_mtime)[:len(files) - PREVIEW_MAX]:
                try:
                    os.remove(e.path)
                except OSError:
                    pass

    def _load_thumb(self, path: str) -> None:
        try:
            img = self._cached(path)
            if img is not None:
                img.thumbnail((400, 400))
                self._thumbs[path] = img
                return
            self._thumbs[path] = load_small_thumb(path, 400)
        except Exception:
            self._thumbs[path] = Image.new("RGB", (120, 80), "#1a1a1a")
        finally:
            self._loading.discard(path)
        if len(self._thumbs) > 400:
            self._thumbs.pop(next(iter(self._thumbs)), None)

    def _prefetch(self) -> None:
        """The next photos' large previews, ready before you get there."""
        W, H = max(400, self._canvas.winfo_width()), max(300, self._canvas.winfo_height())
        for j in (self._i + 1, self._i + 2, self._i + 3, self._i - 1):
            if 0 <= j < len(self._slots):
                path = self._path_of(self._slots[j])
                if path not in self._imgs and ("big", path) not in self._loading:
                    self._loading.add(("big", path))
                    self._pool.submit(self._load_big, path, max(W, H))

    def _load_big(self, path: str, size: int) -> None:
        try:
            img = self._cached(path)
            if img is None:
                if self._worth_caching(path):
                    img = self._make_cached(path)        # (read once: kept for next time)
                else:
                    img = load_preview(path, size)
                    img.load()
            # (made to fit the photo area here, in the background: shown with no waiting)
            W, H = self._area
            if W > 50 and H > 50 and (img.width > W or img.height > H):
                img.thumbnail((W, H), Image.LANCZOS)
            self._imgs[path] = img
            while len(self._imgs) > 40:
                self._imgs.pop(next(iter(self._imgs)), None)
        except Exception:
            pass
        finally:
            self._loading.discard(("big", path))

    def _poll_thumbs(self) -> None:
        """Redraw the strip (and the side-by-side grid) as thumbnails arrive."""
        if getattr(self, "_resolving", False):           # (not while the lists are being changed)
            self.after(300, self._poll_thumbs)
            return
        try:
            self._polls += 1
            if self._polls % 15 == 1 and self._slots:           # (the background previews follow you)
                order = self._slots[self._i:] + self._slots[:self._i]
                self._warm_order = [self._path_of(x) for x in order]
            if self._strip_missing and not self._gallery:
                self._draw_strip()
            wf = getattr(self, "_waiting_full", None)
            if wf and wf in self._full and self._zoom > 1.0:
                self._waiting_full = None
                self._draw()
            if self._grid_missing and self._slots and self._slots[self._i]["level"] == "best":
                self._grid_missing = False
                self._draw()
            if (self._grid_missing and (self._side.get() or self._gallery)) or (
                    getattr(self, "_waiting_for", None) and self._waiting_for in self._imgs):
                self._draw()
            self.after(120, self._poll_thumbs)
        except tk.TclError:
            pass

    # ── the filmstrip ─────────────────────────────────────────────────────────

    def _draw_strip(self) -> None:
        fc = self._strip
        fc.delete("all")
        self._strip_missing = False
        if not self._slots:
            return
        W = max(200, fc.winfo_width())
        th = FILMSTRIP_H - 6
        lo = max(0, self._i - FILMSTRIP_WINDOW)
        hi = min(len(self._slots) - 1, self._i + FILMSTRIP_WINDOW)
        imgs = {j: self._thumb_of(self._path_of(self._slots[j])) for j in range(lo, hi + 1)}
        widths = {j: (max(60, min(200, int(im.width / im.height * th))) if im else int(th * 1.5))
                  for j, im in imgs.items()}
        xs, x = {}, 0
        for j in range(lo, hi + 1):
            xs[j] = x
            x += widths[j] + 2
        offset = W // 2 - (xs[self._i] + widths[self._i] // 2)
        offset = max(min(offset, 0), min(0, W - (x - 2)))
        for j in range(lo, hi + 1):
            x0, cw = xs[j] + offset, widths[j]
            if x0 > W or x0 + cw < 0:
                continue
            x1, current = x0 + cw, j == self._i
            fc.create_rectangle(x0, 0, x1, FILMSTRIP_H, fill="#1e2130" if current else C_BG, outline="")
            im = imgs[j]
            if im is None:
                self._strip_missing = True
            else:
                key = (self._path_of(self._slots[j]), cw)
                photo = self._fs_photos.get(key)
                if photo is None:
                    photo = ImageTk.PhotoImage(im.resize((cw, th), Image.BILINEAR))
                    self._fs_photos[key] = photo
                    while len(self._fs_photos) > 200:
                        self._fs_photos.pop(next(iter(self._fs_photos)))
                fc.create_image(x0, 0, image=photo, anchor="nw")
            if current:
                fc.create_rectangle(x0 + 1, 1, x1 - 1, th - 1, outline=C_CURRENT, width=3)
            sym, col, _w = self._status(self._slots[j])
            fc.create_rectangle(x0, FILMSTRIP_H - 6, x1, FILMSTRIP_H,
                                fill=col if sym != "?" else "#1a1a1a", outline="")
            fc.create_rectangle(x0 + 2, 2, x0 + (26 if sym == "AI" else 18), 17, fill="#000000", outline="")
            fc.create_text(x0 + 5, 3, text=sym, fill=col, font=("Helvetica", 8, "bold"), anchor="nw")
            if j != lo:
                fc.create_line(x0, 0, x0, FILMSTRIP_H, fill="#2a2d38")
            hit = fc.create_rectangle(x0, 0, x1, FILMSTRIP_H, fill="", outline="")
            fc.tag_bind(hit, "<Button-1>", lambda e, _j=j: self._go(_j))

    # ── the photo (or side by side, and iNaturalist) ──────────────────────────

    def _flip(self, var: tk.BooleanVar) -> None:
        var.set(not var.get())
        self._option_changed()

    def _toggle_auto(self) -> None:
        self._auto = not self._auto
        self._settings["species_review_auto"] = self._auto
        save_app_settings(self._settings)
        self._paint_auto()

    def _option_changed(self) -> None:
        self._settings["species_review_side_by_side"] = self._side.get()
        self._settings["species_review_inat"] = self._compare.get()
        save_app_settings(self._settings)
        self._paint_options()
        self._draw()

    def _group(self, slot: dict) -> list[tuple[str, Optional[int]]]:
        """[(path, its place in the strip or None)] filed with this photo: the photos
        under the same best guess, or the species' other sightings."""
        s = slot["s"]
        it = self._item(slot)
        if slot["level"] == "species" and isinstance(it, sl.Entry):
            return [(self._path_of(slot), self._i)] + \
                [(x.pick.path, ("open", x)) for x in it.sightings if x is not s]   # (click: check it alone)

        def key(x):
            g = x["s"].guesses
            return x["s"].iso, (g[0][1] if g else x["s"].species)
        k = key(slot)
        return [(self._path_of(x), j) for j, x in enumerate(self._slots) if key(x) == k]

    def _draw_grid(self, x0: int, y0: int, w: int, h: int) -> None:
        import math
        group = self._group(self._slots[self._i])[:30]
        n = len(group)
        cols = max(1, math.ceil(math.sqrt(n * w / max(1, h))))
        rows = math.ceil(n / cols)
        cw, ch = w // cols, max(40, (h - 24) // rows)
        self._cells, self._tk_cells = [], []
        self._grid_missing = False
        for i, (path, j) in enumerate(group):
            cx, cy = x0 + (i % cols) * cw, y0 + 24 + (i // cols) * ch
            img = self._imgs.get(path)                    # (a large preview if there's room)
            if img is None and max(cw, ch) > 420:
                if ("big", path) not in self._loading:
                    self._loading.add(("big", path))
                    self._pool.submit(self._load_big, path, max(cw, ch))
                self._grid_missing = True
            if img is None:
                img = self._thumb_of(path)
            if img is None:
                self._grid_missing = True
            else:
                t = img.copy()
                t.thumbnail((cw - 8, ch - 14))
                tk_t = ImageTk.PhotoImage(t)
                self._tk_cells.append(tk_t)
                self._canvas.create_image(cx + cw // 2, cy + (ch - 6) // 2, image=tk_t)
            current = j == self._i
            if j is not None:
                one = self._slots[j] if isinstance(j, int) else {"s": j[1], "level": "photo"}
                sym, col, _w = self._status(one)
                self._canvas.create_rectangle(cx + 3, cy + ch - 8, cx + cw - 3, cy + ch - 3,
                                              fill=col if sym != "?" else "#1a1a1a", outline="")
                self._canvas.create_text(cx + 8, cy + 6, text=sym, fill=col, anchor="nw",
                                         font=("Helvetica", 9, "bold"))
            self._canvas.create_rectangle(cx + 2, cy + 2, cx + cw - 2, cy + ch - 2,
                                          outline=C_CURRENT if current else C_BORDER, width=3 if current else 1)
            self._cells.append((cx, cy, cx + cw, cy + ch, j))
        what = "photos under this guess" if self._slots[self._i]["level"] == "photo" else "sightings of this species"
        if n == 1:
            what = what.replace("photos", "photo").replace("sightings", "sighting")
        self._canvas.create_text(x0 + 10, y0 + 6, anchor="nw", fill=C_TEXT, font=("Helvetica", 9, "bold"),
                                 text=f"{n} {what}  (click one to check it on its own: wrong species? "
                                      "name it there · S: one at a time)")

    def _refs(self):
        if self._ref is None:
            from reviewer_inat import ReferencePhotos
            self._ref = ReferencePhotos(os.path.join(str(_user_data_dir()), "inat_photos"))
        return self._ref

    def _ref_cands(self) -> list[tuple[str, str]]:
        """The species to compare with: the name in the box, then the other suggestions."""
        s = self._slots[self._i]["s"]
        out = []
        text = self._species.get().strip()
        if text:
            name, sci, _g = self._resolve(text, s.iso)
            if sci:
                out.append((name, sci))
        for n, sc, _p in self._guesses:
            if sc and sc not in [x[1] for x in out]:
                out.append((n, sc))
        return out[:4]

    def _ref_state(self, sci: str):
        """What's in for a species: None (not downloaded), "…" (coming), a dict, or "! error"."""
        got = self._ref_got.get(sci)
        if got is None:
            got = self._refs().cached(sci)
            if got is not None and got.get("complete", True):
                self._ref_got[sci] = got
            else:
                got = None
        return got

    def _ref_get_all(self, cands: list) -> None:
        """Download the species shown and the other suggestions, all at once (only when asked)."""
        for _n, sc in cands:
            if self._ref_state(sc) is None:
                self._ref_got[sc] = "…"
                self._refs().fetch_async(sc, lambda s_, r, err: self._ref_got.__setitem__(s_, r or f"! {err}"),
                                         lambda s_, r: self._ref_got.__setitem__(s_, dict(r, partial=True)))
        self.after(300, self._ref_poll)

    def _ref_poll(self) -> None:
        try:
            busy = any(v == "…" or (isinstance(v, dict) and v.get("partial")) for v in self._ref_got.values())
            if self._compare.get() and self._slots and not self._gallery:
                self._draw()
            if busy:
                self.after(400, self._ref_poll)
        except tk.TclError:
            pass

    def _ref_facts(self, got: dict, name: str, sci: str) -> list[str]:
        info = got.get("info") or {}
        s = self._slots[self._i]["s"]
        country = sl.country_name(s.iso)
        head = f"{got.get('name') or name} ({sci})"
        kin = ", ".join(x for x in (info.get("family"), info.get("order")) if x)
        lines = [head + (f"  ·  {kin}" if kin else "")]
        if info.get("size"):
            lines.append(f"Size: {info['size']}")
        here = self._records(sci, s.iso) if self._records else None
        local = "" if here is None else (f"not recorded in {country} on iNaturalist" if here == 0 else
                                          f"{here:,} iNaturalist records in {country}"
                                          + (" (rare there)" if here < sl.RARE_RECORDS else ""))
        world = f"{info['observations']:,} worldwide" if info.get("observations") else ""
        if local or world:
            lines.append(" · ".join(x for x in (local, world) if x))
        if info.get("status"):
            lines.append(f"Conservation status: {info['status']}")
        if info.get("summary"):
            text = info["summary"]
            lines.append(text[:280] + ("…" if len(text) > 280 else ""))
        if got.get("partial"):
            lines.append("(more on its way…)")
        return lines

    def _draw_ref(self, x0: int, w: int, h: int) -> None:
        c = self._canvas
        c.create_line(x0, 0, x0, h, fill=C_BORDER, width=2)
        cands = self._ref_cands()
        self._ref_tabs = []
        if not cands:
            self._ref_box = (x0, 0, x0 + w, h, "")
            c.create_text(x0 + w // 2, h // 2, fill=C_MUTED, text="No species to compare with")
            return
        scis = [sc for _n, sc in cands]
        sci = self._ref_choice if self._ref_choice in scis else scis[0]
        name = dict((sc, n) for n, sc in cands)[sci]
        self._ref_box = (x0, 0, x0 + w, h, sci)
        # tabs: the name in the box, then the other suggestions; photos / range map on the right
        ty, mx = 6, x0 + w - 10
        for mode, label in (("map", "Range map"), ("photos", "Photos")):
            tid = c.create_text(mx, ty + 5, anchor="ne", font=("Helvetica", 9, "bold"),
                                fill="white" if self._ref_mode == mode else C_MUTED, text=label)
            bx = c.bbox(tid)
            c.create_rectangle(bx[0] - 8, ty, bx[2] + 8, ty + 24, outline="",
                               fill="#374151" if self._ref_mode == mode else "#1a1d24")
            c.tag_raise(tid)
            self._ref_tabs.append((bx[0] - 8, ty, bx[2] + 8, ty + 24, "mode", mode))
            mx = bx[0] - 16
        tx = x0 + 8
        for k, (n, sc) in enumerate(cands, 1):
            st = self._ref_state(sc)
            mark = "  ✓" if isinstance(st, dict) and st.get("photos") else ("  …" if st == "…" else "")
            tid = c.create_text(tx + 8, ty + 5, anchor="nw", font=("Helvetica", 9, "bold"),
                                fill="white" if sc == sci else C_MUTED, text=f"{k}  {n}{mark}")
            bx = c.bbox(tid)
            if bx[2] + 8 > mx and k > 1:
                c.delete(tid)
                break
            c.create_rectangle(bx[0] - 8, ty, bx[2] + 8, ty + 24, outline="",
                               fill=C_ACCENT if sc == sci else C_BORDER)
            c.tag_raise(tid)
            self._ref_tabs.append((bx[0] - 8, ty, bx[2] + 8, ty + 24, "sci", sc))
            tx = bx[2] + 16
        top, info_h = 40, 150
        got = self._ref_state(sci)
        if got is None:                          # (not downloaded: only when you ask)
            cx, cy = x0 + w // 2, h // 2
            bid = c.create_text(cx, cy, fill="white", font=("Helvetica", 11, "bold"), justify="center",
                                text=f"⬇  Get from iNaturalist: photos, size, range, records\n"
                                     f"{name} ({sci})" + (f"\nand the other {len(cands) - 1} suggestion(s)"
                                                          if len(cands) > 1 else ""))
            bx = c.bbox(bid)
            if bx:
                c.create_rectangle(bx[0] - 18, bx[1] - 12, bx[2] + 18, bx[3] + 12, fill=C_ACCENT, outline="")
                c.tag_raise(bid)
            c.create_text(cx, (bx[3] if bx else cy) + 30, fill=C_MUTED, font=("Helvetica", 8), justify="center",
                          text="Click to download (once; only scientific names are sent)")
            return
        if got == "…":
            c.create_text(x0 + w // 2, h // 2, fill=C_MUTED, text=f"Getting {name} ({sci}) from iNaturalist…")
            return
        if isinstance(got, str):
            c.create_text(x0 + w // 2, h // 2, fill=C_MUTED, width=w - 20,
                          text=f"Couldn't get {name} ({sci}) from iNaturalist\n{got[2:]}")
            return
        area_h = max(120, h - top - info_h)
        if self._ref_mode == "map":
            path = got.get("map")
            if path and os.path.exists(path):
                img = Image.open(path)
                img.load()
                img.thumbnail((w - 12, area_h))
                self._tk_ref = ImageTk.PhotoImage(img)
                c.create_image(x0 + w // 2, top + area_h // 2, image=self._tk_ref)
                c.create_text(x0 + 10, top + area_h + 2, anchor="nw", fill=C_MUTED, font=("Helvetica", 8),
                              text="Orange: where it's been recorded on iNaturalist · map © OpenStreetMap")
            else:
                c.create_text(x0 + w // 2, top + area_h // 2, fill=C_MUTED,
                              text="The map is on its way…" if got.get("partial") else "No range map")
        else:
            photos = got.get("photos") or []
            if not photos:
                c.create_text(x0 + w // 2, top + area_h // 2, fill=C_MUTED, text="iNaturalist has no photos of it")
            else:
                ph = photos[self._ref_i % len(photos)]
                try:
                    img = Image.open(ph["path"])
                    img.load()
                    img.thumbnail((w - 12, area_h))
                    self._tk_ref = ImageTk.PhotoImage(img)
                    c.create_image(x0 + w // 2, top + area_h // 2, image=self._tk_ref)
                except Exception as e:
                    c.create_text(x0 + w // 2, top + area_h // 2, fill=C_MUTED, text=f"Cannot load\n{e}")
                c.create_text(x0 + 10, top + area_h + 2, anchor="nw", fill=C_MUTED, width=w - 20,
                              font=("Helvetica", 8),
                              text=f"Photo {self._ref_i % len(photos) + 1} of {len(photos)} (click: next · "
                                   f"right-click: iNaturalist page) · {ph.get('attribution', '')}")
        c.create_text(x0 + 12, h - info_h + 22, anchor="nw", fill=C_TEXT, width=w - 24, font=("Helvetica", 9),
                      text="\n".join(self._ref_facts(got, name, sci)))

    # ── the gallery: select many, decide them at once ────────────────────────

    def _toggle_gallery(self) -> None:
        self._gallery = not self._gallery
        self._sel.clear()
        (self._strip_frame.grid_remove if self._gallery else self._strip_frame.grid)()
        self._paint_options()
        if self._slots:
            self._go(self._i)

    def _g_layout(self) -> tuple[int, int, int, int]:
        W, H = max(50, self._canvas.winfo_width()), max(50, self._canvas.winfo_height())
        cols = max(1, W // CELL_W)
        return cols, W // cols, CELL_H, max(1, (H - 30) // CELL_H)

    def _draw_gallery(self) -> None:
        self._canvas.delete("all")
        cols, cw, ch, vis = self._g_layout()
        n = len(self._slots)
        row = self._i // cols                                # (the cursor stays in sight)
        if row < self._g_top:
            self._g_top = row
        elif row >= self._g_top + vis:
            self._g_top = row - vis + 1
        self._g_top = max(0, min(self._g_top, max(0, math.ceil(n / cols) - vis)))
        self._cells, self._tk_cells = [], []
        self._grid_missing = False
        c = self._canvas
        sel = f"{len(self._sel)} selected" if self._sel else "click to select"
        c.create_text(10, 8, anchor="nw", fill=C_TEXT, font=("Helvetica", 9, "bold"),
                      text=f"Gallery · {sel} of {n}  ·  Ctrl / Shift / drag for more, Ctrl+A all  ·  "
                           "Del remove · D N F U W remove with that reason · Enter confirm each as its "
                           "best guess  ·  double-click / G / Esc: one at a time")
        first = self._g_top * cols
        for j in range(first, min(n, first + vis * cols)):
            x0, y0 = (j % cols) * cw, 30 + (j // cols - self._g_top) * ch
            slot = self._slots[j]
            path = self._path_of(slot)
            sym, col, words = self._status(slot)
            iw, ih = cw - 12, ch - 46
            photo = self._g_photos.get((path, iw, ih))
            if photo is None:
                img = self._thumb_of(path)
                if img is None:
                    self._grid_missing = True
                else:
                    t = img.copy()
                    t.thumbnail((iw, ih))
                    photo = ImageTk.PhotoImage(t)
                    self._g_photos[(path, iw, ih)] = photo
                    while len(self._g_photos) > 400:
                        self._g_photos.pop(next(iter(self._g_photos)))
            c.create_rectangle(x0 + 3, y0 + 3, x0 + cw - 3, y0 + ch - 3, fill="#111111", outline="")
            if photo is not None:
                c.create_image(x0 + cw // 2, y0 + 6 + ih // 2, image=photo)
                self._tk_cells.append(photo)
            c.create_rectangle(x0 + 6, y0 + ch - 40, x0 + cw - 6, y0 + ch - 36,
                               fill=col if sym != "?" else "#2a2a2a", outline="")
            c.create_rectangle(x0 + 7, y0 + 7, x0 + (31 if sym == "AI" else 23), y0 + 23, fill="#000000", outline="")
            c.create_text(x0 + 10, y0 + 8, text=sym, fill=col, anchor="nw", font=("Helvetica", 9, "bold"))
            label = words.split("  (")[0].replace("To decide: ", "").replace("To research", "❔ research") + ("  · no animal?" if slot["s"].weak
                                                                          and sym in ("?", "AI") else "")
            c.create_text(x0 + 8, y0 + ch - 33, anchor="nw", fill=C_TEXT if sym != "?" else C_MUTED,
                          font=("Helvetica", 8), width=cw - 14,
                          text=f"{label}\n{slot['s'].date} · {sl.country_name(slot['s'].iso)}")
            if j in self._sel:
                c.create_rectangle(x0 + 2, y0 + 2, x0 + cw - 2, y0 + ch - 2, outline=C_SELECT, width=4)
                c.create_text(x0 + cw - 10, y0 + 8, text="●", fill=C_SELECT, anchor="ne", font=("Helvetica", 12))
            if j == self._i:
                c.create_rectangle(x0 + 5, y0 + 5, x0 + cw - 5, y0 + ch - 5, outline=C_FOCUS, width=2)
            self._cells.append((x0, y0, x0 + cw, y0 + ch, j))
        rows = math.ceil(n / cols)
        if rows > vis:                                       # (where you are in the gallery)
            H = self._canvas.winfo_height()
            top = 30 + (H - 30) * self._g_top / rows
            c.create_rectangle(self._canvas.winfo_width() - 5, top, self._canvas.winfo_width() - 1,
                               top + max(20, (H - 30) * vis / rows), fill="#3a3d48", outline="")

    def _cell_at(self, x: int, y: int) -> Optional[int]:
        for x0, y0, x1, y1, j in self._cells:
            if x0 <= x < x1 and y0 <= y < y1 and j is not None:
                return j
        return None

    def _g_press(self, e) -> None:
        j = self._cell_at(e.x, e.y)
        ctrl, shift = bool(e.state & 4), bool(e.state & 1)
        self._press = (e.x, e.y, set(self._sel) if (ctrl or shift) else set())
        if j is None:
            return
        if shift:
            lo, hi = sorted((self._anchor, j))
            self._sel = (self._sel if ctrl else set()) | set(range(lo, hi + 1))
        elif ctrl:
            self._sel ^= {j}
            self._anchor = j
        else:
            self._sel = {j}
            self._anchor = j
        self._go(j)

    def _drag(self, e) -> None:
        if self._pan and not self._gallery and self._zgeom:
            x, y, (zx, zy) = self._pan
            dw, dh = self._zgeom[0], self._zgeom[1]
            self._zc = (zx - (e.x - x) / dw, zy - (e.y - y) / dh)
            self._draw()
            return
        if not self._gallery or not self._press:
            return
        x0, y0, base = self._press
        if abs(e.x - x0) + abs(e.y - y0) < 8:
            return
        lo_x, hi_x, lo_y, hi_y = min(x0, e.x), max(x0, e.x), min(y0, e.y), max(y0, e.y)
        hit = {j for cx0, cy0, cx1, cy1, j in self._cells
               if cx0 < hi_x and cx1 > lo_x and cy0 < hi_y and cy1 > lo_y}
        self._sel = base | hit
        self._draw_gallery()
        self._canvas.create_rectangle(lo_x, lo_y, hi_x, hi_y, outline=C_SELECT, dash=(4, 3), width=2)

    def _release(self, e) -> None:
        self._pan = None
        if self._gallery and self._press:
            self._press = None
            self._draw_gallery()

    def _wheel(self, e) -> None:
        if self._gallery:
            self._g_top = max(0, self._g_top + (-2 if e.delta > 0 else 2))
            cols = self._g_layout()[0]
            self._i = min(len(self._slots) - 1, max(self._i, self._g_top * cols))   # (the cursor follows)
            self._draw_gallery()
            return
        if not self._slots or self._side.get() or self._slots[self._i]["level"] == "best" \
                or e.x > self._area[0]:
            return
        self._zoom_at(e.x, e.y, self._zoom * (1.25 if e.delta > 0 else 1 / 1.25))

    def _zoom_at(self, mx: int, my: int, new: float) -> None:
        """Zoom keeping the point under the mouse where it is (as the main viewer)."""
        new = max(1.0, min(16.0, new))
        if new < 1.03:
            new = 1.0
        mine, H = self._area
        old = self._zoom
        if old > 1.0 and self._zgeom:
            dw, dh, vx0, vy0 = self._zgeom
            px, py = (vx0 + mx) / dw, (vy0 + my) / dh
            dw2, dh2 = dw * new / old, dh * new / old
        elif self._fitgeom:
            iw, ih, ox, oy = self._fitgeom
            px, py = (mx - ox) / max(1, iw), (my - oy) / max(1, ih)
            dw2, dh2 = iw * new, ih * new
        else:
            return
        px, py = min(1.0, max(0.0, px)), min(1.0, max(0.0, py))
        self._zoom = new
        self._zc = ((px * dw2 - mx + mine / 2) / dw2, (py * dh2 - my + H / 2) / dh2)
        self._draw()

    def _toggle_zoom(self) -> None:
        """Z: zoom in ×4 at the mouse, or back to the whole photo."""
        if self._zoom > 1.0:
            self._zoom = 1.0
            self._draw()
            return
        mx = self._canvas.winfo_pointerx() - self._canvas.winfo_rootx()
        my = self._canvas.winfo_pointery() - self._canvas.winfo_rooty()
        mine, H = self._area
        if not (0 <= mx < mine and 0 <= my < H):
            mx, my = mine // 2, H // 2
        self._zoom_at(mx, my, 4.0)

    def _load_full(self, path: str) -> None:
        try:
            img = load_preview(path, 100000)             # (full resolution: zoomed in)
            img.load()
            self._full = {path: img}
        except Exception:
            pass
        finally:
            self._loading.discard(("full", path))

    def _draw_zoomed(self, path: str, mine: int, H: int) -> None:
        src = self._full.get(path) or self._imgs.get(path) or self._thumbs.get(path)
        if path not in self._full and ("full", path) not in self._loading:
            self._loading.add(("full", path))
            self._pool.submit(self._load_full, path)
            self._waiting_full = path
        if src is None:
            src = load_small_thumb(path, 400)
        fit = min(mine / src.width, H / src.height)
        k = self._zoom
        dw, dh = src.width * fit * k, src.height * fit * k
        vx0 = (dw - mine) / 2 if dw <= mine else max(0.0, min(dw - mine, self._zc[0] * dw - mine / 2))
        vy0 = (dh - H) / 2 if dh <= H else max(0.0, min(dh - H, self._zc[1] * dh - H / 2))
        self._zc = ((vx0 + mine / 2) / dw, (vy0 + H / 2) / dh)
        self._zgeom = (dw, dh, vx0, vy0)
        sc = fit * k
        sx0, sy0 = max(0.0, vx0) / sc, max(0.0, vy0) / sc
        sx1, sy1 = min(dw, vx0 + mine) / sc, min(dh, vy0 + H) / sc
        crop = src.crop((int(sx0), int(sy0), int(math.ceil(sx1)), int(math.ceil(sy1))))
        crop = crop.resize((max(1, int((sx1 - sx0) * sc)), max(1, int((sy1 - sy0) * sc))), Image.BILINEAR)
        self._tk_img = ImageTk.PhotoImage(crop)
        self._canvas.create_image(max(0.0, -vx0), max(0.0, -vy0), anchor="nw", image=self._tk_img)
        sharp = "" if path in self._full else "  (sharpening…)"
        self._canvas.create_text(12, 30, anchor="nw", fill=C_FOCUS, font=("Helvetica", 9, "bold"),
                                 text=f"×{k:.1f}  wheel: zoom · drag: move · Z: whole photo{sharp}")

    # ── Best photo per species: you choose, the predicted best first ─────────

    def _best_cands(self, e) -> list[tuple]:
        """[(score or None, sighting, frame)] for a species: every sighting's frames,
        the best predicted first (scored in the background, once)."""
        out, missing = [], []
        for s in sorted(e.sightings, key=lambda x: -x.quality)[:8]:
            for ph in sorted(s.photos, key=lambda p: (p is not s.pick, abs(p.ts - s.pick.ts)))[:8]:
                sc = self._scores.get(ph.key)
                out.append((sc, s, ph))
                if sc is None:
                    missing.append((s, ph))
        if missing and self._frame_score is not None and not self._scoring:
            self._scoring = True

            def work():
                try:
                    for s, ph in missing:
                        if self._closing:
                            return
                        self._scores[ph.key] = self._frame_score(ph, s.box)
                finally:
                    self._scoring = False
            threading.Thread(target=work, daemon=True).start()
            self._grid_missing = True
        return sorted(out[:40], key=lambda x: -(x[0] if x[0] is not None else -1))

    def _draw_best(self, mine: int, H: int) -> None:
        e = self._slots[self._i]["e"]
        cands = self._best_cands(e)
        chosen = self._ed.data.get("best", {}).get(sl.entry_key(e))
        now = e.pick.shot.path
        top = max((x[0] for x in cands if x[0] is not None), default=None)
        n = len(cands)
        cols = max(1, math.ceil(math.sqrt(n * mine / max(1, H))))
        rows = math.ceil(n / cols) if n else 1
        cw, ch = mine // cols, max(60, (H - 28) // rows)
        self._cells, self._tk_cells = [], []
        c = self._canvas
        c.create_text(10, 6, anchor="nw", fill=C_TEXT, font=("Helvetica", 9, "bold"),
                      text=f"{e.species}: {n} photos, the best predicted first (★)  ·  click the one to keep "
                           "(for the list and iNaturalist) · Enter: ★ · double-click: check one on its own "
                           "(wrong species? name it there) · ← →: species")
        for k, (sc, s, ph) in enumerate(cands):
            x0, y0 = (k % cols) * cw, 28 + (k // cols) * ch
            img = self._imgs.get(ph.path) if max(cw, ch) > 420 else None
            img = img or self._thumb_of(ph.path)
            if img is None:
                self._grid_missing = True
            else:
                t = img.copy()
                t.thumbnail((cw - 8, ch - 22))
                photo = ImageTk.PhotoImage(t)
                self._tk_cells.append(photo)
                c.create_image(x0 + cw // 2, y0 + (ch - 18) // 2, image=photo)
            is_now = sl._key(ph.path) == sl._key(chosen or now)
            star = sc is not None and sc == top
            c.create_rectangle(x0 + 2, y0 + 2, x0 + cw - 2, y0 + ch - 2,
                               outline=C_BEST if is_now else (C_FOCUS if star else C_BORDER),
                               width=4 if is_now else (2 if star else 1))
            label = ("★ " if star else "") + (f"{sc:.0f}" if sc is not None else "…") + \
                ("  ✓ in the list" if is_now else "") + f"  · {s.date}"
            c.create_text(x0 + 6, y0 + ch - 16, anchor="nw", fill=C_BEST if is_now else C_TEXT,
                          font=("Helvetica", 8, "bold"), text=label)
            self._cells.append((x0, y0, x0 + cw, y0 + ch, (e, s, ph)))

    def _choose(self, e, s, ph) -> None:
        self._apply(lambda: self._ed.choose_photo(e, s, ph))

    def _choose_top(self) -> None:
        e = self._slots[self._i]["e"]
        cands = [x for x in self._best_cands(e) if x[0] is not None]
        if cands:
            _sc, s, ph = cands[0]
            self._choose(e, s, ph)

    def _batch(self, kind: str, note: str = "") -> None:
        """The selected photos (or the one under the cursor) all at once: removed (with a
        reason), or each confirmed as its best guess. One Ctrl+Z undoes the lot."""
        targets = sorted(self._sel) if self._sel else [self._i]
        done = 0
        try:
            with self._ed.batch():
                for j in targets:
                    slot = self._slots[j]
                    it = self._item(slot)
                    species = slot["level"] == "species" and isinstance(it, sl.Entry)
                    if kind == "unknown":
                        self._ed.mark_unknown(slot["s"], note)
                    elif kind == "remove":
                        if it is None:
                            continue                     # (already removed)
                        if species:
                            self._ed.reject(it, note=note)
                        else:
                            self._ed.reject_photo(slot["s"], note=note)
                    else:
                        if species:
                            self._ed.assign(it, it.species, it.scientific, it.group)
                        else:
                            g = it.guesses if isinstance(it, sl.Clarify) and it.guesses \
                                else self._ed.guesses_for(slot["s"])
                            if not g:
                                continue
                            name, sci = g[0][0], g[0][1]
                            self._ed.assign_photo(slot["s"], name, sci, sl.Identifier._group(sci))
                    done += 1
        except OSError as ex:
            self._state.configure(text=f"Couldn't change the files: {ex}", fg=C_BAD)
            return
        what = (f"Removed {done}" + (f" ({note})" if note else "")) if kind == "remove" \
            else (f"Kept {done} to research" + (f" ({note})" if note else "")) if kind == "unknown" \
            else f"Confirmed {done} as their best guess"
        self._state.configure(text=f"{what} · Ctrl+Z undoes them all", fg=C_BEST)
        self._sel.clear()
        self._paths.clear()
        self._g_photos.clear()
        have = {id(x["s"]) for x in self._slots}
        self._slots += [x for x in self._wanted() if id(x["s"]) not in have]
        self._go(self._i)

    def _gallery_key(self, e) -> Optional[str]:
        ks, shift, ctrl = e.keysym, bool(e.state & 1), bool(e.state & 4)
        cols = self._g_layout()[0]
        moves = {"Right": 1, "Left": -1, "Down": cols, "Up": -cols, "KP_6": 1, "KP_4": -1}
        if ks in moves:
            j = max(0, min(len(self._slots) - 1, self._i + moves[ks]))
            if shift:
                lo, hi = sorted((self._anchor, j))
                self._sel = set(range(lo, hi + 1))
            else:
                self._anchor = j
            self._go(j)
        elif ks in ("Next", "Prior"):
            self._go(max(0, min(len(self._slots) - 1, self._i + (1 if ks == "Next" else -1) * cols * 3)))
        elif ks in ("a", "A") and ctrl:
            self._sel = set(range(len(self._slots)))
            self._draw_gallery()
        elif ks == "space":
            self._sel ^= {self._i}
            self._anchor = self._i
            self._draw_gallery()
        elif ks == "Escape":
            if self._sel:
                self._sel.clear()
                self._draw_gallery()
            else:
                self._toggle_gallery()
        elif ks in ("g", "G"):
            self._toggle_gallery()
        elif ks in ("Delete", "BackSpace", "x", "X"):
            self._batch("remove", self._note.get())
        elif ks.lower() in REASON_KEYS and not ctrl:
            self._batch("remove", REASON_KEYS[ks.lower()])
        elif ks in ("k", "K"):
            self._batch("unknown", self._note.get())
        elif ks in ("Return", "KP_Enter"):
            self._batch("accept")
        elif ks == "Home":
            self._go(0)
        elif ks == "End":
            self._go(len(self._slots) - 1)
        else:
            return None
        return "break"

    def _click(self, e) -> None:
        if self._gallery:
            self._g_press(e)
            return
        box = self._ref_box
        if box and self._compare.get() and box[0] <= e.x <= box[2]:
            for x0, y0, x1, y1, kind, val in self._ref_tabs:
                if x0 <= e.x <= x1 and y0 <= e.y <= y1:
                    if kind == "sci":
                        self._ref_choice, self._ref_i = val, 0
                    else:
                        self._ref_mode = val
                    self._draw()
                    return
            sci = box[4]
            if sci and self._ref_state(sci) is None:
                self._ref_get_all(self._ref_cands())     # (you asked: this one and the others)
            else:
                self._ref_i += 1
            self._draw()
            return
        slot = self._slots[self._i] if self._slots else None
        if slot is not None and slot["level"] == "best":
            for x0, y0, x1, y1, j in self._cells:
                if x0 <= e.x < x1 and y0 <= e.y < y1 and isinstance(j, tuple):
                    self._choose(*j)
                    return
            return
        if self._zoom > 1.0 and not self._side.get():
            self._pan = (e.x, e.y, self._zc)             # (drag to move round the photo)
            return
        if self._side.get():
            for x0, y0, x1, y1, j in self._cells:
                if x0 <= e.x < x1 and y0 <= e.y < y1 and j is not None:
                    if isinstance(j, tuple):
                        self._open_sighting(j[1])
                    else:
                        self._go(j)
                    return

    def _open_sighting(self, s) -> None:
        """Check one sighting of a species on its own (just after this one in the strip):
        name it (it leaves this species: its real one is added, or it merges), remove
        it or keep it to research, like any other photo."""
        self._side.set(False)
        self._paint_options()
        j = next((k for k, x in enumerate(self._slots) if x["s"] is s and x["level"] == "photo"), None)
        if j is None:
            j = self._i + 1
            self._slots.insert(j, {"s": s, "level": "photo"})
        self._state.configure(text="Checking one sighting on its own: name it if it's another species "
                                   "(1–3 or type), or remove it", fg=C_WARN)
        self._go(j)

    def _double(self, e) -> None:
        if self._slots and not self._gallery and self._slots[self._i]["level"] == "best":
            for x0, y0, x1, y1, j in self._cells:
                if x0 <= e.x < x1 and y0 <= e.y < y1 and isinstance(j, tuple):
                    self._open_sighting(j[1])
                    return
            return
        if self._gallery:
            j = self._cell_at(e.x, e.y)
            self._toggle_gallery()
            if j is not None:
                self._go(j)
        else:
            self._open_original()

    def _right_click(self, e) -> None:
        box = self._ref_box
        if box and self._compare.get() and box[0] <= e.x <= box[2]:
            got = self._ref_state(box[4]) if box[4] else None
            if isinstance(got, dict) and got.get("url"):
                import webbrowser
                webbrowser.open(got["url"])

    def _draw(self) -> None:
        if getattr(self, "_resolving", False):           # (the lists are being changed: drawn after)
            return
        self._canvas.delete("all")
        self._cells, self._ref_box = [], None
        if not self._slots:
            return
        if self._gallery:
            self._draw_gallery()
            return
        W, H = max(50, self._canvas.winfo_width()), max(50, self._canvas.winfo_height())
        mine = int(W * 0.55) if self._compare.get() else W
        self._area = (mine, H)
        if self._compare.get():
            self._draw_ref(mine, W - mine, H)
        slot = self._slots[self._i]
        if slot["level"] == "best":
            self._draw_best(mine, H)
            return
        if self._side.get():
            self._draw_grid(0, 0, mine, H)
            return
        frames = self._frames(slot)
        path = frames[self._frame % len(frames)]
        if self._zoom > 1.0:
            self._draw_zoomed(path, mine, H)
            return
        try:
            img = self._imgs.get(path)
            self._waiting_for = None
            if img is None:
                # (not loaded yet: its thumbnail now, the full preview when it arrives)
                if ("big", path) not in self._loading:
                    self._loading.add(("big", path))
                    self._pool.submit(self._load_big, path, max(mine, H))
                self._waiting_for = path
                img = self._thumbs.get(path)
                if img is None:
                    img = load_small_thumb(path, 400)
                    self._thumbs[path] = img
                img = img.resize((max(1, int(img.width * min(mine / img.width, H / img.height))),
                                  max(1, int(img.height * min(mine / img.width, H / img.height)))),
                                 Image.BILINEAR)
            elif img.width > mine or img.height > H:
                img = img.copy()
                img.thumbnail((mine, H), Image.BILINEAR)
        except Exception as e:
            self._canvas.create_text(mine // 2, H // 2, text=f"Cannot load\n{path}\n{e}", fill=C_MUTED)
            return
        self._tk_img = ImageTk.PhotoImage(img)
        self._canvas.create_image(mine // 2, H // 2, image=self._tk_img)
        self._fitgeom = (img.width, img.height, (mine - img.width) / 2, (H - img.height) / 2)
        s_ = slot["s"]
        if getattr(s_, "part", 0) and s_.box and self._frame % len(frames) == 0:
            iw, ih, ox, oy = self._fitgeom                 # (a mixed flock: this one)
            b = s_.box
            self._canvas.create_rectangle(ox + b[0] * iw - 4, oy + b[1] * ih - 4, ox + b[2] * iw + 4,
                                          oy + b[3] * ih + 4, outline="#22D3EE", width=3)
            self._canvas.create_text(ox + b[0] * iw, oy + b[1] * ih - 8, anchor="sw", fill="#22D3EE",
                                     font=("Helvetica", 9, "bold"), text="this one")
        sym, col, words = self._status(slot)
        # (as the main viewer: the file at the top left, the decision as a badge)
        self._canvas.create_text(12, 10, anchor="nw", fill=C_TEXT, font=("Helvetica", 9, "bold"),
                                 text=os.path.basename(path) + (
                                     f"   frame {self._frame % len(frames) + 1} of {len(frames)}  (↑ ↓)"
                                     if len(frames) > 1 else ""))
        badge = f" {sym}  {words.split('  (')[0]} "
        tid = self._canvas.create_text(mine - 14, 12, anchor="ne", fill="white",
                                       font=("Helvetica", 11, "bold"), text=badge)
        bx = self._canvas.bbox(tid)
        if bx:
            self._canvas.create_rectangle(bx[0] - 6, bx[1] - 3, bx[2] + 6, bx[3] + 3,
                                          fill=col if sym != "?" else "#3a2a05", outline="")
            self._canvas.tag_raise(tid)
        if self._compare.get():
            self._canvas.create_text(mine // 2, H - 8, anchor="s", fill=C_MUTED, font=("Helvetica", 8),
                                     text="Yours")
        legend = ("1–3 guess · Enter confirm · Del remove · "
                  + " · ".join(f"{k.upper()} {label.lower()}" for k, label, _n in REASONS))
        lid = self._canvas.create_text(12, H - 10, anchor="sw", fill="#d1d5db", font=("Helvetica", 9),
                                       text=legend)
        lb = self._canvas.bbox(lid)
        if lb:
            self._canvas.create_rectangle(lb[0] - 6, lb[1] - 3, lb[2] + 6, lb[3] + 3, fill="#000000",
                                          outline="")
            self._canvas.tag_raise(lid)

    # ── decisions ─────────────────────────────────────────────────────────────

    def _pick(self, k: int, use_this: bool) -> None:
        if self._slots and k <= len(self._guesses):
            name, sci, _p = self._guesses[k - 1]
            self._decide(name, sci, sl.Identifier._group(sci), use_this)

    def _confirm(self, use_this: bool = False) -> None:
        if not self._slots:
            return
        text = self._species.get().strip()
        if not text:
            self._state.configure(text="Type or pick a species first", fg=C_WARN)
            return
        name, sci, group = self._resolve(text, self._slots[self._i]["s"].iso)
        self._decide(name, sci, group, use_this,
                     "" if sci else " (not a species the model knows: kept as typed)")

    def _decide(self, name: str, sci: str, group, use_this: bool, extra: str = "") -> None:
        slot = self._slots[self._i]
        it = self._item(slot)
        note = self._note.get()
        if slot["level"] == "species" and isinstance(it, sl.Entry):
            # (a species: renamed, every sighting of it in that country)
            self._apply(lambda: self._ed.assign(it, name, sci, group, use_this, note=note), extra)
        else:
            self._apply(lambda: self._ed.assign_photo(slot["s"], name, sci, group, use_this, note=note), extra)

    def _reject(self, reason: Optional[str] = None) -> None:
        if not self._slots:
            return
        slot = self._slots[self._i]
        it = self._item(slot)
        note = reason if reason is not None else self._note.get()
        if slot["level"] == "species" and isinstance(it, sl.Entry):
            def go():
                msg = self._ed.reject(it, note=note)
                if it in self._ed.entries:               # (seen elsewhere: its next photo, to check)
                    self._slots.insert(self._i + 1, {"s": it.pick, "level": "species"})
                return msg
            self._apply(go)
        else:
            self._apply(lambda: self._ed.reject_photo(slot["s"], note=note))

    def _dont_know(self) -> None:
        if not self._slots:
            return
        if self._gallery:
            self._batch("unknown", self._note.get())
            return
        slot = self._slots[self._i]
        hint = self._note.get().strip()
        self._apply(lambda: self._ed.mark_unknown(slot["s"], hint))

    def _recheck(self) -> None:
        """Ask the AI again about the photos kept to research, only among the species
        that fit each one's hint (in the background: photos may need reading)."""
        if self._rescreen is None:
            return
        todo = [c for c in self._ed.clarify if c.unknown]
        if not todo:
            self._state.configure(text="Nothing kept to research: K keeps a photo you don't know", fg=C_MUTED)
            return
        self._state.configure(text=f"Re-checking {len(todo)} photo(s) within your hints…", fg=C_WARN)
        results: dict = {}

        def work():
            for c in todo:
                try:
                    results[id(c)] = (c, self._rescreen(c.sighting, c.sighting.unknown or ""))
                except Exception:
                    results[id(c)] = (c, [])
            results["done"] = True

        def wait():
            if "done" not in results:
                self._state.configure(text=f"Re-checking {len(results)} / {len(todo)} within your hints…")
                self.after(300, wait)
                return
            found = 0
            for key, val in results.items():
                if key == "done":
                    continue
                c, guesses = val
                if guesses:
                    self._ed.set_guesses(c, guesses, c.sighting.unknown or "")
                    found += 1
            self._state.configure(text=f"Re-checked {found} of {len(todo)}: new guesses within each hint "
                                       "(1–3 picks one; still unknown at Resolve → To identify)", fg=C_BEST)
            self._paths.clear()
            self._go(self._i)
        threading.Thread(target=work, daemon=True).start()
        self.after(300, wait)

    def _apply(self, fn, extra: str = "") -> None:
        try:
            msg = fn()
        except OSError as e:
            self._state.configure(text=f"Couldn't change the files: {e}", fg=C_BAD)
            return
        saver = getattr(self._ed, "_saver", None)
        if saver is not None and saver.error:
            self._state.configure(text=f"{msg} · couldn't save the lists: {saver.error}", fg=C_BAD)
        else:
            self._state.configure(text=msg + extra, fg=C_BEST)
        # (previews and thumbnails stay: the next photo is already loaded)
        self._paths.clear()
        self._canvas.focus_set()               # (so 1–3 / Del work again after typing)
        # new photos to decide (a species' other sightings) join the strip
        have = {id(x["s"]) for x in self._slots}
        self._slots += [x for x in self._wanted() if id(x["s"]) not in have]
        self._go(self._i + 1 if self._auto else self._i)

    def _paint_resolve(self) -> None:
        n = self._ed.decided_count()
        self._resolve_btn.configure(text=f"✔ Resolve ({n})" if n else "✔ Resolve",
                                    fg_color=C_ACCENT if n else C_BORDER,
                                    text_color="white" if n else C_MUTED)

    def _summary_text(self, got: dict) -> str:
        lines = [f"You decided {got['decided']} photo{'s' if got['decided'] != 1 else ''} this time:"]
        if got["named"]:
            lines.append(f"  ✓ {got['named']} named"
                         + (f" ({got['new_species']} new species in the list)" if got["new_species"] else ""))
        if got.get("research"):
            lines.append(f"  ❔ {got['research']} kept to research (still unknown: they go to To identify)")
        if got["removed"]:
            lines.append(f"  ✕ {got['removed']} removed: "
                         + ", ".join(f"{n} {why}" for why, n in got["reasons"].items()))
        lines.append("")
        lines.append("In the Species list folder:")
        lines.append(f"  {got['moves']} copies move to their new places")
        lines.append(f"  {got['drops']} copies go to the Recycle Bin (copies only: your originals stay)")
        if got["copy_in"]:
            lines.append(f"  {got['copy_in']} species get their next photo copied in")
        if got["to_copy"]:
            lines.append(f"  {got['to_copy']} photos have no copy yet: Copy list copies them")
        return "\n".join(lines)

    def _resolve_decisions(self, then_close: bool = False) -> bool:
        """Show what will change, then change the files (as the main selector's Process).
        The files are changed in the background, with progress: the window stays
        responsive, and a problem is shown (and can be closed) rather than freezing it."""
        self._ed.flush()
        got = self._ed.summary()
        if not got["pending"] and not got["decided"]:
            if not then_close:
                self._state.configure(text="Nothing to resolve", fg=C_MUTED)
            return True
        dlg = ctk.CTkToplevel(self)
        dlg.title("Resolve")
        dlg.configure(fg_color=C_SURFACE)
        dlg.transient(self)
        dlg.resizable(False, False)
        ctk.CTkLabel(dlg, text="Resolve your decisions", font=ctk.CTkFont("Helvetica", 14, "bold"),
                     text_color=C_TEXT).pack(anchor="w", padx=20, pady=(16, 6))
        ctk.CTkLabel(dlg, text=self._summary_text(got), justify="left", anchor="w", text_color=C_TEXT,
                     font=ctk.CTkFont("Helvetica", 11)).pack(anchor="w", padx=20)
        clear = tk.BooleanVar(value=bool(self._settings.get("species_review_clear_decided", True)))
        if not then_close:
            ctk.CTkCheckBox(dlg, text="Clear what you've decided from the strip (the rest stays to go through)",
                            variable=clear, font=ctk.CTkFont("Helvetica", 10)).pack(anchor="w", padx=20, pady=(10, 0))
        ctk.CTkLabel(dlg, text="Photos you haven't decided stay as they are, for next time.", text_color=C_MUTED,
                     font=ctk.CTkFont("Helvetica", 10)).pack(anchor="w", padx=20, pady=(6, 0))
        prog = ctk.CTkLabel(dlg, text="", text_color=C_MUTED, font=ctk.CTkFont("Helvetica", 10))
        prog.pack(anchor="w", padx=20, pady=(8, 0))
        row = ctk.CTkFrame(dlg, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=16)
        answer = {"ok": False}
        state = {"running": False, "said": "", "result": None}

        def buttons(on: bool) -> None:
            for w in row.winfo_children():
                w.configure(state="normal" if on else "disabled")

        def close_dialog() -> None:
            if not state["running"]:                     # (not while the files are being changed)
                dlg.destroy()

        def work() -> None:
            try:
                out = self._ed.apply(progress=lambda m: state.__setitem__("said", m), book=True)
                state["result"] = ("done", out)
            except Exception as e:                       # (any problem: shown, and the window can be left)
                state["result"] = ("error", f"{e.__class__.__name__}: {e}")

        def poll() -> None:
            try:
                if state["result"] is None:
                    prog.configure(text=state["said"] or "Resolving…")
                    dlg.after(150, poll)
                    return
            except tk.TclError:
                return
            state["running"] = False
            self._resolving = False
            kind, out = state["result"]
            if kind == "error":
                prog.configure(text=f"Couldn't finish: {out}  (your decisions are kept: try again, or close)",
                               text_color=C_BAD)
                buttons(True)
                return
            answer["ok"] = True
            finish(out)
            dlg.destroy()

        def go() -> None:
            if state["running"]:
                return
            state.update(running=True, result=None, said="Resolving…")
            self._resolving = True                       # (no redrawing while the lists change)
            self._i_before = self._i
            buttons(False)
            threading.Thread(target=work, daemon=True).start()
            dlg.after(150, poll)

        def finish(out: dict) -> None:
            msg = f"Resolved: {out['moved']} moved, {out['held']} removed"
            if out["copied"]:
                msg += f", {out['copied']} copied in"
            if out["errors"]:
                msg += f" · {len(out['errors'])} problem(s): {out['errors'][0]}"
            self._state.configure(text=msg, fg=C_BAD if out["errors"] else C_BEST)
            self._ed._decided_before = dict(self._ed.data["decisions"])     # (a fresh count from here)
            # removed photos are gone now; with "clear", everything decided leaves the strip too
            # (named ✓, kept to research ❔): what's left is what still needs deciding
            self._settings["species_review_clear_decided"] = clear.get()
            save_app_settings(self._settings)
            if then_close:
                return
            gone = ("✕", "✓", "❔") if clear.get() else ("✕",)
            here = self._slots[self._i]["s"] if self._slots else None
            for key, slots in self._views.items():
                keep_research = key[0] == "To research"
                slots[:] = [x for x in slots if self._status(x)[0] not in gone
                            or (keep_research and self._status(x)[0] == "❔")]
            self._i = next((j for j, x in enumerate(self._slots) if x["s"] is here), None)
            if self._i is None:                  # (the photo you were on went: the next one left)
                self._i = min(self._i_before, max(0, len(self._slots) - 1))
            self._i = max(0, self._i)
            self._sel.clear()
            self._paths.clear()
            self._g_photos.clear()

        label = "Resolve and close" if then_close else "Resolve now"
        ctk.CTkButton(row, text=label, fg_color=C_ACCENT, hover_color=_darken(C_ACCENT), width=150,
                      command=go).pack(side="right")
        ctk.CTkButton(row, text="Keep reviewing", fg_color=C_BORDER, hover_color="#333333", width=130,
                      command=close_dialog).pack(side="right", padx=8)
        dlg.protocol("WM_DELETE_WINDOW", close_dialog)
        dlg.bind("<Return>", lambda e: go())
        dlg.bind("<Escape>", lambda e: close_dialog())
        dlg.grab_set()
        dlg.focus_force()
        self.wait_window(dlg)
        self._resolving = False
        try:
            if not then_close:
                self.grab_set()
                if self._slots:
                    self._go(self._i)
        except tk.TclError:
            pass
        return answer["ok"]

    def _undo(self) -> None:
        try:
            msg = self._ed.undo()
        except OSError as e:
            msg = f"Couldn't undo: {e}"
        # (undo brings back copies of the photos: the strip follows them)
        now = {}
        for s in (self._ed.all + [x for e in self._ed.entries for x in e.sightings]
                  + [c.sighting for c in self._ed.clarify]):
            now.setdefault(sl.sighting_key(s), s)
        for slots in self._views.values():
            for x in slots:
                x["s"] = now.get(sl.sighting_key(x["s"]), x["s"])
        self._state.configure(text=msg, fg=C_WARN)
        self._imgs.clear()
        self._fs_photos.clear()
        self._paths.clear()
        self._go(self._i)

    # ── the original ──────────────────────────────────────────────────────────

    def _original(self) -> Optional[str]:
        """The original of the frame shown (on the drive's current letter)."""
        if not self._slots:
            return None
        slot = self._slots[self._i]
        frames = [slot["s"].pick.path] + self._frames(slot)[1:]
        return sl.find_original(frames[self._frame % len(frames)])

    def _open_original(self) -> None:
        path = self._original()
        if not path:
            self._state.configure(text="The original isn't there (is the drive plugged in?)", fg=C_WARN)
            return
        try:
            os.startfile(path)                     # (Windows)
        except (OSError, AttributeError):
            pass

    def _show_folder(self) -> None:
        path = self._original()
        if not path or not sl.show_in_folder(path):
            self._state.configure(text="The original isn't there (is the drive plugged in?)", fg=C_WARN)

    # ── keys ──────────────────────────────────────────────────────────────────

    def _key(self, e) -> Optional[str]:
        try:
            typing = self.focus_get() in (self._species, self._note)
        except (KeyError, tk.TclError):          # (Tk: the drop-down list has the focus)
            typing = True
        ks = e.keysym
        if ks == "Escape" and typing:
            self._canvas.focus_set()               # (first Escape: the keys come back, as in the main selector)
            return "break"
        if self._gallery and not typing and self._slots and not (ks in ("z", "Z") and e.state & 4):
            return self._gallery_key(e)
        if ks == "Escape":
            self._close()
            return "break"
        if ks in ("z", "Z") and e.state & 4:          # Ctrl+Z (even while typing)
            self._undo()
            return "break"
        if typing or not self._slots:
            return None
        shift = bool(e.state & 1)
        if ks in ("Right", "Next", "KP_6"):
            self._go(self._i + 1)
        elif ks in ("Left", "Prior", "KP_4"):
            self._go(self._i - 1)
        elif ks == "Home":
            self._go(0)
        elif ks == "End":
            self._go(len(self._slots) - 1)
        elif ks == "Tab":
            self._next_undecided()
        elif ks in ("Down", "Up"):
            self._frame += 1 if ks == "Down" else -1
            self._draw()
        elif ks in ("Return", "KP_Enter") and self._slots[self._i]["level"] == "best":
            self._choose_top()
        elif ks in ("Return", "KP_Enter"):
            self._confirm(use_this=shift)
        elif ks in ("z", "Z") and not e.state & 4:
            self._toggle_zoom()
        elif shift and e.keycode in DIGIT_KEYCODES:
            self._pick(DIGIT_KEYCODES[e.keycode], True)
        elif ks in ("1", "2", "3"):
            self._pick(int(ks), False)
        elif ks in ("Delete", "BackSpace", "x", "X"):
            self._reject()
        elif ks.lower() in REASON_KEYS and not e.state & 4:
            self._reject(REASON_KEYS[ks.lower()])
        elif ks in ("k", "K"):
            self._dont_know()
        elif ks in ("v", "V"):
            self._open_original()
        elif ks in ("o", "O"):
            self._show_folder()
        elif ks in ("g", "G"):
            self._toggle_gallery()
        elif ks in ("s", "S"):
            self._flip(self._side)
        elif ks in ("i", "I"):
            self._flip(self._compare)
        else:
            return None
        return "break"

    def _close(self) -> None:
        if getattr(self, "_resolving", False):
            return                             # (the files are being changed: a moment)
        if (self._ed.decided_count() or self._ed.summary()["pending"]) and                 not self._resolve_decisions(then_close=True):
            return                             # (kept reviewing)
        ed = self._ed

        def tidy():                            # (removed copies → Recycle Bin, the workbook, …)
            try:
                ed.close()
            except Exception:
                pass
        # (in the background: the window closes now; Python waits for it before the app exits)
        threading.Thread(target=tidy, daemon=False).start()
        self._closing = True                   # (the background previews stop)
        self._pool.shutdown(wait=False, cancel_futures=True)
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
        if self._on_close:
            self._on_close()
