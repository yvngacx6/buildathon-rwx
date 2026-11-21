"""Write spec + id lists for instruction-level tracing of function-body code commits."""
import json, sys, os
M16 = '/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m16'
sys.path.insert(0, M16)
from an1 import load, SLOW, eff
from an3 import patch_of, case
import tp
ids = {x['number']: x['id'] for x in json.load(open('/home/aditya/Desktop/hackathons/buildathon-rwx/sample/tests.json'))['tests']}
os.makedirs(f'{M16}/it', exist_ok=True)
jobs = []
for name in sys.argv[1:]:
    for k, r in load(f'{M16}/{name}.r23.jsonl').items():
        if r['kind'] != 'code' or r['isall'] or r['nearall']:
            continue
        pt = patch_of(name, k)
        if case(r, pt) != 'function body':
            continue
        sp = tp.spec(pt)
        if not sp or any(v == 'SYNTAX' for v in sp.values()):
            continue
        sel = [n for n in r['sel'] if n not in SLOW]
        if not sel:
            continue
        tag = f'{name}__{k}'
        json.dump(sp, open(f'{M16}/it/{tag}.spec.json', 'w'))
        open(f'{M16}/it/{tag}.ids', 'w').write(' '.join(ids[n] for n in sel))
        jobs.append(tag)
open(f'{M16}/it/jobs.txt', 'w').write('\n'.join(jobs) + '\n')
print(len(jobs))
