"""Merge per-worker recordings (record.py) into the run-time store (one zip file).

    python -m diffexec.build <rec dir> <out .zip>

Caps are applied AFTER merging all workers, so the store does not depend on how many workers
recorded it (record.py kills a function only when it is already over a cap on one worker, which
implies it is over the cap after merging too):

  DIFFEXEC_CALL_CAP   max calls of a function over the whole suite        (default 50,000)
  DIFFEXEC_BYTE_CAP   max distinct input bytes of a function (pickled)     (default 300 kB)
  DIFFEXEC_INPUT_CAP  max bytes of one call's input (applied while recording, 16 kB)

Tests that failed or errored while being recorded (record.py `failed`) get every phase marked
incomplete, and are listed in the index: the selector never drops them.

Zip layout (deterministic: sorted entries, fixed timestamps):
  index.pkl   {"functions": {(path, qualname, firstlineno): (dead reason | None, n)},
               "failed": [test ids], "caps": {...}}
  c/<n>.pkl   {"blobs": [(input bytes, effect digest, status)], "uses": {phase: (idx..) | None}}
"""

from __future__ import annotations

import glob
import os
import pickle
import sys
import zipfile

CALL_CAP = int(os.environ.get("DIFFEXEC_CALL_CAP", "50000"))
BYTE_CAP = int(float(os.environ.get("DIFFEXEC_BYTE_CAP", "300e3")))


def merge(rec_dir: str) -> tuple[dict, dict, set]:
    merged: dict = {}
    failed: set = set()
    stats = {"workers": 0, "pickle_s": 0.0, "recorded": 0}
    for path in sorted(glob.glob(os.path.join(rec_dir, "rec_*.pkl"))):
        with open(path, "rb") as fh:
            got = pickle.load(fh)
        stats["workers"] += 1
        stats["pickle_s"] += got["stats"]["pickle_s"]
        stats["recorded"] += got["stats"]["recorded"]
        failed.update(got.get("failed", ()))
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
    # global caps (independent of the worker split)
    for m in merged.values():
        if m["dead"]:
            continue
        if m["calls"] > CALL_CAP:
            m["dead"] = "calls"
        elif sum(len(b[0]) for b in m["blobs"].values()) > BYTE_CAP:
            m["dead"] = "bytes"
        if m["dead"]:
            m["blobs"], m["uses"] = {}, {}
            continue
        for phase in m["uses"]:
            if isinstance(phase, tuple) and phase[1] in failed:
                m["uses"][phase] = None
    return merged, stats, failed


def _entry(zf: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    zf.writestr(info, data, compresslevel=9)


def write(merged: dict, failed: set, out: str) -> dict:
    functions = {}
    tmp = out + ".tmp"
    with zipfile.ZipFile(tmp, "w") as zf:
        for n, (key, m) in enumerate(sorted(merged.items())):
            if m["dead"]:
                functions[key] = (m["dead"], None)
                continue
            order = sorted(m["blobs"])
            pos = {h: i for i, h in enumerate(order)}
            body = {"blobs": [tuple(m["blobs"][h]) for h in order],
                    "uses": {p: (None if hs is None else tuple(sorted(pos[h] for h in hs)))
                             for p, hs in sorted(m["uses"].items(), key=lambda kv: repr(kv[0]))}}
            _entry(zf, f"c/{n}.pkl", pickle.dumps(body, protocol=5))
            functions[key] = (None, n)
        index = {"functions": functions, "failed": sorted(failed),
                 "caps": {"calls": CALL_CAP, "bytes": BYTE_CAP}}
        _entry(zf, "index.pkl", pickle.dumps(index, protocol=5))
    os.replace(tmp, out)
    return {"functions": len(functions), "alive": sum(1 for v in functions.values() if not v[0]),
            "failed_tests": len(failed), "zip_bytes": os.path.getsize(out)}


if __name__ == "__main__":
    merged, stats, failed = merge(sys.argv[1])
    stats.update(write(merged, failed, sys.argv[2]))
    dead: dict = {}
    for m in merged.values():
        if m["dead"]:
            dead[m["dead"]] = dead.get(m["dead"], 0) + 1
    stats["dead_by_reason"] = dead
    print(stats)
