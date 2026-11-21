"""Final economics: actual child CPU from oracle runs, fixtures included, label-free gate.

gate: run the oracle on a commit when (cur - cost(slow part)) > M * K * predicted_cpu,
predicted_cpu = sum(base per-test cpu) + setUpClass cpu of touched classes + 0.03.
Charged: K * measured child_cpu (all cheap candidates run, no fail-fast).
Warm-up charged once per run: K * (0.2 + 1.1 if any gated commit needs test_optimizer/executor).
Fidelity: labeled failing cheap candidates the oracle called passing (and not untrusted).
"""
import glob, json, sys
sys.path.insert(0, '/home/aditya/Desktop/hackathons/buildathon-rwx/.claude/worktrees/agent-a15d44c5e7372e666')
from selector import pack

O = "/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m02"
R = "/home/aditya/Desktop/hackathons/buildathon-rwx"
K = 2.0
SLOW = 3.0
b2 = json.load(open(f"{O}/base2.json"))["commits"]["__base__"]
lap = {int(k): v for k, v in b2["times"].items()}
setup = b2["setup_cpu"]
tests = {t["number"]: t for t in json.load(open(f"{R}/sample/tests.json"))["tests"]}
ms = {n: pack.duration_ms(t["seconds"]) for n, t in tests.items()}


def cost(nums):
    nums = set(nums)
    return pack.cost_ms(pack.pack(nums, ms), ms) / 1000 if nums else 0.0


def cls_of(n):
    path, cls, _ = tests[n]["id"].split("::")
    return path[:-3].replace("/", ".") + "." + cls


def pred(cheap):
    return 0.03 + sum(max(lap.get(n, 0), tests[n]["seconds"]) for n in cheap) + sum(setup.get(c, 0) for c in {cls_of(n) for n in cheap})


SETS = [("sample", "or2_sample.json", f"{R}/sample/answers.json"),
        ("priv", "or_priv_*.json", f"{R}/eval/data/private/answers.json"),
        ("P", "or_P_*.json", None)]
for name, pat, ansf in SETS:
    runs = {}
    for f in glob.glob(f"{O}/{pat}"):
        runs.update(json.load(open(f))["commits"])
    ans = {c: set(v["failing"]) for c, v in json.load(open(ansf))["commits"].items()} if ansf else None
    rows = [json.loads(l) for l in open(f"{O}/{name}.out")]
    nall = len(rows)
    # fidelity
    fp = extra = checked = fb = 0
    for c, v in runs.items():
        if "failed" not in v:
            fb += 1
            continue
        if ans is None:
            continue
        sel = None
        checked += 1
        lab = ans[c]
        ours = set(v["failed"]) | set(v.get("untrusted", []))
        r = next(r for r in rows if r["commit"] == c)
        cheap = {n for w in r["workers"] for n in w if tests[n]["seconds"] < SLOW}
        fp += len((lab & cheap) - ours)
        extra += len(set(v["failed"]) - lab)
    print(f"{name}: oracle runs {len(runs)} (fallback/unsupported {fb}); fidelity on {checked} labeled: "
          f"false-pass tests {fp}, false-fail tests {extra}")
    for M in [1.0, 1.5, 2.0, 3.0]:
        gain = cpu = 0.0
        ng = nz = 0
        heavy = False
        for r in rows:
            c = r["commit"]
            sel = {n for w in r["workers"] for n in w}
            cheap = {n for n in sel if tests[n]["seconds"] < SLOW}
            if not cheap:
                continue
            slow = sel - cheap
            cur = cost(sel)
            if cur - cost(slow) <= M * K * pred(cheap):
                continue
            v = runs.get(c)
            if v is None or "failed" not in v:
                # gated but not run (outside --maxrec, or unsupported): count as no gain, no cpu
                continue
            ng += 1
            keep = set(v["failed"]) | set(v.get("untrusted", [])) | slow
            new = cost(keep & sel)
            nz += new == 0
            gain += cur - new
            cpu += K * v["child_cpu"]
            heavy |= any(tests[n]["file"] in ("tests/test_optimizer.py", "tests/test_executor.py") for n in cheap)
        warm = K * (0.2 + (1.1 if heavy else 0))
        f = 40 / nall
        print(f"  M={M}: gated {ng*f:4.1f}/40 (zeroed {nz*f:4.1f}); per 40: sim -{gain*f:5.1f}  cpu +{cpu*f:5.1f}  "
              f"warm +{warm:.1f}  NET {gain*f - cpu*f - warm:+5.1f} s")
