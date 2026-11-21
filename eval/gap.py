#!/usr/bin/env python3
"""Oracle-gap report (idea I14): where does our cost exceed perfect knowledge, and which
selector mechanism put the extra tests there?

Usage:
  python3 eval/gap.py                                 # sample, private, private41, adversarial
  python3 eval/gap.py sample private41 -v             # + per-commit rows, largest gap first
  python3 eval/gap.py --json out.json ...             # also dump every row as JSON

Set names: `sample` → sample/; any other name → $BR_EVAL_DATA/<name> (default eval/data/<name>,
gitignored); or a directory path. Each set is commits/*.patch + tests.json + answers.json.
Needs BR_SQLGLOT (default ~/rwx-research-robust/sqlglot) and the name index / state snapshot
(`python3 -m selector.nameindex build $BR_SQLGLOT index/names.pkl`).

Per commit:
  ours    the scorer cost of our selection (same loop as selector/__main__.py: near-ALL rule,
          error → ALL), in seconds; + 300 s if it misses a failing test
  oracle  the cost of packing exactly the failing tests with selector.pack (0 if none)
  gap     ours − oracle
  reason  the selector's stderr reason

Mechanisms. The selector runs in-process with its rule methods wrapped; each top-level rule
call made while selecting one file is credited with the tests it returned:

  function map            function region → tests that ran it (lines map), fast tests
  statement rule (slow)   … slow tests that ran a touched statement (or the whole-function
                          fallback when there is no statement view)
  signature class users   decorator / def-line change → users of the enclosing classes
  import-time map         a function that runs while some module is imported
  name narrowing          class / module assignment → readers of the changed names (names.py)
  state diff              class-creation consumers → readers of changed import-time values
  conservative def-time   class / module body, nothing narrower → class users ∪ file runners
  library file readers    tests that read the edited sqlglot/ file as data (reads map)
  import bound            tests that load the module (imports map)
  fixture readers         tests/fixtures/** or other data file → reads map
  test-file functions     edited test functions (+ helper functions / names in a test file)
  test-file whole file    test-file setup / helper code → the whole file ∪ importers
  test helper functions / names   helper module function / constant → tests that ran it
  test helper importers   helper module → importing test files
  untraced tests          tests in no map; join every non-empty selection
  ALL fallback: <why>     a rule raised AllTests (whole gap)
  near-ALL rule           selection ≥ 85 % of ALL → ALL; credited with cost(ALL) − cost(selection)

Gap attribution per commit (sums exactly to the gap when nothing is missed): the extra tests
(selected, not failing) each mechanism alone contributed are removed greedily, largest cost
drop first, and each drop is credited to its mechanism; extra tests that several mechanisms
contributed are removed last, per combination (`shared: a + b`); what remains is `packing` (the cost of the failing
tests themselves packed together with nothing else is the oracle, so this is 0 unless a
miss). An ALL fallback commit credits its whole gap to the fallback.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from selector import pack  # noqa: E402
from selector.select import ALL_FRACTION, AllTests, Selector  # noqa: E402

MISS_PENALTY_S = 300.0
DEFAULT_SETS = ["sample", "private", "private41", "adversarial"]

# Rule methods whose top-level result is credited to a mechanism.
WRAPPED = ["_slow_tests_off_statements", "_class_users", "_function_at_import", "_import_time",
           "_definition_time",
           "_import_bound", "_by_names", "_by_state", "_test_code"]

# ALL fallback reasons, grouped for the aggregate table (first match wins).
ALL_GROUPS = [
    ("binding removed", "binding removed"),
    ("base class / module body", "base class body"),
    ("runs during `import sqlglot`", "runs at import sqlglot"),
    ("loaded at collection", "module loaded at collection"),
    ("removed test file", "removed test file"),
    ("hunk does not apply", "hunk does not apply"),
    ("re-exported", "names re-exported"),
    ("test configuration", "test configuration"),
    ("unknown file kind", "unknown file kind"),
    ("error:", "selector error"),
]


def set_dir(name: str) -> Path:
    if name == "sample":
        return REPO / "sample"
    if os.sep in name or Path(name).is_dir():
        return Path(name)
    return Path(os.environ.get("BR_EVAL_DATA", REPO / "eval" / "data")) / name


class TracingSelector(Selector):
    """Selector that records, per file, which rule returned which tests."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.credits: list[tuple[str, set[int]]] = []
        self._depth = 0
        for name in WRAPPED:
            setattr(self, name, self._wrap(name, getattr(self, name)))

    def _wrap(self, name, method):
        def call(*args, **kwargs):
            outer = self._depth == 0
            self._depth += 1
            try:
                result = method(*args, **kwargs)
            finally:
                self._depth -= 1
            if name in ("_by_names", "_by_state"):
                self._def_success = (name, set(result[0]))
            if outer:
                self._credit(name, result, args)
            return result
        return call

    def _credit(self, name, result, args) -> None:
        if name == "_slow_tests_off_statements":
            got = set(result)
            slow = got & self._slow
            self.credits.append(("function map", got - slow))
            if slow:
                self.credits.append(("statement rule (slow)", slow))
        elif name == "_class_users":
            self.credits.append(("signature class users", set(result)))
        elif name == "_function_at_import":  # (tests, import state unchanged)
            self.credits.append(("import-time map", set(result[0])))
        elif name == "_import_time":
            self.credits.append(("import-time map", set(result)))
        elif name == "_definition_time":
            got = set(result)
            how, narrowed = getattr(self, "_def_success", (None, None))
            label = {"_by_names": "name narrowing", "_by_state": "state diff"}.get(how)
            if label is None or narrowed != got:
                label = "conservative def-time"
            self.credits.append((label, got))
        elif name == "_import_bound":
            self.credits.append(("import bound", set(result[0])))
        elif name == "_test_code":
            got, why = result
            label = ("test-file functions" if why.startswith("test functions") else
                     "test helper functions / names" if why.startswith("test helper functions")
                     else "test helper importers" if why.startswith("test helper") else
                     "test-file whole file")
            self.credits.append((label, set(got)))

    def _definition_time(self, *args, **kwargs):  # reset the inner-success marker per call
        self._def_success = (None, None)
        return Selector._definition_time(self, *args, **kwargs)

    def _select_file(self, fp):
        start = len(self.credits)
        got, why = super()._select_file(fp)
        seen = set().union(*(s for _, s in self.credits[start:])) if len(self.credits) > start else set()
        rest = set(got) - seen
        if rest:
            label = ("fixture readers" if why.startswith(("fixture", "data file")) else
                     "library file readers" if fp.path.startswith("sqlglot/") else
                     f"other ({why[:40]})")
            self.credits.append((label, rest))
        return got, why


def all_group(reason: str) -> str:
    for needle, group in ALL_GROUPS:
        if needle in reason:
            return f"ALL fallback: {group}"
    return "ALL fallback: other"


def analyse(data: Path) -> dict:
    tests = json.loads((data / "tests.json").read_text())["tests"]
    answers = json.loads((data / "answers.json").read_text())["commits"]
    ms_of = {t["number"]: pack.duration_ms(t["seconds"]) for t in tests}
    os.environ.setdefault("BR_SQLGLOT", str(Path.home() / "rwx-research-robust" / "sqlglot"))
    sel = TracingSelector(tests, Path(os.environ["BR_SQLGLOT"]), REPO / "maps")
    everything = sel.all_numbers()
    all_cost = pack.cost_ms(pack.pack(everything, ms_of), ms_of)

    def cost(numbers: set[int]) -> int:
        return pack.cost_ms(pack.pack(numbers, ms_of), ms_of)

    rows = []
    for patch in sorted((data / "commits").glob("*.patch")):
        commit = patch.stem
        ans = answers.get(commit, {"failing": [], "kind": "?"})
        failing = set(ans["failing"])
        sel.credits = []
        try:
            numbers, reason = sel.select(patch.read_text(errors="replace"))
        except Exception as exc:  # as selector/__main__.py: any error → ALL
            numbers, reason = everything, f"ALL (error: {type(exc).__name__}: {exc})"
        credits = list(sel.credits)
        if numbers and sel._untraced:
            credits.append(("untraced tests", set(sel._untraced) & numbers))
        attribution: dict[str, float] = defaultdict(float)
        near_all = (numbers != everything and cost(numbers) >= ALL_FRACTION * all_cost)
        is_all = numbers == everything and not near_all
        base = set(numbers)
        if near_all:
            attribution["near-ALL rule"] += (all_cost - cost(numbers)) / 1000
            reason = f"ALL (selection ~ALL cost) {reason}"
            numbers = everything
        ours = cost(numbers) / 1000
        missed = failing - numbers
        oracle = cost(failing) / 1000
        ours_scored = ours + (MISS_PENALTY_S if missed else 0.0)
        gap = ours_scored - oracle
        if is_all:
            attribution[all_group(reason)] += gap
        else:
            # Greedy removal of each mechanism's exclusive extra tests.
            owners: dict[int, set[str]] = defaultdict(set)
            for label, got in credits:
                for n in got & base:
                    owners[n].add(label)
            extra = base - failing
            exclusive: dict[str, set[int]] = defaultdict(set)
            shared: dict[str, set[int]] = defaultdict(set)
            for n in extra:
                labels = owners.get(n) or {"unattributed"}
                if len(labels) == 1:
                    exclusive[next(iter(labels))].add(n)
                else:
                    shared["shared: " + " + ".join(sorted(labels))].add(n)
            current = set(base)
            while exclusive:
                now = cost(current)
                label = max(exclusive, key=lambda m: (now - cost(current - exclusive[m]), m))
                current -= exclusive.pop(label)
                attribution[label] += (now - cost(current)) / 1000
            for label, group in sorted(shared.items(), key=lambda kv: -len(kv[1])):
                now = cost(current)
                current -= group
                attribution[label] += (now - cost(current)) / 1000
            rest = cost(current) / 1000 + (MISS_PENALTY_S if missed else 0.0) - oracle
            if abs(rest) > 1e-9:
                attribution["packing / misses"] += rest
        top = max(attribution, key=attribution.get) if attribution else "-"
        rows.append({"commit": commit, "kind": ans.get("kind", "?"), "ours": round(ours, 3),
                     "oracle": round(oracle, 3), "gap": round(gap, 3), "missed": sorted(missed),
                     "selected": len(numbers), "failing": len(failing), "top": top,
                     "attribution": {k: round(v, 3) for k, v in attribution.items() if v},
                     "reason": reason})
    return {"rows": rows, "all_cost": all_cost / 1000}


def report(name: str, result: dict, verbose: bool) -> None:
    rows = result["rows"]
    ours = sum(r["ours"] for r in rows) + MISS_PENALTY_S * sum(bool(r["missed"]) for r in rows)
    oracle = sum(r["oracle"] for r in rows)
    misses = [r["commit"] for r in rows if r["missed"]]
    print(f"\n== {name}: {len(rows)} commits  ours {ours:.1f}s  oracle {oracle:.1f}s  "
          f"gap {ours - oracle:.1f}s  misses {len(misses)} {misses[:5]}")
    by_mech: dict[str, list] = defaultdict(lambda: [0.0, 0])
    for r in rows:
        for m, v in r["attribution"].items():
            by_mech[m][0] += v
            by_mech[m][1] += 1
    print(f"  {'mechanism':52s} {'gap s':>8s} {'commits':>7s}")
    for m, (v, n) in sorted(by_mech.items(), key=lambda kv: -kv[1][0]):
        print(f"  {m[:52]:52s} {v:8.1f} {n:7d}")
    by_kind: dict[str, list] = defaultdict(lambda: [0.0, 0.0, 0])
    for r in rows:
        k = by_kind[r["kind"]]
        k[0] += r["gap"]
        k[1] += r["oracle"]
        k[2] += 1
    print(f"  {'kind':42s} {'gap s':>8s} {'oracle':>7s} {'n':>4s}")
    for kind, (g, o, n) in sorted(by_kind.items(), key=lambda kv: -kv[1][0])[:15]:
        print(f"  {kind[:42]:42s} {g:8.1f} {o:7.1f} {n:4d}")
    if verbose:
        print(f"  {'commit':10s} {'kind':12s} {'ours':>6s} {'orcl':>6s} {'gap':>6s}  attribution :: reason")
        for r in sorted(rows, key=lambda r: -r["gap"]):
            if r["gap"] <= 0:
                continue
            att = ", ".join(f"{m} {v:.1f}" for m, v in sorted(r["attribution"].items(),
                                                              key=lambda kv: -kv[1]))
            print(f"  {r['commit'][:10]:10s} {r['kind'][:12]:12s} {r['ours']:6.1f} {r['oracle']:6.1f} "
                  f"{r['gap']:6.1f}  {att} :: {r['reason'][:160]}")


def main() -> None:
    argv = sys.argv[1:]
    verbose = "-v" in argv
    dump = None
    if "--json" in argv:
        dump = argv[argv.index("--json") + 1]
        argv.remove(dump)
    names = [a for a in argv if not a.startswith("-")] or DEFAULT_SETS
    out = {}
    for name in names:
        data = set_dir(name)
        if not (data / "answers.json").is_file():
            print(f"{name}: no labeled set at {data}", file=sys.stderr)
            continue
        out[name] = analyse(data)
        report(name, out[name], verbose)
    if len(out) > 1:
        total: dict[str, list] = defaultdict(lambda: [0.0, 0])
        for result in out.values():
            for r in result["rows"]:
                for m, v in r["attribution"].items():
                    total[m][0] += v
                    total[m][1] += 1
        gap = sum(r["gap"] for result in out.values() for r in result["rows"])
        print(f"\n== TOTAL over {', '.join(out)}: gap {gap:.1f}s")
        for m, (v, n) in sorted(total.items(), key=lambda kv: -kv[1][0]):
            print(f"  {m[:52]:52s} {v:8.1f} {n:7d}")
    if dump:
        Path(dump).write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
