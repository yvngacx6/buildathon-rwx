"""Shared helpers: load sample data, exact cost (same arithmetic as score.py), packing."""
import json, math, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import score  # noqa: E402  (reuse the official scorer)

TESTS = json.loads((ROOT / "sample/tests.json").read_text())["tests"]
ANS = json.loads((ROOT / "sample/answers.json").read_text())["commits"]
MS = {t["number"]: math.floor(t["seconds"] * 1000 + 0.5) for t in TESTS}
WORKER = 2000
PENALTY = 300_000


def cost_ms(workers, failing):
    occ = [w for w in workers if w]
    c = 0
    if occ:
        c = max(sum(MS[n] for n in w) for w in occ) + WORKER * len(occ)
    if any(f not in {n for w in occ for n in w} for f in failing):
        c += PENALTY
    return c


def lpt(nums, k):
    ws = [[] for _ in range(k)]
    loads = [0] * k
    for n in sorted(nums, key=lambda n: -MS[n]):
        i = loads.index(min(loads))
        ws[i].append(n)
        loads[i] += MS[n]
    return ws, max(loads)


def exact_makespan(nums, k, budget_s=0.5):
    """Branch & bound for P||Cmax on k machines. Tiny tail items are greedy-filled
    once they cannot change the answer. Returns (workers, makespan, proven)."""
    items = sorted(nums, key=lambda n: -MS[n])
    S = sum(MS[n] for n in items)
    lb = max([MS[items[0]]] if items else [0] + [0])
    lb = max(lb, -(-S // k))
    best_ws, best = lpt(items, k)
    if best == lb:
        return best_ws, best, True
    deadline = time.time() + budget_s
    loads = [0] * k
    assign = [[] for _ in range(k)]
    suffix = [0] * (len(items) + 1)
    for i in range(len(items) - 1, -1, -1):
        suffix[i] = suffix[i + 1] + MS[items[i]]
    proven = True

    def rec(i):
        nonlocal best, best_ws, proven
        if time.time() > deadline:
            proven = False
            return
        cur = max(loads)
        if cur >= best:
            return
        if i == len(items):
            best, best_ws = cur, [list(a) for a in assign]
            return
        # lower bound for this node
        if max(cur, -(-(sum(loads) + suffix[i]) // k)) >= best:
            return
        seen = set()
        for j in sorted(range(k), key=lambda j: loads[j]):
            if loads[j] in seen:
                continue
            seen.add(loads[j])
            if loads[j] + MS[items[i]] >= best:
                continue
            loads[j] += MS[items[i]]
            assign[j].append(items[i])
            rec(i + 1)
            assign[j].pop()
            loads[j] -= MS[items[i]]
            if best == lb:
                return

    sys.setrecursionlimit(10000)
    rec(0)
    return best_ws, best, proven or best == lb


def best_pack(nums, exact=True, kmax=8):
    """Minimise makespan + 2 s * workers over k = 1..kmax. Returns (workers, cost_ms)."""
    nums = list(nums)
    if not nums:
        return [], 0
    best = None
    for k in range(1, min(kmax, len(nums)) + 1):
        ws, m = exact_makespan(nums, k)[:2] if exact else lpt(nums, k)
        ws = [w for w in ws if w]
        c = m + WORKER * len(ws)
        if best is None or c < best[1]:
            best = (ws, c)
    return best


def score_output(lines_by_commit):
    out = "".join(json.dumps({"commit": c, "workers": w}) + "\n" for c, w in lines_by_commit.items())
    r = score.evaluate_run(out.encode(), TESTS, ANS)
    return r["simulated_milliseconds"] / 1000, r["missed_commits"]
