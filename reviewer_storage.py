"""
Persistence for Photo Reviewer: session file, hash / score caches, XMP
sidecars, app settings and recent folders. No Tkinter here.

Session files live in the image folder and keep the same names and formats
as earlier versions; new keys are only ever added.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape as xml_escape

import imagehash

from reviewer_imaging import BITMAP_EXTS, RAW_EXTS

log = logging.getLogger("photo_reviewer")

STATUS_FILENAME  = "photo_reviewer_status.json"
SCORE_CACHE_FILE = "photo_reviewer_scores.json"
HASH_CACHE_FILE  = "photo_reviewer_hashes.json"
DETECT_CACHE_FILE = "photo_reviewer_detections.json"
SETTINGS_FILE      = "photo_reviewer_settings.json"  # user prefs (Topaz path etc.)
RECENT_FILE        = "photo_reviewer_recent.json"
RECENT_MAX         = 5


# ── Persistence helpers ───────────────────────────────────────────────────────
#
# Session files live in the image folder and keep the same names and formats
# as earlier versions:
#   photo_reviewer_status.json  {abs_path: {best, delete, id, star, keywords, …}}
#   photo_reviewer_hashes.json  {abs_path: {hash, _mtime, ts?, af?}}
#   photo_reviewer_detections.json {abs_path: {_mtime, model, det}}
#   photo_reviewer_scores.json  {abs_path: {sharpness, exposure, …, _mtime}}
# New keys are only ever added (older versions ignore them).

_MISSING = object()   # sentinel: "field was not set" (for undo)


def _atomic_write_text(path: str, text: str, backup: bool = False) -> None:
    """
    Write text atomically: write a temp file beside the target, then
    os.replace() it over the target, so a crash or full disk mid-write can
    never leave a truncated file. backup=True keeps the previous version
    as <path>.bak.
    """
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    if backup and os.path.exists(path):
        try:
            shutil.copy2(path, f"{path}.bak")
        except OSError as e:
            log.warning("Could not back up %s: %s", path, e)
    os.replace(tmp, path)


def _read_json_file(path: str) -> Optional[dict]:
    """Read a JSON object from path. None if missing or unreadable."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception as e:
        log.warning("Could not read %s: %s", path, e)
        return None


def _quarantine(path: str) -> None:
    """Move an unreadable file aside so it is never overwritten."""
    dest = f"{path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
    try:
        os.replace(path, dest)
        log.warning("Unreadable file moved aside: %s", dest)
    except OSError as e:
        log.warning("Could not move aside %s: %s", path, e)


def _remap_to_files(data: dict, valid_files: set[str]) -> dict:
    """
    Match stored entries (keyed by absolute path) to the current files.
    Exact path matches win. Entries whose path no longer exists (folder
    moved or copied, drive letter changed) are matched by filename when
    that filename is unique among the current files.
    """
    out: dict = {k: v for k, v in data.items() if k in valid_files}
    by_name: dict[str, list[str]] = defaultdict(list)
    for f in valid_files:
        by_name[Path(f).name.lower()].append(f)
    for key, val in data.items():
        if key in valid_files:
            continue
        cands = by_name.get(Path(key).name.lower(), [])
        if len(cands) == 1 and cands[0] not in out:
            out[cands[0]] = val
    return out


# ── Hash cache ────────────────────────────────────────────────────────────────

def load_hash_cache(
    cache_file: str,
    valid_files: set[str],
) -> tuple[dict[str, imagehash.ImageHash], dict[str, Optional[float]], dict]:
    """
    Load average hashes, capture timestamps and camera focus areas.
    Entries are validated against file mtime so changed files are re-hashed.
    Returns (hashes, timestamps, focus). A file missing from timestamps /
    focus has not had that read yet (a None value means "none recorded").
    """
    raw = _read_json_file(cache_file) or {}
    hashes: dict[str, imagehash.ImageHash] = {}
    stamps: dict[str, Optional[float]] = {}
    focus: dict[str, Optional[tuple]] = {}
    for path, entry in _remap_to_files(raw, valid_files).items():
        try:
            if abs(entry.get("_mtime", 0) - os.path.getmtime(path)) > 1:
                continue
            hashes[path] = imagehash.hex_to_hash(entry["hash"])
            if "ts" in entry:
                stamps[path] = entry["ts"]
            if "af" in entry:
                focus[path] = tuple(entry["af"]) if entry["af"] else None
        except Exception:
            continue
    log.info("Hash cache: loaded %d / %d entries", len(hashes), len(raw))
    return hashes, stamps, focus


def save_hash_cache(
    cache_file: str,
    hashes: dict[str, imagehash.ImageHash],
    stamps: Optional[dict[str, Optional[float]]] = None,
    focus: Optional[dict[str, Optional[tuple]]] = None,
) -> None:
    """Persist hashes (+ capture times, focus areas) with mtime for freshness."""
    stamps = stamps or {}
    focus = focus or {}
    out: dict[str, dict] = {}
    for path, h in hashes.items():
        try:
            out[path] = {"hash": str(h), "_mtime": os.path.getmtime(path)}
        except OSError:
            continue
        if path in stamps:
            out[path]["ts"] = stamps[path]
        if path in focus:
            f = focus[path]
            out[path]["af"] = [round(v, 4) for v in f[:4]] + [f[4]] if f else None
    try:
        if out:
            _atomic_write_text(cache_file, json.dumps(out))
        elif os.path.exists(cache_file):
            os.remove(cache_file)
    except OSError as e:
        log.warning("Could not save hash cache: %s", e)


# ── Animal detections ────────────────────────────────────────────────────────

def load_detections(cache_file: str, valid_files: set[str],
                    model_id: str) -> dict[str, list]:
    """Cached detections made with model_id for unchanged files."""
    raw = _read_json_file(cache_file) or {}
    out: dict[str, list] = {}
    for path, entry in _remap_to_files(raw, valid_files).items():
        try:
            if entry.get("model") != model_id:
                continue
            if abs(entry.get("_mtime", 0) - os.path.getmtime(path)) > 1:
                continue
            out[path] = entry["det"]
        except Exception:
            continue
    return out


def save_detections(cache_file: str, dets: dict[str, list], model_id: str) -> None:
    out: dict[str, dict] = {}
    for path, d in dets.items():
        try:
            out[path] = {"_mtime": os.path.getmtime(path), "model": model_id, "det": d}
        except OSError:
            continue
    try:
        if out:
            _atomic_write_text(cache_file, json.dumps(out))
        elif os.path.exists(cache_file):
            os.remove(cache_file)
    except OSError as e:
        log.warning("Could not save detections: %s", e)


# ── Session status ────────────────────────────────────────────────────────────

def load_status(status_file: str, valid_files: set[str]) -> dict[str, dict]:
    """
    Load the session (tags, ratings, metadata). If the file is unreadable it
    is moved aside (never overwritten) and the .bak copy is used instead.
    """
    if not os.path.exists(status_file):
        data = _read_json_file(status_file + ".bak") or {}
    else:
        data = _read_json_file(status_file)
        if data is None:
            _quarantine(status_file)
            data = _read_json_file(status_file + ".bak") or {}
            if data:
                log.warning("Session restored from %s.bak", status_file)
    return {k: v for k, v in _remap_to_files(data, valid_files).items()
            if isinstance(v, dict)}


def session_status(folder: str, files: list[str]) -> dict[str, dict]:
    """
    Tags for a folder's images: every stored field (stars, keywords,
    captions, …) is kept; files already copied into the Best/ or ID/
    subfolders by an earlier Process are marked Best / ID.
    """
    stored = load_status(os.path.join(folder, STATUS_FILENAME), set(files))

    def folder_lower(name: str) -> set[str]:
        d = os.path.join(folder, name)
        if not os.path.isdir(d):
            return set()
        return {fn.lower() for fn in os.listdir(d)}

    best_set = folder_lower("Best")
    id_set   = folder_lower("ID")
    status: dict[str, dict] = {}
    for f in files:
        bl    = Path(f).name.lower()
        entry = dict(stored.get(f, {}))
        named = (entry.get("best_name") or "").lower()
        entry["best"]   = bool(entry.get("best")) or bl in best_set or             (bool(named) and any(n.startswith(named + ".") for n in best_set))
        entry["delete"] = bool(entry.get("delete"))
        entry["id"]     = bool(entry.get("id")) or (bl in id_set)
        status[f] = entry
    return status


def save_status(status_file: str, status: dict[str, dict],
                backup: bool = True) -> None:
    tagged = {k: v for k, v in status.items() if any(v.values())}
    try:
        if tagged:
            _atomic_write_text(status_file, json.dumps(tagged, indent=2), backup=backup)
        elif os.path.exists(status_file):
            # Nothing tagged any more — keep the last version as .bak
            os.replace(status_file, status_file + ".bak")
    except OSError as e:
        log.error("Could not save session %s: %s", status_file, e)


# ── XMP sidecars ──────────────────────────────────────────────────────────────

_NS_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_NS_XMP = "http://ns.adobe.com/xap/1.0/"
_NS_DC  = "http://purl.org/dc/elements/1.1/"
_NS_XML = "http://www.w3.org/XML/1998/namespace"
# Text unique to sidecars written by this app (they are regenerated in full;
# any other XMP is merged into instead of being replaced).
_XMP_OWN_SIGNATURE = "xmlns:lr='http://ns.adobe.com/lightroom/1.0/'>"
_XMP_OWN_LABELS = ("Pick", "Reject")


def _xmp_path_for(image_path: str) -> Optional[Path]:
    """
    Sidecar path for an image, or None if it must not get one: a JPEG/TIFF
    with a RAW of the same name (IMG_1.NEF + IMG_1.JPG) — the RAW owns
    IMG_1.xmp, and Lightroom reads JPEG metadata from the file itself.
    """
    p = Path(image_path)
    if p.suffix.lower() in BITMAP_EXTS:
        for ext in RAW_EXTS:
            if p.with_suffix(ext).exists() or p.with_suffix(ext.upper()).exists():
                return None
    return p.with_suffix(".xmp")


def _xmp_label(tags: dict) -> str:
    return ("Reject" if tags.get("delete")
            else "Pick" if tags.get("best")
            else "")


def _subject_keywords(tags: dict) -> list[str]:
    """Keywords for dc:subject — your keywords plus the species name."""
    kws = [str(k) for k in tags.get("keywords", [])]
    species = (tags.get("species") or "").strip()
    if species and species not in kws:
        kws.append(species)
    return kws


def _render_xmp(tags: dict) -> str:
    """A complete sidecar in this app's own format (values XML-escaped)."""
    esc = xml_escape
    kw_xml = "".join(f"      <rdf:li>{esc(str(k))}</rdf:li>\n"
                     for k in _subject_keywords(tags))
    return f"""<?xpacket begin='\ufeff' id='W5M0MpCehiHzreSzNTczkc9d'?>
<x:xmpmeta xmlns:x='adobe:ns:meta/'>
 <rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>
  <rdf:Description rdf:about=''
    xmlns:xmp='http://ns.adobe.com/xap/1.0/'
    xmlns:dc='http://purl.org/dc/elements/1.1/'
    xmlns:lr='http://ns.adobe.com/lightroom/1.0/'>
   <xmp:Rating>{int(tags.get("star", 0) or 0)}</xmp:Rating>
   <xmp:Label>{_xmp_label(tags)}</xmp:Label>
   <dc:rights><rdf:Alt><rdf:li xml:lang='x-default'>{esc(tags.get("copyright", ""))}</rdf:li></rdf:Alt></dc:rights>
   <dc:description><rdf:Alt><rdf:li xml:lang='x-default'>{esc(tags.get("caption", ""))}</rdf:li></rdf:Alt></dc:description>
   <dc:subject>
    <rdf:Bag>
{kw_xml}    </rdf:Bag>
   </dc:subject>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end='w'?>"""


def _merge_xmp(text: str, tags: dict) -> Optional[str]:
    """
    Update a sidecar written by another program (e.g. Lightroom, Capture One)
    without losing its other contents (develop settings etc.):
      - Pick/Reject label: set, or cleared only if it was Pick/Reject
      - rating, caption, copyright: replaced only if set in this app
      - keywords: this app's keywords are added to the existing ones
    Returns None if the file can't be parsed (it is then left untouched).
    """
    try:
        for _, (prefix, uri) in ET.iterparse(io.StringIO(text), events=("start-ns",)):
            if prefix:
                try:
                    ET.register_namespace(prefix, uri)
                except ValueError:
                    pass
        root = ET.fromstring(text)
    except Exception:
        return None
    descs = root.findall(f".//{{{_NS_RDF}}}Description")
    if not descs:
        return None

    def q(ns: str, name: str) -> str:
        return f"{{{ns}}}{name}"

    def current(name: str, ns: str = _NS_XMP) -> Optional[str]:
        for d in descs:
            if q(ns, name) in d.attrib:
                return d.attrib[q(ns, name)]
            el = d.find(q(ns, name))
            if el is not None:
                return el.text or ""
        return None

    def drop(name: str, ns: str) -> None:
        for d in descs:
            d.attrib.pop(q(ns, name), None)
            for el in d.findall(q(ns, name)):
                d.remove(el)

    target = descs[0]

    def set_simple(name: str, value: str) -> None:
        drop(name, _NS_XMP)
        ET.SubElement(target, q(_NS_XMP, name)).text = value

    def set_alt(name: str, value: str) -> None:
        drop(name, _NS_DC)
        alt = ET.SubElement(ET.SubElement(target, q(_NS_DC, name)), q(_NS_RDF, "Alt"))
        li = ET.SubElement(alt, q(_NS_RDF, "li"))
        li.set(q(_NS_XML, "lang"), "x-default")
        li.text = value

    label = _xmp_label(tags)
    if label:
        set_simple("Label", label)
    elif current("Label") in _XMP_OWN_LABELS:
        drop("Label", _NS_XMP)
    if "star" in tags:
        set_simple("Rating", str(int(tags.get("star") or 0)))
    if "caption" in tags:
        set_alt("description", tags["caption"])
    if "copyright" in tags:
        set_alt("rights", tags["copyright"])
    if _subject_keywords(tags):
        existing: list[str] = []
        for d in descs:
            for li in d.findall(f"{q(_NS_DC, 'subject')}/{q(_NS_RDF, 'Bag')}/{q(_NS_RDF, 'li')}"):
                if li.text and li.text not in existing:
                    existing.append(li.text)
        merged = existing + [k for k in _subject_keywords(tags) if k not in existing]
        drop("subject", _NS_DC)
        bag = ET.SubElement(ET.SubElement(target, q(_NS_DC, "subject")), q(_NS_RDF, "Bag"))
        for k in merged:
            ET.SubElement(bag, q(_NS_RDF, "li")).text = k

    body = ET.tostring(root, encoding="unicode")
    return ("<?xpacket begin='\ufeff' id='W5M0MpCehiHzreSzNTczkc9d'?>\n"
            + body + "\n<?xpacket end='w'?>")


def write_xmp_sidecar(image_path: str, tags: dict) -> None:
    """
    Write Lightroom-readable metadata (Pick/Reject label, star rating,
    keywords, caption, copyright) to the image's .xmp sidecar.
    Sidecars written by other programs are merged into, never replaced.
    """
    xmp_p = _xmp_path_for(image_path)
    if xmp_p is None:
        return
    try:
        if xmp_p.exists():
            existing = xmp_p.read_text(encoding="utf-8", errors="replace")
            if _XMP_OWN_SIGNATURE not in existing:
                merged = _merge_xmp(existing, tags)
                if merged is None:
                    log.warning("Existing XMP could not be parsed; left untouched: %s", xmp_p)
                    return
                _atomic_write_text(str(xmp_p), merged)
                return
        _atomic_write_text(str(xmp_p), _render_xmp(tags))
    except Exception as e:
        log.warning("XMP write failed %s: %s", image_path, e)


def is_removable_drive(path: str) -> bool:
    """Return True if path is on a removable drive (Windows/Linux/macOS)."""
    try:
        if sys.platform == "win32":
            import ctypes as _ctypes
            drive = Path(path).anchor  # e.g. "D:\\"
            DRIVE_REMOVABLE = 2
            return _ctypes.windll.kernel32.GetDriveTypeW(drive) == DRIVE_REMOVABLE
        else:
            # Linux/Mac: check if under /media/, /mnt/, /Volumes/
            abs_path = str(Path(path).resolve())
            return any(abs_path.startswith(p)
                       for p in ("/media/", "/mnt/", "/Volumes/", "/run/media/"))
    except Exception:
        return False


# ── App settings / recent folders ─────────────────────────────────────────────
#
# Stored per user in %APPDATA%\PhotoReviewer (always writable, even when the
# app is installed under Program Files). Files saved next to the program by
# earlier versions are still read if no new copy exists yet.

def _app_dir() -> Path:
    """Folder holding the program (the .exe folder in a built app)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def _resource_path(name: str) -> Path:
    """Locate a bundled support file (icon etc.)."""
    for d in (_app_dir(), Path(__file__).parent):
        if (d / name).exists():
            return d / name
    return _app_dir() / name


def _user_data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "PhotoReviewer"


def _load_user_json(name: str):
    for p in (_user_data_dir() / name, _app_dir() / name, Path(__file__).parent / name):
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning("Could not read %s: %s", p, e)
    return None


def _save_user_json(name: str, data) -> None:
    try:
        d = _user_data_dir()
        d.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(str(d / name), json.dumps(data, indent=2))
    except Exception as e:
        log.warning("Could not save %s: %s", name, e)


def load_app_settings() -> dict:
    """Load persistent app settings (Topaz path, keywords, etc.)."""
    data = _load_user_json(SETTINGS_FILE)
    return data if isinstance(data, dict) else {}


def save_app_settings(settings: dict) -> None:
    _save_user_json(SETTINGS_FILE, settings)


# ── Score cache ───────────────────────────────────────────────────────────────

def load_score_cache(
    cache_file: str,
    valid_files: set[str],
) -> dict[str, dict[str, int]]:
    """
    Load previously computed quality scores from disk.
    Each entry is keyed by absolute file path and stores a dict of metric→score
    plus a "_mtime" field used to invalidate stale entries (file was modified).
    """
    raw = _read_json_file(cache_file) or {}
    result: dict[str, dict[str, int]] = {}
    for path, entry in _remap_to_files(raw, valid_files).items():
        try:
            if abs(entry.get("_mtime", 0) - os.path.getmtime(path)) > 1:
                continue
            result[path] = {k: v for k, v in entry.items() if not k.startswith("_")}
        except (OSError, AttributeError):
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
    try:
        if out:
            _atomic_write_text(cache_file, json.dumps(out))   # compact
        elif os.path.exists(cache_file):
            os.remove(cache_file)
    except OSError as e:
        log.warning("Could not save score cache: %s", e)


def is_reviewed(tags: dict) -> bool:
    """An image counts as reviewed once it has a tag or a star rating."""
    return bool(tags.get("best") or tags.get("delete")
                or tags.get("id") or tags.get("star"))



def _load_recent_folders() -> list[str]:
    """Load recently opened folders."""
    data = _load_user_json(RECENT_FILE)
    if not isinstance(data, list):
        return []
    return [f for f in data if isinstance(f, str) and os.path.isdir(f)][:RECENT_MAX]


def remap_path(path: str, old: str, new: str) -> str:
    """path with folder old replaced by new (unchanged if not inside old)."""
    p, o = os.path.normcase(os.path.normpath(path)), os.path.normcase(os.path.normpath(old))
    if p == o:
        return str(Path(new))
    if p.startswith(o + os.sep):
        return str(Path(new) / os.path.normpath(path)[len(os.path.normpath(old)) + 1:])
    return path


def rename_recent_folder(old: str, new: str) -> None:
    """Point recent-folder entries at a renamed folder."""
    data = _load_user_json(RECENT_FILE)
    if isinstance(data, list):
        _save_user_json(RECENT_FILE, [remap_path(f, old, new) if isinstance(f, str) else f
                                      for f in data])


def _save_recent_folder(folder: str) -> None:
    """Prepend folder to recent list and save."""
    folder = str(Path(folder))
    recent = [f for f in _load_recent_folders() if str(Path(f)) != folder]
    recent.insert(0, folder)
    _save_user_json(RECENT_FILE, recent[:RECENT_MAX])


