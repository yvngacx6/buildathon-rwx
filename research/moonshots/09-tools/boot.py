import json, glob, random
S = '/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m09/'
pool = {}
for f in glob.glob(S + 'pc_*.json'):
    d = json.load(open(f))
    pool[f.split('pc_')[1][:-5]] = [(v[0], v[1]) for k, v in d.items() if k != '_init']
allc = [x for v in pool.values() for x in v]
for name, v in list(pool.items()) + [('ALL sets', allc)]:
    w = sorted(x[0] for x in v); c = sorted(x[1] for x in v)
    print(f'{name:14s} n={len(v):3d} wall mean {sum(w)/len(w):.2f} p95 {w[int(.95*len(w))]:.2f} max {w[-1]:.2f} | cpu mean {sum(c)/len(c):.2f} max {c[-1]:.2f}')
random.seed(0)
tot = sorted(sum(random.choice(allc)[0] for _ in range(60)) for _ in range(20000))
print('60-commit batch wall (laptop) bootstrap from all sets: median %.1f p99 %.1f p99.99 %.1f' % (tot[10000], tot[19800], tot[19998]))
worst = sorted((x[0] for x in allc), reverse=True)[:60]
print('60 most expensive commits of all sets, summed wall (laptop): %.1f' % sum(worst))
