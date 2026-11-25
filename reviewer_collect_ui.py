"""
The "Collect Best folders" window (see reviewer_collect for the rules).

Pick a parent folder and a collection folder → Scan (in the background) →
a preview of every file grouped by trip, with where it will go → untick, rename
trips (double-click), fix species (select rows, "Set species…") → Copy (in the
background, verified, can be stopped; running it again adds only what's new).
"""

from __future__ import annotations

import os
import queue
import threading
from collections import defaultdict
from tkinter import filedialog as fd, messagebox as mb, simpledialog, ttk
import tkinter as tk

from typing import Optional

import customtkinter as ctk

import reviewer_collect as rc
from reviewer_regions import by_iso, by_name, countries
from reviewer_storage import _user_data_dir, load_app_settings, save_app_settings

C_BG, C_SURFACE, C_BORDER = "#0d0d0d", "#161616", "#2a2a2a"
C_TEXT, C_MUTED, C_ACCENT = "#e8e8e8", "#6B7280", "#2D7A4F"
C_WARN, C_BAD = "#F59E0B", "#EF4444"
HOW_TEXT = {"confirmed": "", "name": "species from file name", "suggested": "species suggested — check",
            "unknown": "species unknown"}


class CollectDialog(ctk.CTkToplevel):
    def __init__(self, master):
        super().__init__(master)
        self.title("Collect Best folders")
        self.geometry("1150x760")
        self.configure(fg_color=C_SURFACE)
        self.transient(master)
        self._settings = load_app_settings()
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._busy = False
        self._items: list[rc.Item] = []
        self._trips: dict[str, str] = {}                 # shoot folder → trip name
        self._trip_names: dict[str, str] = {}            # your renames
        self._species: dict[str, str] = {}               # original src → your species
        self._iid: dict[str, rc.Item] = {}
        self._identifier = None
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._poll)

    # ── layout ───────────────────────────────────────────────────────────────

    def _build(self) -> None:
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=16, pady=(12, 4))
        self._parent = tk.StringVar(value=self._settings.get("collect_parent", ""))
        self._dest = tk.StringVar(value=self._settings.get("collect_dest", ""))
        for row, (label, var, tip) in enumerate((
                ("Look in", self._parent, "parent folder: every Best folder under it is found"),
                ("Collect into", self._dest, "the collection: Year / YYYY-MM Trip / Species"))):
            ctk.CTkLabel(top, text=label, width=90, anchor="w").grid(row=row, column=0, sticky="w")
            ctk.CTkEntry(top, textvariable=var, width=640).grid(row=row, column=1, sticky="we", pady=2)
            ctk.CTkButton(top, text="Browse…", width=80, fg_color=C_BORDER,
                          command=lambda v=var: self._browse(v)).grid(row=row, column=2, padx=6)
            ctk.CTkLabel(top, text=tip, text_color=C_MUTED,
                         font=ctk.CTkFont("Helvetica", 10)).grid(row=row, column=3, sticky="w")
        top.grid_columnconfigure(1, weight=1)
        opts = ctk.CTkFrame(self, fg_color="transparent")
        opts.pack(fill="x", padx=16)
        self._identify = tk.BooleanVar(value=bool(self._settings.get("collect_identify", True)))
        ctk.CTkCheckBox(opts, text="Identify the species of photos the app hasn't named "
                        "(about 1 s a photo the first time; remembered after)",
                        variable=self._identify).pack(side="left")
        # home country: species recorded there rank first (a country named in a trip's
        # folders — Spain, Kenya… — is used for that trip instead)
        home = self._settings.get("collect_region")
        if home is None:
            used = [v for v in (self._settings.get("folder_regions") or {}).values() if v]
            best = max(set(used), key=used.count) if used else ""
            home = best if best and used.count(best) >= 3 else ""      # else Worldwide until chosen
        names = ["Worldwide"] + [c["name"] for c in countries()]
        c = by_iso(home) if home else None
        self._region = tk.StringVar(value=c["name"] if c else "Worldwide")
        ctk.CTkLabel(opts, text="   Home country", text_color=C_MUTED).pack(side="left")
        ttk.Combobox(opts, textvariable=self._region, values=names, width=24,
                     state="readonly").pack(side="left", padx=6)
        self._scan_btn = ctk.CTkButton(opts, text="Scan", width=100, command=self._scan)
        self._scan_btn.pack(side="right")

        self._status = ctk.CTkLabel(self, text="Choose the folders, then Scan. Nothing is copied "
                                    "until you press Copy.", anchor="w", text_color=C_MUTED)
        self._status.pack(fill="x", padx=16, pady=(6, 2))
        self._notes = ctk.CTkLabel(self, text="", anchor="w", justify="left", text_color=C_WARN,
                                   font=ctk.CTkFont("Helvetica", 10), wraplength=1100)
        self._notes.pack(fill="x", padx=16)

        style = ttk.Style(self)
        try:
            style.theme_use("clam")        # (the Windows theme ignores the dark colours)
        except tk.TclError:
            pass
        style.configure("Collect.Treeview", background=C_BG, fieldbackground=C_BG,
                        foreground=C_TEXT, rowheight=20, borderwidth=0)
        style.configure("Collect.Treeview.Heading", background=C_BORDER, foreground=C_TEXT)
        style.map("Collect.Treeview", background=[("selected", C_ACCENT)])
        box = tk.Frame(self, bg=C_BG)
        box.pack(fill="both", expand=True, padx=16, pady=6)
        self._tree = ttk.Treeview(box, columns=("ok", "kind", "dest", "note"), show="tree headings",
                                  style="Collect.Treeview", selectmode="extended")
        self._tree.heading("#0", text="Source")
        for col, title, w in (("ok", "✓", 34), ("kind", "", 70), ("dest", "→ in the collection", 470),
                              ("note", "", 230)):
            self._tree.heading(col, text=title)
            self._tree.column(col, width=w, stretch=(col == "dest"), anchor="w")
        self._tree.column("#0", width=300, stretch=False)
        for tag, colour in (("off", C_MUTED), ("guess", C_WARN), ("unknown", C_BAD), ("trip", C_TEXT)):
            self._tree.tag_configure(tag, foreground=colour)
        self._tree.tag_configure("trip", font=("Helvetica", 10, "bold"))
        sb = ttk.Scrollbar(box, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=sb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self._tree.bind("<ButtonRelease-1>", self._on_click)
        self._tree.bind("<Double-1>", self._on_double)
        self._tree.bind("<space>", lambda e: (self._toggle(list(self._tree.selection())), "break")[1])

        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", padx=16, pady=(4, 12))
        self._summary = ctk.CTkLabel(bottom, text="", anchor="w")
        self._summary.pack(side="left")
        self._copy_btn = ctk.CTkButton(bottom, text="Copy", width=120, state="disabled",
                                       command=self._copy)
        self._copy_btn.pack(side="right")
        self._stop_btn = ctk.CTkButton(bottom, text="Stop", width=80, fg_color=C_BORDER,
                                       state="disabled", command=self._stop.set)
        self._stop_btn.pack(side="right", padx=6)
        ctk.CTkButton(bottom, text="Set species…", width=110, fg_color=C_BORDER,
                      command=self._set_species).pack(side="right", padx=6)
        self._bar = ctk.CTkProgressBar(self)
        self._bar.set(0)
        ctk.CTkLabel(self, text="Click ✓ (or select rows + Space) to leave files out · double-click a "
                     "trip to rename it · select photos and use Set species… to correct a species",
                     text_color=C_MUTED, font=ctk.CTkFont("Helvetica", 10)).pack(fill="x", padx=16,
                                                                                 pady=(0, 8))

    def _browse(self, var: tk.StringVar) -> None:
        d = fd.askdirectory(parent=self, initialdir=var.get() or None)
        if d:
            var.set(os.path.normpath(d))

    # ── scan ─────────────────────────────────────────────────────────────────

    def _scan(self) -> None:
        parent, dest = self._parent.get().strip(), self._dest.get().strip()
        if not os.path.isdir(parent):
            mb.showerror("Collect", "Choose the folder to look in.", parent=self)
            return
        if not dest:
            mb.showerror("Collect", "Choose the folder to collect into.", parent=self)
            return
        if rc._within(parent, dest) and os.path.normcase(parent) != os.path.normcase(dest):
            pass                                          # collection inside the parent: excluded
        self._settings.update(collect_parent=parent, collect_dest=dest,
                              collect_identify=bool(self._identify.get()))
        save_app_settings(self._settings)
        identify = bool(self._identify.get())          # (read here: Tk only on this thread)
        c = by_name(self._region.get())
        home = c["iso"] if c else None
        self._settings["collect_region"] = home or ""
        save_app_settings(self._settings)
        if identify and self._identifier is None:
            self._identifier = rc.SpeciesIdentifier(str(_user_data_dir() / "collect_species.json"),
                                                    str(_user_data_dir()))
        self._start(self._scan_work, parent, dest, identify, dict(self._trip_names),
                    dict(self._species), home)

    def _scan_work(self, parent: str, dest: str, identify: bool, trip_names: dict,
                   species: dict, home: Optional[str]) -> None:
        try:
            items, notes, trips = rc.plan(
                parent, dest, trip_names=trip_names, species_override=species, home_region=home,
                identify=self._identifier if identify else None,
                progress=lambda m: self._q.put(("status", m)), stop=self._stop)
            if self._identifier is not None:
                self._identifier.save()
            self._q.put(("planned", (items, notes, trips)))
        except Exception as e:
            self._q.put(("error", f"Scan failed: {e}"))

    def _show_plan(self, items, notes, trips) -> None:
        self._items, self._trips = items, trips
        open_trips = {self._tree.item(i, "text") for i in self._tree.get_children()
                      if self._tree.item(i, "open")}
        self._tree.delete(*self._tree.get_children())
        self._iid.clear()
        by_trip = defaultdict(list)
        for it in items:
            by_trip[it.trip].append(it)
        folder_of = defaultdict(list)
        for folder, name in trips.items():
            folder_of[name].append(folder)
        for trip in sorted(by_trip):
            its = by_trip[trip]
            src = ", ".join(os.path.relpath(f, self._parent.get()) for f in folder_of.get(trip, []))
            node = self._tree.insert("", "end", text=f"{trip}   ({src})", open=trip in open_trips,
                                     values=("", "", f"{its[0].year}\\{rc.clean(trip)}\\…", ""), tags=("trip",))
            self._iid[node] = None
            for it in sorted(its, key=lambda i: (i.dest or "~", i.src)):
                iid = self._tree.insert(node, "end", text=os.path.relpath(it.src, self._parent.get()))
                self._iid[iid] = it
                self._draw_row(iid)
        if notes:
            shown = notes[:6] + ([f"… and {len(notes) - 6} more"] if len(notes) > 6 else [])
            self._notes.configure(text="\n".join(shown))
        else:
            self._notes.configure(text="")
        self._update_summary()
        self._copy_btn.configure(state="normal" if any(i.include for i in items) else "disabled")

    def _draw_row(self, iid: str) -> None:
        it = self._iid[iid]
        if it is None:
            return
        note = it.note or (HOW_TEXT.get(it.how, "") if it.kind == "original" or it.of else "")
        if it.kind != "original" and it.of:
            note = f"version of {os.path.basename(it.of)}" + (f" · {note}" if note else "")
        tag = "off" if not it.include else "guess" if it.how == "suggested" else \
            "unknown" if it.how == "unknown" else ""
        self._tree.item(iid, values=("✓" if it.include else "✗", it.kind, it.dest or "—", note),
                        tags=(tag,) if tag else ())

    def _update_summary(self) -> None:
        inc = [i for i in self._items if i.include]
        size = sum(i.size for i in inc) / 1e9
        skipped = len(self._items) - len(inc)
        guess = sum(1 for i in inc if i.kind == "original" and i.how == "suggested")
        unknown = sum(1 for i in inc if i.kind == "original" and i.how == "unknown")
        self._summary.configure(
            text=f"{len(inc)} files to copy ({size:.1f} GB) · {skipped} left out"
                 + (f" · {guess} species suggested (amber)" if guess else "")
                 + (f" · {unknown} Unidentified (red)" if unknown else ""))

    # ── editing the plan ─────────────────────────────────────────────────────

    def _on_click(self, e) -> None:
        if self._busy or self._tree.identify_column(e.x) != "#1":
            return
        row = self._tree.identify_row(e.y)
        if row:
            self._toggle([row])

    def _toggle(self, rows: list[str]) -> None:
        if self._busy:
            return
        targets = []
        for r in rows:
            if self._iid.get(r) is None:                      # a trip: all its files
                targets += [c for c in self._tree.get_children(r)]
            else:
                targets.append(r)
        its = [self._iid[r] for r in targets if self._iid.get(r) is not None
               and not self._iid[r].note.startswith("already")]
        if not its:
            return
        on = not all(i.include for i in its)
        for r in targets:
            it = self._iid.get(r)
            if it is not None and not it.note.startswith("already"):
                it.include = on
                self._draw_row(r)
        self._update_summary()

    def _on_double(self, e) -> None:
        row = self._tree.identify_row(e.y)
        if not row or self._busy or self._iid.get(row) is not None:
            return
        trip = self._tree.item(row, "text").split("   (")[0]
        new = simpledialog.askstring("Rename trip", "Trip folder name (YYYY-MM Place):",
                                     initialvalue=trip, parent=self)
        if not new or new.strip() == trip:
            return
        for folder, name in self._trips.items():
            if name == trip:
                self._trip_names[folder] = rc.clean(new)
        self._scan()

    def _set_species(self) -> None:
        rows = [r for r in self._tree.selection() if self._iid.get(r) is not None]
        if not rows or self._busy:
            mb.showinfo("Set species", "Select the photos first (Ctrl / Shift + click).", parent=self)
            return
        first = self._iid[rows[0]]
        new = simpledialog.askstring("Set species", f"Species for {len(rows)} photo(s):",
                                     initialvalue=first.species if first.how != "unknown" else "",
                                     parent=self)
        if not new:
            return
        for r in rows:
            it = self._iid[r]
            self._species[it.of or it.src] = new.strip()
        self._scan()

    # ── copy ─────────────────────────────────────────────────────────────────

    def _copy(self) -> None:
        inc = [i for i in self._items if i.include]
        if not inc:
            return
        size = sum(i.size for i in inc) / 1e9
        if not mb.askyesno("Copy", f"Copy {len(inc)} files ({size:.1f} GB) into\n{self._dest.get()}?\n\n"
                           "Your originals are not changed.", parent=self):
            return
        self._bar.pack(fill="x", padx=16, pady=(0, 6))
        self._start(self._copy_work, list(self._items), self._dest.get().strip())

    def _copy_work(self, items, dest) -> None:
        try:
            done = rc.run(items, dest, verify=True, stop=self._stop,
                          progress=lambda n, total, d: self._q.put(("progress", (n, total, d))))
            self._q.put(("copied", done))
        except Exception as e:
            self._q.put(("error", f"Copy failed: {e}"))

    # ── background plumbing ──────────────────────────────────────────────────

    def _start(self, fn, *args) -> None:
        self._busy = True
        self._stop.clear()
        for b in (self._scan_btn, self._copy_btn):
            b.configure(state="disabled")
        self._stop_btn.configure(state="normal")
        threading.Thread(target=fn, args=args, daemon=True).start()

    def _finish(self) -> None:
        self._busy = False
        self._scan_btn.configure(state="normal")
        self._stop_btn.configure(state="disabled")
        self._copy_btn.configure(state="normal" if any(i.include for i in self._items) else "disabled")

    def _poll(self) -> None:
        try:
            while True:
                kind, data = self._q.get_nowait()
                if kind == "status":
                    self._status.configure(text=data)
                elif kind == "planned":
                    self._finish()
                    items, notes, trips = data
                    self._show_plan(items, notes, trips)
                    self._status.configure(text=f"Found {len(trips)} trips. Check the list, then Copy.")
                elif kind == "progress":
                    n, total, d = data
                    self._bar.set(n / max(1, total))
                    self._status.configure(text=f"Copying {n} / {total}: {d}")
                elif kind == "copied":
                    self._finish()
                    stopped = self._stop.is_set()
                    msg = (f"{'Stopped — ' if stopped else ''}Copied {data['copied']} files "
                           f"({data['bytes'] / 1e9:.1f} GB) into {self._dest.get()}.")
                    if data["errors"]:
                        msg += f"\n\n{len(data['errors'])} problem(s):\n" + "\n".join(data["errors"][:8])
                    self._status.configure(text=msg.split("\n")[0])
                    mb.showinfo("Collect", msg, parent=self)
                    self._scan()                       # refresh: copied files now show as collected
                elif kind == "error":
                    self._finish()
                    self._status.configure(text=data)
                    mb.showerror("Collect", data, parent=self)
        except queue.Empty:
            pass
        except tk.TclError:
            return
        try:
            self.after(100, self._poll)
        except tk.TclError:
            pass

    def _close(self) -> None:
        if self._busy and not mb.askyesno("Collect", "Stop and close?", parent=self):
            return
        self._stop.set()
        if self._identifier is not None:
            self._identifier.save()
        self.destroy()
