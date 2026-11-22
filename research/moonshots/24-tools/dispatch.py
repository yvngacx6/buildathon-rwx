#!/usr/bin/env python3
"""Moonshot 24: split the gap.py "function map" bucket by whether the extra tests ran a
touched STATEMENT or only the function, and by what diffexec said about them.

Usage (repo root, pinned image): python3 research/moonshots/24-tools/dispatch.py SET... [--json out]

Per commit: N = final selection (after diffexec etc., near-ALL rule as gap.py), F = failing.
For every fast extra test credited ONLY to "function map" (gap.py's exclusive extras):
  A  ran no touched statement of any function region that selected it (R23 drops it)
  B  ran a touched statement (only instruction- or value-level reasoning can drop it)
  C  region has no statement view or its def changes code shape (R23 keeps it)
Cost per class = greedy drop A, then C, then B (seconds, scorer pack model).
B is further split by diffexec's verdict (why_kept / commit note).
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path.cwd()
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "eval"))

import gap  # noqa: E402
from selector import pack  # noqa: E402
from selector.select import ALL_FRACTION, Selector  # noqa: E402

_SHAPE = ("co_argcount", "co_posonlyargcount", "co_kwonlyargcount", "co_nlocals",
          "co_flags", "co_varnames", "co_cellvars", "co_freevars", "co_name")


def _code_shape(tree, chain):
    tops = [s for s in tree.body if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                   ast.ClassDef)) and s.name == chain[0]]
    if len(tops) != 1:
        return None
    try:
        code = compile(ast.Module(body=tops, type_ignores=[]), "<shape>", "exec")
    except (SyntaxError, ValueError, TypeError):
        return None
    for name in chain:
        found = [c for c in code.co_consts if isinstance(c, type(code)) and c.co_name == name]
        if len(found) != 1:
            return None
        code = found[0]
    out, todo = [], [code]
    while todo:
        c = todo.pop()
        out.append(tuple(getattr(c, k) for k in _SHAPE))
        todo.extend(x for x in c.co_consts if isinstance(x, type(c)))
    return out


def same_shape(old, new, chain):
    if not chain:
        return False
    o = _code_shape(old, chain)
    return o is not None and o == _code_shape(new, chain)


HAS_OLD = hasattr(Selector, "_slow_tests_off_statements")


class DSel(gap.TracingSelector):
    def __init__(self, *a, **k):
        self.regions = []
        self._trees = (None, None)
        super().__init__(*a, **k)

    def _library(self, path, old_tree, new_tree, found, new_text=""):
        self._trees = (old_tree, new_tree)
        return Selector._library(self, path, old_tree, new_tree, found, new_text)

    def _slow_tests_off_statements(self, path, ran, r, *rest):
        if HAS_OLD:
            got = Selector._slow_tests_off_statements(self, path, ran, r)
        else:
            got = Selector._off_statements(self, path, ran, r, *self._trees)
        stmt = None
        if r.statements is not None:
            stmt = set()
            for a, b in r.statements:
                stmt |= self.maps.span(path, a, b)
        old, new = self._trees
        self.regions.append({"path": path, "chain": list(r.chain), "ran": set(ran), "got": set(got),
                             "stmt": stmt, "shape": bool(old and new and same_shape(old, new, r.chain)),
                             "signature": bool(r.signature)})
        return got

    if not HAS_OLD:
        def _off_statements(self, path, ran, r, old_tree, new_tree):
            return self._slow_tests_off_statements(path, ran, r)


def dispatchy(chain):
    n = chain[-1] if chain else "<lambda>"
    return n.endswith("_sql") or n.startswith("_parse") or n == "<lambda>" or not chain


def analyse(data: Path):
    tests = json.loads((data / "tests.json").read_text())["tests"]
    answers = json.loads((data / "answers.json").read_text())["commits"]
    ms_of = {t["number"]: pack.duration_ms(t["seconds"]) for t in tests}
    sel = DSel(tests, Path(os.environ["BR_SQLGLOT"]), REPO / "maps")
    everything = sel.all_numbers()

    def cost(ns):
        return pack.cost_ms(pack.pack(ns, ms_of), ms_of) / 1000 if ns else 0.0

    all_cost = cost(everything)
    rows = []
    for patch in sorted((data / "commits").glob("*.patch")):
        commit = patch.stem
        ans = answers.get(commit, {"failing": []})
        F = set(ans["failing"])
        sel.credits, sel.regions = [], []
        if sel.diffexec is not None:
            sel.diffexec.why_kept = {}
        try:
            N, reason = sel.select(patch.read_text(errors="replace"))
        except Exception as e:  # noqa: BLE001
            N, reason = everything, f"ALL error {e}"
        N = set(N)
        if N != everything and cost(N) >= ALL_FRACTION * all_cost:
            N = set(everything)
        if N == everything:
            rows.append({"commit": commit, "all": True, "reason": reason[:200]})
            continue
        owners = defaultdict(set)
        for label, got in sel.credits:
            for n in got & N:
                owners[n].add(label)
        if N and sel._untraced:
            for n in sel._untraced & N:
                owners[n].add("untraced")
        excl = {n for n in N - F if owners.get(n) == {"function map"}}
        cls = {}
        why_kept = dict(getattr(sel.diffexec, "why_kept", {}) or {}) if sel.diffexec else {}
        for n in excl:
            regs = [g for g in sel.regions if n in g["got"]]
            if any(g["stmt"] is None or not g["shape"] for g in regs):
                c = "C"
            elif any(n in g["stmt"] for g in regs):
                c = "B"
            else:
                c = "A"
            cls[n] = c
        cur = set(N)
        part = {}
        for c in ("A", "C", "B"):
            drop = {n for n, k in cls.items() if k == c}
            before = cost(cur)
            cur -= drop
            part[c] = before - cost(cur)
        Bn = [n for n, k in cls.items() if k == "B"]
        dx = Counter(why_kept.get(sel._id_of[n], "not-replayed") for n in Bn)
        m = re.search(r"diffexec: [^;\]]*", reason)
        rows.append({
            "commit": commit, "all": False, "kind": ans.get("kind", "?"),
            "N": len(N), "F": len(F), "cost": cost(N), "oracle": cost(F),
            "fm_excl": len(excl), "A": part["A"], "B": part["B"], "C": part["C"],
            "nA": sum(1 for k in cls.values() if k == "A"),
            "nB": len(Bn), "nC": sum(1 for k in cls.values() if k == "C"),
            "dx": dict(dx), "dxnote": m.group(0)[:90] if m else "-",
            "regions": [{"chain": ".".join(g["chain"]), "path": g["path"],
                         "ran": len(g["ran"]), "got": len(g["got"]),
                         "stmt": None if g["stmt"] is None else len(g["stmt"] & g["ran"]),
                         "shape": g["shape"], "sig": g["signature"],
                         "dispatch": dispatchy(g["chain"])} for g in sel.regions],
        })
    return rows


def main():
    argv = sys.argv[1:]
    out = None
    if "--json" in argv:
        out = argv[argv.index("--json") + 1]
        argv.remove(out)
        argv.remove("--json")
    res = {}
    for name in argv:
        rows = analyse(gap.set_dir(name))
        res[name] = rows
        rs = [r for r in rows if not r["all"]]
        tot = {k: sum(r[k] for r in rs) for k in ("A", "B", "C", "cost", "oracle")}
        print(f"== {name}: {len(rows)} commits ({len(rows) - len(rs)} ALL) "
              f"cost {tot['cost']:.1f} oracle {tot['oracle']:.1f} | function-map exclusive extras: "
              f"A(fn only, R23 drops) {tot['A']:.1f}s  C(no stmt view) {tot['C']:.1f}s  "
              f"B(ran stmt) {tot['B']:.1f}s")
        dxs = Counter()
        for r in rs:
            for k, v in r["dx"].items():
                dxs[k] += v
        print("   B tests by diffexec verdict:", dict(dxs))
        for r in sorted(rs, key=lambda r: -(r["A"] + r["B"] + r["C"])):
            if r["A"] + r["B"] + r["C"] < 0.05:
                continue
            regs = "; ".join(f"{g['chain']}[{g['ran']}/{g['got']}/{g['stmt']}{'' if g['shape'] else ' !shape'}]"
                             for g in r["regions"])[:150]
            print(f"  {r['commit'][:10]:10s} {r['kind'][:10]:10s} A {r['A']:5.1f}/{r['nA']:<3d} "
                  f"C {r['C']:5.1f}/{r['nC']:<3d} B {r['B']:5.1f}/{r['nB']:<3d} F{r['F']:<3d} "
                  f"{r['dx']} {r['dxnote'][:50]} :: {regs}")
    if out:
        Path(out).write_text(json.dumps(res, indent=1, default=list))


if __name__ == "__main__":
    main()
