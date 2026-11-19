"""
Sightings: bursts joined into one encounter with an animal.

A sighting is a run of bursts (in capture order) that are close in time and
show the same animal. "Same animal" is decided by species where that is
reliable — a species you confirmed, or a confident suggestion — and
otherwise by how alike the animal crops look (keeper embeddings). Nothing
here tags anything; it only groups frames for review.
"""

from __future__ import annotations

from typing import Callable, Optional

import numpy as np

DEFAULT_GAP_SECS = 180          # a new sighting after 3 minutes without photos
GAP_CHOICES = (60, 180, 300, 600, 1800, 3600)
SURE_SPECIES = 0.5              # suggestions at least this sure may split sightings
MIN_SIMILAR = 0.5               # crop-embedding cosine for "looks like the same animal"


def _cosine(a: Optional[np.ndarray], b: Optional[np.ndarray]) -> Optional[float]:
    if a is None or b is None:
        return None
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na == 0 or nb == 0:
        return None
    return float(a @ b) / (na * nb)


def build_sightings(groups: list[list[str]], stamps: dict[str, float],
                    species_of: Callable[[list[str]], Optional[tuple]],
                    emb_of: Callable[[list[str]], Optional[np.ndarray]],
                    max_gap: float = DEFAULT_GAP_SECS) -> list[dict]:
    """
    groups:     bursts (single frames count as a burst of one)
    stamps:     capture time per path (missing = keep file order)
    species_of: group → (scientific, display name, confirmed: bool, prob) or None
    emb_of:     group → mean animal-crop embedding, or None
    Returns sightings in time order:
      {"groups": [[paths]…], "paths": [...], "start": ts, "end": ts,
       "species": scientific or None, "name": display name or None,
       "confirmed": bool}
    """
    def first_ts(g):
        ts = [stamps[p] for p in g if stamps.get(p) is not None]
        return min(ts) if ts else None

    # time order; groups without a time stay next to their file-order neighbour
    order, last = [], 0.0
    for i, g in enumerate(groups):
        t = first_ts(g)
        last = t if t is not None else last
        order.append((last, i, g))
    order.sort(key=lambda x: (x[0], x[1]))

    out: list[dict] = []
    cur: Optional[dict] = None
    for _t, _i, g in order:
        ts = [stamps[p] for p in g if stamps.get(p) is not None]
        start, end = (min(ts), max(ts)) if ts else (None, None)
        sp = species_of(g)
        sure = sp is not None and (sp[2] or sp[3] >= SURE_SPECIES)
        emb = emb_of(g)
        new = cur is None
        if cur is not None:
            if start is not None and cur["end"] is not None and start - cur["end"] > max_gap:
                new = True
            elif sure and cur["_sure"] and sp[0] != cur["species"]:
                new = True
            elif not (sure and cur["_sure"]):
                sim = _cosine(emb, cur["_emb"])
                new = sim is not None and sim < MIN_SIMILAR
        if new:
            cur = {"groups": [], "paths": [], "start": start, "end": end,
                   "species": None, "name": None, "confirmed": False,
                   "_sure": False, "_emb": None, "_votes": {}}
            out.append(cur)
        cur["groups"].append(list(g))
        cur["paths"].extend(g)
        if start is not None:
            cur["start"] = start if cur["start"] is None else min(cur["start"], start)
            cur["end"] = end if cur["end"] is None else max(cur["end"], end)
        if emb is not None:
            cur["_emb"] = emb                       # compare with the latest burst
        if sp is not None:
            sci, name, confirmed, prob = sp
            if confirmed and not cur["confirmed"]:
                cur.update(species=sci, name=name, confirmed=True, _sure=True)
            elif not cur["confirmed"]:
                v = cur["_votes"]
                v[(sci, name)] = v.get((sci, name), 0.0) + prob * len(g)
                (bsci, bname), _w = max(v.items(), key=lambda kv: kv[1])
                cur.update(species=bsci, name=bname)
                cur["_sure"] = cur["_sure"] or sure
    for s in out:
        for k in ("_sure", "_emb", "_votes"):
            s.pop(k, None)
    return out
