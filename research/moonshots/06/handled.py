"""Why do handled slow tests stay selected? handled.py dir sel.json"""
import json, re, sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, "/home/aditya/Desktop/hackathons/buildathon-rwx/.claude/worktrees/agent-a98424c1132b02e15")
from selector.pack import pack, cost_ms

d = Path(sys.argv[1])
tests = json.load(open(d / "tests.json"))["tests"]
ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
name = {t["number"]: t["id"].split("::")[-1] for t in tests}
ans = json.load(open(d / "answers.json"))["commits"] if (d / "answers.json").exists() else None
H = {n for n in ms if name[n] in ("test_max_nodes", "test_execute_tpcds", "test_generator_memory",
                                   "test_execute_tpch", "test_optimized_tpch")}
sel = json.load(open(sys.argv[2]))


def c(s):
    return cost_ms(pack(set(s), ms), ms) / 1000 if s else 0.0


b = defaultdict(lambda: [0, 0.0, []])
for cid, r in sel.items():
    s = {n for w in r["workers"] for n in w}
    if len(s) > 1500:
        continue
    fail = set(ans[cid]["failing"]) if ans else set()
    keep = (s & H) - fail
    if not keep:
        continue
    rs = r.get("reason", "")
    now = c(s)
    sv = now - c(s - keep)
    for n in keep:
        nm = name[n]
        if "tpc" in nm:
            m = re.search(r"subtest: ([^;]*)", rs)
            why = m.group(1) if m else ("not diffexec-eligible" if "diffexec" not in rs else "no subtest line")
        elif nm == "test_max_nodes":
            m = re.search(r"slowverify: ([^;]*)", rs)
            why = m.group(1) if m else ("not diffexec-eligible" if "diffexec" not in rs else "no slowverify line")
        else:
            m = re.search(r"genmem: ([^;]*)", rs)
            why = m.group(1) if m else "no genmem line"
        why = re.sub(r"[\d.]+", "#", why)[:60]
        b[(nm, why)][0] += 1
        b[(nm, why)][2].append(cid)
    b[("_commit", "saving")][1] += sv
print(d.name, len(sel), "commits; saving if all passing handled dropped:", round(b[("_commit", "saving")][1], 1),
      f"({b[('_commit', 'saving')][1] / len(sel) * 40:.1f}/40)")
for k, v in sorted(b.items(), key=lambda x: -x[1][0]):
    if k[0] != "_commit":
        print(f"   {v[0]:4d} {k[0]:24s} {k[1]}  e.g. {v[2][:4]}")
