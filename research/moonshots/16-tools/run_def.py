"""Apply R-DEF offline on top of the R23 selection; soundness vs labels."""
import json, sys
from pathlib import Path
M16 = '/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m16'
sys.path.insert(0, M16)
from an1 import cost, ms, SLOW, ALL, load, eff, W
from an3 import patch_of
import proto_def
from selector import diff
from selector.maps import Maps, ImportTimeMap
tests = json.load(open(W + '/sample/tests.json'))['tests']
maps = Maps(Path(W) / 'maps', tests)
itm = ImportTimeMap(Path(W) / 'maps' / 'import_time.json.gz')
SG = proto_def.SG
for name in sys.argv[1:]:
    n = load(f'{name}.r23.jsonl')
    tot = [0, 0, 0, 0]; N = 0
    for k, r in n.items():
        if r['kind'] != 'code':
            continue
        N += 1
        ptxt = patch_of(name, k)
        fps = [fp for fp in diff.parse(ptxt)]
        if len(fps) != 1 or not fps[0].path.endswith('.py') or not fps[0].path.startswith('sqlglot/'):
            continue
        p = fps[0].path
        old = open(f'{SG}/{p}').read()
        try:
            new = diff.apply(old, fps[0])
            res = proto_def.select(maps, p, old, new)
        except Exception as e:
            print('ERR', k, e); continue
        if res is None:
            continue
        sel, why = res
        # function runs at import? -> rule must defer
        eds = proto_def.default_edits(old, new)
        imp = set()
        for f, *_ in eds:
            imp |= itm.modules(p, f.body[0].lineno, f.end_lineno)
        F = set(r['failing'] or []) if r['failing'] is not None else None
        before = eff(r)
        after = sel | (SLOW & set())
        cb, ca = cost(before), cost(after)
        miss = (F - after) if F is not None else set()
        tag = 'IMPORT-RUN(defer)' if imp else ''
        if not imp:
            tot[0] += cb; tot[1] += ca
        tot[2] += 1; tot[3] += bool(miss)
        print(f'{name:13s} {k} {cb:6.1f} -> {ca:6.1f}  fail {len(F) if F is not None else "?"}  MISS {sorted(miss)[:5] if miss else 0} {tag} :: {why[:150]}')
    print(f'== {name}: {N} code commits; rule applies {tot[2]}; cost {tot[0]:.1f} -> {tot[1]:.1f} (saves {tot[0]-tot[1]:.1f}, per40 {24*(tot[0]-tot[1])/N:.1f}); commits with miss {tot[3]}')
