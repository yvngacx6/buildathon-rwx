"""Economics + soundness of assertion-unit slicing.

For each commit: tests whose sliced run passed are dropped from the selection (m02 *.out, main 4826380).
Gain = cost(sel) - cost(sel - dropped). CPU charged = K * laptop child CPU (+ warm-up once per 40).
Soundness on labeled sets: dropped tests that are labeled failing (must be 0).
Also compares to the whole-test oracle (forksrv) where available.
"""
import json, sys, glob, collections
W = "/home/aditya/Desktop/hackathons/buildathon-rwx/.claude/worktrees/agent-a8cb1457210c6ab49"
sys.path.insert(0, W)
from selector import pack
O = "/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m02"
M = "/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m11"
K = 2.0
tests = {t["number"]: t for t in json.load(open(f"{W}/sample/tests.json"))["tests"]}
ms = {n: pack.duration_ms(t["seconds"]) for n, t in tests.items()}


def cost(s):
    s = set(s)
    return pack.cost_ms(pack.pack(s, ms), ms) / 1000 if s else 0.0


ANS = {"sample": f"{W}/sample/answers.json",
       "priv": "/home/aditya/Desktop/hackathons/buildathon-rwx/eval/data/private/answers.json"}
ORC = {"sample": [f"{O}/or2_sample.json"], "priv": glob.glob(f"{O}/or_priv_*.json"),
       "P": glob.glob(f"{O}/or_P_*.json") + glob.glob(f"{M}/orP10_*.json")}
name = sys.argv[1]
selfile = sys.argv[2] if len(sys.argv) > 2 else f"{O}/{name}.out"
rows = {json.loads(l)["commit"]: json.loads(l) for l in open(selfile)}
sl = {}
for f in glob.glob(f"{M}/sl_{name}_*.json"):
    sl.update(json.load(open(f)))
ans = None
if name in ANS:
    ans = {c: set(v["failing"]) for c, v in json.load(open(ANS[name]))["commits"].items()}
orc = {}
for f in ORC.get(name, []):
    orc.update(json.load(open(f))["commits"])
k = 40 / len(rows)
T = G = cpu = 0.0
nz = nlw = bad = ncommit = 0
fb = collections.Counter()
per = []
for c, info in sl.items():
    sel = {x for w in rows[c]["workers"] for x in w}
    if "fallback" in info:
        fb[info["fallback"].split(":")[0][:40]] += 1
    t = info.get("tests", {})
    drop = {int(n) for n, v in t.items() if v["v"] == "pass"}
    if ans is not None:
        b = drop & ans[c]
        if b:
            bad += len(b)
            print("FALSE DROP", c, sorted(b))
    before, after = cost(sel), cost(sel - drop)
    G += before - after
    cpu += info.get("child_cpu", 0)
    ncommit += 1
    if sel and not (sel - drop):
        nz += 1
    if len(pack.pack(sel, ms)) > len(pack.pack(sel - drop, ms)) and sel - drop:
        nlw += 1
    per.append((before - after, info.get("child_cpu", 0), c, len(drop), len(t)))
print(f"{name}: commits run {ncommit}/{len(rows)}; fallbacks {dict(fb)}")
print(f"  gain {G * k:.1f}/40, child cpu laptop {cpu * k:.1f}/40 -> scorer {K * cpu * k:.1f}/40; NET {(G - K * cpu) * k:.1f}/40")
print(f"  commits -> [] {nz * k:.1f}/40, lose a worker {nlw * k:.1f}/40; false drops {bad}")
# gated: run only when an upper bound of the saving beats K * predicted cpu -- here simply: ex-post best case
Gg = Cg = 0
for g, cc, c, nd, nt in per:
    if g > K * cc:
        Gg += g; Cg += cc
print(f"  ex-post oracle gate (upper bound): gain {Gg * k:.1f}, cpu {K * Cg * k:.1f}, net {(Gg - K * Cg) * k:.1f}/40")
# agreement with whole-test oracle
agree = collections.Counter()
for c, info in sl.items():
    o = orc.get(c)
    if not o or "failed" not in o:
        continue
    of = set(o["failed"]) | set(o.get("untrusted", []))
    for n, v in info.get("tests", {}).items():
        n = int(n)
        if v["v"] == "nostore":
            continue
        agree[(v["v"], "fail" if n in of else "pass")] += 1
print("  sliced verdict vs whole-test oracle:", dict(agree))
per.sort(reverse=True)
print("  top:", [(c, round(g, 2), round(cc, 2), f"{nd}/{nt}") for g, cc, c, nd, nt in per[:10]])
