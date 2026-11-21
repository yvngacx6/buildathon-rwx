"""Run pytest node ids (cwd = old sqlglot checkout) and record which tests execute any of the
differing OLD instructions (idiff spec). usage: itrace.py spec.json out.json nodeid...

Attribution: hits before the first test (collection/import) -> '*' (all); hits during a test's
setup phase -> 'class:<file::Class>' (setUpClass shared by the class); else the test id.
"""
import sys, os, json
spec = json.load(open(sys.argv[1]))
ROOT = os.path.realpath(os.getcwd()) + os.sep
want = {}
whole = set()
for rel, d in spec.items():
    if not isinstance(d, dict):
        continue
    for k, v in d.items():
        q, l = k.rsplit('|', 1)
        key = (ROOT + rel, q, int(l))
        if v == 'WHOLE':
            whole.add(key)
        else:
            want[key] = set(v)
mon = sys.monitoring; E = mon.events; TOOL = 3
mon.use_tool_id(TOOL, 'itrace')
cur = {'t': '*', 'phase': 'pre'}
hits = set()
dsets = {}


def on_start(code, off):
    key = (code.co_filename, code.co_qualname, code.co_firstlineno)
    if key in whole:
        hits.add(cur['t'] if cur['phase'] == 'call' else (cur['cls'] if cur['phase'] == 'setup' else '*'))
        return None
    if key in want:
        dsets[code] = want[key]
        mon.set_local_events(TOOL, code, E.INSTRUCTION)
        return None
    return mon.DISABLE


def on_ins(code, off):
    d = dsets.get(code)
    if d is None or off not in d:
        return mon.DISABLE
    hits.add(cur['t'] if cur['phase'] == 'call' else (cur['cls'] if cur['phase'] == 'setup' else '*'))


mon.register_callback(TOOL, E.PY_START, on_start)
mon.register_callback(TOOL, E.INSTRUCTION, on_ins)
mon.set_events(TOOL, E.PY_START)

import concurrent.futures, concurrent.futures.process, pickle


def _rt(x):
    return pickle.loads(pickle.dumps(x))


class InlinePool:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*_rt(initargs))

    def submit(self, fn, /, *a, **k):
        f = concurrent.futures.Future()
        try:
            a, k = _rt((a, k)); f.set_result(_rt(fn(*a, **k)))
        except BaseException as e:
            f.set_exception(e)
        return f

    def map(self, fn, *its, timeout=None, chunksize=1):
        return [self.submit(fn, *x).result() for x in zip(*its)]

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *e):
        return False


concurrent.futures.ProcessPoolExecutor = InlinePool
concurrent.futures.process.ProcessPoolExecutor = InlinePool
import pytest


class P:
    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_setup(self, item):
        cur['t'] = item.nodeid; cur['cls'] = 'class:' + item.nodeid.rsplit('::', 1)[0]; cur['phase'] = 'setup'

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_call(self, item):
        cur['phase'] = 'call'

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_teardown(self, item):
        cur['phase'] = 'setup'


pytest.main(sys.argv[3:] + ['-q', '-p', 'no:cacheprovider', '-p', 'no:randomly', '--no-header', '-o', 'addopts='], plugins=[P()])
mon.set_events(TOOL, 0)
json.dump(sorted(hits), open(sys.argv[2], 'w'))
