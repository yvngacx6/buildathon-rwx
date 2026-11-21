"""Upper bound for slicing fixture-only optimizer commits (changed fixture case -> only that case runs)."""
import json, sys, glob
W = "/home/aditya/Desktop/hackathons/buildathon-rwx/.claude/worktrees/agent-a8cb1457210c6ab49"
sys.path.insert(0, W)
from selector import pack
O = "/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m02"
M = "/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m11"
tests = {t["number"]: t for t in json.load(open(f"{W}/sample/tests.json"))["tests"]}
ms = {n: pack.duration_ms(t["seconds"]) for n, t in tests.items()}
def cost(s):
    s = set(s); return pack.cost_ms(pack.pack(s, ms), ms) / 1000 if s else 0.0
sets = {"sample": (f"{W}/sample", f"{W}/sample/answers.json", f"{O}/sample.out"),
        "priv": ("/home/aditya/Desktop/hackathons/buildathon-rwx/eval/data/private", "/home/aditya/Desktop/hackathons/buildathon-rwx/eval/data/private/answers.json", f"{O}/priv.out"),
        "P": ("/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/P", None, f"{O}/P.out")}
orc = {}
for f in glob.glob(f"{O}/or_P_*.json") + glob.glob(f"{M}/orP10_*.json"):
    orc.update(json.load(open(f))["commits"])
for name, (data, ansf, self_) in sets.items():
    rows = [json.loads(l) for l in open(self_)]
    ans = {c: set(v["failing"]) for c, v in json.load(open(ansf))["commits"].items()} if ansf else None
    n = tot = ub = 0
    for r in rows:
        txt = open(f"{data}/commits/{r['commit']}.patch", errors="replace").read()
        paths = [l[6:] for l in txt.splitlines() if l.startswith("+++ b/")]
        if not paths or not all(p.startswith("tests/fixtures/optimizer/") for p in paths):
            continue
        sel = {x for w in r["workers"] for x in w}
        n += 1; c = cost(sel); tot += c
        if ans is not None:
            fail = ans[r["commit"]]
        else:
            o = orc.get(r["commit"])
            if not o or "failed" not in o:
                continue
            fail = set(o["failed"])
        ub += c - cost(sel & fail | {x for x in sel if tests[x]["seconds"] >= 3})
    k = 40 / len(rows)
    print(f"{name}: fixture-only optimizer commits {n * k:.1f}/40, their cost {tot * k:.1f}/40, oracle saving {ub * k:.1f}/40")
