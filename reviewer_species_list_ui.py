"""
The "Species list from archive" window (see reviewer_species_list for the rules).

1. Find folders: every photo under the parent, grouped by place, with the
   country read from folder names / XMP. Set the missing ones (select rows →
   Set country…; remembered for next time).
2. Identify (background, hours for a big archive on a USB drive; can be
   stopped and carries on where it left off).
3. Copy the list: one photo per species per country + To clarify + CSV lists.
"""

from __future__ import annotations

import os
import queue
import threading
from collections import defaultdict
from tkinter import filedialog as fd, messagebox as mb, ttk
import tkinter as tk

import customtkinter as ctk

import reviewer_species_list as sl
from reviewer_regions import by_name, countries
from reviewer_storage import _user_data_dir, load_app_settings, save_app_settings

C_BG, C_SURFACE, C_BORDER = "#0d0d0d", "#161616", "#2a2a2a"
C_TEXT, C_MUTED, C_ACCENT = "#e8e8e8", "#6B7280", "#2D7A4F"
C_WARN, C_BAD = "#F59E0B", "#EF4444"
NOT_KNOWN = "Not known"
AUTOMATIC = "Automatic (inherit / from folder name)"
SURE_CHOICES = ["60%", "70%", "80%", "90%", "95%"]
PER_GUESS_CHOICES = ["1", "3", "7", "10", "All"]


def _duration(s: float) -> str:
    s = int(max(0, s))
    if s < 90:
        return f"{s} s"
    if s < 5400:
        return f"{s // 60} min"
    return f"{s // 3600} h {s % 3600 // 60:02d} min"


class SpeciesListDialog(ctk.CTkToplevel):
    def __init__(self, master):
        super().__init__(master)
        self.title("Species list from archive")
        self.geometry("1100x760")
        self.configure(fg_color=C_SURFACE)
        self.transient(master)
        self._settings = load_app_settings()
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._busy = False
        self._photos: list[sl.Photo] = []
        self._places: dict[str, dict] = {}
        self._place_iid: dict[str, str] = {}        # tree row → place (a folder with photos)
        self._node_path: dict[str, str] = {}        # tree row → folder (every row)
        self._entries: list[sl.Entry] = []
        self._clarify: list[sl.Clarify] = []
        self._identifier = None
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._poll)
        self.after(300, self._load_saved)

    # ── layout ───────────────────────────────────────────────────────────────

    def _build(self) -> None:
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=16, pady=(12, 4))
        self._parent = tk.StringVar(value=self._settings.get("species_list_parent", ""))
        # the list is saved inside the folder you choose, in "Species list" (never read itself)
        self._dest = tk.StringVar()
        self._parent.trace_add("write", lambda *_a: self._dest.set(
            sl.list_folder(self._parent.get().strip()) if self._parent.get().strip() else ""))
        self._dest.set(sl.list_folder(self._parent.get().strip()) if self._parent.get().strip() else "")
        ctk.CTkLabel(top, text="Look in", width=90, anchor="w").grid(row=0, column=0, sticky="w")
        ctk.CTkEntry(top, textvariable=self._parent, width=600).grid(row=0, column=1, sticky="we", pady=2)
        ctk.CTkButton(top, text="Browse…", width=80, fg_color=C_BORDER,
                      command=lambda: self._browse(self._parent)).grid(row=0, column=2, padx=6)
        ctk.CTkLabel(top, text="every photo under it is read", text_color=C_MUTED,
                     font=ctk.CTkFont("Helvetica", 10)).grid(row=0, column=3, sticky="w")
        ctk.CTkLabel(top, text="Saved in", width=90, anchor="w").grid(row=1, column=0, sticky="w")
        ctk.CTkLabel(top, textvariable=self._dest, anchor="w", text_color=C_MUTED).grid(
            row=1, column=1, sticky="we", pady=2)
        ctk.CTkLabel(top, text="Country / Group / one photo per species + CSV lists", text_color=C_MUTED,
                     font=ctk.CTkFont("Helvetica", 10)).grid(row=1, column=3, sticky="w")
        top.grid_columnconfigure(1, weight=1)

        opts = ctk.CTkFrame(self, fg_color="transparent")
        opts.pack(fill="x", padx=16, pady=(4, 0))
        self._find_btn = ctk.CTkButton(opts, text="1  Find folders", width=130, command=self._find)
        self._find_btn.pack(side="left")
        ctk.CTkButton(opts, text="Set country…", width=110, fg_color=C_BORDER,
                      command=self._set_country).pack(side="left", padx=6)
        ctk.CTkLabel(opts, text="   Certain at", text_color=C_MUTED).pack(side="left")
        self._sure = tk.StringVar(value=self._settings.get("species_list_sure", "80%"))
        ttk.Combobox(opts, textvariable=self._sure, values=SURE_CHOICES, width=5,
                     state="readonly").pack(side="left", padx=4)
        ctk.CTkLabel(opts, text="(less sure → To clarify)", text_color=C_MUTED,
                     font=ctk.CTkFont("Helvetica", 10)).pack(side="left")
        ctk.CTkLabel(opts, text="   To clarify: up to", text_color=C_MUTED).pack(side="left")
        self._per_guess = tk.StringVar(value=self._settings.get("species_list_per_guess", str(sl.CLARIFY_PER_GUESS)))
        ttk.Combobox(opts, textvariable=self._per_guess, values=PER_GUESS_CHOICES, width=4,
                     state="readonly").pack(side="left", padx=4)
        ctk.CTkLabel(opts, text="photos per best guess", text_color=C_MUTED,
                     font=ctk.CTkFont("Helvetica", 10)).pack(side="left")
        self._review_btn = ctk.CTkButton(opts, text="4  Review…", width=110, state="disabled",
                                         command=self._review)
        self._review_btn.pack(side="right")
        self._jpeg = tk.BooleanVar(value=bool(self._settings.get("species_list_jpeg", True)))
        ctk.CTkCheckBox(opts, text="RAW as JPEG", variable=self._jpeg, width=20,
                        command=self._jpeg_changed).pack(side="right", padx=(10, 0))
        self._copy_btn = ctk.CTkButton(opts, text="3  Copy list", width=120, state="disabled",
                                       command=self._copy)
        self._copy_btn.pack(side="right", padx=6)
        self._ident_btn = ctk.CTkButton(opts, text="2  Identify species", width=150, state="disabled",
                                        command=self._identify)
        self._ident_btn.pack(side="right", padx=6)

        self._status = ctk.CTkLabel(self, text="Choose the folders, then Find folders. Nothing is "
                                    "copied until you press Copy list.", anchor="w", text_color=C_MUTED)
        self._status.pack(fill="x", padx=16, pady=(8, 2))
        self._bar = ctk.CTkProgressBar(self)
        self._bar.set(0)
        self._bar.pack(fill="x", padx=16, pady=(0, 4))

        style = ttk.Style(self)
        try:
            style.theme_use("clam")        # (the Windows theme ignores the dark colours)
        except tk.TclError:
            pass
        style.configure("SL.Treeview", background=C_BG, fieldbackground=C_BG,
                        foreground=C_TEXT, rowheight=20, borderwidth=0)
        style.configure("SL.Treeview.Heading", background=C_BORDER, foreground=C_TEXT)
        style.map("SL.Treeview", background=[("selected", C_ACCENT)])
        self._tabs = ttk.Notebook(self)
        self._tabs.pack(fill="both", expand=True, padx=16, pady=6)

        f1 = tk.Frame(self._tabs, bg=C_BG)
        self._tabs.add(f1, text="  Folders and countries  ")
        self._ptree = ttk.Treeview(f1, columns=("photos", "country", "how"), show="tree headings",
                                   style="SL.Treeview", selectmode="extended")
        self._ptree.heading("#0", text="Folder")
        for col, title, w in (("photos", "Photos", 80), ("country", "Country", 200), ("how", "From", 160)):
            self._ptree.heading(col, text=title)
            self._ptree.column(col, width=w, stretch=False, anchor="w")
        self._ptree.column("#0", width=560, stretch=True)
        self._ptree.tag_configure("missing", foreground=C_BAD)
        self._pack_tree(f1, self._ptree)
        self._ptree.bind("<Double-1>", lambda e: self._set_country())

        f2 = tk.Frame(self._tabs, bg=C_BG)
        self._tabs.add(f2, text="  Species  ")
        self._rtree = ttk.Treeview(f2, columns=("sci", "seen", "how", "dest"), show="tree headings",
                                   style="SL.Treeview", selectmode="browse")
        self._rtree.heading("#0", text="Species")
        for col, title, w in (("sci", "Scientific name", 190), ("seen", "Seen", 150),
                              ("how", "Identified by · name check", 230), ("dest", "→ copied to", 300)):
            self._rtree.heading(col, text=title)
            self._rtree.column(col, width=w, stretch=(col == "dest"), anchor="w")
        self._rtree.column("#0", width=270, stretch=False)
        self._rtree.tag_configure("head", font=("Helvetica", 10, "bold"))
        self._rtree.tag_configure("unsure", foreground=C_WARN)
        self._rtree.tag_configure("kept", foreground=C_MUTED)
        self._pack_tree(f2, self._rtree)
        self._rtree.bind("<Double-1>", self._open_original)
        self._rtree.bind("<Button-3>", self._row_menu)

        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", padx=16, pady=(0, 12))
        self._summary = ctk.CTkLabel(bottom, text="", anchor="w", wraplength=900, justify="left")
        self._summary.pack(side="left")
        self._stop_btn = ctk.CTkButton(bottom, text="Stop", width=80, fg_color=C_BORDER,
                                       state="disabled", command=self._stop.set)
        self._stop_btn.pack(side="right")
        ctk.CTkLabel(self, text="Everything read is remembered: Stop any time and Identify again "
                     "to carry on · double-click a species to open its photo · your originals are "
                     "never changed", text_color=C_MUTED,
                     font=ctk.CTkFont("Helvetica", 10)).pack(fill="x", padx=16, pady=(0, 8))

    def _pack_tree(self, frame, tree) -> None:
        sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def _browse(self, var: tk.StringVar) -> None:
        d = fd.askdirectory(parent=self, initialdir=var.get() or None)
        if d:
            var.set(os.path.normpath(d))

    def _folders(self) -> tuple[str, str] | None:
        parent = self._parent.get().strip()
        if not os.path.isdir(parent):
            mb.showerror("Species list", "Choose the folder to look in.", parent=self)
            return None
        dest = sl.list_folder(parent)
        self._settings.update(species_list_parent=parent, species_list_sure=self._sure.get(),
                              species_list_per_guess=self._per_guess.get())
        save_app_settings(self._settings)
        return parent, dest

    # ── 1. folders and countries ─────────────────────────────────────────────

    def _saved_countries(self) -> dict:
        return self._settings.setdefault("species_list_countries", {})

    def _find(self) -> None:
        got = self._folders()
        if got:
            self._start(self._find_work, *got)

    def _find_work(self, parent: str, dest: str) -> None:
        try:
            photos = sl.find_photos(parent, exclude=dest, stop=self._stop,
                                    progress=lambda m: self._q.put(("status", m)))
            self._q.put(("status", "Reading countries from folder names and XMP…"))
            places = sl.guess_places(photos, parent)
            self._q.put(("found", (photos, places)))
        except Exception as e:
            self._q.put(("error", f"Couldn't read the folders: {e}"))

    def _country(self, place: str) -> tuple:
        """(iso or None, where from) for a place, with what you set above it inherited."""
        return sl.effective_country(place, self._parent.get().strip(), self._places[place],
                                    self._saved_countries())

    def _missing(self) -> list[str]:
        """Places with no country (and not marked 'not known' by you)."""
        out = []
        for place in self._places:
            iso, how = self._country(place)
            if not iso and not how:
                out.append(place)
        return out

    def _show_places(self) -> None:
        """The folders as a tree: set a country on any folder and everything under
        it inherits it (a folder below with its own keeps that)."""
        tree = self._ptree
        opened = {self._node_path.get(i) for i in self._all_nodes() if tree.item(i, "open")}
        tree.delete(*tree.get_children())
        self._node_path.clear()
        self._place_iid.clear()
        parent = self._parent.get().strip()
        folders = set()
        for place in self._places:
            f = place
            while True:
                folders.add(f)
                if sl._key(f) == sl._key(parent) or os.path.dirname(f) == f:
                    break
                f = os.path.dirname(f)
        node_of: dict[str, str] = {}
        for f in sorted(folders, key=lambda x: (len(os.path.relpath(x, parent).split(os.sep)), x.lower())):
            up = node_of.get(sl._key(os.path.dirname(f)), "") if sl._key(f) != sl._key(parent) else ""
            text = parent if sl._key(f) == sl._key(parent) else os.path.basename(f)
            iid = tree.insert(up, "end", text=text,
                              open=(f in opened) or not opened)
            node_of[sl._key(f)] = iid
            self._node_path[iid] = f
            if f in self._places:
                self._place_iid[iid] = f
        for iid in self._node_path:
            self._draw_place(iid)
        missing = self._missing()
        n_missing = sum(self._places[p]["photos"] for p in missing)
        self._summary.configure(
            text=f"{len(self._photos):,} photos in {len(self._places)} folders"
                 + (f" · {len(missing)} folders ({n_missing:,} photos) have no country (red)"
                    if missing else " · every folder has a country"))

    def _all_nodes(self, node: str = "") -> list[str]:
        out = []
        for c in self._ptree.get_children(node):
            out.append(c)
            out += self._all_nodes(c)
        return out

    def _draw_place(self, iid: str) -> None:
        folder = self._node_path[iid]
        under = [p for p in self._places if sl._within(p, folder)]
        photos = sum(self._places[p]["photos"] for p in under)
        missing = any(not self._country(p)[0] and not self._country(p)[1] for p in under)
        if folder in self._places:
            iso, how = self._country(folder)
            c = sl.country_name(iso) if iso else ("not known" if how else "?  — select and Set country…")
        else:                                         # a folder above: only what you set on it
            saved = {sl._key(k): v for k, v in self._saved_countries().items()}
            v = saved.get(sl._key(folder))
            c = "" if v is None else (sl.country_name(v) if v else "not known")
            how = "you (applies to the folders below)" if v is not None else ""
        self._ptree.item(iid, values=(f"{photos:,}", c, how), tags=("missing",) if missing else ())

    def _set_country(self) -> None:
        rows = [r for r in self._ptree.selection() if r in self._node_path]
        if not rows or self._busy:
            mb.showinfo("Set country", "Select folders first (Ctrl / Shift + click for several). "
                        "A country set on a folder applies to every folder under it.", parent=self)
            return
        dlg = ctk.CTkToplevel(self)
        dlg.title("Set country")
        dlg.geometry("420x190")
        dlg.configure(fg_color=C_SURFACE)
        dlg.transient(self)
        ctk.CTkLabel(dlg, text=f"Country for {len(rows)} folder(s) and everything under them:").pack(pady=(14, 6))
        first = self._node_path[rows[0]]
        saved = {sl._key(k): v for k, v in self._saved_countries().items()}
        v = saved.get(sl._key(first))
        var = tk.StringVar(value="" if v is None else (sl.country_name(v) if v else NOT_KNOWN))
        box = ttk.Combobox(dlg, textvariable=var, width=40,
                           values=[AUTOMATIC, NOT_KNOWN] + [c["name"] for c in countries()])
        box.pack()
        box.focus_set()
        ctk.CTkLabel(dlg, text="Folders below with their own country (from their name, or set by you)\n"
                     f"keep it. {AUTOMATIC}: remove what you set, so it's inherited again.",
                     text_color=C_MUTED, font=ctk.CTkFont("Helvetica", 10)).pack(pady=(6, 0))

        def ok(_e=None):
            name = var.get().strip()
            c = by_name(name) if name and name not in (NOT_KNOWN, AUTOMATIC) else None
            if name and name not in (NOT_KNOWN, AUTOMATIC) and c is None:
                mb.showerror("Set country", f"Unknown country: {name}", parent=dlg)
                return
            store = self._saved_countries()
            for r in rows:
                folder = self._node_path[r]
                for k in [k for k in store if sl._key(k) == sl._key(folder)]:
                    del store[k]
                if name != AUTOMATIC:
                    store[folder] = c["iso"] if c else ""
            save_app_settings(self._settings)
            dlg.destroy()
            self._show_places()
        box.bind("<Return>", ok)
        ctk.CTkButton(dlg, text="OK", width=90, command=ok).pack(pady=10)
        dlg.grab_set()

    # ── 2. identify ──────────────────────────────────────────────────────────

    def _identify(self) -> None:
        got = self._folders()
        if not got or not self._photos:
            return
        missing = self._missing()
        if missing:
            n = sum(self._places[p]["photos"] for p in missing)
            if not mb.askyesno(
                    "Countries missing",
                    f"{len(missing)} folders ({n:,} photos) have no country (red in the list).\n\n"
                    "Without one, species are identified worldwide (less accurate for "
                    f"look-alikes) and listed under '{sl.UNKNOWN}'.\n\n"
                    "Carry on anyway? (No: set them first with Set country…)", parent=self):
                return
        sure = int(self._sure.get().rstrip("%")) / 100
        if self._identifier is None or self._identifier.sure != sure:
            if self._identifier is not None:
                self._identifier.close()
            self._identifier = sl.Identifier(str(_user_data_dir()), sure)
        countries_of = {p: self._country(p)[0] for p in self._places}
        per_guess = None if self._per_guess.get() == "All" else int(self._per_guess.get())
        self._start(self._identify_work, list(self._photos), countries_of, got[0], got[1], per_guess,
                    self._jpeg.get())

    def _identify_work(self, photos, countries_of, parent, dest, per_guess, jpeg) -> None:
        try:
            res = self._identifier.run(photos, countries_of, parent,
                                       progress=lambda d: self._q.put(("ident", d)), stop=self._stop,
                                       decisions=sl.load_list(dest).get("decisions"))
            entries, clarify = sl.build(res["sightings"], per_guess, name_of=self._identifier.local_name)
            sl.plan_copies(entries, clarify, dest, jpeg)      # (Tk is only read on its own thread)
            self._q.put(("identified", (res, entries, clarify)))
        except Exception as e:
            self._q.put(("error", f"Identifying failed: {e}"))

    def _show_results(self, res: dict, entries, clarify, show: bool = True) -> None:
        self._res, self._entries, self._clarify = res, entries, clarify
        self._rtree.delete(*self._rtree.get_children())
        self._rpath: dict[str, str] = {}
        self._rcopy: dict[str, str] = {}
        by_country = defaultdict(lambda: ([], []))
        for e in entries:
            by_country[sl.country_name(e.iso)][0].append(e)
        for c in clarify:
            by_country[sl.country_name(c.sighting.iso)][1].append(c)
        for country in sorted(by_country):
            es, cs = by_country[country]
            node = self._rtree.insert("", "end", text=f"{country}  ({len(es)} species)", open=True,
                                      tags=("head",))
            groups = defaultdict(list)
            for e in es:
                groups[sl.GROUP_FOLDERS.get(e.group, "Other animals")].append(e)
            for g in sorted(groups):
                gn = self._rtree.insert(node, "end", text=f"{g}  ({len(groups[g])})", open=False,
                                        tags=("head",))
                for e in groups[g]:
                    seen = e.first if e.first == e.last else f"{e.first} … {e.last}"
                    iid = self._rtree.insert(
                        gn, "end", text=e.species,
                        values=(e.scientific, f"{seen} ({len(e.sightings)}×)",
                                e.how + (f" · {e.name_note}" if e.name_note else "") + (" · learned" if e.learned else ""),
                                ("already there: " if e.kept else "") + e.dest),
                        tags=("kept",) if e.kept else ("unsure",) if e.name_note.startswith("⚠") else ())
                    self._rpath[iid] = e.pick.pick.path
                    self._rcopy[iid] = e.dest if e.kept else ""
            if cs:
                cn = self._rtree.insert(node, "end", text=f"To clarify  ({len(cs)} photos)", open=False,
                                        tags=("head", "unsure"))
                for c in cs:
                    s = c.sighting
                    guesses = ", ".join(f"{n} {p:.0%}" for n, _sc, p in c.guesses[:3])
                    iid = self._rtree.insert(cn, "end", text=f"{c.name}?",
                                             values=(guesses, s.date, f"AI {s.p:.0%}" + (f" · learned: {s.learned}" if s.learned else ""),
                                                     ("already there: " if c.kept else "") + c.dest),
                                             tags=("kept",) if c.kept else ("unsure",))
                    self._rpath[iid] = s.pick.path
                    self._rcopy[iid] = c.dest if c.kept else ""
        n_species = len(entries)
        new = sum(1 for e in entries if not e.kept) + sum(1 for c in clarify if not c.kept)
        stopped = res["done"] < res["bursts"]
        text = (f"{'Stopped part-way — ' if stopped else ''}{n_species} species "
                f"(across {len(by_country)} countr{'y' if len(by_country) == 1 else 'ies'}) · "
                f"{len(clarify)} photos to clarify · {res['unique']:,} photos in {res['bursts']:,} "
                f"bursts, {res['no_animal']:,} without an animal · {new} files to copy")
        learn = res.get("learning") or {}
        if learn.get("active"):
            text += (f" · learning from {learn['examples']} of your decisions (on photos held out: "
                     f"right {learn['before']:.0%} → {learn['after']:.0%}); "
                     f"{learn.get('changed', 0)} results changed by it")
        elif learn.get("examples"):
            text += f" · {learn['examples']} decisions to learn from: {learn.get('why', '')}"
        if res.get("errors"):
            text += f" · {len(res['errors'])} bursts failed ({res['errors'][0]})"
        if res.get("regions_missing"):
            text += " · no species list for " + ", ".join(sl.country_name(i) for i in res["regions_missing"])
        self._summary.configure(text=text)
        if show:
            self._tabs.select(1)
        self._copy_btn.configure(state="normal" if (entries or clarify) else "disabled")
        self._review_btn.configure(state="normal" if (entries or clarify) else "disabled")

    def _open_original(self, e) -> None:
        row = self._rtree.identify_row(e.y)
        path = sl.find_original(getattr(self, "_rpath", {}).get(row, ""))
        if path:
            try:
                os.startfile(path)                        # (Windows)
            except (OSError, AttributeError):
                pass

    def _row_menu(self, e) -> None:
        """Right-click a species: open it, or show the original / the list's copy in its folder."""
        row = self._rtree.identify_row(e.y)
        original = getattr(self, "_rpath", {}).get(row)
        if not original:
            return
        self._rtree.selection_set(row)
        copy = getattr(self, "_rcopy", {}).get(row, "")
        copy = os.path.join(self._dest.get().strip(), copy) if copy else ""

        def missing():
            self._status.configure(text="The original isn't there (is the drive plugged in?)")
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Open the original", command=lambda: self._open_original(e))
        menu.add_command(label="Show original in folder",
                         command=lambda: sl.show_in_folder(original) or missing())
        if copy and os.path.exists(copy):
            menu.add_command(label="Show the copy in the list",
                             command=lambda: sl.show_in_folder(copy))
        menu.tk_popup(e.x_root, e.y_root)

    # ── 4. review ────────────────────────────────────────────────────────────

    def _ident(self) -> "sl.Identifier":
        if self._identifier is None:
            sure = int(self._sure.get().rstrip("%")) / 100
            self._identifier = sl.Identifier(str(_user_data_dir()), sure)
        return self._identifier

    def _load_saved(self, after_find: bool = False) -> None:
        """The last Identify for this folder (with your Review changes): Review
        and Copy list work straight away, without identifying again."""
        parent = self._parent.get().strip()
        if self._busy or not parent or self._entries or self._clarify:
            return
        got = sl.load_results(str(_user_data_dir()), parent)
        if not got:
            return
        self._show_results(got["res"], got["entries"], got["clarify"], show=not after_find)
        if after_find:
            return                              # (the countries come first: that tab stays)
        import datetime
        when = datetime.datetime.fromtimestamp(got["t"]).strftime("%d %b %Y %H:%M")
        self._status.configure(text=f"The last results for this folder ({when}): Review… and Copy list "
                               "work straight away. Find folders and Identify species again to "
                               "include new photos or changes.")

    def _save_results(self) -> None:
        parent = self._parent.get().strip()
        if parent and getattr(self, "_res", None) is not None:
            sl.save_results(str(_user_data_dir()), parent, self._res, self._entries, self._clarify)

    def _review(self) -> None:
        if self._busy or not (self._entries or self._clarify):
            return
        from reviewer_species_review_ui import SpeciesReviewWindow
        ident = self._ident()
        editor = sl.ListEditor(self._entries, self._clarify, self._dest.get().strip(),
                               examples=ident.examples, sightings=self._res.get("sightings"),
                               name_of=ident.local_name)
        editor.feature_of = lambda k: (ident.cache.get(k).get("a") or {}).get("f")

        def closed():
            self._show_results(self._res, self._entries, self._clarify)
            self._save_results()
        SpeciesReviewWindow(self, editor, ident.resolve, ident.names, on_close=closed)

    # ── 3. copy ──────────────────────────────────────────────────────────────

    def _jpeg_changed(self) -> None:
        """RAW as JPEG: remembered; the photos still to copy are named to match
        (copies already in the list stay as they are)."""
        self._settings["species_list_jpeg"] = self._jpeg.get()
        save_app_settings(self._settings)
        if (self._entries or self._clarify) and not self._busy:
            sl.plan_copies(self._entries, self._clarify, self._dest.get().strip(), self._jpeg.get())
            self._show_results(self._res, self._entries, self._clarify, show=False)

    def _copy(self) -> None:
        dest = self._dest.get().strip()
        todo = [e for e in self._entries if not e.kept] + [c for c in self._clarify if not c.kept]
        size = n_jpeg = 0
        for item in todo:
            src = item.pick.pick.path if isinstance(item, sl.Entry) else item.sighting.pick.path
            if not os.path.exists(src):
                continue
            if self._jpeg.get() and os.path.splitext(src)[1].lower() in sl.RAW_EXTS:
                n_jpeg += 1
                size += os.path.getsize(src) // 7          # (roughly: a 60 MB D850 NEF → a 7–9 MB JPEG)
            else:
                size += os.path.getsize(src)
        made = f", {n_jpeg} of them RAW made into JPEGs" if n_jpeg else ""
        if not mb.askyesno("Copy list", f"Copy {len(todo)} photos (about {size / 1e9:.1f} GB{made}) and "
                           f"write the CSV lists into\n{dest}?\n\nYour originals are not changed.", parent=self):
            return
        self._start(self._copy_work, dest)

    def _copy_work(self, dest: str) -> None:
        try:
            done = sl.copy_list(self._entries, self._clarify, dest, stop=self._stop, jpeg=self._jpeg.get(),
                                progress=lambda n, t, d: self._q.put(("progress", (n, t, d))))
            self._q.put(("copied", done))
        except Exception as e:
            self._q.put(("error", f"Copy failed: {e}"))

    # ── background plumbing ──────────────────────────────────────────────────

    def _start(self, fn, *args) -> None:
        self._busy = True
        self._stop.clear()
        for b in (self._find_btn, self._ident_btn, self._copy_btn, self._review_btn):
            b.configure(state="disabled")
        self._stop_btn.configure(state="normal")
        self._bar.set(0)
        threading.Thread(target=fn, args=args, daemon=True).start()

    def _finish(self) -> None:
        self._busy = False
        self._find_btn.configure(state="normal")
        self._ident_btn.configure(state="normal" if self._photos else "disabled")
        self._copy_btn.configure(state="normal" if (self._entries or self._clarify) else "disabled")
        self._review_btn.configure(state="normal" if (self._entries or self._clarify) else "disabled")
        self._stop_btn.configure(state="disabled")

    def _poll(self) -> None:
        try:
            while True:
                kind, data = self._q.get_nowait()
                if kind == "status":
                    self._status.configure(text=data)
                elif kind == "found":
                    self._photos, self._places = data
                    self._entries, self._clarify = [], []
                    self._finish()
                    self._show_places()
                    self._tabs.select(0)
                    self._status.configure(text="Check the countries (red = not known: select rows and "
                                           "Set country…), then Identify species.")
                    self._load_saved(after_find=True)
                elif kind == "ident":
                    if "status" in data:
                        self._status.configure(text=data["status"])
                    else:
                        self._bar.set(data["done"] / max(1, data["total"]))
                        self._status.configure(
                            text=f"Identifying: burst {data['done']:,} / {data['total']:,} · "
                                 f"{data['species']} species certain so far · "
                                 f"{data['rate']:.1f} photos read a second · "
                                 f"about {_duration(data['left'])} left")
                elif kind == "identified":
                    self._finish()
                    res, entries, clarify = data
                    self._show_results(res, entries, clarify)
                    self._save_results()
                    self._status.configure(text="Check the list, then Copy list. (Change a country or "
                                           "'Certain at' and Identify again: instant for photos "
                                           "already read.)")
                elif kind == "progress":
                    n, total, d = data
                    self._bar.set(n / max(1, total))
                    self._status.configure(text=f"Copying {n} / {total}: {d}")
                elif kind == "copied":
                    self._finish()
                    msg = (f"{'Stopped — ' if self._stop.is_set() else ''}Copied {data['copied']} photos "
                           f"({data['bytes'] / 1e9:.1f} GB); {data['kept']} species were already in the "
                           f"list.\n\nLists: All species.csv, To clarify.csv and one per country in\n"
                           f"{self._dest.get()}")
                    if data["errors"]:
                        msg += f"\n\n{len(data['errors'])} problem(s):\n" + "\n".join(data["errors"][:8])
                    self._status.configure(text=msg.split("\n")[0])
                    mb.showinfo("Species list", msg, parent=self)
                    sl.plan_copies(self._entries, self._clarify, self._dest.get().strip(), self._jpeg.get())
                    self._save_results()
                elif kind == "error":
                    self._finish()
                    self._status.configure(text=data)
                    mb.showerror("Species list", data, parent=self)
        except queue.Empty:
            pass
        except tk.TclError:
            return
        try:
            self.after(150, self._poll)
        except tk.TclError:
            pass

    def _close(self) -> None:
        if self._busy and not mb.askyesno("Species list", "Stop and close? (What's been read so far "
                                          "is remembered.)", parent=self):
            return
        self._stop.set()
        if self._identifier is not None:
            self._identifier.close()
        self.destroy()
