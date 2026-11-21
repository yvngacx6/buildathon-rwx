"""Offline policy simulator: uses measured per-test CPU (sync pool) and the sample answers as the
outcome exec would observe. Cost = simulated (exact scorer arithmetic) + exec CPU + per-run overhead."""
import json, sys, pickle, os, itertools
sys.path.insert(0, os.path.dirname(__file__) or ".")
from select_cands import Selector
from run import apply_patch, ast_equal, partition
S = sys.argv[1]; B = sys.argv[2]
tests = json.load(open(B + "tests.json"))["tests"]; ans = json.load(open(B + "answers.json"))["commits"]
rec = {t["number"]: t["seconds"] for t in tests}
cpuj = json.load(open(S + "cpu.json")); cpu = {t["number"]: cpuj.get(t["id"], t["seconds"]) for t in tests}
sel = Selector(pickle.load(open(S + "map.pkl", "rb")), tests, S + "src")
OH = float(os.environ.get("OH", "0.7")); PFAIL = float(os.environ.get("PFAIL", "0.4")); PFIX = float(os.environ.get("PFIX", "0.9"))
def P(ns):
    w = partition(sorted(ns), rec)
    return max((sum(round(rec[n] * 1000) for n in x) for x in w), default=0) / 1000 + 2 * len(w)
def plan(C, pf=PFAIL):
    """choose R (run) ⊆ C by cpu threshold; returns (R, S) minimising predicted cost"""
    best = (P(C), set(), set(C))
    for X in sorted({cpu[n] for n in C}):
        R = {n for n in C if cpu[n] <= X}; Sx = set(C) - R
        pred = OH + sum(cpu[n] for n in R) + (1 - pf) * P(Sx) + pf * P(C)
        if pred < best[0]: best = (pred, R, Sx)
    return best[1], best[2]
tot = {"static": 0, "exec_all": 0, "hybrid": 0, "oracle": 0}; cpu_tot = {k: 0 for k in tot}
for cid, a in sorted(ans.items()):
    patch = open(B + f"commits/{cid}.patch").read()
    from select_cands import parse_patch
    files = parse_patch(patch); skip = set()
    for f, info in files.items():
        p = S + "src/" + f
        if f.endswith(".py") and os.path.exists(p):
            old = open(p).read()
            if ast_equal(old, apply_patch(old, info["hunks"])): skip.add(f)
    C, _ = sel.candidates(patch, skip); F = set(a["failing"])
    assert not (F - C), (cid, F - C)
    # static: output all candidates
    tot["static"] += P(C)
    # exec_all: run every candidate
    c = (OH + sum(cpu[n] for n in C)) if C else 0; cpu_tot["exec_all"] += c; tot["exec_all"] += c + P(F)
    datafile = any(not f.endswith(".py") for f in files)
    R, Sx = plan(C, PFIX if datafile else PFAIL); c = (OH + sum(cpu[n] for n in R)) if R else 0
    cpu_tot["hybrid"] += c; tot["hybrid"] += c + P((F & R) | Sx)
    tot["oracle"] += P(F)
    print(f"{cid} C={len(C):4} cpuC={sum(cpu[n] for n in C):6.2f} static={P(C):6.2f} run={len(R):4} cpuR={c:6.2f} hyb={c + P((F & R) | Sx):6.2f} oracle={P(F):6.2f}")
print({k: round(v, 1) for k, v in tot.items()}, "cpu:", {k: round(v, 1) for k, v in cpu_tot.items()})
