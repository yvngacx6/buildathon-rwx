"""Run the selector on labeled sets and dump per-(commit, slow test) features."""
import ast, json, os, sys, time
from pathlib import Path
REPO = Path(sys.argv[1]); sys.path.insert(0, str(REPO))
from selector import pack, diff
from selector.select import Selector, ALL_FRACTION
import gc; gc.disable()
SLOW = [1490, 1106, 1224, 1107, 1123]
out = open(sys.argv[2], "w")
sets = sys.argv[3:]
sqlglot = Path(os.environ["BR_SQLGLOT"])
tests = json.loads((REPO / "sample/tests.json").read_text())["tests"]
ms_of = {t["number"]: pack.duration_ms(t["seconds"]) for t in tests}
sel = Selector(tests, sqlglot, REPO / "maps")
everything = sel.all_numbers()
all_workers = pack.pack(everything, ms_of)
all_cost = pack.cost_ms(all_workers, ms_of)
cap = {}
if sel.diffexec is not None:
    orig = sel.diffexec.prune

    def prune(*a, **k):
        sel.diffexec.why_kept = {}
        r = orig(*a, **k)
        cap["why"] = dict(sel.diffexec.why_kept)
        cap["note"] = r[1]
        cap["cand"] = set().union(*[set(j[2]) for j in a[0]]) if a[0] else set()
        cap["dropped"] = set().union(*r[0].values()) if r[0] else set()
        return r
    sel.diffexec.prune = prune
idof = {t["number"]: t["id"] for t in tests}


def enclosing(tree, line):
    best = None
    exc = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.lineno <= line <= node.end_lineno:
            if best is None or node.lineno >= best.lineno:
                best = node
        if isinstance(node, (ast.ExceptHandler, ast.Raise)) and node.lineno <= line <= (node.end_lineno or node.lineno):
            exc = True
    return best, exc


trees = {}
for s in sets:
    d = Path(s)
    name = d.name
    ans = json.loads((d / "answers.json").read_text())["commits"]
    for pf in sorted((d / "commits").glob("*.patch")):
        c = pf.stem
        if c not in ans:
            continue
        text = pf.read_text(errors="replace")
        cap.clear()
        try:
            nums, why = sel.select(text)
        except Exception as e:
            nums, why = everything, f"ALL (error {e})"
        workers = all_workers if nums == everything else pack.pack(nums, ms_of)
        if nums != everything and pack.cost_ms(workers, ms_of) >= ALL_FRACTION * all_cost:
            nums, workers, why = everything, all_workers, "ALL (selection ~ALL cost) " + why
        cost = pack.cost_ms(workers, ms_of)
        fail = set(ans[c]["failing"])
        changed = []
        fnspans = []
        exc_any = False
        files = []
        try:
            for fp in diff.parse(text):
                p = fp.path
                files.append(p)
                if not p.endswith(".py"):
                    continue
                src = sqlglot / p
                if src.is_file():
                    diff.apply(src.read_text(), fp)
                    old_lines, new_lines = diff.changed_lines(fp)
                    if p not in trees:
                        trees[p] = ast.parse(src.read_text())
                    for l in old_lines:
                        changed.append((p, l))
                        fn, exc = enclosing(trees[p], l)
                        exc_any |= exc
                        if fn is not None:
                            fnspans.append((p, fn.lineno, fn.end_lineno))
        except Exception:
            pass
        fnspans = sorted(set(fnspans))
        row_c = dict(set=name, commit=c, kind=ans[c].get("kind"), why=why[:600], all=why.startswith("ALL"),
                     cost=cost, nsel=len(nums), nfail=len(fail), missed=len(fail - nums),
                     dx_note=cap.get("note"), files=files, nums=sorted(nums) if len(nums) < 1620 else "ALL")
        pop = len(set().union(*[sel.maps.lines.get(f"{p}:{l}") for (p, l) in changed])) if changed else 0
        for t in SLOW:
            line_hits = sum(1 for (p, l) in changed if t in sel.maps.lines.get(f"{p}:{l}"))
            fn_hits = 0
            fn_lines = 0
            for (p, a, b) in fnspans:
                for l in range(a, b + 1):
                    s_ = sel.maps.lines.get(f"{p}:{l}")
                    if s_:
                        fn_lines += 1
                        fn_hits += t in s_
            fn_pop = len(sel.maps.span(*fnspans[0])) if fnspans else 0
            sel2 = set(nums) - {t}
            cost_wo = pack.cost_ms(pack.pack(sel2, ms_of), ms_of) if sel2 else 0
            out.write(json.dumps(dict(row_c, test=t, sel=t in nums, y=t in fail, line_hits=line_hits,
                      n_changed=len(changed), pop=pop, fn_hits=fn_hits, fn_lines=fn_lines, fn_pop=fn_pop,
                      exc=exc_any, dx_why=cap.get("why", {}).get(idof[t]), dx_cand=t in cap.get("cand", set()),
                      dx_dropped=t in cap.get("dropped", set()), saving=cost - cost_wo)) + "\n")
        out.flush()
        print(name, c, len(nums), cost / 1000, file=sys.stderr, flush=True)
