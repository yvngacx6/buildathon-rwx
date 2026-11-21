"""Evaluate line-level vs file-level coverage selection on labeled patches.
usage: evalcov.py linemap.json tests.json answers.json patchdir"""
import json, re, sys, pathlib
lm = json.load(open(sys.argv[1]))
tests = json.load(open(sys.argv[2]))["tests"]
ans = json.load(open(sys.argv[3]))["commits"]
ctx2num = {t["id"].replace(".py::", "::").replace("/", ".").replace("::", "."): t["number"] for t in tests}
dur = {t["number"]: t["seconds"] for t in tests}
def changed(patch):
    res = {}; f = None
    for line in patch.splitlines():
        if line.startswith("+++ "): f = line[6:]; continue
        m = re.match(r"@@ -(\d+)(?:,\d+)? ", line)
        if m: o = int(m.group(1)); continue
        if f is None or line.startswith("--- "): continue
        if line.startswith("-"): res.setdefault(f, set()).add(o); o += 1
        elif line.startswith("+"): res.setdefault(f, set()).update({o - 1, o})
        else: o += 1
    return res
def nums(ctxs): return {ctx2num[c] for c in ctxs if c in ctx2num}
for pid, a in sorted(ans.items()):
    fail = set(a["failing"])
    ch = changed(pathlib.Path(sys.argv[4], pid + ".patch").read_text())
    line_sel, file_sel, notes = set(), set(), []
    for f, ls in ch.items():
        if not f.startswith("sqlglot/"): notes.append("nonpy:" + f.split("/")[-1]); continue
        m = lm.get(f, {})
        for l in ls:
            c = m.get(str(l), [])
            if c == [""]: notes.append(f"L{l}=import-only")
            elif not c: notes.append(f"L{l}=uncovered")
            line_sel |= nums(c)
        for c in m.values(): file_sel |= nums(c)
    print(f"{pid} {a['kind']:8} fail={len(fail):2} line:sel={len(line_sel):4} miss={len(fail-line_sel):2} file:sel={len(file_sel):4} miss={len(fail-file_sel):2} {' '.join(notes)}")
