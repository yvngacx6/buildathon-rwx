"""Value-profile recorder (moonshot 03 prototype).

pytest -p vprof_plugin: every sqlglot module is compiled from source with each BoolOp and each
single-operator Compare wrapped in a recording call. Per test (setup / call / teardown phases kept
apart, '<out>' for everything outside a test) and per site, the set of abstract evaluations:

  BoolOp  -> tuple of operand states (0 not evaluated, 1 falsy, 2 truthy, 3 None, 4 False,
             5 True, 6 bool() raised; a trailing 7 = an operand raised, evaluation unfinished)
  Compare -> (abs(left), abs(right), result_is_bool, is_bit, eq_bit)
             abs(v) = ('v', type, value) for None/bool/small int/float/short str/enum members,
                      else ('t', type)
             is_bit / eq_bit: `left is right`, `left == right` when not both exact (eq only
             for is / is not / == / != sites; None if __eq__ raised or returned non-bool)

Output: $VPROF_OUT/<pid>.pkl = {'sites': {sid: (file, line, col, end_line, end_col, kind)},
                                 'prof': {phase: {sid: set}}, 'outcome': {nodeid: passed}}
"""
from __future__ import annotations

import ast
import builtins
import enum
import importlib.machinery
import os
import pickle
import sys

ROOT = None
SITES = {}
SITE_KEY = {}
PROF = {}
OUTCOME = {}
CUR = ['<out>']
CAP = 64  # distinct entries per (phase, site); overflow marker 'X'
_stacks = {}
_cur_d = [None]


D = {}
PROF['<out>'] = D
GUARD = [False]


def _set_phase(name):
    global D
    CUR[0] = name
    D = PROF.get(name)
    if D is None:
        D = PROF[name] = {}


def _add(sid, entry):
    s = D.get(sid)
    if s is None:
        D[sid] = s = set()
    s.add(entry)


def _state(v):
    if v is None:
        return 3
    if v is False:
        return 4
    if v is True:
        return 5
    try:
        return 2 if v else 1
    except BaseException:
        return 6


def vp_a0(sid, v):
    st = _stacks.get(sid)
    if st is None:
        _stacks[sid] = st = []
    st.append([_state(v)])
    return v


def vp_a(sid, k, v):
    st = _stacks[sid]
    vec = st[-1]
    while len(vec) < k:
        vec.append(0)
    vec.append(_state(v))
    return v


def vp_e(sid, n, r):
    vec = _stacks[sid].pop()
    while len(vec) < n:
        vec.append(0)
    _add(sid, tuple(vec))
    return r


_SIMPLE = (int, float, str)


def _abs(v):
    if v is None or v is True or v is False:
        return ('v', type(v).__name__, v)
    t = type(v)
    if isinstance(v, enum.Enum):
        return ('v', t.__module__ + '.' + t.__qualname__, v.name)
    if t is int and -1000 <= v <= 1000:
        return ('v', 'int', v)
    if t is float:
        return ('v', 'float', v)
    if t is str and len(v) <= 24:
        return ('v', 'str', v)
    return ('t', t.__module__ + '.' + t.__qualname__)


import operator as _op
_OPS = [_op.eq, _op.ne, _op.lt, _op.le, _op.gt, _op.ge, _op.is_, _op.is_not,
        lambda a, b: a in b, lambda a, b: a not in b]
_IDENT = {0, 1, 6, 7}


def vp_c(sid, k, a, b):
    r = _OPS[k](a, b)  # may raise: propagates exactly like the original
    if GUARD[0]:
        return r
    aa, bb = _abs(a), _abs(b)
    isb = eqb = None
    if aa[0] != 'v' or bb[0] != 'v':
        isb = a is b
        if k in _IDENT:
            GUARD[0] = True
            try:
                e = (a == b)
                eqb = e if type(e) is bool else None
            except BaseException:
                eqb = None
            GUARD[0] = False
    _add(sid, (aa, bb, type(r) is bool, isb, eqb))
    return r


builtins._vp_a0 = vp_a0
builtins._vp_a = vp_a
builtins._vp_e = vp_e
builtins._vp_c = vp_c

_CMP = {ast.Eq: 0, ast.NotEq: 1, ast.Lt: 2, ast.LtE: 3, ast.Gt: 4, ast.GtE: 5, ast.Is: 6,
        ast.IsNot: 7, ast.In: 8, ast.NotIn: 9}


def _site(rel, node, kind):
    key = (rel, node.lineno, node.col_offset, node.end_lineno, node.end_col_offset, kind)
    sid = SITE_KEY.get(key)
    if sid is None:
        sid = SITE_KEY[key] = len(SITE_KEY) + (os.getpid() << 0) * 0
        SITES[sid] = key
    return sid


def _call(name, args, like):
    c = ast.Call(func=ast.Name(id=name, ctx=ast.Load()), args=args, keywords=[])
    return ast.copy_location(c, like)


class Inst(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        sid = _site(self.rel, node, 'B')
        k = ast.Constant(sid)
        vals = [_call('_vp_a0', [k, node.values[0]], node.values[0])]
        for i, v in enumerate(node.values[1:], 1):
            vals.append(_call('_vp_a', [ast.Constant(sid), ast.Constant(i), v], v))
        node.values = vals
        return _call('_vp_e', [ast.Constant(sid), ast.Constant(len(vals)), node], node)

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1:
            return node
        sid = _site(self.rel, node, 'C')
        return _call('_vp_c', [ast.Constant(sid), ast.Constant(_CMP[type(node.ops[0])]),
                                node.left, node.comparators[0]], node)


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        path = self.get_filename(fullname)
        src = self.get_data(path)
        tree = ast.parse(src, path)
        rel = os.path.relpath(path, ROOT)
        tree = Inst(rel).visit(tree)
        ast.fix_missing_locations(tree)
        return compile(tree, path, 'exec', dont_inherit=True)


class Finder:
    def find_spec(self, name, path, target=None):
        if name != 'sqlglot' and not name.startswith('sqlglot.'):
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or not isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            return spec
        if not spec.origin.startswith(ROOT):
            return spec
        spec.loader = Loader(spec.loader.name, spec.loader.path)
        return spec


def pytest_configure(config):
    global ROOT
    ROOT = str(config.rootpath) + os.sep
    sys.dont_write_bytecode = True
    sys.meta_path.insert(0, Finder())
    if os.environ.get('VPROF_SYNCPOOL'):
        # spawn-pool jobs run in this process, so their evaluations are recorded
        import concurrent.futures as cf

        class SyncPool:
            def __init__(self, *a, **k):
                pass

            def submit(self, fn, *a, **k):
                f = cf.Future()
                try:
                    f.set_result(fn(*a, **k))
                except BaseException as e:
                    f.set_exception(e)
                return f

            def map(self, fn, *its, **k):
                return map(fn, *its)

            def shutdown(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        cf.ProcessPoolExecutor = SyncPool


def _flush():
    # unfinished evaluations (an operand raised): record with marker 7
    for sid, st in _stacks.items():
        while st:
            vec = st.pop()
            _add(sid, tuple(vec) + (7,))


import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _flush(); _set_phase(item.nodeid + '|setup')
    yield
    _flush(); _set_phase('<out>')


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _flush(); _set_phase(item.nodeid)
    yield
    _flush(); _set_phase('<out>')


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    _flush(); _set_phase(item.nodeid + '|teardown')
    yield
    _flush(); _set_phase('<out>')


def pytest_runtest_logreport(report):
    if report.failed:
        OUTCOME[report.nodeid] = False
    else:
        OUTCOME.setdefault(report.nodeid, True)


def pytest_sessionfinish(session):
    _flush()
    out = os.environ.get('VPROF_OUT')
    if out:
        with open(os.path.join(out, '%d.pkl' % os.getpid()), 'wb') as f:
            pickle.dump({'sites': SITES, 'prof': PROF, 'outcome': OUTCOME}, f, protocol=5)
