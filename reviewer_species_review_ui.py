"""
Review a species list (opened from the Species list window after Identify).

Like ID Review in the main window: one photo at a time, large, with the
model's guesses. Pick a guess (1–3), type your own name, or remove it (Del).
Naming a species that's already in that country's list keeps the photo there
and removes this copy (Shift: use this photo instead). Removing a confident
species' photo keeps the species if it was seen elsewhere in that country (the
next photo is shown to check). Ctrl+Z undoes, back to the start of the session. The copies in the
Species list folder are moved / removed to match, and every decision is
remembered for the next Identify (see reviewer_species_list.ListEditor).
Options: G shows every photo filed under the same guess side by side (click
one to pick it); I puts an iNaturalist photo of the species in the box next
to yours (click it for the next photo, right-click for the iNaturalist page).
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

import customtkinter as ctk
from PIL import Image, ImageTk

import reviewer_species_list as sl
from reviewer_imaging import load_preview, load_small_thumb
from reviewer_storage import _user_data_dir, load_app_settings, save_app_settings

C_BG, C_SURFACE, C_BORDER = "#0a0a0a", "#141414", "#222222"
C_TEXT, C_MUTED, C_ACCENT = "#E8EAF0", "#6B7280", "#2D7A4F"
C_BEST, C_WARN, C_BAD = "#22C55E", "#F59E0B", "#EF4444"
SHOW = ["To clarify", "Name check ⚠", "All species"]
ALL_COUNTRIES = "All countries"
NOTES = ["", "juvenile", "female", "male", "eclipse / winter plumage", "heard / call",
         "duplicate", "no animal", "too far / blurred to tell", "not wild (zoo, farm, pet)"]
DIGIT_KEYCODES = {49: 1, 50: 2, 51: 3}           # (Windows: Shift+1 is "exclam")


class SpeciesReviewWindow(ctk.CTkToplevel):
    def __init__(self, master, editor: sl.ListEditor,
                 resolve: Callable[[str, Optional[str]], tuple],
                 names: Callable[[Optional[str]], list[str]],
                 on_close: Optional[Callable[[], None]] = None):
        super().__init__(master)
        self.title("Review species list")
        self.geometry("1400x860")
        self.configure(fg_color=C_SURFACE)
        self.transient(master)
        self._ed, self._resolve, self._names_of, self._on_close = editor, resolve, names, on_close
        self._show = tk.StringVar(value=SHOW[0])
        self._country = tk.StringVar(value=ALL_COUNTRIES)
        self._items: list = []
        self._i = 0
        self._frame = 0
        self._imgs: dict[str, Image.Image] = {}
        self._tk_img = None
        self._tk_thumb = None
        self._all_names: dict = {}
        self._done = 0
        # (the Species list window's settings: saving replaces the file, so one copy)
        self._settings = getattr(master, "_settings", None)
        if not isinstance(self._settings, dict):
            self._settings = load_app_settings()
        self._side = tk.BooleanVar(value=bool(self._settings.get("species_review_side_by_side", False)))
        self._compare = tk.BooleanVar(value=bool(self._settings.get("species_review_inat", False)))
        self._thumbs: dict[str, Image.Image] = {}
        self._cells: list = []
        self._tk_cells: list = []
        self._ref = None                         # reviewer_inat.ReferencePhotos (made when first used)
        self._ref_got: dict = {}                 # scientific → result / error
        self._ref_i = 0
        self._ref_box = None
        self._tk_ref = None
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Key>", self._key)
        self.after(50, self._reload)
        self.after(20, self._maximise)
        self.grab_set()

    def _maximise(self) -> None:
        try:
            self.state("zoomed")             # (fits any screen and display scaling)
        except tk.TclError:
            pass

    # ── layout ───────────────────────────────────────────────────────────────

    def _build(self) -> None:
        bar = tk.Frame(self, bg=C_SURFACE, height=44)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        self._title = tk.Label(bar, text="", bg=C_SURFACE, fg=C_TEXT, font=("Helvetica", 12, "bold"))
        self._title.pack(side="left", padx=12)
        self._countries = ttk.Combobox(bar, textvariable=self._country, width=22, state="readonly")
        self._countries.pack(side="right", padx=(4, 12))
        tk.Label(bar, text="Country", bg=C_SURFACE, fg=C_MUTED).pack(side="right")
        ttk.Combobox(bar, textvariable=self._show, values=SHOW, width=16,
                     state="readonly").pack(side="right", padx=(4, 16))
        self._show.trace_add("write", lambda *_a: self._reload())
        self._country.trace_add("write", lambda *_a: self._reload())
        tk.Label(bar, text="Show", bg=C_SURFACE, fg=C_MUTED).pack(side="right")
        self._undo_btn = tk.Button(bar, text="↶ Undo  (Ctrl+Z)", bg=C_BORDER, fg=C_TEXT, relief="flat",
                                   bd=0, padx=10, font=("Helvetica", 9), command=self._undo)
        self._undo_btn.pack(side="right", padx=12)
        for text, var in (("Compare with iNaturalist  (I)", self._compare),
                          ("Side by side  (G)", self._side)):
            tk.Checkbutton(bar, text=text, variable=var, command=self._option_changed, bg=C_SURFACE, fg=C_MUTED,
                           selectcolor=C_BG, activebackground=C_SURFACE,
                           font=("Helvetica", 9)).pack(side="right", padx=6)

        body = tk.Frame(self, bg=C_BG)
        body.pack(fill="both", expand=True)
        left = tk.Frame(body, bg=C_SURFACE, width=260)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        self._list = tk.Listbox(left, bg=C_BG, fg=C_TEXT, selectbackground=C_ACCENT,
                                font=("Helvetica", 9), bd=0, highlightthickness=0,
                                activestyle="none", exportselection=False)
        self._list.pack(fill="both", expand=True, padx=4, pady=4)
        self._list.bind("<<ListboxSelect>>", lambda e: self._list.curselection()
                        and self._select(self._list.curselection()[0]))
        # (keys belong to the review, not the list)
        self._list.bind("<ButtonRelease-1>", lambda e: self._canvas.focus_set())

        right = tk.Frame(body, bg=C_SURFACE, width=330)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)
        self._canvas = tk.Canvas(body, bg="#050505", highlightthickness=0)
        self._canvas.pack(side="left", fill="both", expand=True)
        self._canvas.bind("<Configure>", lambda e: self._draw())
        self._canvas.bind("<Double-Button-1>", lambda e: self._open_original())
        self._canvas.bind("<Button-1>", self._click)
        self._canvas.bind("<Button-3>", self._right_click)

        self._info = tk.Label(right, text="", bg=C_SURFACE, fg=C_TEXT, justify="left",
                              wraplength=300, font=("Helvetica", 9), anchor="w")
        self._info.pack(fill="x", padx=12, pady=(12, 4))
        tk.Label(right, text="GUESSES  (1–3 · Shift: use this photo)", bg=C_SURFACE, fg=C_MUTED,
                 font=("Helvetica", 8, "bold")).pack(anchor="w", padx=12, pady=(8, 4))
        self._sugg = tk.Frame(right, bg=C_SURFACE)
        self._sugg.pack(fill="x", padx=8)
        tk.Label(right, text="Species (type to search: common or scientific name)", bg=C_SURFACE,
                 fg=C_MUTED, font=("Helvetica", 8)).pack(anchor="w", padx=12, pady=(12, 0))
        self._species = ttk.Combobox(right, font=("Helvetica", 11))
        self._species.pack(fill="x", padx=12)
        self._species.bind("<KeyRelease>", self._search)
        self._species.bind("<<ComboboxSelected>>", lambda e: (self._show_existing(),
                                                              self._compare.get() and self._draw()))
        self._species.bind("<Return>", lambda e: (self._confirm(use_this=bool(e.state & 1)), "break")[1])
        tk.Label(right, text="Reason / note (optional): why it's this, or why it's removed",
                 bg=C_SURFACE, fg=C_MUTED, font=("Helvetica", 8)).pack(anchor="w", padx=12, pady=(8, 0))
        self._note = ttk.Combobox(right, font=("Helvetica", 10), values=NOTES)
        self._note.pack(fill="x", padx=12)
        self._note.bind("<Return>", lambda e: (self._confirm(use_this=bool(e.state & 1)), "break")[1])
        self._buttons = []
        for text, cmd, col in (("✓ Confirm  (Enter)", lambda: self._confirm(), C_ACCENT),
                               ("✓ Confirm, use this photo  (Shift+Enter)", lambda: self._confirm(True),
                                C_BORDER),
                               ("✕ Remove  (Del)", self._reject, C_BAD),
                               ("Open the original  (V)", self._open_original, C_BORDER)):
            b = tk.Button(right, text=text, bg=col, fg="white", relief="flat", bd=0,
                          font=("Helvetica", 10, "bold"), command=cmd)
            b.pack(fill="x", padx=12, pady=(8, 0), ipady=3)
            self._buttons.append(b)
        self._existing = tk.Label(right, text="", bg=C_SURFACE, fg=C_MUTED, justify="left",
                                  wraplength=300, font=("Helvetica", 9), anchor="w")
        self._existing.pack(fill="x", padx=12, pady=(12, 2))
        self._thumb = tk.Label(right, bg=C_SURFACE)
        self._thumb.pack(padx=12)
        self._state = tk.Label(right, text="", bg=C_SURFACE, fg=C_BEST, justify="left",
                               wraplength=300, font=("Helvetica", 9))
        self._state.pack(fill="x", padx=12, pady=8)
        tk.Label(right, text="← → photo · ↑ ↓ other frames of the burst · Esc closes\n"
                 "Ctrl+Z undoes · removed copies go to the Recycle Bin when you close;\n"
                 "your originals are never changed",
                 bg=C_SURFACE, fg=C_MUTED, font=("Helvetica", 8)).pack(side="bottom", pady=8)

    # ── data ─────────────────────────────────────────────────────────────────

    def _wanted(self) -> list:
        show = self._show.get()
        if show == SHOW[0]:
            items = list(self._ed.clarify)
        elif show == SHOW[1]:
            items = [e for e in self._ed.entries if e.name_note.startswith("⚠")]
        else:
            items = list(self._ed.entries)
        country = self._country.get()
        if country != ALL_COUNTRIES:
            items = [i for i in items if sl.country_name(self._ed.iso(i)) == country]
        return items

    def _label(self, item) -> str:
        country = sl.country_name(self._ed.iso(item))
        if isinstance(item, sl.Entry):
            return f" {country} · {item.species}"
        return f" {country} · {item.name}?  {item.sighting.date}"

    def _reload(self) -> None:
        names = sorted({sl.country_name(self._ed.iso(i)) for i in self._ed.entries + self._ed.clarify})
        self._countries.configure(values=[ALL_COUNTRIES] + names)
        self._undo_btn.configure(state="normal" if self._ed.can_undo else "disabled")
        self._items = self._wanted()
        n_clarify = len(self._ed.clarify)
        self._title.configure(text=f"Review species list — {n_clarify} to clarify · "
                                   f"{len(self._ed.entries)} species · {self._done} done this time")
        self._list.delete(0, "end")
        for item in self._items:
            self._list.insert("end", self._label(item))
            if isinstance(item, sl.Clarify) or item.name_note.startswith("⚠"):
                self._list.itemconfig("end", fg=C_WARN)
        if not self._items:
            self._canvas.delete("all")
            self._canvas.create_text(max(300, self._canvas.winfo_width() // 2), 200, fill=C_MUTED,
                                     font=("Helvetica", 12), text="Nothing to review here 🎉")
            for w in self._sugg.winfo_children():
                w.destroy()
            self._info.configure(text="")
            self._existing.configure(text="")
            self._thumb.configure(image="")
            return
        self._select(min(self._i, len(self._items) - 1))

    def _select(self, i: int) -> None:
        if not self._items:
            return
        self._i = max(0, min(len(self._items) - 1, i))
        self._frame = 0
        self._ref_i = 0
        self._list.selection_clear(0, "end")
        self._list.selection_set(self._i)
        self._list.see(self._i)
        item = self._items[self._i]
        iso = self._ed.iso(item)
        pick = self._ed.pick(item)
        guesses = item.guesses if isinstance(item, sl.Clarify) else (
            [(item.species, item.scientific, max(s.p for s in item.sightings))]
            + [g for g in (pick.guesses or []) if g[1] != item.scientific][:2])
        self._guesses = list(guesses)[:3]
        where = pick.pick.path
        if isinstance(item, sl.Entry):
            head = (f"{item.species}  ({item.scientific})\n{sl.country_name(iso)} · "
                    f"{len(item.sightings)} sighting(s) · {item.how}"
                    + (f"\n{item.name_note}" if item.name_note else ""))
        else:
            head = f"To clarify: {item.name}?\n{sl.country_name(iso)} · {pick.date} · AI {pick.p:.0%}"
        if pick.learned:
            head += f"\nWith your examples: {pick.learned}"
        self._note.set(pick.note)
        self._info.configure(text=f"{head}\n{where}" + (
            "" if self._ed.on_disk(item) else "\n(not copied yet: Copy list copies it)"))
        for w in self._sugg.winfo_children():
            w.destroy()
        for k, (name, sci, p) in enumerate(self._guesses, 1):
            have = self._ed.entry_for(iso, sci, name, besides=item)
            b = tk.Button(self._sugg, text=f"{k}  {name}  {p:.0%}" + ("   ✓ in list" if have else ""),
                          anchor="w", bg=C_BG, fg=C_BEST if p >= 0.6 else (C_WARN if p >= 0.25 else C_TEXT),
                          relief="flat", bd=0, font=("Helvetica", 10), padx=6)
            b.configure(command=lambda k=k: self._pick(k, False))
            b.bind("<Shift-Button-1>", lambda e, k=k: (self._pick(k, True), "break")[1])
            b.pack(fill="x", pady=1)
            tk.Label(self._sugg, text=f"     {sci}", anchor="w", bg=C_SURFACE, fg=C_MUTED,
                     font=("Helvetica", 7, "italic")).pack(fill="x")
        if iso not in self._all_names:
            self._all_names[iso] = self._names_of(iso)
        first = self._guesses[0] if self._guesses else None
        self._species.set(f"{first[0]} ({first[1]})" if first and first[1] else (first[0] if first else ""))
        self._species.configure(values=[f"{n} ({sc})" for n, sc, _p in self._guesses if sc])
        self._show_existing()
        self._draw()

    def _search(self, e) -> None:
        if e.keysym in ("Return", "Up", "Down", "Escape", "Tab"):
            return
        text = self._species.get().strip().lower()
        if len(text) < 2 or not self._items:
            return
        names = self._all_names.get(self._ed.iso(self._items[self._i]), [])
        starts = [n for n in names if n.lower().startswith(text)]
        within = [n for n in names if text in n.lower() and n not in starts] if len(starts) < 40 else []
        self._species.configure(values=(starts + within)[:60])
        self._show_existing()

    def _show_existing(self) -> None:
        """What's already in the list under the name in the box (to compare with)."""
        if not self._items:
            return
        item = self._items[self._i]
        text = self._species.get().strip()
        e = None
        if text:
            name, sci, _g = self._resolve(text, self._ed.iso(item))
            e = self._ed.entry_for(self._ed.iso(item), sci, name, besides=item)
        if e is None:
            self._existing.configure(text="Not in this country's list yet: Confirm adds it." if text else "")
            self._thumb.configure(image="")
            self._tk_thumb = None
            return
        self._existing.configure(text=f"Already in the list: {e.species} ({e.pick.date}).\n"
                                      "Confirm keeps that photo and removes this copy; "
                                      "Shift+Enter uses this photo instead.")
        path = self._ed.on_disk(e) or e.pick.pick.path
        try:
            img = load_small_thumb(path, 300)
            img.thumbnail((300, 220))
            self._tk_thumb = ImageTk.PhotoImage(img)
            self._thumb.configure(image=self._tk_thumb)
        except Exception:
            self._thumb.configure(image="")
            self._tk_thumb = None

    def _frames(self, item) -> list[str]:
        pick = self._ed.pick(item)
        first = self._ed.on_disk(item) or pick.pick.path
        return [first] + [p.path for p in pick.photos if p is not pick.pick]

    # ── options: side by side, compare with iNaturalist ──────────────────────

    def _option_changed(self) -> None:
        self._settings["species_review_side_by_side"] = self._side.get()
        self._settings["species_review_inat"] = self._compare.get()
        save_app_settings(self._settings)
        self._draw()

    def _group(self, item) -> list:
        """[(path, item it selects or None)] filed with this one: the photos under the
        same best guess (To clarify), or the species' other sightings."""
        if isinstance(item, sl.Entry):
            return [(self._ed.on_disk(item) or item.pick.pick.path, None)] + \
                [(s.pick.path, None) for s in item.sightings if s is not item.pick]

        def key(c):
            return self._ed.iso(c), (c.guesses[0][1] or c.name) if c.guesses else c.name
        k = key(item)
        return [(self._ed.on_disk(c) or c.sighting.pick.path, c) for c in self._items
                if isinstance(c, sl.Clarify) and key(c) == k]

    def _thumb_of(self, path: str) -> Optional[Image.Image]:
        img = self._thumbs.get(path)
        if img is None:
            try:
                img = load_small_thumb(path, 400)
            except Exception:
                return None
            if len(self._thumbs) > 80:
                self._thumbs.pop(next(iter(self._thumbs)))
            self._thumbs[path] = img
        return img

    def _draw_grid(self, item, x0: int, y0: int, w: int, h: int) -> None:
        import math
        group = self._group(item)[:24]
        n = len(group)
        cols = max(1, math.ceil(math.sqrt(n * w / max(1, h))))
        rows = math.ceil(n / cols)
        cw, ch = w // cols, max(40, (h - 24) // rows)
        self._cells, self._tk_cells = [], []
        for i, (path, it) in enumerate(group):
            cx, cy = x0 + (i % cols) * cw, y0 + 24 + (i // cols) * ch
            img = self._thumb_of(path)
            if img is not None:
                t = img.copy()
                t.thumbnail((cw - 8, ch - 8))
                tk_t = ImageTk.PhotoImage(t)
                self._tk_cells.append(tk_t)
                self._canvas.create_image(cx + cw // 2, cy + ch // 2, image=tk_t)
            current = it is item or (it is None and i == 0)
            self._canvas.create_rectangle(cx + 2, cy + 2, cx + cw - 2, cy + ch - 2,
                                          outline=C_WARN if current else C_BORDER, width=3 if current else 1)
            self._cells.append((cx, cy, cx + cw, cy + ch, it))
        what = "photos under this guess" if isinstance(item, sl.Clarify) else "sightings of this species"
        self._canvas.create_text(x0 + 10, y0 + 6, anchor="nw", fill=C_TEXT, font=("Helvetica", 9, "bold"),
                                 text=f"{n} {what.replace('photos', 'photo') if n == 1 else what} (click one to pick it · G: one at a time)"
                                 if isinstance(item, sl.Clarify) else f"{n} {what} (G: one at a time)")

    def _ref_sci(self) -> tuple[str, str]:
        """(name, scientific) to compare with: the name in the box, else the best guess."""
        item = self._items[self._i]
        text = self._species.get().strip()
        if text:
            name, sci, _g = self._resolve(text, self._ed.iso(item))
            if sci:
                return name, sci
        g = self._guesses[0] if self._guesses else None
        return (g[0], g[1]) if g else ("", "")

    def _draw_ref(self, x0: int, w: int, h: int) -> None:
        name, sci = self._ref_sci()
        self._ref_box = (x0, 0, x0 + w, h, sci)
        self._canvas.create_line(x0, 0, x0, h, fill=C_BORDER, width=2)
        if not sci:
            self._canvas.create_text(x0 + w // 2, h // 2, fill=C_MUTED, text="No species to compare with")
            return
        got = self._ref_got.get(sci)
        if got is None:
            if self._ref is None:
                from reviewer_inat import ReferencePhotos
                self._ref = ReferencePhotos(os.path.join(str(_user_data_dir()), "inat_photos"))
            got = self._ref.cached(sci)
            if got is None:
                self._ref_got[sci] = "…"
                self._ref.fetch_async(sci, lambda s, r, err: self._ref_got.__setitem__(s, r or f"! {err}"))
                self.after(300, self._ref_wait, sci)
            else:
                self._ref_got[sci] = got
        got = self._ref_got.get(sci)
        if got == "…":
            self._canvas.create_text(x0 + w // 2, h // 2, fill=C_MUTED,
                                     text=f"Getting iNaturalist photos of\n{name} ({sci})…")
            return
        if isinstance(got, str) or not got.get("photos"):
            why = got[2:] if isinstance(got, str) else "iNaturalist has no photos of it"
            self._canvas.create_text(x0 + w // 2, h // 2, fill=C_MUTED, width=w - 20,
                                     text=f"No iNaturalist photo of {name} ({sci})\n{why}")
            return
        photos = got["photos"]
        ph = photos[self._ref_i % len(photos)]
        try:
            img = Image.open(ph["path"])
            img.load()
            img.thumbnail((w - 12, h - 60))
        except Exception as e:
            self._canvas.create_text(x0 + w // 2, h // 2, fill=C_MUTED, text=f"Cannot load\n{e}")
            return
        self._tk_ref = ImageTk.PhotoImage(img)
        self._canvas.create_image(x0 + w // 2, (h - 40) // 2 + 20, image=self._tk_ref)
        self._canvas.create_text(x0 + 10, 10, anchor="nw", fill=C_TEXT, font=("Helvetica", 9, "bold"),
                                 text=f"iNaturalist: {got.get('name') or name} ({sci})")
        self._canvas.create_text(x0 + 10, h - 8, anchor="sw", fill=C_MUTED, width=w - 20,
                                 font=("Helvetica", 8),
                                 text=f"Photo {self._ref_i % len(photos) + 1} of {len(photos)} "
                                      f"(click: next · right-click: iNaturalist page) · {ph.get('attribution', '')}")

    def _ref_wait(self, sci: str) -> None:
        try:
            if self._ref_got.get(sci) == "…":
                self.after(300, self._ref_wait, sci)
            elif self._compare.get() and self._items:
                self._draw()
        except tk.TclError:
            pass

    def _click(self, e) -> None:
        box = self._ref_box
        if box and self._compare.get() and box[0] <= e.x <= box[2]:
            self._ref_i += 1
            self._draw()
            return
        if self._side.get():
            for x0, y0, x1, y1, it in self._cells:
                if x0 <= e.x < x1 and y0 <= e.y < y1 and it is not None and it in self._items:
                    self._select(self._items.index(it))
                    return

    def _right_click(self, e) -> None:
        box = self._ref_box
        if box and self._compare.get() and box[0] <= e.x <= box[2]:
            got = self._ref_got.get(box[4])
            if isinstance(got, dict) and got.get("url"):
                import webbrowser
                webbrowser.open(got["url"])

    def _draw(self) -> None:
        self._canvas.delete("all")
        self._cells, self._ref_box = [], None
        if not self._items:
            return
        W, H = max(50, self._canvas.winfo_width()), max(50, self._canvas.winfo_height())
        mine = int(W * 0.55) if self._compare.get() else W
        if self._compare.get():
            self._draw_ref(mine, W - mine, H)
        if self._side.get():
            self._draw_grid(self._items[self._i], 0, 0, mine, H)
            return
        frames = self._frames(self._items[self._i])
        path = frames[self._frame % len(frames)]
        cw, ch = mine, H
        try:
            img = self._imgs.get(path)
            if img is None:
                img = load_preview(path, max(cw, ch))
                if len(self._imgs) > 6:
                    self._imgs.pop(next(iter(self._imgs)))
                self._imgs[path] = img
            img = img.copy()
            img.thumbnail((cw, ch), Image.LANCZOS)
        except Exception as e:
            self._canvas.create_text(cw // 2, ch // 2, text=f"Cannot load\n{path}\n{e}", fill=C_MUTED)
            return
        self._tk_img = ImageTk.PhotoImage(img)
        self._canvas.create_image(cw // 2, ch // 2, image=self._tk_img)
        if self._compare.get():
            self._canvas.create_text(cw // 2, ch - 8, anchor="s", fill=C_MUTED, font=("Helvetica", 8),
                                     text="Yours")
        self._canvas.create_text(10, 10, anchor="nw", fill=C_TEXT, font=("Helvetica", 9, "bold"),
                                 text=os.path.basename(path) + (
                                     f"   frame {self._frame % len(frames) + 1} of {len(frames)} (↑ ↓)"
                                     if len(frames) > 1 else ""))

    # ── actions ──────────────────────────────────────────────────────────────

    def _pick(self, k: int, use_this: bool) -> None:
        if self._items and k <= len(self._guesses):
            name, sci, _p = self._guesses[k - 1]
            note = self._note.get()
            self._apply(lambda item: self._ed.assign(item, name, sci, sl.Identifier._group(sci), use_this,
                                                     note=note))

    def _confirm(self, use_this: bool = False) -> None:
        if not self._items:
            return
        text = self._species.get().strip()
        if not text:
            self._state.configure(text="Type or pick a species first", fg=C_WARN)
            return
        item = self._items[self._i]
        name, sci, group = self._resolve(text, self._ed.iso(item))
        note = self._note.get()
        self._apply(lambda it: self._ed.assign(it, name, sci, group, use_this, note=note),
                    note=" · the AI learns from it" if sci else
                    " (not a species the model knows: kept as typed, nothing for the AI to learn)")

    def _reject(self) -> None:
        if self._items:
            note = self._note.get()
            self._apply(lambda it: self._ed.reject(it, note=note))

    def _apply(self, fn, note: str = "") -> None:
        item = self._items[self._i]
        try:
            msg = fn(item)
        except OSError as e:
            self._state.configure(text=f"Couldn't change the files: {e}", fg=C_BAD)
            return
        self._done += 1
        self._state.configure(text=msg + note, fg=C_BEST)
        self._imgs.clear()
        self._canvas.focus_set()               # (so 1–3 / Del work again after typing)
        self._reload()                         # (the next one moves up into this place)

    def _undo(self) -> None:
        try:
            msg = self._ed.undo()
        except OSError as e:
            msg = f"Couldn't undo: {e}"
        self._state.configure(text=msg, fg=C_WARN)
        self._done = max(0, self._done - 1)
        self._imgs.clear()
        self._reload()

    def _open_original(self) -> None:
        if self._items:
            frames = self._frames(self._items[self._i])
            path = frames[self._frame % len(frames)]
            if self._frame % len(frames) == 0:
                path = self._ed.pick(self._items[self._i]).pick.path
            try:
                os.startfile(path)                     # (Windows)
            except (OSError, AttributeError):
                pass

    def _key(self, e) -> Optional[str]:
        try:
            typing = self.focus_get() in (self._species, self._note)
        except (KeyError, tk.TclError):          # (Tk: the drop-down list has the focus)
            typing = True
        ks = e.keysym
        if ks == "Escape":
            self._close()
            return "break"
        if ks in ("z", "Z") and e.state & 4:          # Ctrl+Z (even while typing)
            self._undo()
            return "break"
        if typing or not self._items:
            return None
        shift = bool(e.state & 1)
        if ks in ("Right", "Next"):
            self._select(self._i + 1)
        elif ks in ("Left", "Prior"):
            self._select(self._i - 1)
        elif ks in ("Down", "Up"):
            self._frame += 1 if ks == "Down" else -1
            self._draw()
        elif ks in ("Return", "KP_Enter"):
            self._confirm(use_this=shift)
        elif shift and e.keycode in DIGIT_KEYCODES:
            self._pick(DIGIT_KEYCODES[e.keycode], True)
        elif ks in ("1", "2", "3"):
            self._pick(int(ks), False)
        elif ks in ("Delete", "x", "X"):
            self._reject()
        elif ks in ("v", "V"):
            self._open_original()
        elif ks in ("g", "G"):
            self._side.set(not self._side.get())
            self._option_changed()
        elif ks in ("i", "I"):
            self._compare.set(not self._compare.get())
            self._option_changed()
        else:
            return None
        return "break"

    def _close(self) -> None:
        try:
            self._ed.close()                   # (removed copies → Recycle Bin)
        except OSError:
            pass
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
        if self._on_close:
            self._on_close()
