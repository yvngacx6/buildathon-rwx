import json, sys, collections
sys.path.insert(0, sys.argv[1])
from selector import pack
from pathlib import Path
S = Path(sys.argv[2])
showall = '-a' in sys.argv
for d in [a for a in sys.argv[3:] if a != '-a']:
    d = Path(d); name = d.name
    tests = json.load(open(d / 'tests.json'))['tests']; ms = {t['number']: pack.duration_ms(t['seconds']) for t in tests}
    ans = json.load(open(d / 'answers.json'))['commits']
    rows = {}
    for l in open(S / f'{name}.out'):
        r = json.loads(l); rows[r['commit']] = r['workers']
    nv = json.load(open(S / f'naive_{name}.json'))
    agg = collections.defaultdict(lambda: [0] * 9)
    for c, a in ans.items():
        w = [x for x in rows.get(c, []) if x]; cost = (max(sum(ms[n] for n in x) for x in w) + 2000 * len(w)) if w else 0
        miss = bool(set(a['failing']) - {n for x in w for n in x})
        f = set(a['failing']); oc = pack.cost_ms(pack.pack(f, ms), ms) if f else 0
        k = a.get('kind', '?'); g = agg[k]; g[0] += 1; g[1] += cost; g[2] += miss; g[3] += oc
        n1 = nv[c][1]['N1']; g[4] += n1[0] * 1000; g[5] += n1[1]; g[6] += bool(f)
        n2 = nv[c][1]['N2']; g[7] += n2[0] * 1000; g[8] += n2[1]
    print('==', name, ' kind n n_with_fail | ours oracle excess misses | N1 cost miss | N2 cost miss')
    T = [0] * 9
    for k, g in sorted(agg.items(), key=lambda x: -x[1][1] + x[1][3]):
        print(f'  {k[:28]:28s} {g[0]:3d} {g[6]:3d} | {g[1]/1000:7.1f} {g[3]/1000:7.1f} {(g[1]-g[3])/1000:7.1f} {g[2]}m | {g[4]/1000:7.1f} {g[5]}m | {g[7]/1000:7.1f} {g[8]}m')
        T = [a + b for a, b in zip(T, g)]
    print(f'  TOTAL n={T[0]} ours {T[1]/1000:.1f} miss {T[2]} oracle {T[3]/1000:.1f} | N1 {T[4]/1000:.1f} miss {T[5]} | N2 {T[7]/1000:.1f} miss {T[8]}')
