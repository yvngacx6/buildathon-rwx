"""Fail-fast cheap-test oracle economics with labels.

Policy per commit: if the commit's cheap candidates cost <= T laptop-cpu-seconds, run them
cheapest first; stop at the first failure; drop every test proven passing; submit the rest
(failing + not yet run + slow). CPU = K * (cpu of tests run + OVH).
Labels: answers.json, or an oracle-run json (failed lists) for unlabeled sets.
"""
import json, sys
sys.path.insert(0, '/home/aditya/Desktop/hackathons/buildathon-rwx/.claude/worktrees/agent-a15d44c5e7372e666')
from selector import pack

O = "/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m02"
R = "/home/aditya/Desktop/hackathons/buildathon-rwx"
S = "/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad"
SETS = {"sample": f"{R}/sample", "priv": f"{R}/eval/data/private", "P": f"{S}/P", "Q": f"{S}/Q"}
K = 2.0
OVH = 0.03
SLOW = 3.0
base = json.load(open(f"{O}/base.json"))["commits"]["__base__"]["times"]
lap = {int(k): v for k, v in base.items()}
tests = {t["number"]: t for t in json.load(open(f"{R}/sample/tests.json"))["tests"]}
ms = {n: pack.duration_ms(t["seconds"]) for n, t in tests.items()}


def lcpu(n):
    return max(lap.get(n, 0.0), 0.002 + 1.0 * tests[n]["seconds"])


def cost(nums):
    nums = set(nums)
    return pack.cost_ms(pack.pack(nums, ms), ms) / 1000 if nums else 0.0


def labels(name, d):
    try:
        return {c: set(v["failing"]) for c, v in json.load(open(f"{d}/answers.json"))["commits"].items()}
    except FileNotFoundError:
        pass
    try:  # oracle-run labels (only commits that were run)
        j = json.load(open(f"{O}/or_{name}.json"))["commits"]
        return {c: set(v["failed"]) for c, v in j.items() if "failed" in v}
    except FileNotFoundError:
        return None


for name, d in SETS.items():
    try:
        rows = [json.loads(l) for l in open(f"{O}/{name}.out")]
    except FileNotFoundError:
        continue
    lab = labels(name, d)
    if lab is None:
        continue
    for T in [0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.0, 1e9]:
        cur_t = new_t = cpu_t = 0.0
        n = ran = zeroed = 0
        for r in rows:
            c = r["commit"]
            sel = {x for w in r["workers"] for x in w}
            cheap = sorted((x for x in sel if tests[x]["seconds"] < SLOW), key=lcpu)
            if c not in lab and cheap and sum(map(lcpu, cheap)) <= T:
                continue  # unlabeled commit inside the gate: cannot evaluate
            n += 1
            cur = cost(sel)
            cur_t += cur
            if not cheap or sum(map(lcpu, cheap)) > T:
                new_t += cur
                continue
            ran += 1
            fail = lab[c]
            passed, cpu = set(), OVH
            for x in cheap:
                cpu += lcpu(x)
                if x in fail:
                    break
                passed.add(x)
            new = cost(sel - passed)
            zeroed += new == 0
            new_t += new
            cpu_t += K * cpu
        f = 40 / max(n, 1)
        print(f"{name:6s} T={T:<6} n={n:4d} oracle on {ran*f:4.1f}/40 (to zero {zeroed*f:4.1f}) per40: cur={cur_t*f:6.1f} "
              f"sim={new_t*f:6.1f} cpu={cpu_t*f:5.1f} net={(cur_t-new_t-cpu_t)*f:+6.1f}")
    print()
