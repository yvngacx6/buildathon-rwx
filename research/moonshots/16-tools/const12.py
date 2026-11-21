"""R-DEF extension: module constant read only as a def default (errors.py ERROR_MESSAGE_CONTEXT_DEFAULT)."""
import sys, json, ast
sys.path.insert(0, '/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m16')
from pathlib import Path
import proto_def
from an1 import cost, load, W, SLOW
from selector.maps import Maps
tests = json.load(open(W + '/sample/tests.json'))['tests']
maps = Maps(Path(W) / 'maps', tests)
src = open(proto_def.SG + '/sqlglot/errors.py').read()
f = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == 'highlight_sql'][0]
obs = []; esc = []; deft = []
for p, stmt, kind, node, at_def in proto_def.sites('highlight_sql'):
    if kind == 'call':
        if proto_def.supplies(node, 'context_length', 2, False):
            continue
        (deft if at_def else obs).append((p, stmt))
    else:
        (deft if at_def else esc).append((p, stmt.lineno, kind))
print('observing calls', [(p, s.lineno) for p, s in obs], 'escaping', esc, 'deftime', deft)
flev = maps.span('sqlglot/errors.py', f.body[0].lineno, f.end_lineno)
got = set()
for p, st in obs:
    a, b = proto_def.stmt_span(st)
    got |= maps.span(p, a, b)
sel = got & flev
print('function-level', len(flev), 'rule', len(sel), 'cost', round(cost(sel), 1), 'slow in rule', sel & SLOW)
r = load('private.code.r23.jsonl')['12xo']
print('12xo failing', r['failing'], 'R23 cost', round(cost(set(r['sel'])), 1), 'missed', set(r['failing']) - sel)
