"""Baseline vs R23 emulation; R23 extras by mechanism (exclusive greedy attribution)."""
import json, sys
from collections import defaultdict, Counter
W = '/home/aditya/Desktop/hackathons/buildathon-rwx/.claude/worktrees/agent-ab99fdbafdbe08bd4'
sys.path.insert(0, W)
from selector import pack
tests = json.load(open(W + '/sample/tests.json'))['tests']
ms = {t['number']: pack.duration_ms(t['seconds']) for t in tests}
ALL = set(ms)
SLOW = {n for n, v in ms.items() if v >= 3000}
cost = lambda s: pack.cost_ms(pack.pack(set(s), ms), ms) / 1000
allc = cost(ALL)

def load(fn):
    return {json.loads(l)['commit']: json.loads(l) for l in open(fn)}

def eff(r):
    return ALL if r['nearall'] else set(r['sel'])

def _main():
  pass
for name in (sys.argv[1:] if __name__ == "__main__" else []):
    b = load(f'{name}.jsonl'); n = load(f'{name}.r23.jsonl')
    keys = [k for k in n if k in b and n[k]['kind'] == 'code']
    T = Counter(); mech = Counter(); mechn = Counter()
    for k in keys:
        rb, rn = b[k], n[k]
        F = set(rb['failing'] or [])
        cb, cn = cost(eff(rb)), cost(eff(rn))
        T['base'] += cb; T['r23'] += cn; T['oracle'] += cost(F); T['n'] += 1
        if F - eff(rn): T['r23miss'] += 1
        if rn['nearall'] or rn['isall']:
            mech['ALL/nearALL'] += cn - cost(F); mechn['ALL/nearALL'] += 1; continue
        cur = set(rn['sel']); extra = cur - F
        groups = defaultdict(set)
        for t in extra:
            labs = rn['owners'].get(str(t), ['unattributed'])
            lab = ' + '.join(labs)
            if t in SLOW: lab = 'SLOW: ' + lab
            groups[lab].add(t)
        while groups:
            now = cost(cur)
            lab = max(groups, key=lambda m: now - cost(cur - groups[m]))
            cur -= groups.pop(lab)
            mech[lab] += now - cost(cur); mechn[lab] += 1
    nn = T['n']
    print(f"== {name}: {nn} code commits  base {T['base']:.1f}  r23 {T['r23']:.1f}  oracle {T['oracle']:.1f}  r23 misses {T['r23miss']}  per40: base {40*T['base']/nn:.1f} r23 {40*T['r23']/nn:.1f} oracle {40*T['oracle']/nn:.1f}")
    for m, v in mech.most_common(14):
        print(f"   {v:8.1f}  {40*v/nn:6.1f}/40  n={mechn[m]:3d}  {m}")
