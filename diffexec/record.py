"""pytest plugin (build time, pinned sqlglot only): record every call of every sqlglot function.

For each code object under <rootdir>/sqlglot/ and each test phase, record the call inputs
(arguments + closure cells, InPickler bytes, deduplicated by digest) and, at return or
unwind, a digest of (arguments after the call, result or exception) (CmpPickler).

A function stops being recorded ("dead") when it is a generator/coroutine, when it is called
more than DIFFEXEC_CALL_CAP times in this worker, or when its distinct inputs exceed
DIFFEXEC_BYTE_CAP bytes. A (function, phase) pair whose inputs could not be pickled is
marked incomplete (None). Dead functions and incomplete pairs never let a test be dropped.

Use with PYTHONHASHSEED=0 (set ordering must match the run-time replay) and the process
pools replaced by an in-process pool (done here), so pool work is seen and attributed.

Env: DIFFEXEC_OUT (dir), DIFFEXEC_CALL_CAP (default 50000), DIFFEXEC_BYTE_CAP (default 8e6).
"""

from __future__ import annotations

import hashlib
import os
import pickle
import sys
import time
import tracemalloc as _tracemalloc

from diffexec import serial

OUT = os.environ.get("DIFFEXEC_OUT", ".")
# Caps. The store applies the same caps again after merging all workers (diffexec/build.py):
# merged calls >= any worker's calls and merged distinct bytes >= any worker's, so a function a
# worker kills here would be killed after merging too, and the store does not depend on -n.
CALL_CAP = int(os.environ.get("DIFFEXEC_CALL_CAP", "50000"))
# Distinct input bytes kept per function in this process. Over it, later NEW inputs are not
# stored and their phase is marked incomplete (the function stays alive for other tests);
# build.py chooses which phases fit the store's per-function budget.
BYTE_CAP = int(float(os.environ.get("DIFFEXEC_REC_BYTE_CAP", "2e6")))
INPUT_CAP = int(float(os.environ.get("DIFFEXEC_INPUT_CAP", "16e3")))  # one call's input bytes
# An input over INPUT_CAP is pickled again with every list / dict / set of more than ELIDE_K
# items replaced by a length-only stand-in (serial.Elided). Still over: phase incomplete.
ELIDE_K = int(os.environ.get("DIFFEXEC_ELIDE_K", "32"))
# Safety only (worker-dependent): pickling seconds per function per worker. Default: off.
TIME_CAP = float(os.environ.get("DIFFEXEC_TIME_CAP", "inf"))
# The effect digest (pure-Python pickler, ~85 % of recording time) is taken for the first
# EFFECTS_PER_INPUT calls with each distinct input; later calls with the same input only
# record the use. (Disagreeing digests mark the input "unstable".)
EFFECTS_PER_INPUT = int(os.environ.get("DIFFEXEC_EFFECTS_PER_INPUT", "2"))
ROOT = os.path.realpath(os.environ.get("DIFFEXEC_ROOT", os.getcwd())) + "/sqlglot/"
# Measurement only: record just these "path:qualname" functions (all others are skipped, as
# if dead). Used to simulate the full recorder on the functions a labeled set changes.
ONLY = None
if os.environ.get("DIFFEXEC_ONLY"):
    with open(os.environ["DIFFEXEC_ONLY"]) as _fh:
        ONLY = {line.strip() for line in _fh if line.strip()}
# Functions whose suite-wide call count (census pass, diffexec_run.py) is over CALL_CAP: they
# would be dropped after merging anyway, so never pay for recording them.
SKIP: set = set()
if os.environ.get("DIFFEXEC_SKIP"):
    with open(os.environ["DIFFEXEC_SKIP"], "rb") as _fh:
        SKIP = pickle.load(_fh)
CENSUS = os.environ.get("DIFFEXEC_CENSUS") == "1"  # count calls only
GEN_FLAGS = 0x20 | 0x80 | 0x100 | 0x200  # GENERATOR | COROUTINE | ITERABLE_COROUTINE | ASYNC_GENERATOR

mon = sys.monitoring
TOOL = 2
E = mon.events


class Rec:
    __slots__ = ("key", "calls", "nbytes", "blobs", "uses", "dead", "argn", "nfree", "secs", "big")

    def __init__(self, code):
        self.key = (code.co_filename[len(ROOT) - len("sqlglot/"):], code.co_qualname, code.co_firstlineno)
        self.calls = 0
        self.nbytes = 0
        self.blobs: dict[bytes, list] = {}  # in-digest -> [in bytes, out digest | None, status]
        self.uses: dict[object, set | None] = {}
        self.dead = None
        n = code.co_argcount + code.co_kwonlyargcount
        n += bool(code.co_flags & 0x04) + bool(code.co_flags & 0x08)
        self.argn = code.co_varnames[:n]
        self.nfree = code.co_freevars
        self.secs = 0.0
        self.big = False  # an input was over INPUT_CAP: pickle later calls elided right away
        if code.co_flags & GEN_FLAGS:
            self.dead = "generator"
        elif ONLY is not None and f"{self.key[0]}:{self.key[1]}" not in ONLY:
            self.dead = "skipped"
        elif self.key in SKIP:
            self.dead = "calls"


recs: dict = {}
stack: list = []  # (rec, frame, arg values | None, in digest | None, phase, elided | None)
cur = {"phase": "IMPORT"}
busy = [False]
stats = {"pickle_s": 0.0, "recorded": 0, "elided": 0, "incomplete": 0}


def _kill(rec, why):
    rec.dead = why
    rec.blobs = {}
    rec.uses = {}


def _count(code, offset):
    rec = recs.get(code)
    if rec is None:
        if not code.co_filename.startswith(ROOT):
            return mon.DISABLE
        rec = recs[code] = Rec(code)
    rec.calls += 1
    if rec.calls > CALL_CAP:
        return mon.DISABLE  # already over the cap: the exact count no longer matters
    return None


def _start(code, offset):
    rec = recs.get(code)
    if rec is None:
        if not code.co_filename.startswith(ROOT):
            return mon.DISABLE
        rec = recs[code] = Rec(code)
        if not rec.dead:
            mon.set_local_events(TOOL, code, E.PY_RETURN)
    if rec.dead or busy[0]:
        return mon.DISABLE if rec.dead else None
    rec.calls += 1
    if rec.calls > CALL_CAP:
        _kill(rec, "calls")
        mon.set_local_events(TOOL, code, 0)
        return mon.DISABLE
    phase = cur["phase"]
    frame = sys._getframe(1)
    if _tracemalloc.is_tracing():
        # A memory-measuring test (test_generator_memory): recording would distort what it
        # measures and is ~100x slower under tracemalloc. Mark the phase incomplete (its test
        # can never be dropped) and do not record.
        rec.uses[phase] = None
    if phase in rec.uses and rec.uses[phase] is None:
        # this (function, phase) is already incomplete: nothing more to learn, do not pickle
        stack.append((rec, frame, None, None, phase, None))
        return None
    busy[0] = True
    t0 = time.perf_counter()
    elided = None
    try:
        loc = frame.f_locals
        vals = tuple(loc[n] for n in rec.argn)
        cells = tuple(loc.get(n, serial._EMPTY) for n in rec.nfree)
        try:
            if rec.big:
                raise serial.TooBig()
            data = serial.dumps_in((vals, cells), INPUT_CAP)
        except serial.TooBig:
            rec.big = True
            elided = {}
            try:
                data = serial.dumps_in((vals, cells), INPUT_CAP, ELIDE_K, elided)
            except serial.TooBig:
                data = None
    except BaseException:
        data = None
    finally:
        busy[0] = False
        dt = time.perf_counter() - t0
        stats["pickle_s"] += dt
        rec.secs += dt
    if rec.secs > TIME_CAP:
        _kill(rec, "time")
        mon.set_local_events(TOOL, code, 0)
        return mon.DISABLE
    h = None
    if data is not None:
        h = hashlib.blake2b(data, digest_size=12).digest()
        blob = rec.blobs.get(h)
        if blob is None and rec.nbytes + len(data) <= BYTE_CAP:
            rec.nbytes += len(data)
            blob = rec.blobs[h] = [data, None, "ok", 0]
            stats["elided"] += elided is not None
    if h is None or blob is None:
        # unpicklable, too big even with elision, or over this function's byte budget: the
        # phase is incomplete (its test is never dropped); other phases stay usable
        stats["incomplete"] += 1
        rec.uses[phase] = None
        stack.append((rec, frame, None, None, phase, None))
        return None
    u = rec.uses.setdefault(phase, set())
    if u is not None:
        u.add(h)
    blob[3] += 1
    stack.append((rec, frame, vals if blob[3] <= EFFECTS_PER_INPUT else None, h, phase, elided))
    stats["recorded"] += 1
    return None


def _finish(code, outcome):
    frame = sys._getframe(2)
    while stack:
        rec, fr, vals, h, phase, elided = stack[-1]
        if fr is frame:
            break
        # a frame we missed leaving: its outcome is unknown
        stack.pop()
        if rec.dead is None and h is not None and h in rec.blobs:
            rec.blobs[h][2] = "lost"
    else:
        return
    stack.pop()
    if rec.dead or vals is None or h not in rec.blobs:
        return
    busy[0] = True
    t0 = time.perf_counter()
    try:
        d = serial.digest_cmp((vals, outcome), elided)
    except BaseException:
        d = None
    finally:
        busy[0] = False
        dt = time.perf_counter() - t0
        stats["pickle_s"] += dt
        rec.secs += dt
    blob = rec.blobs[h]
    if d is None:
        blob[2] = "outfail"
    elif blob[1] is None:
        blob[1] = d
    elif blob[1] != d:
        blob[2] = "unstable"  # same input, different effect: depends on state we did not record


def _return(code, offset, retval):
    if busy[0]:
        return None
    _finish(code, ("ret", retval))


def _unwind(code, offset, exc):
    if busy[0] or not stack:
        return None
    rec = recs.get(code)
    if rec is None or rec.dead:
        return None
    _finish(code, ("exc", type(exc).__module__, type(exc).__qualname__, str(exc)))


mon.use_tool_id(TOOL, "diffexec")
mon.register_callback(TOOL, E.PY_START, _count if CENSUS else _start)
mon.register_callback(TOOL, E.PY_RETURN, _return)
mon.register_callback(TOOL, E.PY_UNWIND, _unwind)
mon.set_events(TOOL, E.PY_START if CENSUS else E.PY_START | E.PY_UNWIND)

import pytest  # noqa: E402
from concurrent.futures import Future  # noqa: E402


class SyncPool:
    """In-process stand-in for the tests' spawn pools; still pickles args and results."""

    def __init__(self, *a, initializer=None, initargs=(), **k):
        if initializer:
            initializer(*initargs)

    def submit(self, fn, *args, **kwargs):
        f = Future()
        try:
            a, k = pickle.loads(pickle.dumps((args, kwargs)))
            f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
        except BaseException as e:
            f.set_exception(e)
        return f

    def map(self, fn, *its):
        return [self.submit(fn, *x).result() for x in zip(*its)]

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


import concurrent.futures  # noqa: E402
import concurrent.futures.process  # noqa: E402

concurrent.futures.ProcessPoolExecutor = SyncPool
concurrent.futures.process.ProcessPoolExecutor = SyncPool


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    cur["phase"] = ("SETUP", item.nodeid)
    yield
    cur["phase"] = "OTHER"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    cur["phase"] = ("CALL", item.nodeid)
    yield
    cur["phase"] = "OTHER"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    cur["phase"] = ("TEARDOWN", item.nodeid)
    yield
    cur["phase"] = "OTHER"


failed_tests: set = set()


def pytest_runtest_logreport(report):
    """A test (or one of its subtests) that fails or errors under recording: its recording may
    be cut short or differ from a clean run, so the store marks its phases incomplete."""
    if report.failed:
        failed_tests.add(report.nodeid)


def pytest_sessionfinish(session):
    mon.set_events(TOOL, 0)
    for code in recs:
        mon.set_local_events(TOOL, code, 0)
    out = {}
    for rec in recs.values():
        out[rec.key] = {"dead": rec.dead, "calls": rec.calls, "secs": rec.secs, "nbytes": rec.nbytes,
                        "blobs": rec.blobs, "uses": rec.uses}
    wid = os.environ.get("DIFFEXEC_TAG") or os.environ.get("PYTEST_XDIST_WORKER", "main")
    with open(os.path.join(OUT, f"rec_{wid}.pkl"), "wb") as fh:
        pickle.dump({"recs": out, "stats": stats, "failed": sorted(failed_tests)}, fh, protocol=5)
