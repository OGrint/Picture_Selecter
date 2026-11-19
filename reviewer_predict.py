"""
Keeper prediction for Photo Reviewer: learns which frames YOU keep.

How it learns
  Within a burst you picked a Best from, the Best frames are "kept" and the
  rest "not kept"; single frames count when tagged Best or Delete. Each frame
  is described by
    - an image embedding of the animal (MobileNetV3, ONNX) — pose, head angle,
      wing position, composition, background;
    - how that differs from the rest of its burst;
    - sharpness at the focus point, exposure, animal size / position, focus-
      on-animal, and the frame's sharpness rank within the burst.
  A regularised logistic regression on those features is trained from your
  past folders (history) plus the folder you are culling now, and retrained
  as you tag. It only ever *suggests* — nothing is tagged automatically.

Files
  per folder   photo_reviewer_features.npz   cached embeddings (paths, mtimes)
  per user     keeper_history.npz            labelled frames from past folders
               keeper_model.npz              the trained model
"""

from __future__ import annotations

import logging
import math
import os
import sys
import threading
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

log = logging.getLogger("photo_reviewer")

EMBED_FILE     = "embed_mobilenet_v3_large.onnx"
FEATURES_FILE  = "photo_reviewer_features.npz"
HISTORY_FILE   = "keeper_history.npz"
MODEL_FILE     = "keeper_model.npz"
EMBED_DIM      = 960
PCA_DIMS       = 32
MIN_BURSTS     = 20          # labelled bursts needed before predictions are shown
FEATURE_VERSION = 1

_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
_STD  = np.array([0.229, 0.224, 0.225], np.float32)


# ── Embedding model ──────────────────────────────────────────────────────────

def _model_dirs() -> list[Path]:
    here = Path(__file__).resolve().parent
    exe = Path(sys.executable).parent if getattr(sys, "frozen", False) else here
    return [exe / "models", here / "models"]


class Embedder:
    """MobileNetV3-Large (ImageNet) → 960-d embedding of the animal crop."""

    def __init__(self, path: Path):
        from reviewer_accel import Runner
        self._s = Runner(path, 2, "keeper")
        self._in = self._s.input_name

    def embed(self, img: Image.Image, box: Optional[list] = None) -> np.ndarray:
        """Embedding of the animal (box = normalised x0,y0,x1,y1) or whole frame."""
        iw, ih = img.size
        if box is not None:
            x0, y0, x1, y1 = box
            cx, cy = (x0 + x1) / 2 * iw, (y0 + y1) / 2 * ih
            side = max((x1 - x0) * iw, (y1 - y0) * ih) * 1.25    # square, with margin
            crop = (int(max(0, cx - side / 2)), int(max(0, cy - side / 2)),
                    int(min(iw, cx + side / 2)), int(min(ih, cy + side / 2)))
            if crop[2] - crop[0] >= 16 and crop[3] - crop[1] >= 16:
                img = img.crop(crop)
        x = np.asarray(img.convert("RGB").resize((224, 224), Image.BILINEAR), np.float32) / 255.0
        x = ((x - _MEAN) / _STD).transpose(2, 0, 1)[None]
        return self._s.run(None, {self._in: x})[0][0].astype(np.float32)


_embedder: Optional[Embedder] = None
_embedder_error: Optional[str] = None
_lock = threading.Lock()


def get_embedder() -> Optional[Embedder]:
    global _embedder, _embedder_error
    with _lock:
        if _embedder is None and _embedder_error is None:
            path = next((d / EMBED_FILE for d in _model_dirs() if (d / EMBED_FILE).exists()), None)
            if path is None:
                _embedder_error = f"model file {EMBED_FILE} not found in 'models' folder"
            else:
                try:
                    _embedder = Embedder(path)
                except Exception as e:
                    _embedder_error = str(e)
            if _embedder_error:
                log.warning("Keeper prediction unavailable: %s", _embedder_error)
        return _embedder


def embedder_status() -> str:
    get_embedder()
    return _embedder_error or ""


# ── Per-folder embedding cache ───────────────────────────────────────────────

def load_features(folder: str, valid: set[str]) -> dict[str, np.ndarray]:
    p = os.path.join(folder, FEATURES_FILE)
    if not os.path.exists(p):
        return {}
    try:
        with np.load(p, allow_pickle=False) as z:
            if int(z["version"]) != FEATURE_VERSION:
                return {}
            names, mtimes, emb = z["names"], z["mtimes"], z["emb"]
    except Exception as e:
        log.warning("Could not read %s: %s", p, e)
        return {}
    out = {}
    for name, mt, e in zip(names, mtimes, emb):
        path = os.path.join(folder, str(name))
        if path in valid:
            try:
                if abs(os.path.getmtime(path) - float(mt)) <= 1:
                    out[path] = e.astype(np.float32)
            except OSError:
                pass
    return out


def save_features(folder: str, feats: dict[str, np.ndarray]) -> None:
    """Saved by file name (not full path), so moved folders keep their cache."""
    names, mtimes, emb = [], [], []
    for path, e in feats.items():
        try:
            mtimes.append(os.path.getmtime(path))
        except OSError:
            continue
        names.append(os.path.basename(path))
        emb.append(e.astype(np.float16))
    if not names:
        return
    p = os.path.join(folder, FEATURES_FILE)
    tmp = p + ".tmp.npz"
    try:
        np.savez_compressed(tmp, version=FEATURE_VERSION, names=np.array(names),
                            mtimes=np.array(mtimes), emb=np.stack(emb))
        os.replace(tmp, p)
    except OSError as e:
        log.warning("Could not save %s: %s", p, e)


# ── Bursts (shared with the app) ─────────────────────────────────────────────

def group_bursts(files: list[str], hashes: dict, stamps: dict,
                 max_dist: int = 6, max_secs: float = 30, neighbours: int = 10
                 ) -> list[list[str]]:
    """Group similar frames shot close together (see app for details)."""
    hashed = [f for f in files if f in hashes]
    parent = {f: f for f in hashed}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def similar(a: str, b: str) -> bool:
        return abs(hashes[a] - hashes[b]) <= max_dist

    timed = sorted((stamps[f], f) for f in hashed if stamps.get(f) is not None)
    for i, (ta, fa) in enumerate(timed):
        for tb, fb in timed[i + 1:]:
            if tb - ta > max_secs:
                break
            if similar(fa, fb):
                parent[find(fa)] = find(fb)
    pos = {f: i for i, f in enumerate(files)}
    for fa in hashed:
        if stamps.get(fa) is not None:
            continue
        i = pos[fa]
        for fb in files[max(0, i - neighbours): i + neighbours + 1]:
            if fb != fa and fb in parent and similar(fa, fb):
                parent[find(fa)] = find(fb)
    groups: dict[str, list[str]] = {}
    for f in files:
        groups.setdefault(find(f) if f in parent else f, []).append(f)
    return list(groups.values())


# ── Features and labels ──────────────────────────────────────────────────────

HAND_NAMES = ("sharpness", "exposure", "sharp_at_af", "animal", "animal_conf",
              "animal_area", "edge", "focus_off_animal", "n_animals", "person",
              "burst_size", "sharp_vs_best", "sharp_rank", "is_sharpest", "area_vs_mean")


def _frame_hand(score: Optional[dict], dets: Optional[list], focus) -> list[float]:
    from reviewer_detect import animals, main_animal, suggestions
    s = score or {}
    a = main_animal(dets)
    area = (a[4] - a[2]) * (a[5] - a[3]) if a else 0.0
    sugg = suggestions(dets, focus) if dets is not None else []
    return [s.get("sharpness", 0) / 100, s.get("exposure_jpeg", s.get("exposure", 0)) / 100,
            1.0 if s.get("sharp_at") == "af" else 0.0,
            1.0 if a else 0.0, a[1] if a else 0.0,
            math.log10(area + 1e-4) + 4 if a else 0.0,
            1.0 if "edge" in sugg else 0.0, 1.0 if "missed" in sugg else 0.0,
            float(len(animals(dets))), 1.0 if "person" in sugg else 0.0]


def build_features(paths: list[str], bursts: list[list[str]], scores: dict,
                   dets: dict, focus: dict, emb: dict
                   ) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    """
    Feature arrays for the paths that have an embedding:
    (hand [n,15], embedding [n,960], embedding minus burst mean [n,960], kept paths)
    """
    burst_of = {p: g for g in bursts for p in g}
    hand, E, R, kept = [], [], [], []
    cache: dict[int, tuple] = {}
    for p in paths:
        if p not in emb:
            continue
        g = [q for q in burst_of.get(p, [p]) if q in emb]
        key = id(burst_of.get(p, p))
        if key not in cache:
            sharp = [(scores.get(q) or {}).get("sharpness", 0) for q in g]
            areas = []
            for q in g:
                from reviewer_detect import main_animal
                a = main_animal(dets.get(q))
                areas.append((a[4] - a[2]) * (a[5] - a[3]) if a else 0.0)
            mean_e = np.mean([emb[q] for q in g], axis=0)
            cache[key] = (g, sharp, areas, mean_e)
        g, sharp, areas, mean_e = cache[key]
        i = g.index(p) if p in g else 0
        my = sharp[i] if g else 0
        rank = sum(1 for v in sharp if v < my) / max(1, len(sharp) - 1) if len(g) > 1 else 1.0
        h = _frame_hand(scores.get(p), dets.get(p), focus.get(p))
        h += [math.log(len(g)), (my - max(sharp)) / 100 if sharp else 0.0, rank,
              1.0 if sharp and my >= max(sharp) else 0.0,
              (areas[i] - float(np.mean(areas))) * 10 if areas else 0.0]
        hand.append(h)
        E.append(emb[p])
        R.append(emb[p] - mean_e if len(g) > 1 else np.zeros(EMBED_DIM, np.float32))
        kept.append(p)
    if not kept:
        z = np.zeros((0, EMBED_DIM), np.float32)
        return np.zeros((0, len(HAND_NAMES)), np.float32), z, z, []
    return (np.array(hand, np.float32), np.array(E, np.float32),
            np.array(R, np.float32), kept)


def labels_for(paths: list[str], status: dict, bursts: list[list[str]]
               ) -> tuple[dict[str, float], dict[str, int]]:
    """
    Training labels from tags: in a burst with a Best, Best = 1 and the other
    frames = 0; elsewhere Best = 1 and Delete = 0. Returns (label, group id).
    """
    labels: dict[str, float] = {}
    group: dict[str, int] = {}
    wanted = set(paths)
    for gi, g in enumerate(bursts):
        g = [p for p in g if p in wanted]
        if not g:
            continue
        best = [p for p in g if status.get(p, {}).get("best")]
        if len(g) > 1 and best:
            for p in g:
                labels[p] = 1.0 if p in best else 0.0
                group[p] = gi
        else:
            for p in g:
                t = status.get(p, {})
                if t.get("best"):
                    labels[p], group[p] = 1.0, gi
                elif t.get("delete"):
                    labels[p], group[p] = 0.0, gi
    return labels, group


# ── Model ────────────────────────────────────────────────────────────────────

class KeeperModel:
    """PCA-compressed embeddings + hand features → logistic regression."""

    def __init__(self):
        self.pca_mean = self.pca = self.rel_pca = None
        self.mu = self.sd = self.w = None
        self.b = 0.0
        self.meta: dict = {}

    def _design(self, H, E, R) -> np.ndarray:
        e = (E - self.pca_mean) @ self.pca.T
        r = R @ self.rel_pca.T
        return np.hstack([H, e, r])

    def fit(self, H, E, R, y, weight=None, l2: float = 1.0, iters: int = 300) -> "KeeperModel":
        n = len(y)
        self.pca_mean = E.mean(0)
        self.pca = _pca(E - self.pca_mean, PCA_DIMS)
        self.rel_pca = _pca(R, PCA_DIMS)
        X = self._design(H, E, R)
        self.mu, self.sd = X.mean(0), X.std(0) + 1e-6
        X = (X - self.mu) / self.sd
        w = np.zeros(X.shape[1])
        b = 0.0
        sw = np.ones(n) if weight is None else weight
        sw = sw / sw.mean()
        # Newton's method (IRLS) — small, well-conditioned problem
        for _ in range(min(iters, 30)):
            p = 1 / (1 + np.exp(-np.clip(X @ w + b, -30, 30)))
            g = X.T @ (sw * (p - y)) / n + l2 * w / n
            gb = float(np.sum(sw * (p - y)) / n)
            Wd = sw * p * (1 - p) / n
            Hm = (X.T * Wd) @ X + np.eye(X.shape[1]) * (l2 / n)
            hb = float(Wd.sum()) + 1e-9
            try:
                step = np.linalg.solve(Hm, g)
            except np.linalg.LinAlgError:
                step = g
            w -= step
            b -= gb / hb
            if np.abs(step).max() < 1e-6:
                break
        self.w, self.b = w, b
        return self

    def predict(self, H, E, R) -> np.ndarray:
        X = (self._design(H, E, R) - self.mu) / self.sd
        return 1 / (1 + np.exp(-np.clip(X @ self.w + self.b, -30, 30)))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(path) + ".tmp.npz"
        np.savez(tmp, pca_mean=self.pca_mean, pca=self.pca, rel_pca=self.rel_pca,
                 mu=self.mu, sd=self.sd, w=self.w, b=self.b,
                 meta=np.array(repr(self.meta)))
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path) -> Optional["KeeperModel"]:
        try:
            with np.load(path, allow_pickle=False) as z:
                m = cls()
                m.pca_mean, m.pca, m.rel_pca = z["pca_mean"], z["pca"], z["rel_pca"]
                m.mu, m.sd, m.w, m.b = z["mu"], z["sd"], z["w"], float(z["b"])
                import ast
                m.meta = ast.literal_eval(str(z["meta"]))
            return m
        except Exception:
            return None


def _pca(X: np.ndarray, k: int) -> np.ndarray:
    k = max(1, min(k, X.shape[0] - 1, X.shape[1]))
    _, _, vt = np.linalg.svd(X - X.mean(0) if X.shape[0] > 1 else X, full_matrices=False)
    return vt[:k].astype(np.float32)


def _burst_weights(groups: np.ndarray) -> np.ndarray:
    """Each burst counts the same however many frames it has."""
    _, inv, counts = np.unique(groups, return_inverse=True, return_counts=True)
    return 1.0 / counts[inv]


def agreement(probs: np.ndarray, y: np.ndarray, groups: np.ndarray) -> Optional[float]:
    """Share of multi-frame bursts where the top-scored frame is one you kept."""
    hits = total = 0
    for g in np.unique(groups):
        m = groups == g
        if m.sum() < 2 or y[m].max() < 1 or y[m].min() > 0:
            continue
        total += 1
        hits += int(y[m][np.argmax(probs[m])] == 1)
    return hits / total if total else None


def train(H, E, R, y, groups, sharp) -> tuple[Optional[KeeperModel], dict]:
    """
    Fit on all data, after measuring honest accuracy on 20 % of bursts held
    out (and the same for sharpness alone, as a baseline).
    """
    info: dict = {"frames": int(len(y))}
    uniq, counts = np.unique(groups, return_counts=True)
    bursts = uniq[counts > 1]
    info["bursts"] = int(len(bursts))
    if len(y) < 10 or y.min() == y.max():
        return None, info
    rng = np.random.default_rng(0)
    held = set(rng.choice(bursts, size=max(1, len(bursts) // 5), replace=False).tolist()) \
        if len(bursts) >= 10 else set()
    if held:
        test = np.isin(groups, list(held))
        if y[~test].min() != y[~test].max():
            m = KeeperModel().fit(H[~test], E[~test], R[~test], y[~test], _burst_weights(groups[~test]))
            info["agree"] = agreement(m.predict(H[test], E[test], R[test]), y[test], groups[test])
            info["agree_sharp"] = agreement(sharp[test], y[test], groups[test])
    model = KeeperModel().fit(H, E, R, y, _burst_weights(groups))
    model.meta = info
    return model, info


# ── History of labelled frames from past folders ─────────────────────────────

class History:
    """Labelled training frames, one block per folder (replaced on update)."""

    def __init__(self, path: Path):
        self.path = path
        self.blocks: dict[str, dict] = {}
        if path.exists():
            try:
                with np.load(path, allow_pickle=False) as z:
                    folders = z["folder"]
                    for f in np.unique(folders):
                        m = folders == f
                        self.blocks[str(f)] = {k: z[k][m] for k in ("H", "E", "R", "y", "g", "s")}
            except Exception as e:
                log.warning("Could not read %s: %s", path, e)

    def set_folder(self, folder: str, H, E, R, y, g, s) -> None:
        if len(y):
            self.blocks[folder] = {"H": H, "E": E.astype(np.float16), "R": R.astype(np.float16),
                                   "y": y, "g": g, "s": s}
        else:
            self.blocks.pop(folder, None)

    def rename_folder(self, old: str, new: str) -> bool:
        """Re-key blocks after a folder (or a parent of it) was renamed."""
        from reviewer_storage import remap_path
        changed = False
        for f in list(self.blocks):
            nf = remap_path(f, old, new)
            if nf != f:
                self.blocks[nf] = self.blocks.pop(f)
                changed = True
        return changed

    def arrays(self, exclude: Optional[str] = None):
        """Stacked (H, E, R, y, groups, sharp) with group ids unique per folder."""
        parts = [(f, b) for f, b in self.blocks.items() if f != exclude]
        if not parts:
            return None
        out = {k: [] for k in ("H", "E", "R", "y", "g", "s")}
        for i, (_f, b) in enumerate(parts):
            for k in out:
                v = b[k]
                out[k].append(v.astype(np.int64) + (i + 1) * 1_000_000 if k == "g" else v)
        return tuple(np.concatenate(out[k]).astype(np.float32 if k in "HERys" else np.int64)
                     for k in ("H", "E", "R", "y", "g", "s"))

    def save(self) -> None:
        if not self.blocks:
            return
        cols = {k: [] for k in ("H", "E", "R", "y", "g", "s")}
        folders = []
        for f, b in self.blocks.items():
            for k in cols:
                cols[k].append(b[k])
            folders += [f] * len(b["y"])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(self.path) + ".tmp.npz"
        np.savez_compressed(tmp, folder=np.array(folders),
                            **{k: np.concatenate(v) for k, v in cols.items()})
        os.replace(tmp, self.path)

    @property
    def folders(self) -> int:
        return len(self.blocks)


def folder_samples(paths, status, bursts, scores, dets, focus, emb):
    """Training arrays (H, E, R, y, groups, sharpness) for one folder."""
    labels, group = labels_for(paths, status, bursts)
    lab = [p for p in paths if p in labels]
    H, E, R, kept = build_features(lab, bursts, scores, dets, focus, emb)
    y = np.array([labels[p] for p in kept], np.float32)
    g = np.array([group[p] for p in kept], np.int64)
    s = np.array([(scores.get(p) or {}).get("sharpness", 0) for p in kept], np.float32)
    return H, E, R, y, g, s
