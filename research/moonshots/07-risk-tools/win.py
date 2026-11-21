"""Pareto (saved s vs P(0 misses)) from CV probabilities, and P(first) under leader scenarios."""
import json, math, random, collections
from pathlib import Path
D = Path(__file__).parent
rows = json.load(open(D / "pcv.json"))
# AUC
pos = [r["p"] for r in rows if r["y"]]; neg = [r["p"] for r in rows if not r["y"]]
auc = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))
print(f"CV AUC {auc:.3f}  n={len(rows)} pos={len(pos)}")
# per-commit: drop the best (max saving) slow test only, ranked by p/saving (single-drop savings; conservative)
best = {}
for r in rows:
    if r["saving"] <= 500: continue
    k = (r["set"], r["commit"])
    if k not in best or r["saving"] > best[k]["saving"]:
        best[k] = r
cands = sorted(best.values(), key=lambda r: r["p"] / r["saving"])
ncom = 470
print("Pareto on labeled pool (realized): cum drops, saved s, realized misses, expected misses, P0=prod(1-p)")
cs = cm = ce = 0; lp0 = 0.0
for i, r in enumerate(cands, 1):
    cs += r["saving"] / 1000; cm += r["y"]; ce += r["p"]; lp0 += math.log(1 - r["p"])
    if i in (1, 2, 5, 10, 20, 40, 80, 120, len(cands)):
        print(f"  {i:4d} saved={cs:7.1f} real_miss={cm:3d} exp_miss={ce:5.2f} P0={math.exp(lp0):.3f}  ratio s/miss={cs/max(cm,1):.0f}")

# ── P(first) model on the final (60 commits) ──
# Practice board (40): ours 194.0 = 189.1 sim + 4.9 CPU; slow spend ~61 s; non-slow ~128; 1 commit needs slow.
# Final scaling x1.5. Our levers: drop a fraction f of slow spend; the needed-commit risk:
#   P(miss) per dropped slow-needing commit. Model A (calibrated model can't separate): misses ~ Poisson(f * 1.5)
#   (S11: dropping all slow = 1 miss / 40 → 1.5 expected / 60), saved = f * 61 * 1.5.
#   Model B (oracle-ish structural rule that keeps the needing commit): misses 0 w.p. 0.9 (rule fails 10%).
random.seed(1)
def sim(f, leaders, q, sd_ours=6.0, sd_lead=6.0, model="A", N=100000, base=194.0):
    win = 0
    for _ in range(N):
        common = random.gauss(0, 8)  # commit-set draw shifts everyone similarly
        ours = 1.5 * base - f * 61 * 1.5 + common + random.gauss(0, sd_ours)
        if model == "A":
            lam = f * 1.5
            miss = random.random() > math.exp(-lam)
        else:
            miss = random.random() < 0.10 * f
        if miss: ours += 300
        best = min((1.5 * L + common + random.gauss(0, sd_lead) + (300 if random.random() < q else 0)) for L in leaders)
        win += ours < best
    return win / N
leaders = [126.1, 130.3, 134.3, 135.3, 139.9]
print("\nP(first): leaders", leaders, "x1.5; q = P(a leader misses >=1 final commit)")
for q in (0.0, 0.2, 0.4, 0.6):
    line = []
    for f in (0.0, 0.25, 0.5, 0.75, 1.0):
        line.append(f"f={f}: A={sim(f, leaders, q, N=20000):.3f}/B={sim(f, leaders, q, model='B', N=20000):.3f}")
    print(f"  q={q}: " + "  ".join(line))
# expected final score per f (model A)
print("\nexpected final score (model A): ", {f: round(1.5*194 - f*61*1.5 + 300*f*1.5, 1) for f in (0, .25, .5, 1)})
