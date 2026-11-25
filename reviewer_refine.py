"""
Learning from your species decisions (Photo Reviewer).

Every photo you name or accept in the species list's Review becomes an
example: the species model's fingerprint of the photo, and your answer.
When identifying, a photo is compared with your examples; the more it looks
like the ones you called X, the more X is favoured. It's a soft pull, not a
rule: one example moves little, many similar ones a lot, and a photo that
doesn't look like any of them is left as the model sees it. (A few-shot
"cache adapter" on the model's own fingerprints, as in Tip-Adapter.)

How strong the pull is, and how alike photos must be, is fitted to your own
decisions by testing on examples it hasn't learned from (each day and place
in turn is held out). If your examples don't make it better at that, the
pull is zero: switched off until there are more.

No Tkinter here; numpy only.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

EXAMPLES_FILE = "species_examples.json"
MIN_EXAMPLES = 8          # fewer than this: not enough to learn from
MIN_GROUPS = 3            # (days / places): needed for a fair test
ALPHAS = (0.0, 1.0, 2.0, 4.0, 8.0, 16.0)    # strength of the pull (in logits)
BETAS = (5.0, 10.0, 20.0, 40.0)             # how alike photos must be
MIN_GAIN = 0.02           # better by at least this (log-likelihood a photo) to switch on


def pack(feat: np.ndarray) -> str:
    return base64.b64encode(np.asarray(feat, np.float16).tobytes()).decode("ascii")


def unpack(s: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(s), np.float16).astype(np.float32)


class Examples:
    """Your decisions as examples: {photo key: {path, name, sci, iso, group, f}}
    in %APPDATA%\\PhotoReviewer\\species_examples.json (shared by every list)."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.data: dict = self._read()
        self._changed: set = set()            # (saving merges with what's on disk: the main
        self._removed: set = set()            #  window and the species list both add examples)

    def _read(self) -> dict:
        try:
            data = json.loads(Path(self.path).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def has(self, key: str) -> bool:
        return key in self.data

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.data)

    def restore(self, snap: dict) -> None:
        """Back to a snapshot (undo)."""
        with self.lock:
            for k in set(self.data) - set(snap):
                self._removed.add(k)
                self._changed.discard(k)
            for k, v in snap.items():
                if self.data.get(k) is not v:
                    self._changed.add(k)
                    self._removed.discard(k)
            self.data = dict(snap)

    def add(self, key: str, path: str, name: str, sci: str, iso: Optional[str], group: str,
            feat: Optional[str] = None, note: str = "") -> None:
        """group: the day and place (a fair test holds each one out in turn).
        note: your reason, kept with the example (the AI learns from the photo
        and the answer; the note is for you)."""
        with self.lock:
            old = self.data.get(key, {})
            self.data[key] = {"path": path, "name": name, "sci": sci, "iso": iso or "",
                              "group": group, "t": time.time(), "note": note,
                              "f": feat or old.get("f")}
            self._changed.add(key)
            self._removed.discard(key)

    def remove(self, key: str) -> None:
        with self.lock:
            if self.data.pop(key, None) is not None:
                self._removed.add(key)
                self._changed.discard(key)

    def need_features(self) -> list[tuple[str, str]]:
        with self.lock:
            return [(k, e["path"]) for k, e in self.data.items()
                    if not e.get("f") and e.get("sci") and not e.get("read")]

    def set_features(self, key: str, feat: Optional[str]) -> None:
        """The fingerprint, once read ('' / None: read, but nothing usable: no animal)."""
        with self.lock:
            if key in self.data:
                self.data[key] = dict(self.data[key], f=feat or None, read=True)
                self._changed.add(key)

    def usable(self) -> list[dict]:
        with self.lock:
            return [e for e in self.data.values() if e.get("f") and e.get("sci")]

    def count(self, sci: str) -> int:
        with self.lock:
            return sum(1 for e in self.data.values() if e.get("sci") == sci)

    def _merged(self) -> dict:
        disk = self._read()
        for k in self._removed:
            disk.pop(k, None)
        for k in self._changed:
            if k in self.data:
                disk[k] = self.data[k]
        return disk

    def refresh(self) -> None:
        """Pick up examples added elsewhere (the main window / the species list)."""
        with self.lock:
            self.data = self._merged()

    def save(self) -> None:
        with self.lock:
            if not self._changed and not self._removed:
                return
            disk = self._merged()
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                tmp = self.path + ".tmp"
                Path(tmp).write_text(json.dumps(disk), encoding="utf-8")
                os.replace(tmp, self.path)
                self.data = disk
                self._changed.clear()
                self._removed.clear()
            except OSError:
                pass


def photo_key(path: str) -> Optional[str]:
    """The key a photo is known by (as in the species list's scan)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return f"{os.path.normcase(os.path.abspath(path))}|{st.st_size}|{int(st.st_mtime)}"


class Refiner:
    """The model's guesses for a fingerprint, pulled towards your examples."""

    def __init__(self, labels: list, emb: np.ndarray, scale: float, top_of=None,
                 same_as: Optional[dict] = None):
        self.top_of = top_of                      # (the model's: merges species listed twice)
        self.same_as = same_as or {}
        self.labels = labels                      # [(name, scientific)]
        self.index = {s: i for i, (_n, s) in enumerate(labels)}
        self.emb, self.scale = emb, scale
        self.alpha, self.beta = 0.0, 10.0
        self.keys = np.zeros((0, emb.shape[1]), np.float32)
        self.cls = np.zeros(0, int)
        self.report: dict = {"examples": 0, "active": False, "why": "no examples yet"}

    @property
    def active(self) -> bool:
        return self.alpha > 0 and len(self.cls) > 0

    @staticmethod
    def _norm(v: np.ndarray) -> np.ndarray:
        n = np.linalg.norm(v, axis=-1, keepdims=True)
        return v / np.maximum(n, 1e-8)

    def _logits(self, feat: np.ndarray) -> np.ndarray:
        return self.scale * (self.emb @ feat)

    # ── learning ──────────────────────────────────────────────────────────────

    def train(self, examples: list[dict]) -> dict:
        """Fit the pull to your examples (see the module notes). Returns a report:
        {examples, species, active, before, after (right first time, on held-out
        photos), alpha, beta, why}."""
        canon = lambda sci: self.same_as.get(sci, sci)          # noqa: E731
        rows = [(unpack(e["f"]), self.index[canon(e["sci"])], e.get("group", "")) for e in examples
                if e.get("f") and canon(e.get("sci", "")) in self.index]
        self.alpha = 0.0
        n = len(rows)
        groups = {g for _f, _c, g in rows}
        rep = {"examples": n, "species": len({c for _f, c, _g in rows}), "active": False}
        if n < MIN_EXAMPLES or len(groups) < MIN_GROUPS:
            rep["why"] = f"needs at least {MIN_EXAMPLES} of your decisions, from {MIN_GROUPS} days"
            self.report = rep
            return rep
        feats = np.stack([f for f, _c, _g in rows])
        cls = np.array([c for _f, c, _g in rows])
        grp = np.array([hash(g) for _f, _c, g in rows])
        keys = self._norm(feats)
        K = np.unique(cls)                        # the species your examples are of
        col = {c: j for j, c in enumerate(K)}
        y = np.array([col[c] for c in cls])
        # each example's own model logits: over the K species, and the rest summed
        LK = np.zeros((n, len(K)), np.float64)
        rest_lse = np.zeros(n)
        rest_max = np.zeros(n)
        mask = np.ones(self.emb.shape[0], bool)
        mask[K] = False
        for i0 in range(0, n, 128):                   # (in blocks: one matrix product each)
            L = (self.scale * (feats[i0:i0 + 128] @ self.emb.T)).astype(np.float64)
            LK[i0:i0 + 128] = L[:, K]
            R = L[:, mask]
            m = R.max(1)
            rest_max[i0:i0 + 128] = m
            rest_lse[i0:i0 + 128] = m + np.log(np.exp(R - m[:, None]).sum(1))
        sim = keys @ keys.T
        same = grp[:, None] == grp[None, :]       # held out: the same day / place
        onehot = np.eye(len(K))[y]

        def score(alpha, beta):
            A = np.where(same, 0.0, np.exp(-beta * (1 - sim))) @ onehot     # (n, K)
            Z = LK + alpha * A
            m = np.maximum(Z.max(1), rest_max)
            lse = m + np.log(np.exp(Z - m[:, None]).sum(1) + np.exp(rest_lse - m))
            ll = Z[np.arange(n), y] - lse
            right = (Z.argmax(1) == y) & (Z.max(1) >= rest_max)
            return ll.mean(), right.mean()

        base_ll, base_acc = score(0.0, 10.0)
        best = (base_ll, 0.0, 10.0, base_acc)
        for a in ALPHAS[1:]:
            for b in BETAS:
                ll, acc = score(a, b)
                if ll > best[0]:
                    best = (ll, a, b, acc)
        ll, a, b, acc = best
        rep.update(before=round(float(base_acc), 3), after=round(float(acc), 3),
                   alpha=a, beta=b)
        if a > 0 and ll - base_ll >= MIN_GAIN:
            self.alpha, self.beta = a, b
            self.keys, self.cls = keys, cls
            rep["active"] = True
            rep["why"] = "learning from your decisions"
        else:
            rep["why"] = "your examples don't help yet (tested on photos held out)"
        self.report = rep
        return rep

    # ── using it ──────────────────────────────────────────────────────────────

    def top(self, feat: np.ndarray, k: int = 10) -> Optional[list[list]]:
        """[[name, scientific, p]] like the model's own top, with your examples'
        pull; None when switched off."""
        if not self.active:
            return None
        L = self._logits(feat).astype(np.float64)
        s = self.keys @ self._norm(feat)
        w = np.exp(-self.beta * (1 - s))
        np.add.at(L, self.cls, self.alpha * w)
        p = np.exp(L - L.max())
        p /= p.sum()
        if self.top_of is not None:
            return self.top_of(p, k)
        idx = np.argsort(-p)[:k]
        return [[self.labels[i][0], self.labels[i][1], round(float(p[i]), 4)] for i in idx]


class Learning:
    """For the main window: your examples, and the pull fitted to them
    (trained in the background; retrained as you confirm more)."""

    RETRAIN_EVERY = 5

    def __init__(self, user_dir: str):
        self.examples = Examples(os.path.join(user_dir, EXAMPLES_FILE))
        self.refiner: Optional[Refiner] = None
        self._model = None
        self._busy = False
        self._new = 0
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        r = self.refiner
        return r is not None and r.active

    def train_async(self, model) -> None:
        if model is None or not hasattr(model, "_emb"):
            return
        self._model = model
        with self._lock:
            if self._busy:
                return
            self._busy = True

        def work():
            try:
                self.examples.refresh()
                r = Refiner(model.labels, model._emb, model._scale, getattr(model, "top_of", None),
                            getattr(model, "same_as", None))
                r.train(self.examples.usable())
                self.refiner = r
            except Exception:
                pass
            finally:
                self._busy = False
        threading.Thread(target=work, daemon=True).start()

    def summary(self) -> str:
        """One line for the window: what the suggestions have learned from."""
        r = self.refiner
        if r is None:
            n = len(self.examples.data)
            return f"🧠 {n} of your IDs to learn from" if n else "🧠 Learns from the species you confirm"
        rep = r.report
        if rep.get("active"):
            return (f"🧠 Learned from {rep['examples']} of your IDs "
                    f"(on photos held out: right {rep['before']:.0%} → {rep['after']:.0%})")
        return f"🧠 {rep.get('examples', 0)} of your IDs: {rep.get('why', '')}"

    def top(self, feat: Optional[np.ndarray], k: int = 25) -> Optional[list]:
        r = self.refiner
        if feat is None or r is None or not r.active:
            return None
        return r.top(feat, k)

    def add(self, path: str, name: str, sci: str, iso: Optional[str] = None,
            feat: Optional[np.ndarray] = None, note: str = "") -> None:
        """A species you confirmed in the main window: an example to learn from."""
        key = photo_key(path)
        if not key or not sci:
            return
        self.examples.add(key, path, name, sci, iso, f"{os.path.dirname(path)}|",
                          pack(feat) if feat is not None else None, note)
        threading.Thread(target=self.examples.save, daemon=True).start()   # (off the window's thread)
        self._new += 1
        if self._new >= self.RETRAIN_EVERY and self._model is not None:
            self._new = 0
            self.train_async(self._model)


_learning: Optional[Learning] = None


def get_learning(user_dir: str) -> Learning:
    global _learning
    if _learning is None:
        _learning = Learning(user_dir)
    return _learning
