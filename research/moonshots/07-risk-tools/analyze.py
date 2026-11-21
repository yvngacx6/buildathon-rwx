"""Calibrated P(slow test fails | structural evidence), grouped CV, decision simulation."""
import json, math, sys, collections
from pathlib import Path
import numpy as np
sys.path.insert(0, sys.argv[1])
from selector import pack
D = Path(__file__).parent
SETS = ["sample", "practice_like", "private", "private41", "adversarial", "adversarial2"]
tests = json.loads((Path(sys.argv[1]) / "sample/tests.json").read_text())["tests"]
ms_of = {t["number"]: pack.duration_ms(t["seconds"]) for t in tests}
ALLN = set(ms_of)
SLOW = [1490, 1106, 1224, 1107, 1123]
NAME = {1490: "max_nodes", 1106: "tpcds", 1224: "genmem", 1107: "tpch", 1123: "opt_tpch"}
rows = []
for s in SETS:
    for l in open(D / f"{s}.jsonl"):
        rows.append(json.loads(l))
commits = collections.OrderedDict()
for r in rows:
    commits.setdefault((r["set"], r["commit"]), []).append(r)
sel = [r for r in rows if r["sel"]]
print("commits", len(commits), "selected slow rows", len(sel), "positives", sum(r["y"] for r in sel))
print("failing slow tests NOT selected (pre-existing misses):", sum(1 for r in rows if r["y"] and not r["sel"]))


def dxcat(r):
    if not r["dx_note"]:
        return "dx_none"
    if r["dx_why"]:
        return "dx_" + r["dx_why"]
    n = r["dx_note"]
    if "keep all" in n or "bail" in n or "no answer" in n:
        return "dx_keepall"
    if "skipped" in n:
        return "dx_skipped"
    return "dx_other"


sys.path.insert(0, sys.argv[1] + "/eval")
from reasons import bucket
def allkind(r):
    b = bucket(r["why"])
    w = r["why"]
    if "rror" in w[:200] or "collect" in w[:200] or "compile" in w[:200] or "syntax" in w[:200]:
        return "ALL:error/collection"
    return b[:45]
def mech(r):
    w = r["why"]
    if r["all"]:
        return allkind(r)
    if "state:" in w:
        return "state"
    if "fixture" in w:
        return "fixture"
    if "def " in w:
        return "func"
    return "other"


print("\n== strata (selected slow tests): n, fails, rate")
def strat(key):
    c = collections.defaultdict(lambda: [0, 0])
    for r in sel:
        k = key(r); c[k][0] += 1; c[k][1] += r["y"]
    for k, (n, f) in sorted(c.items(), key=lambda x: -x[1][0]):
        print(f"  {str(k):40s} n={n:4d} fail={f:4d} rate={f/n:.2f}")
for nm, key in [("test", lambda r: NAME[r["test"]]), ("mechanism", mech), ("diffexec", dxcat),
                ("line_hits", lambda r: min(r["line_hits"], 3)),
                ("fn_hits>0", lambda r: r["fn_hits"] > 0), ("exc", lambda r: r["exc"]),
                ("set", lambda r: r["set"]),
                ("mech x line_hits", lambda r: (mech(r), min(r["line_hits"], 2)))]:
    print(" by", nm); strat(key)

# ── features ──
CATS = ["dx_none", "dx_incomplete", "dx_differs", "dx_unseen", "dx_keepall", "dx_skipped", "dx_other"]
def feats(r):
    f = [1.0]
    f += [float(r["test"] == t) for t in SLOW[1:]]
    f += [float(r["all"]), float(mech(r) == "ALL:error/collection"), float(mech(r) == "state"), float(mech(r) == "func")]
    lh = r["line_hits"]
    f += [float(lh == 0), math.log1p(lh)]
    f += [r["fn_hits"] / r["fn_lines"] if r["fn_lines"] else 0.0]
    f += [math.log1p(r["pop"]) / 7]
    f += [float(dxcat(r) == c) for c in CATS[1:]]
    f += [float(r["exc"]), float(len(r["files"]) > 1)]
    return f

def fit(X, y, lam=1.0, w=None):
    b = np.zeros(X.shape[1])
    for _ in range(50):
        p = 1 / (1 + np.exp(-X @ b))
        g = X.T @ (p - y) + lam * np.r_[0, b[1:]]
        H = X.T @ (X * (p * (1 - p))[:, None]) + lam * np.diag(np.r_[0, np.ones(len(b) - 1)])
        step = np.linalg.solve(H, g); b -= step
        if np.abs(step).max() < 1e-8: break
    return b

X = np.array([feats(r) for r in sel]); y = np.array([float(r["y"]) for r in sel])
groups = [(r["set"], r["commit"]) for r in sel]
ug = sorted(set(groups)); gi = {g: i % 10 for i, g in enumerate(ug)}  # 10-fold grouped by commit
folds = np.array([gi[g] for g in groups])
for lam in (0.3, 1.0, 3.0):
    p = np.zeros(len(y))
    for k in range(10):
        tr, te = folds != k, folds == k
        b = fit(X[tr], y[tr], lam); p[te] = 1 / (1 + np.exp(-X[te] @ b))
    brier = np.mean((p - y) ** 2); ll = -np.mean(y * np.log(p + 1e-9) + (1 - y) * np.log(1 - p + 1e-9))
    print(f"\nlam={lam}: CV brier {brier:.4f} logloss {ll:.4f} (base rate {y.mean():.3f}, brier0 {y.mean()*(1-y.mean()):.4f})")
    if lam == 1.0:
        pcv = p.copy()
order = np.argsort(pcv)
print("\nreliability (CV, lam=1), bins by predicted p:")
for lo, hi in [(0, .01), (.01, .03), (.03, .07), (.07, .15), (.15, .3), (.3, .6), (.6, 1.01)]:
    m = (pcv >= lo) & (pcv < hi)
    if m.sum():
        print(f"  [{lo:.2f},{hi:.2f}) n={m.sum():4d} mean_pred={pcv[m].mean():.3f} observed={y[m].mean():.3f} fails={int(y[m].sum())}")
print("lowest-p positives (CV):")
for i in order:
    if y[i]:
        r = sel[i]; print(f"  p={pcv[i]:.4f} {r['set']}/{r['commit']} {NAME[r['test']]} {mech(r)} lh={r['line_hits']} {dxcat(r)} {r['why'][:120]}")
        if pcv[i] > 0.1: break
bfull = fit(X, y, 1.0)
print("coef", np.round(bfull, 2).tolist())
for i, r in enumerate(sel):
    r["p"] = float(pcv[i])

# ── decision sim: drop selected slow test t if p*300*lam_risk < marginal saving ──
def cost(nums):
    nums = set(nums)
    return pack.cost_ms(pack.pack(nums, ms_of), ms_of) if nums else 0

def simulate(mult, only_sets=None, rule=None):
    saved = 0; misses = 0; mcommits = []; dropped_n = 0
    per = collections.Counter()
    for key, rs in commits.items():
        if only_sets and key[0] not in only_sets: continue
        nums = ALLN if rs[0]["nums"] == "ALL" else set(rs[0]["nums"])
        base = rs[0]["cost"]
        cand = [r for r in rs if r["sel"] and "p" in r]
        # greedy: consider dropping in order of p; accept if EV improves
        drop = set(); cur = base
        for r in sorted(cand, key=lambda r: r["p"]):
            if rule is not None:
                ok = rule(r)
                if ok: drop.add(r["test"])
                continue
            c2 = cost(nums - drop - {r["test"]})
            if r["p"] * 300_000 * mult < cur - c2:
                drop.add(r["test"]); cur = c2
        if drop:
            c2 = cost(nums - drop); saved += base - c2; dropped_n += len(drop)
            if any(r["y"] for r in rs if r["test"] in drop):
                misses += 1; mcommits.append(key)
        per[key[0]] += 1
    return saved / 1000, misses, mcommits, dropped_n

print("\n== decision sim (CV probabilities). mult=1 is EV-neutral; mult>1 more conservative")
for mult in (0.5, 1, 2, 4, 8, 16):
    for ss in (None, {"sample", "practice_like"}, {"private"}):
        sv, m, mc, dn = simulate(mult, ss)
        print(f"  mult={mult:4} sets={'all' if ss is None else ','.join(sorted(ss)):22s} saved={sv:8.1f}s misses={m} net={sv-300*m:8.1f} dropped={dn} {mc[:4]}")

# total slow spend on selections
tot = 0
for key, rs in commits.items():
    nums = ALLN if rs[0]["nums"] == "ALL" else set(rs[0]["nums"])
    tot += rs[0]["cost"] - cost(nums - set(SLOW))
print("\nslow-test spend over all labeled commits:", tot / 1000, "s on", len(commits), "commits")
need = sum(1 for rs in commits.values() if any(r["y"] for r in rs))
print("commits needing a slow test:", need)
json.dump([dict(set=r["set"], commit=r["commit"], test=r["test"], y=r["y"], p=r["p"], mech=mech(r), dx=dxcat(r),
                lh=r["line_hits"], all=r["all"], saving=r["saving"]) for r in sel], open(D / "pcv.json", "w"))

# ── shift test: train on private+adv sets, test on sample+practice_like ──
tr = np.array([r["set"] in ("private", "private41", "adversarial", "adversarial2") for r in sel])
te = ~tr
b = fit(X[tr], y[tr], 1.0); pt = 1 / (1 + np.exp(-X[te] @ b))
b0 = np.log(y[tr].mean() / (1 - y[tr].mean()))
print(f"\nshift test: n_test={te.sum()} fails={int(y[te].sum())} brier model={np.mean((pt-y[te])**2):.4f} intercept-only={np.mean((1/(1+np.exp(-b0))-y[te])**2):.4f}")
for i, j in enumerate(np.where(te)[0]):
    r = sel[j]; print(f"  {r['set']}/{r['commit']} {NAME[r['test']]:9s} y={int(r['y'])} p_shift={pt[i]:.3f} p_cv={pcv[j]:.3f} {mech(r)} lh={r['line_hits']} {dxcat(r)} save={r['saving']/1000:.1f}")

# ── hand-written structural rules, judged by Wilson upper bound ──
def wilson_up(f, n, z=1.96):
    if n == 0: return 1.0
    ph = f / n; d = 1 + z*z/n
    return (ph + z*z/(2*n) + z*math.sqrt(ph*(1-ph)/n + z*z/(4*n*n))) / d
RULES = {
  "R0 any selected slow": lambda r: True,
  "R1 ran 0 touched lines": lambda r: r["line_hits"] == 0,
  "R2 ran 0 touched lines, not ALL": lambda r: r["line_hits"] == 0 and not r["all"],
  "R3 ALL (conservative, not error)": lambda r: r["all"] and mech(r) != "ALL:error/collection",
  "R4 ALL conservative & ran 0 touched lines": lambda r: r["all"] and mech(r) != "ALL:error/collection" and r["line_hits"] == 0,
  "R5 0 touched lines & 0 fn lines": lambda r: r["line_hits"] == 0 and r["fn_hits"] == 0,
  "R6 dx incomplete": lambda r: dxcat(r) == "dx_incomplete",
  "R7 ran touched line (lh>=1)": lambda r: r["line_hits"] >= 1,
  "R8 lh>=1 & dx_none": lambda r: r["line_hits"] >= 1 and dxcat(r) == "dx_none",
  "R9 state mechanism": lambda r: mech(r) == "state",
}
print("\n== structural rules over selected slow rows: n, fails, upper95, single-drop savings")
for nm, f in RULES.items():
    m = [r for r in sel if f(r)]
    n = len(m); k = sum(r["y"] for r in m)
    pr = [r for r in m if r["set"] in ("sample", "practice_like", "private")]
    print(f"  {nm:45s} n={n:4d} fail={k:3d} rate={k/max(n,1):.3f} up95={wilson_up(k,n):.3f}  practice-ish n={len(pr)} fail={sum(r['y'] for r in pr)} save1={sum(r['saving'] for r in m)/1000:.0f}s")
for nm in ("R1 ran 0 touched lines", "R4 ALL conservative & ran 0 touched lines", "R5 0 touched lines & 0 fn lines"):
    sv, mm, mc, dn = simulate(1, None, rule=RULES[nm])
    print(f"  joint drop by {nm}: saved={sv:.1f}s misses={mm} {mc[:6]} dropped={dn}")
    for ss in ({"sample", "practice_like"}, {"private"}):
        sv, mm, mc, dn = simulate(1, ss, rule=RULES[nm]); print(f"     {ss}: saved={sv:.1f}s misses={mm} {mc[:6]}")
