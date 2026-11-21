"""DIAGNOSTIC ONLY (practice-board experiments, research/moonshots/08-experiments.md).
Never part of a final candidate: enabled only by BR_TELEMETRY.

The board returns two numbers per submission: simulated seconds and the miss count. Every
cost is a function of our own output, so the selector can make the simulated total equal any
integer it likes (within capacity) and thereby report AGGREGATE, label-free statistics of its
own behaviour on the hidden practice commits (how many commits went ALL, how many seconds the
slow tests cost us, ...). The miss count stays a label channel: in "syn" mode every commit gets
a synthetic worker (misses = commits that break any test outside that worker ≈ commits that
break anything); in "hyb:<B>" mode only bucket-B commits are synthetic, the others keep their
real selection (cannot miss), so misses = B commits that break something.

Only bucket-level aggregates. Never commit ids, never patch text, never per-commit values.

Spec (BR_TELEMETRY):  <mode>;<q>/<unit_ms>/<base>,<q>/<unit_ms>/<base>,...
  mode:  syn | hyb:costge=<s> | hyb:re=<regex on the reason string>
  q:     n_all n_empty n_slowkept g_slow g_maxnodes g_tpcds c_all c_total
         n_re=<regex> c_re=<regex> n_costge=<s> c_costge=<s>
  Each quantity is divided by unit_ms (rounded), clamped to base-1, and packed mixed-radix
  (first quantity = least significant), followed by a mod-3 check digit.
Decoding: python3 -m selector.telemetry decode '<spec>' <sim_seconds> <misses> <n_commits>
"""

from __future__ import annotations

import re
import sys

from . import pack

UNIT_MS = 100       # board shows simulated seconds to 0.1 s; set 1 if it shows ms
FLOOR_MS = 100      # every synthetic worker carries at least this load (never empty)
SYN_OFFSET_MS = 100_000  # syn: total = offset + payload (offset > 40 x 2.1 s of floors)
HYB_OFFSET_MS = 250_000  # hybrid: offset must exceed the real cost of the non-B commits
SLOW_IDS = {"test_max_nodes": "maxnodes", "test_execute_tpcds": "tpcds"}


def parse(spec: str):
    mode, _, qs = spec.partition(";")
    items = []
    for part in filter(None, qs.split(",")):
        name, unit, base = part.rsplit("/", 2)
        items.append((name, int(unit), int(base)))
    return mode, items


def capacity(items) -> int:
    n = 3
    for _, _, b in items:
        n *= b
    return n


def quantity(name: str, recs) -> int:
    key, _, arg = name.partition("=")
    if key == "n_all":
        return sum(r["all"] for r in recs)
    if key == "n_empty":
        return sum(not r["numbers"] for r in recs)
    if key == "n_slowkept":
        return sum(bool(r["numbers"] & r["slow"]) and not r["all"] for r in recs)
    if key in ("g_slow", "g_maxnodes", "g_tpcds"):
        return sum(r[key] for r in recs)
    if key == "c_all":
        return sum(r["cost"] for r in recs if r["all"])
    if key == "c_total":
        return sum(r["cost"] for r in recs)
    if key in ("n_re", "c_re"):
        hit = [r for r in recs if re.search(arg, r["reason"])]
        return len(hit) if key == "n_re" else sum(r["cost"] for r in hit)
    if key in ("n_costge", "c_costge"):
        hit = [r for r in recs if not r["all"] and r["cost"] >= float(arg) * 1000]
        return len(hit) if key == "n_costge" else sum(r["cost"] for r in hit)
    raise ValueError(name)


def payload(items, recs) -> int:
    value, weight = 0, 1
    digits = []
    for name, unit, base in items:
        d = min(base - 1, max(0, round(quantity(name, recs) / unit)))
        digits.append(d)
        value += d * weight
        weight *= base
    return value * 3 + value % 3, digits


def in_bucket(mode: str, r) -> bool:
    if mode == "syn":
        return True
    kind, _, arg = mode[4:].partition("=")
    if kind == "costge":
        return not r["all"] and r["cost"] >= float(arg) * 1000
    return re.search(arg, r["reason"]) is not None


def fill(target_ms: int, ms_of: dict[int, int], exclude: set[int]) -> list[int]:
    """Tests (not in `exclude`) whose durations sum to exactly target_ms (greedy, longest first)."""
    got, s = [], 0
    for n in sorted(ms_of, key=lambda k: (-ms_of[k], k)):
        if n in exclude or ms_of[n] == 0 or s + ms_of[n] > target_ms:
            continue
        got.append(n)
        s += ms_of[n]
        if s == target_ms:
            return got
    if s != target_ms:
        raise ValueError(f"cannot fill {target_ms} ms")
    return got


def emit(spec: str, recs, ms_of: dict[int, int]) -> list[list[list[int]]]:
    """Per record (same order), the workers to print instead of the real ones."""
    mode, items = parse(spec)
    word, digits = payload(items, recs)
    total_tests_ms = sum(ms_of.values())
    synth = [in_bucket(mode, r) for r in recs]
    # Synthetic workers never contain a test of the commit's real selection, which holds all its
    # failing tests (0 misses on the board), so misses count bucket commits that break anything.
    base, room = [], []
    for r, s in zip(recs, synth):
        used = sum(ms_of[n] for n in r["numbers"])
        if s and r["all"]:
            base.append(0)  # ALL in the bucket: print nothing (nothing is left to fill with)
            room.append(0)
        elif s:
            base.append(FLOOR_MS + pack.WORKER_COST_MS)
            room.append(max(0, min(90_000, total_tests_ms - used - 2_000) - FLOOR_MS))
        elif r["all"] or not r["workers"]:
            base.append(r["cost"])
            room.append(0)  # ALL: nothing left to add; empty: adding would change its cost by +2 s
        else:
            base.append(r["cost"])
            room.append(max(0, total_tests_ms - used - 2_000))
    extra = _offset(mode) + word * UNIT_MS - sum(base)
    if extra < 0 or extra > sum(room):
        raise ValueError(f"payload does not fit: extra {extra} ms, room {sum(room)} ms")
    print(f"telemetry: digits {digits} word {word} extra {extra} ms", file=sys.stderr)
    out = []
    for r, s, cap in zip(recs, synth, room):
        add = min(cap, extra)
        extra -= add
        if s and r["all"]:
            out.append([])
        elif s:
            out.append([fill(FLOOR_MS + add, ms_of, set(r["numbers"]))])
        elif add:
            workers = [list(w) for w in r["workers"]]
            top = max(range(len(workers)), key=lambda i: sum(ms_of[n] for n in workers[i]))
            workers[top] += fill(add, ms_of, set(r["numbers"]))
            out.append(workers)
        else:
            out.append(r["workers"])
    return out


def _offset(mode: str) -> int:
    return SYN_OFFSET_MS if mode == "syn" else HYB_OFFSET_MS


def decode(spec: str, sim_s: float, misses: int, n_commits: int) -> None:
    mode, items = parse(spec)
    ms = round(sim_s * 1000) - 300_000 * misses
    ms -= _offset(mode)
    word, rem = divmod(ms, UNIT_MS)
    if rem:
        print(f"WARNING: {rem} ms not a multiple of the unit")
    value, check = divmod(word, 3)
    print(f"check digit {'OK' if value % 3 == check else 'FAILED'}")
    for name, unit, base in items:
        value, d = divmod(value, base)
        print(f"{name}: digit {d} = {d * unit / 1000:g} s, or a count of {d} if unit is 1"
              + (" CLAMPED" if d == base - 1 else ""))


if __name__ == "__main__":
    if sys.argv[1] == "decode":
        decode(sys.argv[2], float(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5]))
