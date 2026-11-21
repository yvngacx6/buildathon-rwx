"""Merge per-worker recordings (record.py) into the run-time store (one zip file).

    python -m diffexec.build <rec dir> <out .zip> [tests.json]

Caps are applied AFTER merging all workers, so the store does not depend on how many workers
recorded it:

  DIFFEXEC_CALL_CAP   max calls of a function over the whole suite        (default 50,000)
  DIFFEXEC_BYTE_CAP   distinct input bytes kept per function, LZMA-packed  (default 64 kB)
  DIFFEXEC_BYTES_PER_SEC  more bytes per second of test time kept droppable (default 8 kB)
  DIFFEXEC_MAX_BYTES  most bytes kept per function, LZMA-packed           (default 256 kB)
  DIFFEXEC_INPUT_CAP  max bytes of one call's input (applied while recording, 16 kB)

A function over the byte budget is not dropped: its calls outside tests (import, collection)
are kept first (if they do not fit, the function is dead), then whole tests, longest first
(test seconds from tests.json, not labels), while they fit. Phases of tests that do not fit
are marked incomplete, so those tests are never dropped.

Tests that failed or errored while being recorded (record.py `failed`) get every phase marked
incomplete, and are listed in the index: the selector never drops them.

Zip layout (deterministic: sorted entries, fixed timestamps, LZMA):
  index.pkl   {"functions": {(path, qualname, firstlineno): (dead reason | None, n)},
               "failed": [test ids], "caps": {...}}
  c/<n>.pkl   {"blobs": [(input bytes, effect digest, status)], "uses": {phase: (idx..) | None}}
  u/<n>.pkl   {phase: True (complete) | None (incomplete)}  (cheap parent-side gate)
"""

from __future__ import annotations

import glob
import json
import lzma
import os
import pickle
import sys
import zipfile

CALL_CAP = int(os.environ.get("DIFFEXEC_CALL_CAP", "50000"))
BYTE_CAP = int(float(os.environ.get("DIFFEXEC_BYTE_CAP", "64e3")))
BYTES_PER_SEC = float(os.environ.get("DIFFEXEC_BYTES_PER_SEC", "8e3"))
MAX_BYTES = float(os.environ.get("DIFFEXEC_MAX_BYTES", "256e3"))


def merge(rec_dir: str) -> tuple[dict, dict, set]:
    merged: dict = {}
    failed: set = set()
    stats = {"workers": 0, "pickle_s": 0.0, "recorded": 0, "elided": 0, "incomplete": 0}
    for path in sorted(glob.glob(os.path.join(rec_dir, "rec_*.pkl"))):
        with open(path, "rb") as fh:
            got = pickle.load(fh)
        stats["workers"] += 1
        for k in ("pickle_s", "recorded", "elided", "incomplete"):
            stats[k] += got["stats"].get(k, 0)
        failed.update(got.get("failed", ()))
        for key, r in got["recs"].items():
            m = merged.setdefault(key, {"dead": None, "calls": 0, "blobs": {}, "uses": {}})
            m["calls"] += r["calls"]
            if r["dead"] and not m["dead"]:
                m["dead"] = r["dead"]
            if m["dead"]:
                m["blobs"], m["uses"] = {}, {}
                continue
            for h, b in r["blobs"].items():
                data, d, status = b[:3]
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
    return merged, stats, failed


def apply_caps(merged: dict, failed: set, secs_of: dict) -> None:
    """Global caps (independent of the worker split); see the module docstring."""
    for m in merged.values():
        if m["dead"]:
            continue
        if m["calls"] > CALL_CAP:
            m["dead"] = "calls"
        else:
            for phase in m["uses"]:
                if isinstance(phase, tuple) and phase[1] in failed:
                    m["uses"][phase] = None
            if not _fit(m, BYTE_CAP, secs_of):
                m["dead"] = "bytes"
        if m["dead"]:
            m["blobs"], m["uses"] = {}, {}


def _fit(m: dict, budget: int, secs_of: dict) -> bool:
    """Keep the blobs of whole tests within the function's byte allowance (LZMA-packed):
    BYTE_CAP, plus BYTES_PER_SEC for every second of test time kept droppable, at most
    MAX_BYTES. Calls outside tests come first (if they alone do not fit: False, dead), then
    tests, longest first. Tests left out get their phases marked incomplete."""
    uses = m["uses"]
    used_all = set().union(*(hs for hs in uses.values() if hs)) if uses else set()
    m["blobs"] = {h: b for h, b in m["blobs"].items() if h in used_all}
    by_test: dict = {}
    class_setups: dict = {}  # the selector needs the SETUP (setUpClass) phases of classmates too
    for p in uses:
        if isinstance(p, tuple):
            by_test.setdefault(p[1], []).append(p)
            if p[0] == "SETUP":
                class_setups.setdefault(_class_of(p[1]), []).append(p)
    size = {h: len(b[0]) for h, b in m["blobs"].items()}
    cands = []
    for tid, phases in by_test.items():
        phases = set(phases) | set(class_setups.get(_class_of(tid), ()))
        if any(uses[p] is None for p in phases):
            continue  # never droppable: its blobs are not needed
        need = set().union(*(uses[p] for p in phases))
        cands.append((-secs_of.get(tid, 0.0), sum(size[h] for h in need), tid, need))
    cands.sort(key=lambda c: c[:3])

    def allowance(secs: float) -> float:
        return min(MAX_BYTES, budget + BYTES_PER_SEC * secs)

    raw = b"".join(m["blobs"][h][0] for h in sorted(m["blobs"]))
    packed = len(lzma.compress(raw, preset=6)) if raw else 0
    if packed <= allowance(-sum(c[0] for c in cands)):
        return True
    ratio = len(raw) / packed  # select in raw bytes, at this function's compression ratio
    keep: set = set()
    for p, hs in uses.items():
        if not isinstance(p, tuple) and hs:
            keep |= hs
    used = sum(size[h] for h in keep)
    if used > allowance(0) * ratio:
        return False
    kept_tests, kept_secs = set(), 0.0
    for neg_secs, _, tid, need in cands:
        extra = sum(size[h] for h in need - keep)
        if used + extra <= allowance(kept_secs - neg_secs) * ratio:
            keep |= need
            used += extra
            kept_secs -= neg_secs
            kept_tests.add(tid)
    for tid, phases in by_test.items():
        if tid not in kept_tests:
            for p in phases:
                if uses[p] is not None and not uses[p] <= keep:
                    uses[p] = None
    m["blobs"] = {h: b for h, b in m["blobs"].items() if h in keep}
    return True


def _class_of(tid: str) -> str:
    parts = tid.split("::")
    return "::".join(parts[:-1]) if len(parts) > 2 else tid


def _entry(zf: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_LZMA
    info.external_attr = 0o644 << 16
    zf.writestr(info, data)


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
            uses = sorted(m["uses"].items(), key=lambda kv: repr(kv[0]))
            body = {"blobs": [tuple(m["blobs"][h]) for h in order],
                    "uses": {p: (None if hs is None else tuple(sorted(pos[h] for h in hs)))
                             for p, hs in uses}}
            _entry(zf, f"c/{n}.pkl", pickle.dumps(body, protocol=5))
            _entry(zf, f"u/{n}.pkl", pickle.dumps({p: (None if hs is None else True)
                                                   for p, hs in uses}, protocol=5))
            functions[key] = (None, n)
        index = {"functions": functions, "failed": sorted(failed),
                 "caps": {"calls": CALL_CAP, "bytes": BYTE_CAP,
                          "bytes_per_test_second": BYTES_PER_SEC, "max_bytes": MAX_BYTES}}
        _entry(zf, "index.pkl", pickle.dumps(index, protocol=5))
    os.replace(tmp, out)
    return {"functions": len(functions), "alive": sum(1 for v in functions.values() if not v[0]),
            "failed_tests": len(failed), "zip_bytes": os.path.getsize(out)}


def test_seconds(path: str | None) -> dict:
    if not path:
        return {}
    with open(path) as fh:
        return {t["id"]: float(t["seconds"]) for t in json.load(fh)["tests"]}


if __name__ == "__main__":
    merged, stats, failed = merge(sys.argv[1])
    apply_caps(merged, failed, test_seconds(sys.argv[3] if len(sys.argv) > 3 else None))
    stats.update(write(merged, failed, sys.argv[2]))
    dead: dict = {}
    for m in merged.values():
        if m["dead"]:
            dead[m["dead"]] = dead.get(m["dead"], 0) + 1
    stats["dead_by_reason"] = dead
    stats["incomplete_phases"] = sum(1 for m in merged.values() for v in m["uses"].values()
                                     if v is None)
    print(stats)
