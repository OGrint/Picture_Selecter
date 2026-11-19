"""
Import from a memory card: copy images into dated folders, optionally make a
second (backup) copy at the same time, verify each copy, and open the first
folder for culling while the rest is still copying.

- Two saved destinations ("Everyday", "Holiday"), each remembering its
  folder, optional backup folder, year sub-folder and grouping.
- Grouping: one folder per day, per week (Mon–Sun), per month, or one folder
  for the whole import, e.g. "2026-06-02 Minsmere",
  "2026-06-01 to 06-07 Kenya", "2026-06 Garden birds".
- Each folder gets its own label, which can be typed while the import runs;
  it is added to the folder name when that folder has finished copying.
- Files already imported are skipped (remembered by name, size and capture
  time, so changing the grouping doesn't copy a card twice). Nothing on the
  card is changed or deleted.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import os
import queue
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk
import tkinter as tk
import tkinter.filedialog as fd
import tkinter.messagebox as mb
from PIL import ImageTk

from reviewer_imaging import SUPPORTED_EXTS, _capture_timestamp, load_small_thumb
from reviewer_storage import _load_user_json, _save_user_json

log = logging.getLogger("photo_reviewer")

CHUNK = 4 * 1024 * 1024
IMPORT_SETTINGS = "import_settings.json"
LEDGER_FILE = "imported.json"
GROUPINGS = {"day": "Per day", "week": "Per week", "month": "Per month", "one": "One folder"}
PRESET_NAMES = ("Everyday", "Holiday")
_BAD_CHARS = re.compile(r'[<>:"/\\|?*]')


# ── Card scanning and grouping (no Tk) ───────────────────────────────────────

def find_card_images(source: str) -> list[str]:
    """All supported images under source (e.g. DCIM/100NIKON, 101NIKON…)."""
    out = []
    for root, _dirs, files in os.walk(source):
        for f in files:
            if Path(f).suffix.lower() in SUPPORTED_EXTS:
                out.append(os.path.join(root, f))
    return sorted(out)


def capture_time(path: str) -> float:
    """Capture time (file time if EXIF has none)."""
    return _capture_timestamp(path) or os.path.getmtime(path)


def capture_day(path: str) -> str:
    """YYYY-MM-DD from the capture time."""
    return datetime.datetime.fromtimestamp(capture_time(path)).strftime("%Y-%m-%d")


def read_card(files: list[str], progress: Optional[Callable[[int], None]] = None
              ) -> list[tuple[str, int, float]]:
    """(path, size, capture time) for each file, read in parallel."""
    def one(p):
        return p, os.path.getsize(p), capture_time(p)
    out = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for i, item in enumerate(ex.map(one, files)):
            out.append(item)
            if progress and i % 50 == 0:
                progress(i)
    return out


def _group_key(day: datetime.date, mode: str) -> str:
    if mode == "week":
        y, w, _ = day.isocalendar()
        return f"{y}-W{w:02d}"
    if mode == "month":
        return f"{day:%Y-%m}"
    if mode == "one":
        return "all"
    return f"{day:%Y-%m-%d}"


def folder_base(days: list[datetime.date], mode: str) -> str:
    """Date part of a folder name: 2026-06-02 · 2026-06-01 to 06-07 · 2026-06."""
    first, last = min(days), max(days)
    if mode == "month":
        return f"{first:%Y-%m}"
    if first == last:
        return f"{first:%Y-%m-%d}"
    if first.year == last.year:
        return f"{first:%Y-%m-%d} to {last:%m-%d}"
    return f"{first:%Y-%m-%d} to {last:%Y-%m-%d}"


def clean_label(label: str) -> str:
    return re.sub(r"\s+", " ", _BAD_CHARS.sub("-", label or "")).strip().rstrip(". ")


def folder_name(base: str, label: str) -> str:
    label = clean_label(label)
    return f"{base} {label}" if label else base


def group_imports(items: list[tuple[str, int, float]], mode: str) -> list[dict]:
    """Files grouped into folders, in time order."""
    groups: dict[str, dict] = {}
    for path, size, ts in sorted(items, key=lambda x: (x[2], x[0])):
        day = datetime.date.fromtimestamp(ts)
        g = groups.setdefault(_group_key(day, mode), {"files": [], "days": set()})
        g["files"].append((path, size, ts))
        g["days"].add(day)
    out = []
    for key, g in groups.items():
        days = sorted(g["days"])
        out.append({"key": key, "days": days, "files": g["files"],
                    "base": folder_base(days, mode)})
    return sorted(out, key=lambda g: g["days"][0])


def import_root(dest: str, year_folder: bool, days: list[datetime.date]) -> str:
    return os.path.join(dest, str(days[0].year)) if year_folder else dest


def existing_label(root: str, base: str) -> str:
    """Label of an earlier import's folder for the same dates ("" if none / several)."""
    try:
        names = [n for n in os.listdir(root) if os.path.isdir(os.path.join(root, n))]
    except OSError:
        return ""
    found = [n[len(base):].strip() for n in names if n == base or n.startswith(base + " ")]
    labelled = [f for f in found if f]
    return labelled[0] if len(labelled) == 1 and len(found) == 1 else ""


def _in_folder(path: str, folder: str) -> bool:
    p, f = os.path.normcase(os.path.normpath(path)), os.path.normcase(os.path.normpath(folder))
    return p.startswith(f + os.sep)


class Ledger:
    """Card files already imported: name|size|capture time → copies made."""

    def __init__(self):
        data = _load_user_json(LEDGER_FILE)
        self.data: dict[str, list[str]] = data if isinstance(data, dict) else {}

    @staticmethod
    def key(path: str, size: int, ts: float) -> str:
        return f"{Path(path).name.lower()}|{size}|{int(ts)}"

    def find(self, key: str, root: str, size: int) -> Optional[str]:
        """An existing copy under root, if any."""
        for p in self.data.get(key, []):
            try:
                if _in_folder(p, root) and os.path.getsize(p) == size:
                    return p
            except OSError:
                continue
        return None

    def add(self, key: str, path: str) -> None:
        paths = [p for p in self.data.get(key, []) if p != path]
        self.data[key] = paths + [path]

    def save(self) -> None:
        _save_user_json(LEDGER_FILE, self.data)


def _copy_verified(src: str, dests: list[str], verify: bool) -> None:
    """Copy src to every dest in one read; optionally re-read to verify."""
    for d in dests:
        os.makedirs(os.path.dirname(d), exist_ok=True)
    tmp = [d + ".part" for d in dests]
    h = hashlib.blake2b(digest_size=16)
    outs = [open(t, "wb") for t in tmp]
    try:
        with open(src, "rb") as f:
            while chunk := f.read(CHUNK):
                h.update(chunk)
                for o in outs:
                    o.write(chunk)
    finally:
        for o in outs:
            o.close()
    for t, d in zip(tmp, dests):
        if verify:
            h2 = hashlib.blake2b(digest_size=16)
            with open(t, "rb") as f:
                while chunk := f.read(CHUNK):
                    h2.update(chunk)
            if h2.digest() != h.digest():
                os.remove(t)
                raise IOError(f"verification failed for {d}")
        st = os.stat(src)
        os.utime(t, (st.st_atime, st.st_mtime))       # keep the original times
        os.replace(t, d)


def _target(folder: str, name: str, size: int) -> tuple[str, bool]:
    """(path, already_there) for a file in folder; renames on a name clash."""
    dest = os.path.join(folder, name)
    stem, ext = os.path.splitext(name)
    n = 1
    while os.path.exists(dest):
        if os.path.getsize(dest) == size:
            return dest, True                    # imported before
        dest = os.path.join(folder, f"{stem}_{n}{ext}")
        n += 1
    return dest, False


def _destination(root: str, day: str, label: str, name: str, size: int) -> tuple[str, bool]:
    """Older per-day layout (kept for callers of the previous version)."""
    folder = os.path.join(root, folder_name(day, label))
    os.makedirs(folder, exist_ok=True)
    return _target(folder, name, size)


def move_folder(src: str, dst: str) -> tuple[str, dict[str, str]]:
    """
    Rename folder src to dst; if dst already exists (an earlier import with
    that label) move the files into it. Returns (final folder, renamed files).
    """
    if not os.path.exists(dst):
        os.rename(src, dst)
        return dst, {}
    renamed = {}
    for name in sorted(os.listdir(src)):
        s = os.path.join(src, name)
        if not os.path.isfile(s):
            continue
        d, same = _target(dst, name, os.path.getsize(s))
        if same:
            os.remove(s)                         # identical copy already there
        else:
            os.replace(s, d)
        if os.path.basename(d) != name:
            renamed[name] = os.path.basename(d)
    try:
        os.rmdir(src)
    except OSError:
        pass
    return dst, renamed


# ── Settings ─────────────────────────────────────────────────────────────────

def load_import_settings(app_settings: Optional[dict] = None) -> dict:
    """Saved destinations etc. (migrated from the older single destination)."""
    data = _load_user_json(IMPORT_SETTINGS)
    if not isinstance(data, dict):
        data = {}
    s = app_settings or {}
    presets = data.get("presets") if isinstance(data.get("presets"), dict) else {}
    defaults = {
        # an existing destination keeps its layout (no year folder added)
        "Everyday": {"dest": s.get("import_dest", ""), "backup": s.get("import_backup", ""),
                     "year_folder": not s.get("import_dest"), "group": "day"},
        "Holiday": {"dest": "", "backup": "", "year_folder": False, "group": "week"},
    }
    for name in PRESET_NAMES:
        p = presets.get(name) if isinstance(presets.get(name), dict) else {}
        presets[name] = {**defaults[name], **p}
        if presets[name]["group"] not in GROUPINGS:
            presets[name]["group"] = "day"
    data["presets"] = presets
    data.setdefault("preset", "Everyday")
    if data["preset"] not in PRESET_NAMES:
        data["preset"] = "Everyday"
    data.setdefault("verify", bool(s.get("import_verify", True)))
    data.setdefault("open_early", True)
    return data


def save_import_settings(data: dict) -> None:
    _save_user_json(IMPORT_SETTINGS, data)


# ── Dialog ───────────────────────────────────────────────────────────────────

C_MUTED, C_OK, C_WARN = "#6B7280", "#22C55E", "#F59E0B"


class ImportDialog(ctk.CTkToplevel):
    """Import settings, then per-folder progress with labels, time left and Cancel."""

    _active: set[str] = set()                 # folders being written right now

    @classmethod
    def is_importing(cls, folder: str) -> bool:
        """True while an import is writing into folder (or a folder inside it)."""
        f = os.path.normcase(os.path.normpath(folder))
        active = list(cls._active)               # snapshot (the copy thread updates it)
        return any(a == f or a.startswith(f + os.sep) for a in active)

    def __init__(self, master, settings: dict, on_done: Callable[[Optional[str]], None],
                 on_folder_ready: Optional[Callable[[str], None]] = None):
        super().__init__(master)
        self.title("Import from card")
        self.geometry("720x560")
        self.configure(fg_color="#141414")
        self.transient(master)
        self.grab_set()
        self._master = master
        self._cfg = load_import_settings(settings)
        self._on_done = on_done
        self._on_ready = on_folder_ready
        self._opened: Optional[str] = None     # folder opened while importing
        self._cancel = threading.Event()
        self._q: queue.Queue = queue.Queue()
        self._folders: list[str] = []
        self._labels: dict[int, str] = {}      # group → label (read by the copy thread)
        self._rows: dict[int, dict] = {}
        self._thumbs: dict[int, ImageTk.PhotoImage] = {}

        self._src  = tk.StringVar(value=self._guess_card())
        self._preset = tk.StringVar(value=self._cfg["preset"])
        self._dst  = tk.StringVar()
        self._bak  = tk.StringVar()
        self._year = tk.BooleanVar()
        self._group = tk.StringVar()
        self._ver  = tk.BooleanVar(value=self._cfg["verify"])
        self._early = tk.BooleanVar(value=self._cfg["open_early"])
        self._load_preset(self._preset.get())
        self._build_form()
        self.protocol("WM_DELETE_WINDOW", self._close)

    @staticmethod
    def _guess_card() -> str:
        """First removable drive with a DCIM folder, if any (Windows)."""
        try:
            import ctypes
            import string
            for letter in string.ascii_uppercase:
                root = f"{letter}:\\"
                if ctypes.windll.kernel32.GetDriveTypeW(root) == 2 and \
                        os.path.isdir(os.path.join(root, "DCIM")):
                    return os.path.join(root, "DCIM")
        except Exception:
            pass
        return ""

    # ── Presets ──────────────────────────────────────────────────────────────

    def _load_preset(self, name: str) -> None:
        p = self._cfg["presets"][name]
        self._dst.set(p["dest"])
        self._bak.set(p["backup"])
        self._year.set(bool(p["year_folder"]))
        self._group.set(GROUPINGS[p["group"]])

    def _store_preset(self, name: str) -> None:
        mode = next(k for k, v in GROUPINGS.items() if v == self._group.get())
        self._cfg["presets"][name] = {"dest": self._dst.get().strip(),
                                      "backup": self._bak.get().strip(),
                                      "year_folder": bool(self._year.get()), "group": mode}

    def _switch_preset(self, name: str) -> None:
        self._store_preset(self._cfg["preset"])
        self._cfg["preset"] = name
        self._load_preset(name)
        self._update_example()

    # ── Form ─────────────────────────────────────────────────────────────────

    def _row(self, parent, label: str, var: tk.StringVar, clear: bool = False) -> None:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=4)
        ctk.CTkLabel(row, text=label, width=150, anchor="w").pack(side="left")
        ctk.CTkEntry(row, textvariable=var).pack(side="left", fill="x", expand=True)
        ctk.CTkButton(row, text="…", width=32,
                      command=lambda: self._browse(var)).pack(side="left", padx=(4, 0))
        if clear:
            ctk.CTkButton(row, text="✕", width=28, fg_color="#333333",
                          command=lambda: var.set("")).pack(side="left", padx=(4, 0))

    def _browse(self, var: tk.StringVar) -> None:
        d = fd.askdirectory(parent=self, initialdir=var.get() or None)
        if d:
            var.set(str(Path(d)))

    def _build_form(self) -> None:
        self._form = ctk.CTkFrame(self, fg_color="transparent")
        self._form.pack(fill="both", expand=True)
        ctk.CTkLabel(self._form, text="Import from card",
                     font=ctk.CTkFont("Helvetica", 15, "bold")).pack(pady=(14, 6))
        top = ctk.CTkFrame(self._form, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(0, 6))
        ctk.CTkLabel(top, text="Destination", width=150, anchor="w").pack(side="left")
        ctk.CTkSegmentedButton(top, values=list(PRESET_NAMES), variable=self._preset,
                               command=self._switch_preset).pack(side="left")
        self._row(self._form, "Card folder", self._src)
        self._row(self._form, "Copy to", self._dst)
        ctk.CTkCheckBox(self._form, text="Put folders inside a year folder (…\\2026\\…)",
                        variable=self._year, command=self._update_example
                        ).pack(padx=(170, 20), pady=(0, 4), anchor="w")
        self._row(self._form, "Backup copy to (optional)", self._bak, clear=True)
        grp = ctk.CTkFrame(self._form, fg_color="transparent")
        grp.pack(fill="x", padx=20, pady=6)
        ctk.CTkLabel(grp, text="Group into", width=150, anchor="w").pack(side="left")
        ctk.CTkSegmentedButton(grp, values=list(GROUPINGS.values()), variable=self._group,
                               command=lambda _v: self._update_example()).pack(side="left")
        self._example = ctk.CTkLabel(self._form, text="", text_color=C_MUTED, justify="left",
                                     font=ctk.CTkFont("Helvetica", 10), anchor="w")
        self._example.pack(padx=20, anchor="w", fill="x")
        for v in (self._dst,):
            v.trace_add("write", lambda *_a: self._update_example())
        ctk.CTkLabel(self._form, text="You can type a label for each folder once the import "
                     "starts. The backup is a second copy (e.g. on your external drive) — "
                     "leave it empty to make just one copy.",
                     text_color=C_MUTED, font=ctk.CTkFont("Helvetica", 10),
                     wraplength=660, justify="left").pack(padx=20, pady=(6, 0), anchor="w")
        ctk.CTkCheckBox(self._form, text="Verify every copy (slower, safest)",
                        variable=self._ver).pack(padx=20, pady=(10, 4), anchor="w")
        ctk.CTkCheckBox(self._form, text="Open the first folder for culling while the rest copies",
                        variable=self._early).pack(padx=20, pady=4, anchor="w")
        ctk.CTkButton(self._form, text="Start import", height=38,
                      command=self._start).pack(fill="x", padx=20, pady=(10, 16))
        self._update_example()

    def _update_example(self) -> None:
        if not hasattr(self, "_example"):
            return
        mode = next((k for k, v in GROUPINGS.items() if v == self._group.get()), "day")
        eg = {"day": "2026-06-02 Masai Mara", "week": "2026-06-01 to 06-07 Kenya",
              "month": "2026-06 Garden birds", "one": "2026-06-01 to 06-14 Kenya"}[mode]
        dest = self._dst.get().strip() or "<Copy to>"
        path = os.path.join(dest, "2026", eg) if self._year.get() else os.path.join(dest, eg)
        self._example.configure(text=f"Folders will look like:  {path}")

    # ── Run ──────────────────────────────────────────────────────────────────

    def _start(self) -> None:
        src, dst, bak = self._src.get().strip(), self._dst.get().strip(), self._bak.get().strip()
        if not os.path.isdir(src):
            mb.showerror("Import", "Choose the card folder (e.g. the DCIM folder).", parent=self)
            return
        if not dst:
            mb.showerror("Import", "Choose where to copy the images to.", parent=self)
            return
        for d in [dst] + ([bak] if bak else []):
            if os.path.abspath(d).startswith(os.path.abspath(src)):
                mb.showerror("Import", "The destination can't be on the card folder.", parent=self)
                return
        if bak and os.path.normcase(os.path.abspath(bak)) == os.path.normcase(os.path.abspath(dst)):
            mb.showerror("Import", "The backup must be a different folder (ideally another drive).",
                         parent=self)
            return
        files = find_card_images(src)
        if not files:
            mb.showinfo("Import", "No supported images found on the card.", parent=self)
            return
        self._store_preset(self._cfg["preset"])
        self._cfg.update(verify=bool(self._ver.get()), open_early=bool(self._early.get()))
        save_import_settings(self._cfg)
        p = self._cfg["presets"][self._cfg["preset"]]
        self._form.destroy()
        self._build_progress(len(files))
        threading.Thread(target=self._work, daemon=True,
                         args=(files, p["dest"], p["year_folder"], p["backup"] or None,
                               p["group"], bool(self._ver.get()))).start()
        self.after(100, self._poll)

    def _build_progress(self, total: int) -> None:
        self._total = total
        self._start_t = None
        self._done = self._copied = self._skipped = 0
        self._errors: list[str] = []
        f = ctk.CTkFrame(self, fg_color="transparent")
        f.pack(fill="both", expand=True, padx=20, pady=12)
        self._p_title = ctk.CTkLabel(f, text="Reading capture dates…",
                                     font=ctk.CTkFont("Helvetica", 15, "bold"))
        self._p_title.pack(pady=(4, 4))
        self._p_lbl = ctk.CTkLabel(f, text="")
        self._p_lbl.pack()
        self._p_bar = ctk.CTkProgressBar(f, height=8)
        self._p_bar.set(0)
        self._p_bar.pack(fill="x", pady=8)
        self._p_eta = ctk.CTkLabel(f, text="", text_color=C_MUTED)
        self._p_eta.pack()
        ctk.CTkLabel(f, text="Type a label for each folder — it's added to the folder name "
                     "when that folder has finished copying. Finished folders can be renamed "
                     "later from the folder list in the reviewer (✎ Rename).",
                     text_color=C_MUTED, font=ctk.CTkFont("Helvetica", 10),
                     wraplength=660, justify="left").pack(pady=(6, 4), anchor="w")
        self._table = ctk.CTkScrollableFrame(f, fg_color="#1b1b1b")
        self._table.pack(fill="both", expand=True)
        self._p_btn = ctk.CTkButton(f, text="Cancel", width=100, command=self._cancel.set)
        self._p_btn.pack(pady=8)

    def _add_rows(self, plan: list[dict]) -> None:
        for gi, g in enumerate(plan):
            row = ctk.CTkFrame(self._table, fg_color="#222222")
            row.pack(fill="x", padx=4, pady=3)
            thumb = tk.Label(row, bg="#222222", width=12, height=4, text="…", fg=C_MUTED)
            thumb.pack(side="left", padx=6, pady=4)
            info = ctk.CTkFrame(row, fg_color="transparent")
            info.pack(side="left", padx=4)
            ctk.CTkLabel(info, text=g["base"], anchor="w",
                         font=ctk.CTkFont("Helvetica", 12, "bold")).pack(anchor="w")
            ctk.CTkLabel(info, text=f"{len(g['files'])} photos", anchor="w",
                         text_color=C_MUTED, font=ctk.CTkFont("Helvetica", 10)).pack(anchor="w")
            var = tk.StringVar(value=self._labels.get(gi, ""))
            var.trace_add("write", lambda *_a, i=gi, v=var: self._labels.__setitem__(i, v.get()))
            entry = ctk.CTkEntry(row, textvariable=var, width=240,
                                 placeholder_text="Label, e.g. Masai Mara")
            entry.pack(side="left", padx=8)
            state = ctk.CTkLabel(row, text="waiting", text_color=C_MUTED, width=150, anchor="w")
            state.pack(side="left", padx=6)
            self._rows[gi] = {"thumb": thumb, "entry": entry, "state": state, "n": 0,
                              "total": len(g["files"])}

    def _work(self, files, dst, year_folder, bak, mode, verify) -> None:
        try:
            items = read_card(files, lambda i: self._q.put(("scan", i)))
            plan = group_imports(items, mode)
            for gi, g in enumerate(plan):                 # earlier import's label for these dates
                self._labels[gi] = existing_label(import_root(dst, year_folder, g["days"]), g["base"])
            self._q.put(("plan", plan))
            threading.Thread(target=self._thumb_worker, args=(plan,), daemon=True).start()
            ledger = Ledger()
            for gi, g in enumerate(plan):
                if self._cancel.is_set():
                    break
                self._copy_group(gi, g, dst, year_folder, bak, verify, ledger)
                ledger.save()
        except Exception as e:
            log.exception("Import failed: %s", e)
            self._q.put(("error", f"Import stopped: {e}"))
        self._q.put(("end",))

    def _copy_group(self, gi, g, dst, year_folder, bak, verify, ledger) -> None:
        roots = [import_root(dst, year_folder, g["days"])]
        if bak:
            roots.append(import_root(bak, year_folder, g["days"]))
        folders = [os.path.join(r, folder_name(g["base"], self._labels.get(gi, ""))) for r in roots]
        active = {os.path.normcase(os.path.normpath(f)) for f in folders}
        ImportDialog._active |= active
        self._q.put(("group_start", gi))
        rec: list[tuple[str, int, str]] = []            # (ledger key, root index, file name)
        found_main: Optional[str] = None                # earlier copy (all skipped case)
        try:
            for src, size, ts in g["files"]:
                if self._cancel.is_set():
                    break
                key = Ledger.key(src, size, ts)
                name = os.path.basename(src)
                targets = []
                try:
                    for ri, (root, folder) in enumerate(zip(roots, folders)):
                        prev = ledger.find(key, root, size)
                        if prev:
                            if ri == 0:
                                found_main = found_main or os.path.dirname(prev)
                            continue
                        dest, exists = _target(folder, name, size)
                        if exists:
                            rec.append((key, ri, os.path.basename(dest)))
                            continue
                        targets.append((ri, dest))
                    if targets:
                        _copy_verified(src, [d for _ri, d in targets], verify)
                        rec += [(key, ri, os.path.basename(d)) for ri, d in targets]
                    self._q.put(("file", gi, "copied" if targets else "skipped", ""))
                except Exception as e:
                    log.error("Import failed %s: %s", src, e)
                    self._q.put(("file", gi, "error", f"{name}: {e}"))
            # the label typed while copying goes into the folder name now
            label = self._labels.get(gi, "")
            finals, renames = [], []
            for root, folder in zip(roots, folders):
                want = os.path.join(root, folder_name(g["base"], label))
                final, ren = folder, {}
                if os.path.isdir(folder) and os.path.normcase(want) != os.path.normcase(folder):
                    try:
                        final, ren = move_folder(folder, want)
                    except OSError as e:
                        log.error("Could not rename %s: %s", folder, e)
                        self._q.put(("error", f"Couldn't add the label to {Path(folder).name}: {e}"))
                finals.append(final)
                renames.append(ren)
            for key, ri, fname in rec:
                ledger.add(key, os.path.join(finals[ri], renames[ri].get(fname, fname)))
            main = finals[0] if os.path.isdir(finals[0]) else found_main
            self._q.put(("group_done", gi, main))
        finally:
            ImportDialog._active -= active

    def _thumb_worker(self, plan) -> None:
        for gi, g in enumerate(plan):
            try:
                img = load_small_thumb(g["files"][0][0], 120)
                img.thumbnail((96, 64))
                self._q.put(("thumb", gi, img))
            except Exception as e:
                log.debug("Import thumbnail failed: %s", e)

    def _poll(self) -> None:
        if not self.winfo_exists():
            return
        finished = False
        while True:
            try:
                msg = self._q.get_nowait()
            except queue.Empty:
                break
            kind = msg[0]
            if kind == "end":
                finished = True
                break
            if kind == "scan":
                self._p_lbl.configure(text=f"{msg[1]} / {self._total} images")
                self._p_bar.set(msg[1] / max(1, self._total))
            elif kind == "plan":
                self._p_title.configure(text="Importing…")
                self._start_t = time.monotonic()
                self._add_rows(msg[1])
            elif kind == "thumb":
                gi, img = msg[1], msg[2]
                if gi in self._rows:
                    photo = self._thumbs[gi] = ImageTk.PhotoImage(img)
                    self._rows[gi]["thumb"].configure(image=photo, text="", width=96, height=64)
            elif kind == "group_start":
                self._rows[msg[1]]["state"].configure(text="copying…", text_color=C_WARN)
            elif kind == "file":
                gi, what, err = msg[1], msg[2], msg[3]
                self._done += 1
                r = self._rows[gi]
                r["n"] += 1
                r["state"].configure(text=f"copying {r['n']}/{r['total']}")
                if what == "copied":
                    self._copied += 1
                elif what == "skipped":
                    self._skipped += 1
                else:
                    self._errors.append(err)
            elif kind == "group_done":
                self._group_done(msg[1], msg[2])
            elif kind == "error":
                self._errors.append(msg[1])
        if self._start_t is not None:
            self._p_bar.set(self._done / max(1, self._total))
            self._p_lbl.configure(text=f"{self._done} / {self._total} images")
            elapsed = time.monotonic() - self._start_t
            if 0 < self._done < self._total and elapsed > 2:
                left = elapsed / self._done * (self._total - self._done)
                m, s = divmod(int(left), 60)
                self._p_eta.configure(text=f"about {m} min {s:02d} s left" if m else f"about {s} s left")
        if finished:
            self._finish()
        else:
            self.after(150, self._poll)

    def _group_done(self, gi: int, folder: Optional[str]) -> None:
        r = self._rows[gi]
        cancelled = self._cancel.is_set() and r["n"] < r["total"]
        r["state"].configure(text="stopped" if cancelled else "✓ copied",
                             text_color=C_WARN if cancelled else C_OK)
        r["entry"].configure(state="disabled")         # rename later from the folder list
        if folder and folder not in self._folders:
            self._folders.append(folder)
        if folder and self._opened is None and self._early_open_ok():
            self._open_now(folder)

    def _early_open_ok(self) -> bool:
        return bool(self._cfg.get("open_early")) and self._on_ready is not None \
            and not self._cancel.is_set() and self._done < self._total

    def _open_now(self, folder: str) -> None:
        """Open a finished folder in the reviewer while the import carries on."""
        self._opened = folder
        try:
            self.grab_release()
            self.tk.call("wm", "transient", self._w, "")    # stay visible over the reviewer
        except tk.TclError:
            pass
        self._on_ready(folder)
        self.after(1500, self._attach_to_reviewer)

    def _attach_to_reviewer(self) -> None:
        """Keep this window above the reviewer once it has opened."""
        try:
            for w in self._master.winfo_children():
                if hasattr(w, "base_dir") and w.winfo_exists():
                    self.transient(w)
                    break
            self.lift()
        except tk.TclError:
            pass

    def _finish(self) -> None:
        folders = self._folders
        text = (f"Copied {self._copied}, already imported {self._skipped}"
                + (f", failed {len(self._errors)}" if self._errors else "")
                + (" (cancelled)" if self._cancel.is_set() else "") + ".")
        if self._errors:
            text += "\n\n" + "\n".join(self._errors[:10])
        if folders:
            text += "\n\nImported into:\n" + "\n".join(f"  {f}" for f in folders)
            if len(folders) > 1 and not self._opened:
                text += "\n\nThe first folder will open; use the folder panel for the others."
        mb.showinfo("Import finished", text, parent=self)
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
        self._on_done(None if self._opened else (folders[0] if folders else None))

    def _close(self) -> None:
        if hasattr(self, "_p_btn"):
            self._cancel.set()                  # finish the current file, then stop
            return
        self._store_preset(self._cfg["preset"])
        save_import_settings(self._cfg)
        self.grab_release()
        self.destroy()
        self._on_done(None)
