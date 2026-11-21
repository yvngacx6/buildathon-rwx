"""Gated economics of assertion-unit slicing from per-test sliced runs (sl_<set>_*.json).

Ex-ante gate (no labels): per test predicted cpu `pred` (store: recorded cpu of units that must run);
attempt test t iff pred_t <= THETA; attempt the commit iff UB saving > K * (sum pred + setup estimate + 0.05).
Charged: K * (actual per-test cpu of attempted tests + setUpClass cpu of their classes + 0.03 fork/prep)
+ K * WARM per 40 commits (warm-up: sqlglot + test modules).
"""
import json, sys, glob, collections
W = "/home/aditya/Desktop/hackathons/buildathon-rwx/.claude/worktrees/agent-a8cb1457210c6ab49"
sys.path.insert(0, W)
from selector import pack
O = "/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m02"
M = "/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m11"
K = 2.0
WARM = 1.5
SETUP_EST = {"tests.test_optimizer.TestOptimizer": 0.15}
tests = {t["number"]: t for t in json.load(open(f"{W}/sample/tests.json"))["tests"]}
ms = {n: pack.duration_ms(t["seconds"]) for n, t in tests.items()}


def cost(s):
    s = set(s)
    return pack.cost_ms(pack.pack(s, ms), ms) / 1000 if s else 0.0


ANS = {"sample": f"{W}/sample/answers.json",
       "priv": "/home/aditya/Desktop/hackathons/buildathon-rwx/eval/data/private/answers.json"}
name = sys.argv[1]
selfile = sys.argv[2] if len(sys.argv) > 2 else f"{O}/{name}.out"
rows = {json.loads(l)["commit"]: json.loads(l) for l in open(selfile)}
sl = {}
for f in glob.glob(f"{M}/sl_{name}_*.json"):
    sl.update(json.load(open(f)))
ans = None
if name in ANS:
    ans = {c: set(v["failing"]) for c, v in json.load(open(ANS[name]))["commits"].items()}
k = 40 / len(rows)
import pickle
_st = pickle.load(open(f"{M}/store.pkl", "rb"))["tests"]
RESID = {n: max(0.0, r.get("cpu", 0) - sum(u[3] for u in r["units"])) for n, r in _st.items() if "units" in r}
del _st
for info in sl.values():
    for n, v in info.get("tests", {}).items():
        if v["v"] != "nostore":
            v["pred"] = v["pred"] + RESID.get(int(n), 1.0)
base_total = sum(cost({x for w in r["workers"] for x in w}) for r in rows.values())
print(f"{name}: {len(rows)} commits, selection cost {base_total * k:.1f}/40; sliceable commits {sum('tests' in v for v in sl.values())}")
bad_all = 0
for c, info in sl.items():
    if ans is None:
        break
    for n, v in info.get("tests", {}).items():
        if v["v"] == "pass" and int(n) in ans[c]:
            bad_all += 1
            print("  FALSE DROP", c, n)
agree = collections.Counter()
if ans is not None:
    for c, info in sl.items():
        for n, v in info.get("tests", {}).items():
            if v["v"] != "nostore":
                agree[(v["v"], "labfail" if int(n) in ans[c] else "labpass")] += 1
    print("  verdict vs labels:", dict(agree), "false drops", bad_all)
for THETA in [0.02, 0.05, 0.1, 0.3, 1e9]:
    G = C = 0.0
    nz = natt = 0
    top = []
    for c, info in sl.items():
        t = {int(n): v for n, v in info.get("tests", {}).items() if v["v"] != "nostore"}
        if not t:
            continue
        sel = {x for w in rows[c]["workers"] for x in w}
        A = {n for n, v in t.items() if v["pred"] <= THETA}
        if not A:
            continue
        ub = cost(sel) - cost(sel - A)
        classes = {t[n]["cls"] for n in A}
        pc = sum(t[n]["pred"] for n in A) + sum(SETUP_EST.get(x, 0.0) for x in classes) + 0.05
        if ub <= K * pc:
            continue
        natt += 1
        drop = {n for n in A if t[n]["v"] == "pass"}
        g = cost(sel) - cost(sel - drop)
        cc = sum(t[n]["cpu"] for n in A) + sum((info.get("setup", {}).get(x) or 0) for x in classes) + 0.03
        G += g
        C += cc
        nz += bool(sel and not (sel - drop))
        top.append((round(g - K * cc, 2), c))
    net = (G - K * C) * k - (K * WARM if natt else 0)
    print(f"  theta {THETA:>6}: attempted {natt * k:.1f}/40 commits, gain {G * k:.1f}, cpu(scorer) {K * C * k:.1f} + warm {K * WARM:.1f}, NET {net:+.1f}/40; ->[] {nz * k:.1f}/40")
    if THETA == 0.1:
        top.sort(reverse=True)
        print("     best:", top[:6], "worst:", top[-4:])
