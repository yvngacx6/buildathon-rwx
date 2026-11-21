"""Generate default-flip mutants where R-DEF would DROP tests; write the dropped tests to verify."""
import ast, json, os, random, sys
from pathlib import Path
M16 = '/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m16'
sys.path.insert(0, M16)
import proto_def
from an1 import W, SLOW, cost
from selector.maps import Maps, ImportTimeMap
tests = json.load(open(W + '/sample/tests.json'))['tests']
ids = {t['number']: t['id'] for t in tests}
maps = Maps(Path(W) / 'maps', tests)
SG = proto_def.SG
cands = []
for p, tree in proto_def.files().items():
    if not p.startswith('sqlglot/'):
        continue
    src = open(f'{SG}/{p}').read()
    lines = src.splitlines(keepends=True)
    for f in ast.walk(tree):
        if not isinstance(f, (ast.FunctionDef,)):
            continue
        A = f.args
        pos = A.posonlyargs + A.args
        for i, d in enumerate(A.defaults):
            if isinstance(d, ast.Constant) and isinstance(d.value, bool) and d.lineno == d.end_lineno:
                L = lines[d.lineno - 1]
                tok = repr(d.value)
                if L[d.col_offset:d.end_col_offset] != tok:
                    continue
                new_line = L[:d.col_offset] + repr(not d.value) + L[d.end_col_offset:]
                new_src = ''.join(lines[:d.lineno - 1] + [new_line] + lines[d.lineno:])
                cands.append((p, f.name, d.lineno, src, new_src))
random.seed(16)
random.shuffle(cands)
out = []
for p, fn, ln, old, new in cands:
    if fn.endswith('_sql') or fn.startswith('__'):
        continue
    res = proto_def.select(maps, p, old, new)
    if res is None:
        continue
    sel, why = res
    eds = proto_def.default_edits(old, new)
    f = eds[0][0]
    flev = maps.span(p, f.body[0].lineno, f.end_lineno)
    dropped = sorted((flev - sel) - SLOW)
    if not dropped or len(dropped) > 700:
        continue
    out.append(dict(path=p, func=fn, line=ln, rule=sorted(sel), dropped=dropped, why=why, new=new))
    if len(out) >= int(sys.argv[1]):
        break
os.makedirs(f'{M16}/dm', exist_ok=True)
for i, m in enumerate(out):
    json.dump(m, open(f'{M16}/dm/m{i}.json', 'w'))
    open(f'{M16}/dm/m{i}.ids', 'w').write(' '.join(ids[n] for n in m['dropped'] + m['rule'] if n not in SLOW))
    print(i, m['path'], m['func'], m['line'], 'rule', len(m['rule']), 'dropped', len(m['dropped']), m['why'][:100])
