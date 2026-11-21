"""pytest plugin: per test, record repo files opened (fixtures etc). writes $READMAP_OUT.<pid>.json"""
import builtins, io, json, os, collections
_cur = [None]; _r = collections.defaultdict(set); _orig = builtins.open; ROOT = os.getcwd() + "/"
def _open(f, *a, **k):
    if _cur[0] and isinstance(f, (str, os.PathLike)):
        p = os.path.abspath(os.fspath(f))
        if p.startswith(ROOT): _r[_cur[0]].add(p[len(ROOT):])
    return _orig(f, *a, **k)
builtins.open = _open; io.open = _open
def pytest_runtest_setup(item): _cur[0] = item.nodeid
def pytest_runtest_teardown(item): _cur[0] = None
def pytest_unconfigure(config):
    json.dump({k: sorted(v) for k, v in _r.items()}, open(f"{os.environ['READMAP_OUT']}.{os.getpid()}.json", "w"))
