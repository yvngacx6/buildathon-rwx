"""Measure the diffexec pruner on a labeled set (selector in-process, store given by env).

    BR_SQLGLOT=... BR_DIFFEXEC_STORE=... python -m diffexec.measure <set dir>

Per commit where the replay ran: candidates diffexec could drop, how many of them fail
(labels), how many were dropped, and the cost before / after. A dropped failing test is a
miss caused by diffexec (must be 0). "failing candidates kept" counts the failing tests that
diffexec examined and correctly kept: the evidence that it discriminates.
"""

from __future__ import annotations

import json
import os
import resource
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from selector import pack  # noqa: E402
from selector.select import Selector  # noqa: E402


def main() -> None:
    data = Path(sys.argv[1])
    tests = json.loads((data / "tests.json").read_text())["tests"]
    answers = json.loads((data / "answers.json").read_text())["commits"]
    ms_of = {t["number"]: pack.duration_ms(t["seconds"]) for t in tests}
    sel = Selector(tests, Path(os.environ["BR_SQLGLOT"]), Path(__file__).resolve().parent.parent / "maps")
    de = sel.diffexec
    assert de is not None, "no store"
    tot = {"ran": 0, "cands": 0, "fail_cands": 0, "dropped": 0, "bad_drops": 0, "saved_ms": 0,
           "child_cpu": 0.0}
    seen_prune = []
    orig = de.prune

    def spy(jobs, benefit_ms=1):
        dropped, note = orig(jobs, benefit_ms)
        seen_prune.append((jobs, dropped, note))
        return dropped, note

    de.prune = spy
    for p in sorted((data / "commits").glob("*.patch")):
        seen_prune.clear()
        c0 = resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime
        nums, why = sel.select(p.read_text(errors="replace"))
        tot["child_cpu"] += resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime - c0
        if not seen_prune:
            continue
        jobs, dropped, note = seen_prune[0]
        if "dropped" not in note:
            continue
        failing = set(answers[p.stem]["failing"])
        cands = set().union(*(n for _, _, n in jobs))
        drop = set().union(*dropped.values()) if dropped else set()
        before = nums | drop
        cost_b = pack.cost_ms(pack.pack(before, ms_of), ms_of)
        cost_a = pack.cost_ms(pack.pack(nums, ms_of), ms_of)
        tot["ran"] += 1
        tot["cands"] += len(cands)
        tot["fail_cands"] += len(cands & failing)
        tot["dropped"] += len(drop)
        tot["bad_drops"] += len((failing & drop) - nums)
        tot["saved_ms"] += cost_b - cost_a
        print(f"{p.stem:14s} cands={len(cands):4d} failing_cands={len(cands & failing):3d} "
              f"dropped={len(drop):4d} failing_dropped={len(failing & drop - nums)} "
              f"cost {cost_b / 1000:.1f}->{cost_a / 1000:.1f}s :: {note[:80]}")
    tot["child_cpu"] = round(tot["child_cpu"], 2)
    print(json.dumps(tot))


if __name__ == "__main__":
    main()
