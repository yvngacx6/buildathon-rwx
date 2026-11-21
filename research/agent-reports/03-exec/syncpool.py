# Replace the tests' spawn ProcessPoolExecutor with an in-process executor that still pickles
# args and results (so serde/pickle breakage still surfaces). Saves ~1 s CPU per spawned worker.
import pickle
from concurrent.futures import Future
import concurrent.futures, concurrent.futures.process
class SyncPool:
    def __init__(self, *a, initializer=None, initargs=(), **k):
        if initializer: initializer(*initargs)
    def submit(self, fn, *args, **kwargs):
        f = Future()
        try:
            a, k = pickle.loads(pickle.dumps((args, kwargs)))
            f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
        except BaseException as e:
            f.set_exception(e)
        return f
    def map(self, fn, *its): return [self.submit(fn, *x).result() for x in zip(*its)]
    def shutdown(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
def install():
    concurrent.futures.ProcessPoolExecutor = SyncPool
    concurrent.futures.process.ProcessPoolExecutor = SyncPool
