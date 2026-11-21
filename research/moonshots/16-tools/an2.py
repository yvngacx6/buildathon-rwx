"""Per-commit R23 waste listing, largest first."""
import json, sys
from collections import defaultdict, Counter
sys.path.insert(0, '/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m16')
from an1 import cost, ms, SLOW, ALL, load, eff
rows = []
for name in sys.argv[1:]:
    n = load(f'{name}.r23.jsonl')
    for k, r in n.items():
        if r['kind'] != 'code':
            continue
        F = set(r['failing'] or [])
        c = cost(eff(r)); o = cost(F)
        sel = set(r['sel'])
        ex = sel - F
        labs = Counter()
        for t in ex:
            labs[('S:' if t in SLOW else '') + '+'.join(r['owners'].get(str(t), ['?']))] += ms[t] / 1000
        rows.append((c - o, name, k, c, o, len(ex), len(F), dict(labs.most_common(3)), r['why'][:230]))
rows.sort(reverse=True)
for row in rows[:int(40)]:
    g, name, k, c, o, ne, nf, labs, why = row
    print(f'{g:6.1f} {name[:7]:7s} {k} cost {c:5.1f} or {o:5.1f} extra {ne:4d} fail {nf:3d} {json.dumps({a: round(b, 1) for a, b in labs.items()})}\n        {why}')
