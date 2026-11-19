"""
AI search for Photo Reviewer: find photos by description ("robin in snow",
"would make a good Christmas card") across a whole photo library.

Uses OpenCLIP ViT-B-32 (LAION-2B weights, MIT licence) via onnxruntime:
  - image encoder (int8) — every photo gets a 512-d embedding, stored per
    folder in photo_reviewer_clip.npz (by file name, so moved folders keep it)
  - text encoder — turns your query into the same space; ranking is cosine
    similarity. The CLIP byte-pair tokenizer is implemented here (no extra
    packages needed).
"""

from __future__ import annotations

import gzip
import html
import logging
import os
import re
import sys
import threading
from functools import lru_cache
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

log = logging.getLogger("photo_reviewer")

IMAGE_MODEL = "clip_image_int8.onnx"
TEXT_MODEL  = "clip_text.onnx"
VOCAB_FILE  = "clip_bpe_vocab.txt.gz"
INDEX_FILE  = "photo_reviewer_clip.npz"
INDEX_VERSION = 1
CONTEXT = 77
_MEAN = np.array((0.48145466, 0.4578275, 0.40821073), np.float32)
_STD  = np.array((0.26862954, 0.26130258, 0.27577711), np.float32)
SKIP_DIRS = {"Best", "ID"}        # copies made by Process — searched via the originals


def _model_dirs() -> list[Path]:
    here = Path(__file__).resolve().parent
    exe = Path(sys.executable).parent if getattr(sys, "frozen", False) else here
    return [exe / "models", here / "models"]


def _find(name: str) -> Optional[Path]:
    return next((d / name for d in _model_dirs() if (d / name).exists()), None)


# ── CLIP tokenizer (port of OpenCLIP's SimpleTokenizer) ──────────────────────

@lru_cache()
def _bytes_to_unicode() -> dict[int, str]:
    bs = list(range(ord("!"), ord("~") + 1)) + list(range(ord("¡"), ord("¬") + 1)) + \
        list(range(ord("®"), ord("ÿ") + 1))
    cs = bs[:]
    n = 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    return dict(zip(bs, (chr(c) for c in cs)))


class ClipTokenizer:
    def __init__(self, vocab_path: Path):
        merges = gzip.open(vocab_path).read().decode("utf-8").split("\n")
        merges = [tuple(m.split()) for m in merges[1:49152 - 256 - 2 + 1]]
        vocab = list(_bytes_to_unicode().values())
        vocab += [v + "</w>" for v in vocab]
        vocab += ["".join(m) for m in merges]
        vocab += ["<start_of_text>", "<end_of_text>"]
        self.encoder = {v: i for i, v in enumerate(vocab)}
        self.ranks = {m: i for i, m in enumerate(merges)}
        self.byte_enc = _bytes_to_unicode()
        self.cache: dict[str, str] = {}
        # \p{L} / \p{N} approximated with the standard library's \w classes
        self.pat = re.compile(r"""<start_of_text>|<end_of_text>|'s|'t|'re|'ve|'m|'ll|'d|"""
                              r"""[^\W\d_]+|\d|(?:[^\s\w]|_)+""", re.IGNORECASE)
        self.sot, self.eot = self.encoder["<start_of_text>"], self.encoder["<end_of_text>"]

    def _bpe(self, token: str) -> str:
        if token in self.cache:
            return self.cache[token]
        word = tuple(token[:-1]) + (token[-1] + "</w>",)
        while len(word) > 1:
            pairs = {(word[i], word[i + 1]) for i in range(len(word) - 1)}
            best = min(pairs, key=lambda p: self.ranks.get(p, float("inf")))
            if best not in self.ranks:
                break
            first, second = best
            new, i = [], 0
            while i < len(word):
                if i < len(word) - 1 and word[i] == first and word[i + 1] == second:
                    new.append(first + second)
                    i += 2
                else:
                    new.append(word[i])
                    i += 1
            word = tuple(new)
        out = " ".join(word)
        self.cache[token] = out
        return out

    def encode(self, text: str) -> list[int]:
        text = html.unescape(html.unescape(text)).strip()
        text = re.sub(r"\s+", " ", text).strip().lower()
        ids = []
        for tok in self.pat.findall(text):
            tok = "".join(self.byte_enc[b] for b in tok.encode("utf-8"))
            ids.extend(self.encoder[t] for t in self._bpe(tok).split(" "))
        return ids

    def __call__(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), CONTEXT), np.int64)
        for i, t in enumerate(texts):
            ids = [self.sot] + self.encode(t)[:CONTEXT - 2] + [self.eot]
            out[i, :len(ids)] = ids
        return out


# ── Encoders ─────────────────────────────────────────────────────────────────

def _session(path: Path, threads: int):
    import onnxruntime as ort
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = threads
    return ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])


def _prep(img: Image.Image) -> np.ndarray:
    """Resize shortest side to 224 (bicubic), centre-crop 224, normalise."""
    img = img.convert("RGB")
    w, h = img.size
    s = 224 / min(w, h)
    img = img.resize((max(224, round(w * s)), max(224, round(h * s))), Image.BICUBIC)
    w, h = img.size
    left, top = (w - 224) // 2, (h - 224) // 2
    img = img.crop((left, top, left + 224, top + 224))
    return ((np.asarray(img, np.float32) / 255 - _MEAN) / _STD).transpose(2, 0, 1)


class ImageEncoder:
    def __init__(self, path: Path):
        from reviewer_accel import Runner
        self._s = Runner(path, max(1, (os.cpu_count() or 4) // 4), "search", dequantize=True)

    def embed(self, images: list[Image.Image]) -> np.ndarray:
        x = np.stack([_prep(im) for im in images]).astype(np.float32)
        return self._s.run(None, {"image": x})[0].astype(np.float32)


class TextEncoder:
    def __init__(self, path: Path, vocab: Path):
        self._s = _session(path, max(1, (os.cpu_count() or 4) // 2))
        self.tok = ClipTokenizer(vocab)

    def embed(self, texts: list[str]) -> np.ndarray:
        return self._s.run(None, {"text": self.tok(texts)})[0].astype(np.float32)


_lock = threading.Lock()
_image: Optional[ImageEncoder] = None
_text: Optional[TextEncoder] = None
_errors: dict[str, str] = {}


def get_image_encoder() -> Optional[ImageEncoder]:
    global _image
    with _lock:
        if _image is None and "image" not in _errors:
            p = _find(IMAGE_MODEL)
            try:
                if p is None:
                    raise FileNotFoundError(f"{IMAGE_MODEL} not found in 'models' folder")
                _image = ImageEncoder(p)
            except Exception as e:
                _errors["image"] = str(e)
                log.warning("AI search unavailable: %s", e)
        return _image


def get_text_encoder() -> Optional[TextEncoder]:
    """Loaded on first search (it is only needed for queries)."""
    global _text
    with _lock:
        if _text is None and "text" not in _errors:
            p, v = _find(TEXT_MODEL), _find(VOCAB_FILE)
            try:
                if p is None or v is None:
                    raise FileNotFoundError(f"{TEXT_MODEL} / {VOCAB_FILE} not found in 'models' folder")
                _text = TextEncoder(p, v)
            except Exception as e:
                _errors["text"] = str(e)
                log.warning("AI search queries unavailable: %s", e)
        return _text


def search_status() -> str:
    get_image_encoder()
    return _errors.get("image", "")


# ── Per-folder index ─────────────────────────────────────────────────────────

def load_index(folder: str) -> dict[str, tuple[float, np.ndarray]]:
    """{file name: (mtime, embedding)} for a folder."""
    p = os.path.join(folder, INDEX_FILE)
    if not os.path.exists(p):
        return {}
    try:
        with np.load(p, allow_pickle=False) as z:
            if int(z["version"]) != INDEX_VERSION:
                return {}
            return {str(n): (float(m), e.astype(np.float32))
                    for n, m, e in zip(z["names"], z["mtimes"], z["emb"])}
    except Exception as e:
        log.warning("Could not read %s: %s", p, e)
        return {}


def save_index(folder: str, index: dict[str, tuple[float, np.ndarray]]) -> None:
    if not index:
        return
    names = list(index)
    p = os.path.join(folder, INDEX_FILE)
    tmp = p + ".tmp.npz"
    try:
        np.savez_compressed(tmp, version=INDEX_VERSION, names=np.array(names),
                            mtimes=np.array([index[n][0] for n in names]),
                            emb=np.stack([index[n][1] for n in names]).astype(np.float16))
        os.replace(tmp, p)
    except OSError as e:
        log.warning("Could not save %s: %s", p, e)


def library_folders(root: str, exts) -> list[str]:
    """Folders under root that contain photos (Best/ and ID/ copies skipped)."""
    out = []
    for d, subdirs, files in os.walk(root):
        subdirs[:] = [s for s in subdirs if s not in SKIP_DIRS and not s.startswith(".")]
        if any(Path(f).suffix.lower() in exts for f in files):
            out.append(str(Path(d)))
    return sorted(out)


def stale_files(folder: str, exts, index: dict) -> list[str]:
    """Photos in folder whose embedding is missing or out of date."""
    todo = []
    for f in sorted(os.listdir(folder)):
        if Path(f).suffix.lower() not in exts:
            continue
        try:
            mt = os.path.getmtime(os.path.join(folder, f))
        except OSError:
            continue
        e = index.get(f)
        if e is None or abs(e[0] - mt) > 1:
            todo.append(f)
    return todo


# ── Library folders on several drives ────────────────────────────────────────
#
# Each library folder is remembered with its drive's volume serial number, so
# an external drive is found again when Windows gives it another letter. For
# folders that aren't on the system drive, an offline copy of the index (with
# small thumbnails and the Best / Delete / stars / species of each photo) is
# kept in %APPDATA%\PhotoReviewer\library, so search results still show while
# the drive is unplugged.

THUMB_SIDE = 192


def volume_of(path: str) -> tuple[str, str]:
    """(serial number, volume label) of the drive holding path ("" if unknown)."""
    if sys.platform != "win32":
        return "", ""
    try:
        import ctypes
        drive = os.path.splitdrive(os.path.abspath(path))[0]
        if not drive:
            return "", ""
        root = drive + "\\"
        name = ctypes.create_unicode_buffer(261)
        serial = ctypes.c_uint32()
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root), name, 261, ctypes.byref(serial), None, None, None, 0)
        return (f"{serial.value:08X}", name.value) if ok else ("", "")
    except Exception:
        return "", ""


def is_external(path: str) -> bool:
    """True if path isn't on the Windows system drive (e.g. a USB hard drive)."""
    drive = os.path.splitdrive(os.path.abspath(path))[0].upper()
    system = (os.environ.get("SystemDrive") or "C:").upper()
    return bool(drive) and drive != system


def make_root(path: str) -> dict:
    serial, label = volume_of(path)
    return {"path": str(Path(path)), "volume": serial, "label": label}


def resolve_root(entry: dict) -> Optional[str]:
    """Where a library folder is now (its drive letter may have changed), or None."""
    p = entry.get("path") or ""
    vol = entry.get("volume") or ""
    if os.path.isdir(p) and (not vol or volume_of(p)[0] == vol):
        return p
    if vol and sys.platform == "win32":
        rest = os.path.splitdrive(p)[1]
        import string
        for letter in string.ascii_uppercase:
            cand = f"{letter}:{rest}"
            if os.path.isdir(cand) and volume_of(cand)[0] == vol:
                return cand
    return None


def root_name(entry: dict) -> str:
    """Short description: 'Holidays on HDD-2TB'."""
    name = Path(entry.get("path") or "").name or entry.get("path", "")
    return f"{name} on {entry['label']}" if entry.get("label") else name


class OfflineIndex:
    """Offline copy of one library folder's index + thumbnails."""

    def __init__(self, base: Path, entry: dict):
        import hashlib
        rest = os.path.splitdrive(entry.get("path") or "")[1].lower()
        key = f"{entry.get('volume') or 'local'}_{hashlib.sha1(rest.encode()).hexdigest()[:12]}"
        self.file = base / f"{key}.npz"
        self.thumbs = base / key

    def thumb_path(self, rel: str) -> Path:
        import hashlib
        return self.thumbs / (hashlib.sha1(rel.lower().encode()).hexdigest()[:20] + ".jpg")

    def load(self) -> dict[str, dict]:
        """{relative path: {mtime, emb, best, delete, star, species}}"""
        if not self.file.exists():
            return {}
        try:
            with np.load(self.file, allow_pickle=False) as z:
                return {str(r): {"mtime": float(m), "emb": e.astype(np.float32), "best": bool(b),
                                 "delete": bool(d), "star": int(s), "species": str(sp)}
                        for r, m, e, b, d, s, sp in zip(z["rel"], z["mtime"], z["emb"], z["best"],
                                                        z["delete"], z["star"], z["species"])}
        except Exception as e:
            log.warning("Could not read %s: %s", self.file, e)
            return {}

    def save(self, entries: dict[str, dict]) -> None:
        if not entries:
            return
        rels = list(entries)
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(self.file) + ".tmp.npz"
        try:
            np.savez_compressed(
                tmp, rel=np.array(rels),
                mtime=np.array([entries[r]["mtime"] for r in rels]),
                emb=np.stack([entries[r]["emb"] for r in rels]).astype(np.float16),
                best=np.array([entries[r]["best"] for r in rels], bool),
                delete=np.array([entries[r]["delete"] for r in rels], bool),
                star=np.array([entries[r]["star"] for r in rels], np.int8),
                species=np.array([entries[r]["species"] or "" for r in rels]))
            os.replace(tmp, self.file)
        except OSError as e:
            log.warning("Could not save %s: %s", self.file, e)

    def save_thumb(self, rel: str, img: Image.Image) -> None:
        t = self.thumb_path(rel)
        if t.exists():
            return
        t.parent.mkdir(parents=True, exist_ok=True)
        img = img.convert("RGB")
        img.thumbnail((THUMB_SIDE, THUMB_SIDE))
        img.save(t, "JPEG", quality=72)
