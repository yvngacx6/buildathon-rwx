"""Measure the diffexec pruner on a labeled set (selector in-process, store given by env).

    BR_SQLGLOT=... BR_DIFFEXEC_STORE=... PYTHONHASHSEED=0 python -m diffexec.measure <set dir>

Per commit with diffexec candidates: candidates, how many fail (labels), how many were
dropped, cost before / after. A dropped failing test is a miss caused by diffexec (must be 0).
Failing candidates that were kept are split by reason:
  differs     a replay of the patched function gave another effect (the comparison worked)
  incomplete  a phase had unpicklable inputs
  unseen      no recorded call in the test's own phases
  resource    the test measures time / memory / gc
  no-data     the whole commit was kept before the comparison (function dead, no calls,
              precheck, bail, mixed edit, budget) – the recorder could not help
"""

from __future__ import annotations

import collections
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
    tot = collections.Counter()
    kept_why = collections.Counter()
    seen_prune = []
    orig = de.prune

    def spy(jobs, benefit_ms=1):
        de.why_kept = {}
        dropped, note = orig(jobs, benefit_ms)
        seen_prune.append((jobs, dropped, note, dict(de.why_kept)))
        return dropped, note

    de.prune = spy
    child = 0.0
    for p in sorted((data / "commits").glob("*.patch")):
        seen_prune.clear()
        c0 = resource.getrusage(resource.RUSAGE_CHILDREN)
        nums, why = sel.select(p.read_text(errors="replace"))
        c1 = resource.getrusage(resource.RUSAGE_CHILDREN)
        child += (c1.ru_utime - c0.ru_utime) + (c1.ru_stime - c0.ru_stime)
        if not seen_prune:
            continue
        jobs, dropped, note, why_kept = seen_prune[0]
        failing = set(answers[p.stem]["failing"])
        cands = set().union(*(n for _, _, n in jobs))
        drop = set().union(*dropped.values()) if dropped else set()
        before = nums | drop
        cost_b = pack.cost_ms(pack.pack(before, ms_of), ms_of)
        cost_a = pack.cost_ms(pack.pack(nums, ms_of), ms_of)
        tot["commits_with_candidates"] += 1
        tot["replayed"] += "dropped" in note
        tot["cands"] += len(cands)
        tot["fail_cands"] += len(cands & failing)
        tot["dropped"] += len(drop)
        tot["bad_drops"] += len((failing & drop) - nums)
        tot["saved_ms"] += cost_b - cost_a
        for n in (cands & failing) - drop:
            kept_why[why_kept.get(sel._id_of[n], "no-data") if "dropped" in note else "no-data"] += 1
        if drop or cands & failing or cost_b > 3000:
            print(f"{p.stem:14s} cands={len(cands):4d} failing={len(cands & failing):3d} "
                  f"dropped={len(drop):4d} cost {cost_b / 1000:5.1f}->{cost_a / 1000:5.1f}s :: {note[:90]}")
    tot["child_cpu_s"] = round(child, 2)
    print(json.dumps(dict(tot)), "failing kept by reason:", dict(kept_why))


if __name__ == "__main__":
    main()
