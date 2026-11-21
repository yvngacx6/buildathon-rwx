#!/usr/bin/env python3
"""Replay runverify gate policies from two runs of the same set:
   OFF.err (BR_RUNVERIFY=0) and OPEN.err (gate wide open: every eligible commit verified).

usage: rv_sim.py OFF.err OPEN.err [--slow 2.0] [--share S ...] [--gate G ...]

For a policy "verify iff scorer_est <= max(G, S * static_cost)", a verified commit costs its
OPEN-run simulated seconds plus slow x its measured child CPU (EC2 seconds); any other commit
costs its OFF-run seconds. Prints net seconds vs OFF and per 40 commits. (Ignores the fork's
small parent-side CPU and the selector's map load, ~10 ms per run.)
"""

import re
import sys

LINE = re.compile(r"^\[(\S+)\] (\d+) tests, (\d+) workers, ([\d.]+)s :: (.*)$")
NOTE = re.compile(r"runverify: [^;]*; est ([\d.]+)s, child ([\d.]+)s\)")


def load(path):
    out = {}
    for line in open(path):
        m = LINE.match(line.rstrip("\n"))
        if m:
            out[m[1]] = (float(m[4]), m[5])
    return out


args = sys.argv[1:]
slow = 2.0
gates, shares = [], []
for flag, dst in (("--slow", None), ("--gate", gates), ("--share", shares)):
    while flag in args:
        i = args.index(flag)
        if dst is None:
            slow = float(args[i + 1])
        else:
            dst.append(float(args[i + 1]))
        del args[i:i + 2]
off, opn = load(args[0]), load(args[1])
gates = gates or [0.4, 0.6, 0.8, 1.0, 1.5, 2.0]
shares = shares or [0.0]
n = len(off)
for share in shares:
    for g in gates:
        base = on = cpu = 0.0
        nv = ne = 0
        for c, (cost_off, _) in off.items():
            base += cost_off
            cost_open, why = opn[c]
            m = NOTE.search(why)
            if m and slow * float(m[1]) <= max(g, share * cost_off):
                child = float(m[2])
                nv += 1
                ne += cost_open == 0
                on += cost_open + slow * child
                cpu += slow * child
            else:
                on += cost_off
        net = on - base
        print(f"share {share:.2f} gate {g:4.2f}: verified {nv:4d} emptied {ne:4d} "
              f"cpu(scorer) {cpu:6.1f}s net {net:+7.1f}s = {40 * net / n:+6.2f}s per 40 ({n} commits)")
