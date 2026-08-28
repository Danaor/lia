# -*- coding: utf-8 -*-
"""Lia — cross-meeting speaker voiceprints (the LEARNING half of speaker naming).

pyannote community-1 hands every diarized meeting one 256-dim embedding per
speaker cluster (verified live 2026-08-27: well-separated for real speakers,
cos ~0.15 between two different people in the same meeting). This module keeps
a tiny local store of NAMED voiceprints and matches new meetings against it:

  learn(name, embedding)   after the user names a speaker in the rename dialog
                           (or the mic-correlation names the local user) - a
                           running centroid per name, so every confirmation
                           sharpens the profile.
  match(embeddings)        before writing a new meeting's transcript - clusters
                           whose voiceprint is close to a known centroid get the
                           real name automatically. CONSERVATIVE by design: a
                           wrong name is worse than 'Speaker B', so a match
                           needs BOTH a minimum cosine similarity AND a clear
                           margin over the runner-up name.

Storage: %APPDATA%/Lia/speaker_profiles.json - names + centroids only, fully
local, no audio retained. Stdlib + numpy; no pyannote import (embeddings arrive
from the diarize_local subprocess).
"""
from __future__ import annotations

import json
import os
import time

import numpy as np

# Calibrated conservatively for ECAPA-family embeddings: same-speaker cosine
# typically lands 0.5-0.8 across sessions; different speakers 0.1-0.3.
MATCH_THRESHOLD = 0.55
MATCH_MARGIN = 0.08
MAX_PROFILES = 200


def _store_path():
    base = os.path.join(os.environ.get("APPDATA", "."), "Lia")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "speaker_profiles.json")


def _norm(vec):
    v = np.asarray(vec, dtype=np.float64)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def load():
    try:
        with open(_store_path(), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data.get("profiles"), list):
            return data
    except Exception:
        pass
    return {"profiles": []}


def _save(data):
    path = _store_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def learn(name, embedding):
    """Fold one confirmed (name, voiceprint) pair into the store: a running
    centroid per name. Returns the profile's new sample count, 0 on bad input."""
    name = (name or "").strip()
    if not name or embedding is None:
        return 0
    e = _norm(embedding)
    if not np.isfinite(e).all() or float(np.linalg.norm(e)) == 0.0:
        return 0
    data = load()
    for p in data["profiles"]:
        if p.get("name", "").casefold() == name.casefold():
            c = _norm(p.get("centroid", []))
            k = int(p.get("count", 1))
            p["centroid"] = list(_norm(c * k + e))
            p["count"] = k + 1
            p["updated"] = int(time.time())
            _save(data)
            return p["count"]
    if len(data["profiles"]) >= MAX_PROFILES:
        data["profiles"].sort(key=lambda p: p.get("updated", 0))
        data["profiles"] = data["profiles"][1:]
    data["profiles"].append({"name": name, "centroid": list(e), "count": 1,
                             "updated": int(time.time())})
    _save(data)
    return 1


def match(embeddings_by_label, threshold=MATCH_THRESHOLD, margin=MATCH_MARGIN):
    """{label: embedding} -> {label: name} for clusters that CLEARLY match a
    stored profile: best cosine >= threshold AND (best - second_best_name) >=
    margin. Unmatched labels are simply absent. Never raises."""
    out = {}
    try:
        profiles = load()["profiles"]
        if not profiles or not embeddings_by_label:
            return out
        names = [p.get("name", "") for p in profiles]
        cents = np.stack([_norm(p.get("centroid", [])) for p in profiles])
        for lab, emb in embeddings_by_label.items():
            e = _norm(emb)
            if e.shape != cents.shape[1:] or not np.isfinite(e).all():
                continue
            sims = cents @ e
            order = np.argsort(sims)[::-1]
            best = float(sims[order[0]])
            second = float(sims[order[1]]) if len(order) > 1 else -1.0
            if best >= threshold and (best - second) >= margin:
                out[lab] = names[int(order[0])]
    except Exception:
        return {}
    return out


def stats():
    """[(name, count, updated_epoch)] - for a future Settings surface."""
    return [(p.get("name", ""), int(p.get("count", 0)), int(p.get("updated", 0)))
            for p in load()["profiles"]]
