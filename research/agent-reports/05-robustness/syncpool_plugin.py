"""pytest plugin for MAP BUILDING ONLY: run ProcessPoolExecutor work inline so coverage attributes it to the test."""
import concurrent.futures as cf, pickle
_rt = lambda x: pickle.loads(pickle.dumps(x))  # keep the pickling (serde) path the real pool exercises
class SyncPool:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer: initializer(*_rt(initargs))
    def submit(self, fn, *a, **k):
        f = cf.Future()
        try: a, k = _rt((a, k)); f.set_result(_rt(fn(*a, **k)))
        except BaseException as e: f.set_exception(e)
        return f
    def map(self, fn, *its, **kw): return map(fn, *its)
    def shutdown(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass
cf.ProcessPoolExecutor = SyncPool
