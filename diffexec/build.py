"""Merge per-worker recordings (record.py) into the run-time store (one zip file).

    python -m diffexec.build <rec dir> <out .zip> [tests.json]

Caps are applied AFTER merging all workers, so the store does not depend on how many workers
recorded it:

  DIFFEXEC_CALL_CAP      max calls of a function over the whole suite         (default 50,000)
  DIFFEXEC_STORE_BUDGET  estimated packed bytes of the whole store (default 13.5e6, which
                         gives a zip of about 13 MB)
  DIFFEXEC_STAKES_POWER  value = (seconds a change could save) ** this   (default 1.5)
  DIFFEXEC_EXPENSIVE_S   tests this long can be kept alone                (default 0.5 s)
  DIFFEXEC_LINE_WEIGHT   weight a function by its lines of code           (default 1 = on)
  DIFFEXEC_INPUT_CAP     max bytes of one call's input (applied while recording, 16 kB; over
                         it, the input is pickled with big containers elided, see serial.py)

Budget (two passes over the recordings, see choose()): per function, keeping all its droppable
tests, or only its expensive ones, is an option that costs the packed bytes of the inputs it
needs and is worth lines(f) * saving ** STAKES_POWER, where saving is what a commit changing
only f would save at the scorer's packing cost (test seconds from tests.json, never labels).
Options are taken greedily by worth per byte. A test that is not kept for a function gets its
phases marked incomplete, so it is never dropped for a change to that function; a function
with nothing kept is dead ("budget"). The hyperparameters (power 1.5, budget) were picked by
the four labeled sets' totals; which functions and tests are kept never looks at labels.

Tests that failed or errored while being recorded (record.py `failed`) get every phase marked
incomplete, and are listed in the index: the selector never drops them.

Zip layout (deterministic: sorted entries, fixed timestamps, LZMA):
  index.pkl   {"functions": {(path, qualname, firstlineno): (dead reason | None, n)},
               "failed": [test ids], "tests": [test ids], "caps": {...}}
  (<n> = a digest of the function key; functions are merged in DIFFEXEC_BUILD_BUCKETS buckets
   of keys, one at a time, so the build's memory is a fraction of the recordings)
  c/<n>.pkl   {"blobs": [(input bytes, 8-byte effect digest, status)],
               "uses": {phase: (blob idx..) | None}}   phase: "IMPORT" | "OTHER" | int
               (test number in index["tests"] * 3 + SETUP 0 / CALL 1 / TEARDOWN 2)
"""

from __future__ import annotations

import glob
import hashlib
import json
import lzma
import os
import pickle
import sys
import zipfile

CALL_CAP = int(os.environ.get("DIFFEXEC_CALL_CAP", "50000"))
STORE_BUDGET = float(os.environ.get("DIFFEXEC_STORE_BUDGET", "13.5e6"))
EXPENSIVE_S = float(os.environ.get("DIFFEXEC_EXPENSIVE_S", "0.5"))
LINE_WEIGHT = os.environ.get("DIFFEXEC_LINE_WEIGHT", "1") == "1"
STAKES_POWER = float(os.environ.get("DIFFEXEC_STAKES_POWER", "1.5"))
SOURCE_ROOT = os.environ.get("DIFFEXEC_ROOT", "/opt/sqlglot")


def _records(path: str):
    """(header, iterator of (key, record)) of one recording (record.py's pickle stream)."""
    fh = open(path, "rb")
    head = pickle.load(fh)
    if "recs" in head:  # format 1: one dict (whole file in memory)
        fh.close()
        return head, iter(head.pop("recs").items())

    def gen():
        with fh:
            while True:
                item = pickle.load(fh)
                if item is None:
                    return
                yield item
    return head, gen()


def bucket_of(key, buckets: int) -> int:
    return int.from_bytes(hashlib.blake2b(repr(key).encode(), digest_size=4).digest(), "big") % buckets


def headers(rec_dir: str) -> tuple[dict, set]:
    failed: set = set()
    stats = {"workers": 0, "pickle_s": 0.0, "recorded": 0, "elided": 0, "incomplete": 0}
    for path in sorted(glob.glob(os.path.join(rec_dir, "rec_*.pkl"))):
        with open(path, "rb") as fh:
            head = pickle.load(fh)
        stats["workers"] += 1
        for k in ("pickle_s", "recorded", "elided", "incomplete"):
            stats[k] += head["stats"].get(k, 0)
        failed.update(head.get("failed", ()))
    return stats, failed


def merge(rec_dir: str, bucket: int = 0, buckets: int = 1) -> dict:
    """Merge the recordings of the functions in one bucket (of `buckets`)."""
    merged: dict = {}
    for path in sorted(glob.glob(os.path.join(rec_dir, "rec_*.pkl"))):
        _, recs = _records(path)
        for key, r in recs:
            if buckets > 1 and bucket_of(key, buckets) != bucket:
                continue
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
    return merged


def prepare(m: dict, failed: set) -> None:
    """Call cap, tests that failed while recorded, unused blobs."""
    if not m["dead"] and m["calls"] > CALL_CAP:
        m["dead"] = "calls"
    if m["dead"]:
        m["blobs"], m["uses"] = {}, {}
        return
    for phase in m["uses"]:
        if isinstance(phase, tuple) and phase[1] in failed:
            m["uses"][phase] = None
    used = set().union(*(hs for hs in m["uses"].values() if hs)) if m["uses"] else set()
    m["blobs"] = {h: b for h, b in m["blobs"].items() if h in used}


def _needs(m: dict) -> tuple[set, dict]:
    """(blobs of the calls outside tests, {droppable test: blobs it needs}). A test needs its own
    phases and the SETUP (setUpClass) phases of its classmates, all complete."""
    uses = m["uses"]
    glob_need: set = set()
    by_test: dict = {}
    class_setups: dict = {}
    for p, hs in uses.items():
        if not isinstance(p, tuple):
            if hs:
                glob_need |= hs
            continue
        by_test.setdefault(p[1], []).append(p)
        if p[0] == "SETUP":
            class_setups.setdefault(_class_of(p[1]), []).append(p)
    need = {}
    for tid, phases in by_test.items():
        phases = set(phases) | set(class_setups.get(_class_of(tid), ()))
        if any(uses[p] is None for p in phases):
            continue
        need[tid] = set().union(*(uses[p] for p in phases))
    return glob_need, need


def plan_of(m: dict, secs_of: dict) -> dict | None:
    """What keeping this function costs and is worth (see choose)."""
    if m["dead"] or any(not isinstance(p, tuple) and hs is None for p, hs in m["uses"].items()):
        return None  # dead, or a call outside tests is incomplete: no test can ever be dropped
    glob_need, need = _needs(m)
    if not need:
        return None
    size = {h: len(b[0]) for h, b in m["blobs"].items()}
    raw = b"".join(m["blobs"][h][0] for h in sorted(m["blobs"]))
    # packed size estimate: a fast preset on at most 4 MB (deterministic)
    sample = raw[:4_000_000]
    packed = len(lzma.compress(sample, preset=1)) * len(raw) / len(sample) if raw else 1
    ratio = max(1.0, len(raw) / max(packed, 1))
    big = sorted(t for t in need if secs_of.get(t, 0.0) >= EXPENSIVE_S)
    big_need = set(glob_need).union(*(need[t] for t in big)) if big else set()
    return {"ratio": ratio, "seen": sorted({p[1] for p in m["uses"] if isinstance(p, tuple)}),
            "all": sorted(need), "all_raw": len(raw),
            "big": big, "big_raw": sum(size[h] for h in big_need)}


def _saving(seen: list, dropped: list, ms_of: dict) -> float:
    """Seconds saved on a commit that changes only this function and selects the tests that
    call it, if `dropped` of them are dropped (the scorer's packing cost, selector/pack.py)."""
    from selector import pack
    nums = {ms_of[t][0] for t in seen if t in ms_of}
    rest = nums - {ms_of[t][0] for t in dropped if t in ms_of}
    ms = {n: m for n, m in ms_of.values()}
    return (pack.cost_ms(pack.pack(nums, ms), ms) - pack.cost_ms(pack.pack(rest, ms), ms)) / 1000


_LINES: dict = {}


def lines_of(key) -> int:
    """Lines of the function's own body that hold code (the pinned source; nested functions
    not counted): a single-line edit is about as likely on any of them."""
    path, qualname, first = key
    if path not in _LINES:
        import types
        got: dict = {}
        try:
            with open(os.path.join(SOURCE_ROOT, path), "rb") as fh:
                todo = [compile(fh.read(), path, "exec", dont_inherit=True)]
        except (OSError, SyntaxError, ValueError):
            todo = []
        while todo:
            c = todo.pop()
            got[(c.co_qualname, c.co_firstlineno)] = len({ln for _, _, ln in c.co_lines()
                                                         if ln is not None and ln != c.co_firstlineno})
            todo.extend(k for k in c.co_consts if isinstance(k, types.CodeType))
        _LINES[path] = got
    return max(1, _LINES[path].get((qualname, first), 1))


def choose(plans: dict, secs_of: dict, budget: float) -> dict:
    """Which (function, test) pairs the store keeps, within `budget` packed bytes.

    Per function two options: keep only its expensive tests (>= EXPENSIVE_S), or keep all its
    droppable tests. An option is worth what it would save on a commit that changes only that
    function (a function is as likely to change as it has lines of code, lines_of), at the
    scorer's packing cost; it costs the
    packed bytes of the inputs it needs. Options are taken greedily by worth per byte; "all"
    after "expensive" pays the difference. Test seconds come from tests.json, never labels."""
    ms_of = {t: (i, round(s * 1000)) for i, (t, s) in enumerate(sorted(secs_of.items()))}
    items = []
    for key, pl in plans.items():
        r = pl["ratio"]
        w = lines_of(key) if LINE_WEIGHT else 1
        if pl["big"]:
            v = w * _saving(pl["seen"], pl["big"], ms_of) ** STAKES_POWER
            items.append((v / (pl["big_raw"] / r + 200), key, "big", v, pl["big_raw"] / r + 200))
        v = w * _saving(pl["seen"], pl["all"], ms_of) ** STAKES_POWER
        items.append((v / (pl["all_raw"] / r + 200), key, "all", v, pl["all_raw"] / r + 200))
    items.sort(key=lambda it: (-it[0], repr(it[1]), it[2]))
    kept: dict = {}
    paid: dict = {}
    total = 0.0
    for _, key, kind, v, cost in items:
        if v <= 0 or kept.get(key) == "all":
            continue
        extra = cost - paid.get(key, 0.0)
        if kind == "big" and key in kept:
            continue
        if total + extra > budget:
            continue
        total += extra
        paid[key] = cost
        kept[key] = kind
    return {key: set(plans[key]["all" if kind == "all" else "big"]) for key, kind in kept.items()}


def restrict(m: dict, tests: set | None) -> None:
    """Keep the blobs of the calls outside tests and of `tests`; mark every other test's
    phases incomplete. None: nothing kept, the function is dead ("budget")."""
    if m["dead"]:
        return
    if not tests:
        m["dead"] = "budget"
        m["blobs"], m["uses"] = {}, {}
        return
    glob_need, need = _needs(m)
    keep = set(glob_need)
    for tid in tests:
        keep |= need[tid]
    for p, hs in m["uses"].items():
        if isinstance(p, tuple) and hs is not None and not (p[1] in tests or hs <= keep):
            m["uses"][p] = None
    m["blobs"] = {h: b for h, b in m["blobs"].items() if h in keep}


def _class_of(tid: str) -> str:
    parts = tid.split("::")
    return "::".join(parts[:-1]) if len(parts) > 2 else tid


def _entry(zf: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_LZMA
    info.external_attr = 0o644 << 16
    zf.writestr(info, data)


def entry_name(key) -> str:
    return hashlib.blake2b(repr(key).encode(), digest_size=6).hexdigest()


KINDS = ("SETUP", "CALL", "TEARDOWN")


def write_functions(zf: zipfile.ZipFile, merged: dict, functions: dict, test_no: dict) -> None:
    """One entry per live function. A test phase (kind, test id) is written as the int
    test_no[test id] * 3 + KINDS.index(kind) (index.pkl "tests" maps back). Effect digests are
    cut to 8 bytes; inputs whose recording is not "ok" are written empty (never replayed)."""
    for key, m in sorted(merged.items()):
        if m["dead"]:
            functions[key] = (m["dead"], None)
            continue
        n = entry_name(key)
        order = sorted(m["blobs"])
        pos = {h: i for i, h in enumerate(order)}

        def code(p):
            return p if not isinstance(p, tuple) else test_no[p[1]] * 3 + KINDS.index(p[0])
        uses = sorted(((code(p), hs) for p, hs in m["uses"].items()), key=lambda kv: repr(kv[0]))
        blobs = []
        for h in order:
            data, d, status = m["blobs"][h]
            ok = status == "ok" and d is not None
            blobs.append((data if ok else b"", d[:8] if ok else None, status))
        body = {"blobs": blobs,
                "uses": {p: (None if hs is None else tuple(sorted(pos[h] for h in hs)))
                         for p, hs in uses}}
        _entry(zf, f"c/{n}.pkl", pickle.dumps(body, protocol=5))
        functions[key] = (None, n)


def build(rec_dir: str, out: str, secs_of: dict, buckets: int) -> dict:
    stats, failed = headers(rec_dir)
    # pass 1: what each function's tests would cost; then choose globally
    plans: dict = {}
    test_ids: set = set()
    for bucket in range(buckets):
        merged = merge(rec_dir, bucket, buckets)
        for key, m in merged.items():
            test_ids.update(p[1] for p in m["uses"] if isinstance(p, tuple))
            prepare(m, failed)
            got = plan_of(m, secs_of)
            if got is not None:
                plans[key] = got
        del merged
    kept = choose(plans, secs_of, STORE_BUDGET)
    tests = sorted(test_ids)
    test_no = {t: i for i, t in enumerate(tests)}
    # pass 2: write what was chosen
    functions: dict = {}
    dead: dict = {}
    incomplete = 0
    tmp = out + ".tmp"
    with zipfile.ZipFile(tmp, "w") as zf:
        for bucket in range(buckets):
            merged = merge(rec_dir, bucket, buckets)
            for key, m in merged.items():
                prepare(m, failed)
                restrict(m, kept.get(key))
            write_functions(zf, merged, functions, test_no)
            for m in merged.values():
                if m["dead"]:
                    dead[m["dead"]] = dead.get(m["dead"], 0) + 1
                incomplete += sum(1 for v in m["uses"].values() if v is None)
            del merged
        index = {"functions": functions, "failed": sorted(failed), "tests": tests,
                 "caps": {"calls": CALL_CAP, "store_budget": STORE_BUDGET}}
        _entry(zf, "index.pkl", pickle.dumps(index, protocol=5))
    os.replace(tmp, out)
    stats.update({"functions": len(functions), "alive": sum(1 for v in functions.values() if not v[0]),
                  "tests_kept": sum(len(v) for v in kept.values()),
                  "failed_tests": len(failed), "zip_bytes": os.path.getsize(out),
                  "dead_by_reason": dead, "incomplete_phases": incomplete})
    return stats


def test_seconds(path: str | None) -> dict:
    if not path:
        return {}
    with open(path) as fh:
        return {t["id"]: float(t["seconds"]) for t in json.load(fh)["tests"]}


if __name__ == "__main__":
    print(build(sys.argv[1], sys.argv[2], test_seconds(sys.argv[3] if len(sys.argv) > 3 else None),
                int(os.environ.get("DIFFEXEC_BUILD_BUCKETS", "8"))))
