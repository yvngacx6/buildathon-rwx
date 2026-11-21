"""Instruction-level (sub-statement) refinement: re-score R23 selections with itrace hits."""
import json, os, sys
from collections import Counter
M16 = '/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m16'
sys.path.insert(0, M16)
from an1 import cost, SLOW, load, eff
tests = json.load(open('/home/aditya/Desktop/hackathons/buildathon-rwx/sample/tests.json'))['tests']
num = {t['id']: t['number'] for t in tests}
rs = {}
for name in ('practice_like', 'private.code', 'sample'):
    for k, r in load(f'{M16}/{name}.r23.jsonl').items():
        rs[(name.split('.')[0], k)] = r
T = Counter(); rows = []
for f in sorted(os.listdir(f'{M16}/it')):
    if not f.endswith('.hits.json'):
        continue
    tag = f[:-10]; name, k = tag.split('__')
    log = open(f'{M16}/it/{tag}.log').read()
    if 'passed' not in log and 'failed' not in log:
        continue
    r = rs[(name, k)]
    hits = json.load(open(f'{M16}/it/{f}'))
    sel = set(r['sel']); ns = sel - SLOW
    if '*' in hits:
        keep = set(ns)
    else:
        keep = set()
        for t in ns:
            tid = tests[t]['id'] if tests[t]['number'] == t else None
        idof = {t['number']: t['id'] for t in tests}
        for t in ns:
            i = idof[t]
            if i in hits or any(h.startswith('class:') and i.startswith(h[6:] + '::') for h in hits):
                keep.add(t)
    after = keep | (sel & SLOW)
    F = set(r['failing'] or [])
    miss = F - after
    cb, ca = cost(sel), cost(after)
    T['n'] += 1; T['b'] += cb; T['a'] += ca; T['miss'] += bool(miss); T[name] += cb - ca; T['n' + name] += 1
    rows.append((cb - ca, tag, len(ns), len(keep), len(F), cb, ca, sorted(miss)))
for row in sorted(rows, reverse=True)[:25]:
    print('%5.1f %-22s nonslow %4d -> %4d  fail %3d  %5.1f -> %5.1f  miss %s' % row)
print(dict(T))
