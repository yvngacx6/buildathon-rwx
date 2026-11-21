"""Expected-cost selection: minimise  C(S) + 300 * P(miss | S)  for one commit.

p: {test_number: probability the test fails}.  Independence approx:
    P(miss | S) = 1 - prod_{t not in S} (1 - p_t)
C(S) = min_k [ makespan_k(S) + 2k ]   (packed with LPT, k = 1..8)

Algorithm (fast, near-optimal):
  1. heavy tests (>= 1 s) are few: enumerate every subset of the heavy candidates.
  2. light tests sorted by p/d (risk removed per ms) -> sweep prefixes; evaluate
     each prefix's expected cost; keep the best.
  3. 'free fill': after packing, drop any remaining test with p > 0 into worker
     slack (load + d <= makespan) -- costs exactly 0.
"""
import itertools, math
from lib import MS, TESTS, lpt, best_pack

PEN = 300_000  # ms


def pack_cost(nums):
    return best_pack(nums, exact=False)


def expected_cost(sel, p):
    ws, c = pack_cost(sel)
    s = set(sel)
    surv = math.prod(1 - q for t, q in p.items() if t not in s)
    return c + PEN * (1 - surv), ws


def free_fill(ws, p):
    if not ws:
        return ws
    loads = [sum(MS[n] for n in w) for w in ws]
    M = max(loads)
    used = {n for w in ws for n in w}
    for t in sorted((t for t in p if t not in used and p[t] > 0), key=lambda t: -p[t]):
        j = loads.index(min(loads))
        if loads[j] + MS[t] <= M:
            ws[j].append(t); loads[j] += MS[t]
    return ws


def optimise(p, heavy_ms=1000):
    cand = [t for t, q in p.items() if q > 0]
    heavy = [t for t in cand if MS[t] >= heavy_ms]
    light = sorted((t for t in cand if MS[t] < heavy_ms), key=lambda t: -p[t] / max(MS[t], 1))
    # O(1)-per-step estimate: cost ~= min_k max(maxd, ceil(S/k)) + 2k (tight for many small items)
    def est(S, mx):
        return 0 if S == 0 and mx == 0 else min(max(mx, -(-S // k)) + 2000 * k for k in range(1, 9))
    logsurv_all = sum(math.log1p(-p[t]) for t in cand)
    best = (PEN * (1 - math.exp(logsurv_all)), None, "run nothing")
    for r in range(len(heavy) + 1):
        for H in itertools.combinations(heavy, r):
            S = sum(MS[t] for t in H); mx = max([MS[t] for t in H] + [0])
            ls = logsurv_all - sum(math.log1p(-p[t]) for t in H)
            n_items = len(H)
            for i in range(len(light) + 1):
                if i:
                    t = light[i - 1]; S += MS[t]; mx = max(mx, MS[t]); ls -= math.log1p(-p[t])
                if n_items + i == 0:
                    continue
                e = est(S, mx) + PEN * (1 - math.exp(ls))
                if e < best[0]:
                    best = (e, list(H) + light[:i], f"{len(H)} heavy + {i} light")
    _, sel, why = best
    if sel is None:
        return best[0], [], why
    e, ws = expected_cost(sel, p)  # exact packing / cost of the chosen set
    return e, free_fill([list(w) for w in ws], p), why


if __name__ == "__main__":
    name = {t["id"].split("::")[-1]: t["number"] for t in TESTS}
    print("Break-even failure probability p* = marginal cost / 300 s")
    for label, d in [("tiny test into existing slack", 0), ("0.6 s test, 1 worker already busy 0.1 s", 0.5),
                     ("any test on an otherwise-empty commit (+worker)", 2.0),
                     ("test_generator_memory 6.7 s, commit already at 2.6 s", 6.686 - 0.596),
                     ("test_execute_tpcds 21.5 s on light commit", 21.454 - 0.6),
                     ("test_max_nodes 28.8 s on light commit", 28.817 - 0.6),
                     ("test_max_nodes when tpcds already runs (2nd worker)", 28.817 - 21.454 + 2)]:
        print(f"  {label:55s} dC={d:6.2f}s  p*={d/300:.4f}")

    for po, pd, pt in [(0.004, 0.0005, 0.02), (0.0005, 0.0001, 0.005), (0.0, 0.0, 0.0)]:
        p = {}
        for t in TESTS:
            if t["file"] == "tests/dialects/test_tsql.py": p[t["number"]] = 0.05
            elif t["file"] == "tests/test_optimizer.py" and po: p[t["number"]] = po
            elif t["file"].startswith("tests/dialects/") and pd: p[t["number"]] = pd
        if pt: p[name["test_execute_tpcds"]] = pt
        e, ws, why = optimise(p)
        sel = {x for w in ws for x in w}
        print(f"Toy dialect commit, p(tsql)=0.05 each, p(optimizer)={po}, p(other dialect)={pd}, p(tpcds)={pt}:")
        print(f"  -> {why}; {len(ws)} worker(s), {len(sel)} tests after free-fill; E={e/1000:.2f}s"
              f" sim={best_pack(list(sel), exact=False)[1]/1000:.2f}s"
              f" P(miss)={1-math.prod(1-q for t,q in p.items() if t not in sel):.4f}")
