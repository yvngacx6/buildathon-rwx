"""Merge per-worker recordings (record.py) into the run-time store.

    python -m diffexec.build <rec dir> <store dir>

store/index.pkl          {(path, qualname, firstlineno): (dead reason | None, file number)}
store/c/<n>.pkl.gz       {"blobs": [(input bytes, effect digest, status)], "uses": {phase: (idx..) | None}}
"""

from __future__ import annotations

import glob
import gzip
import os
import pickle
import sys


def merge(rec_dir: str) -> tuple[dict, dict]:
    merged: dict = {}
    stats = {"workers": 0, "pickle_s": 0.0, "recorded": 0}
    for path in sorted(glob.glob(os.path.join(rec_dir, "rec_*.pkl"))):
        with open(path, "rb") as fh:
            got = pickle.load(fh)
        stats["workers"] += 1
        stats["pickle_s"] += got["stats"]["pickle_s"]
        stats["recorded"] += got["stats"]["recorded"]
        for key, r in got["recs"].items():
            m = merged.setdefault(key, {"dead": None, "calls": 0, "blobs": {}, "uses": {}})
            m["calls"] += r["calls"]
            if r["dead"] and not m["dead"]:
                m["dead"] = r["dead"]
            if m["dead"]:
                m["blobs"], m["uses"] = {}, {}
                continue
            for h, (data, d, status) in r["blobs"].items():
                old = m["blobs"].get(h)
                if old is None:
                    m["blobs"][h] = [data, d, status]
                    continue
                if old[2] == "ok" and status != "ok":
                    old[2] = status
                if old[1] is None:
                    old[1] = d
                elif d is not None and d != old[1]:
                    old[2] = "unstable"
            for phase, hs in r["uses"].items():
                if phase in m["uses"] and m["uses"][phase] is None:
                    continue
                if hs is None:
                    m["uses"][phase] = None
                else:
                    m["uses"].setdefault(phase, set()).update(hs)
    return merged, stats


def write(merged: dict, store: str) -> dict:
    os.makedirs(os.path.join(store, "c"), exist_ok=True)
    index = {}
    total = 0
    for n, (key, m) in enumerate(sorted(merged.items())):
        if m["dead"]:
            index[key] = (m["dead"], None)
            continue
        order = list(m["blobs"])
        pos = {h: i for i, h in enumerate(order)}
        body = {"blobs": [tuple(m["blobs"][h]) for h in order],
                "uses": {p: (None if hs is None else tuple(sorted(pos[h] for h in hs)))
                         for p, hs in m["uses"].items()}}
        fn = os.path.join(store, "c", f"{n}.pkl.gz")
        with gzip.open(fn, "wb", compresslevel=6) as fh:
            pickle.dump(body, fh, protocol=5)
        total += os.path.getsize(fn)
        index[key] = (None, n)
    with open(os.path.join(store, "index.pkl"), "wb") as fh:
        pickle.dump(index, fh, protocol=5)
    return {"functions": len(index), "dead": sum(1 for v in index.values() if v[0]),
            "gz_bytes": total + os.path.getsize(os.path.join(store, "index.pkl"))}


if __name__ == "__main__":
    merged, stats = merge(sys.argv[1])
    stats.update(write(merged, sys.argv[2]))
    dead = {}
    for m in merged.values():
        if m["dead"]:
            dead[m["dead"]] = dead.get(m["dead"], 0) + 1
    stats["dead_by_reason"] = dead
    print(stats)
