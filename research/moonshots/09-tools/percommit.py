"""Per-commit wall and CPU (self + children) of the selector. Run from the repo root with -S,
PYTHONHASHSEED=0. Usage: percommit.py <data dir> <out json>"""
import gc, json, os, sys, time
from pathlib import Path
sys.path.insert(0, os.getcwd())
from selector import pack
from selector.select import Selector
gc.disable()
data = Path(sys.argv[1])
t0 = time.monotonic(); c0 = os.times()
tests = json.loads((data / 'tests.json').read_text())['tests']
sel = Selector(tests, Path(os.environ.get('BR_SQLGLOT', '/opt/sqlglot')), Path('maps'))
c1 = os.times()
res = {'_init': [time.monotonic() - t0, c1.user + c1.system - c0.user - c0.system]}
for pf in sorted((data / 'commits').glob('*.patch')):
    a = os.times(); w = time.monotonic()
    try:
        nums, why = sel.select(pf.read_text(errors='replace'))
    except Exception as e:
        why = f'ERR {e}'
    b = os.times()
    cpu = (b.user + b.system + b.children_user + b.children_system) - (a.user + a.system + a.children_user + a.children_system)
    res[pf.stem] = [round(time.monotonic() - w, 3), round(cpu, 3), why[:120]]
json.dump(res, open(sys.argv[2], 'w'), indent=0)
v = sorted(((x[1], k) for k, x in res.items() if k != '_init'), reverse=True)
print('init', res['_init'], 'total cpu', round(sum(x for x, _ in v), 1), 'n', len(v), 'top', v[:8])
