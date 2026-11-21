"""Collect changed old lines of commits where v0 dropped tests vs v05b; write targets + drops."""
import json, sys
from pathlib import Path
sys.path.insert(0, "/home/aditya/Desktop/hackathons/buildathon-rwx/.claude/worktrees/agent-a98424c1132b02e15")
from selector import diff

S = Path("/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m06")
E = Path("/home/aditya/Desktop/hackathons/buildathon-rwx/eval/data")
SG = "/home/aditya/rwx-research-robust/sqlglot/"
targets, drops = set(), {}
for s in sys.argv[1:]:
    d = E / s if (E / s).exists() else Path(s)
    tests = json.load(open(d / "tests.json"))["tests"]
    tid = {t["number"]: t["id"] for t in tests}
    a = json.load(open(S / f"v05b_{d.name}.json"))
    b = json.load(open(S / f"v0_{d.name}.json"))
    for cid in a:
        ga = {n for w in a[cid]["workers"] for n in w}
        gb = {n for w in b[cid]["workers"] for n in w}
        dropped = ga - gb
        if not dropped:
            continue
        lines = []
        for fp in diff.parse((d / "commits" / f"{cid}.patch").read_text()):
            p = fp.path
            if not (p and p.endswith(".py") and p.startswith("sqlglot/")):
                continue
            try:
                diff.apply(open(SG + p).read(), fp)
            except Exception:
                continue
            old, new = diff.changed_lines(fp)
            for ln in old:
                lines.append((p, ln))
        targets.update(lines)
        drops[f"{d.name}/{cid}"] = {"lines": lines, "dropped": sorted(tid[n] for n in dropped)}
json.dump(sorted(targets), open(S / "targets.json", "w"))
json.dump(drops, open(S / "drops.json", "w"))
print(len(targets), "target lines;", len(drops), "commits;", sum(len(v["dropped"]) for v in drops.values()), "dropped (commit,test)")
