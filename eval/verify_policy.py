#!/usr/bin/env python3
"""Fit selector/verify.py's CPU model and replay its cost policy on audit rows.

usage: verify_policy.py <audit.jsonl.rows.json>...   (written by eval/verify_audit.py)

Fits cpu ≈ a + b·files + c·recorded_seconds (least squares on clean runs with R under 3 recorded
seconds: the only ones the policy can pick), then for a grid of (P_FAIL, MARGIN) replays
"run iff (1-p)·saving > estimate + margin" and prints the realized net per set (simulated
seconds saved − CPU spent). A run slower than the kill limit max(2, 3·estimate) is charged the
limit and saves nothing. The ~0.2 s server start per batch is not included.
"""

from __future__ import annotations

import json
import sys


def fit(rows):
    """Least squares y = a + b·x1 + c·x2 via the normal equations."""
    X = [(1.0, r["nf"], r["rsec"]) for r in rows]
    y = [r["cpu"] for r in rows]
    M = [[sum(x[i] * x[j] for x in X) for j in range(3)] + [sum(x[i] * t for x, t in zip(X, y))]
         for i in range(3)]
    for i in range(3):
        for k in range(3):
            if k != i:
                f = M[k][i] / M[i][i]
                M[k] = [a - f * b for a, b in zip(M[k], M[i])]
    return [M[i][3] / M[i][i] for i in range(3)]


def main() -> None:
    sets = {p.split("audit_")[-1].split(".")[0]: json.load(open(p)) for p in sys.argv[1:]}
    rows = [r for rs in sets.values() for r in rs]
    clean = [r for r in rows if r["rsec"] < 3 and not r["anomaly"]]
    a, b, c = fit(clean)
    print(f"fit on {len(clean)} runs: cpu ≈ {a:.3f} + {b:.3f}·files + {c:.3f}·seconds")
    for name, rs in sets.items():
        none = sum(r["cv"] >= r["cs"] - 1e-9 for r in rs)
        print(f"  {name}: {len(rs)} runnable, nothing saved on {none}")
    print("policy replay: commits run, net seconds")
    for p in (0.4, 0.5, 0.6, 0.7, 0.8):
        for margin in (0.0, 0.1, 0.3, 0.5):
            out = []
            for name, rs in sets.items():
                net, n = 0.0, 0
                for r in rs:
                    est = a + b * r["nf"] + c * r["rsec"]
                    if (1 - (0.9 if r["data"] else p)) * (r["cs"] - r["cs_keep"]) <= est + margin:
                        continue
                    n += 1
                    limit = max(2.0, 3 * est)
                    net += (r["cs"] - r["cv"] - r["cpu"]) if r["cpu"] < limit else -limit
                out.append(f"{name} {n:3d} {net:+6.1f}")
            print(f"  p={p} margin={margin}: " + " | ".join(out))


if __name__ == "__main__":
    main()
