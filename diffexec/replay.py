"""Run time: replay recorded calls of the patched functions, old code vs new code.

Invoked by the selector as a child process (``python -m diffexec.replay``) with
PYTHONHASHSEED=0 and a JSON request on stdin:

    {"root": "/opt/sqlglot", "store": "<store .zip>", "deadline_s": 20,
     "files": [{"path": "sqlglot/x.py", "new_text": "..."}]}

Answer on stdout (JSON):

    {"bail": null | "<reason>",
     "codes": {"<path>::<qualname>::<line>": {"state": "ok" | "dead:<why>" | "nocalls",
                                              "phases": {"<phase>": "ok" | "bad" | "incomplete"}}},
     "stats": {...}}

A blob (one recorded call input) is "ok" only if
  recorded effect digest == digest(old code on the input) == digest(new code on the input),
the old and new runs emit the same log records, warnings and stdout, and the new run is not
much slower. The effect digest covers the arguments after the call (mutations, aliasing)
and the result or exception. During the "new" run every live function object of a changed
code object has its ``__code__`` swapped, so recursive and nested calls also run new code.
Anything unexpected → the phase is not "ok" → the selector keeps the tests.
"""

from __future__ import annotations

import contextlib
import functools
import gc
import zipfile
import importlib
import io
import json
import logging
import os
import pickle
import signal
import sys
import time
import types
import warnings

DEBUG = bool(os.environ.get("DIFFEXEC_DEBUG"))
GEN_FLAGS = 0x20 | 0x80 | 0x100 | 0x200


def _walk(top):
    """Code objects of a module in a deterministic order, keyed (qualname, occurrence)."""
    out, seen = {}, {}
    todo = [top]
    while todo:
        c = todo.pop(0)
        n = seen.get(c.co_qualname, 0)
        seen[c.co_qualname] = n + 1
        out[(c.co_qualname, n)] = c
        todo.extend(k for k in c.co_consts if isinstance(k, types.CodeType))
    return out


class _OverTime(BaseException):
    pass


def _raise_overtime(signum, frame):
    raise _OverTime()


@contextlib.contextmanager
def _cpu_limit(seconds: float):
    """Interrupt a replayed call (e.g. a patched loop that never ends) after `seconds` of CPU."""
    old = signal.signal(signal.SIGPROF, _raise_overtime)
    signal.setitimer(signal.ITIMER_PROF, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_PROF, 0)
        signal.signal(signal.SIGPROF, old)


from diffexec.serial import TOUCHED as _TOUCHED, Elided  # noqa: E402

_OWN = {Elided.__len__.__code__, Elided.__bool__.__code__}  # the stand-in reading itself
_MON = sys.monitoring
_MON_TOOL = 4
_PY_CALLABLES = (types.FunctionType,)


def _on_call(code, offset, callee, arg0):
    """CALL event while a replayed input holds elided stand-ins: a builtin, class or other
    non-Python callable applied to a stand-in (type(), id(), isinstance(), list(), a slot
    wrapper ...) may look at it without going through its methods, so count it as a use.
    len() and bool() are answered exactly by the stand-in. Python functions are fine: what
    their bodies do with the stand-in is watched in turn."""
    if type(arg0) is Elided and callee is not len and callee is not bool and code not in _OWN:
        f = getattr(callee, "__func__", callee) if type(callee) is types.MethodType else callee
        if type(f) not in _PY_CALLABLES:
            _TOUCHED[0] = True
            if DEBUG:
                print(f"[diffexec] touch: {callee!r} at {code.co_qualname}:{offset}", file=sys.stderr)


def _watch_calls(on: bool) -> None:
    if on:
        if _MON.get_tool(_MON_TOOL) is None:
            _MON.use_tool_id(_MON_TOOL, "diffexec-replay")
            _MON.register_callback(_MON_TOOL, _MON.events.CALL, _on_call)
        _MON.set_events(_MON_TOOL, _MON.events.CALL)
    else:
        _MON.set_events(_MON_TOOL, 0)


_MATCH_OPS = {"MATCH_SEQUENCE", "MATCH_MAPPING", "MATCH_CLASS", "MATCH_KEYS"}


def uses_match(code) -> bool:
    """Does this code object, or one nested in it, use structural pattern matching?"""
    import dis
    todo = [code]
    while todo:
        c = todo.pop()
        if any(i.opname in _MATCH_OPS for i in dis.get_instructions(c)):
            return True
        todo.extend(k for k in c.co_consts if isinstance(k, types.CodeType))
    return False


_KINDS = ("SETUP", "CALL", "TEARDOWN")


def phase_name(p, tests=()) -> str:
    """"IMPORT" / "OTHER", or "<kind>|<test id>" (store: int test number * 3 + kind)."""
    if isinstance(p, int):
        return f"{_KINDS[p % 3]}|{tests[p // 3]}"
    return p if isinstance(p, str) else f"{p[0]}|{p[1]}"


class _Logs(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.got = []

    def emit(self, record):
        try:
            self.got.append((record.name, record.levelno, record.getMessage()))
        except Exception as e:  # noqa: BLE001
            self.got.append(("<format error>", type(e).__name__))


def main() -> None:
    print(json.dumps(run_request(json.load(sys.stdin))))


def run_request(req: dict) -> dict:
    t_start = time.process_time()
    deadline = t_start + float(req.get("deadline_s", 20))
    root = os.path.realpath(req["root"])
    sys.path.insert(0, root)
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 10000))
    from diffexec import serial

    out = {"bail": None, "codes": {}, "stats": {}}
    store = zipfile.ZipFile(req["store"])
    meta = pickle.loads(store.read("index.pkl"))
    index, tests = meta["functions"], meta.get("tests", ())

    match_ops = False  # a changed code object uses structural pattern matching
    plan = []  # (key str, store entry, old code, new code, module name)
    swaps = []  # (old code, new code)
    new_defaults = {}  # (filename, qualname, first line) → {param: new default}
    for f in req["files"]:
        path = f["path"]
        filename = os.path.join(root, path)
        with open(filename, "rb") as fh:
            old_top = compile(fh.read(), filename, "exec", dont_inherit=True)
        new_top = compile(f["new_text"].encode(), filename, "exec", dont_inherit=True)
        old_codes, new_codes = _walk(old_top), _walk(new_top)
        if old_codes.keys() != new_codes.keys():
            out["bail"] = f"{path}: functions added, removed or reordered"
            break
        module = path[:-3].replace("/", ".").removesuffix(".__init__")
        for k, o in old_codes.items():
            n = new_codes[k]
            if serial.code_digest(o) == serial.code_digest(n):
                continue
            swaps.append((filename, o, n))
            match_ops = match_ops or uses_match(n)
            if o.co_freevars != n.co_freevars or o.co_cellvars != n.co_cellvars:
                out["bail"] = f"{path}: {o.co_qualname} changes its closure layout"
                break
            if serial.code_digest(o, shallow=True) == serial.code_digest(n, shallow=True):
                continue  # differs only in nested code objects, which are verified themselves
            if o is old_top or not (o.co_flags & 0x01):  # module or class body: definition time
                out["bail"] = f"{path}: {o.co_qualname} body (definition time) changed"
                break
            key = f"{path}::{o.co_qualname}::{o.co_firstlineno}"
            plan.append((key, index.get((path, o.co_qualname, o.co_firstlineno)), o, n, module,
                         None))
        if out["bail"]:
            break
        # Default-only edits (selector `_default_changes`; new_text is then the old source):
        # the same code object with other literal defaults. A recorded call holds every
        # parameter bound; one whose value is not the old default (same type, equal) was
        # passed by the caller and is unaffected. Otherwise it may have been the default:
        # the new run gets the new default (if the caller passed it, the old and new code
        # would really agree, so a difference found here only keeps tests).
        for d in req.get("defaults", ()):
            if d["path"] != path or out["bail"]:
                continue
            hits = [o for o in old_codes.values()
                    if o.co_firstlineno == d["line"] and o.co_name == d["name"]
                    and o.co_flags & 0x01]
            names = hits[0].co_varnames[:hits[0].co_argcount + hits[0].co_kwonlyargcount] \
                if len(hits) == 1 else ()
            if len(hits) != 1 or not set(d["params"]) <= set(names):
                out["bail"] = f"{path}: default change of {d['name']} not matched"
                break
            o = hits[0]
            key = f"{path}::{o.co_qualname}::{o.co_firstlineno}"
            subst = {names.index(p): tuple(v) for p, v in d["params"].items()}
            new_defaults[(filename, o.co_qualname, o.co_firstlineno)] = {
                p: v[1] for p, v in d["params"].items()}
            plan.append((key, index.get((path, o.co_qualname, o.co_firstlineno)), o, o, module,
                         subst))
        if out["bail"]:
            break
    if out["bail"] or not plan:
        out["stats"]["cpu_s"] = round(time.process_time() - t_start, 3)
        return out

    modules = {}
    for _, _, _, _, m, _ in plan:
        modules[m] = importlib.import_module(m)
    for f in req["files"]:
        importlib.import_module(f["path"][:-3].replace("/", ".").removesuffix(".__init__"))

    # live function objects of every changed code object (methods, closures, dict entries ...)
    by_key = {}
    for filename, o, n in swaps:
        by_key[(filename, o.co_qualname, o.co_firstlineno)] = (o, n)
    live = []  # (function, old live code, new code)
    live_defaults = []  # (function, old / new __defaults__, old / new __kwdefaults__)
    caches = []
    for obj in gc.get_objects():
        if type(obj) is types.FunctionType:
            c = obj.__code__
            hit = by_key.get((c.co_filename, c.co_qualname, c.co_firstlineno))
            if hit is not None:
                live.append((obj, c, hit[1]))
            params = new_defaults.get((c.co_filename, c.co_qualname, c.co_firstlineno))
            if params is not None:
                old_d, old_kw = obj.__defaults__, obj.__kwdefaults__
                npos = c.co_argcount
                pos_names = c.co_varnames[npos - len(old_d or ()):npos]
                new_d = tuple(params.get(nm, v) for nm, v in zip(pos_names, old_d or ()))
                new_kw = ({k: params.get(k, v) for k, v in old_kw.items()}
                          if old_kw is not None else None)
                live_defaults.append((obj, old_d, old_kw, new_d if old_d is not None else None,
                                      new_kw))
        elif type(obj) is functools._lru_cache_wrapper:
            caches.append(obj)
    if {(f.__code__.co_filename, f.__code__.co_qualname, f.__code__.co_firstlineno)
            for f, *_ in live_defaults} != set(new_defaults):
        out["bail"] = "a default-changed function has no live function object"
        out["stats"]["cpu_s"] = round(time.process_time() - t_start, 3)
        return out

    def set_mode(new: bool):
        for fn, old_d, old_kw, new_d, new_kw in live_defaults:
            fn.__defaults__ = new_d if new else old_d
            fn.__kwdefaults__ = new_kw if new else old_kw
        for fn, old_c, new_c in live:
            fn.__code__ = new_c if new else old_c
        serial.CODE_OVERRIDE.clear()
        if new:
            for (filename, q, line), (_, n) in by_key.items():
                serial.CODE_OVERRIDE[(filename, q, line)] = n
        for c in caches:
            c.cache_clear()

    def run(code, module, data, subst=None):
        made = serial.MADE[0]
        vals, cells = serial.loads_in(data)
        stubs = serial.MADE[0] > made  # some containers were elided at record time
        if stubs and match_ops:
            # MATCH_SEQUENCE / MATCH_MAPPING test the type's flags directly (no dunder, no
            # CALL event): a stand-in would fail to match silently. The pinned sqlglot has no
            # `match` statement, so only patched code can bring one.
            raise serial.ElidedAccess()
        closure = tuple(types.CellType() if c == serial._EMPTY else types.CellType(c)
                        for c in cells) or None
        fn = types.FunctionType(code, modules[module].__dict__, code.co_name, None, closure)
        npos = code.co_argcount
        nkw = code.co_kwonlyargcount
        names = code.co_varnames
        args = list(vals[:npos])
        kwargs = {names[npos + i]: vals[npos + i] for i in range(nkw)}
        rest = list(vals[npos + nkw:])
        if code.co_flags & 0x04:
            args.extend(rest.pop(0))
        if code.co_flags & 0x08:
            kwargs.update(rest.pop(0))
        for i, (old, new) in (subst or {}).items():  # vals keep the old value: same digest
            cur = args[i] if i < npos else kwargs[names[i]]
            if type(cur) is type(old) and cur == old:
                if i < npos:
                    args[i] = new
                else:
                    kwargs[names[i]] = new
        handler = _Logs()
        rootlog = logging.getLogger()
        rootlog.addHandler(handler)
        stdout = io.StringIO()
        t0 = time.process_time()
        try:
            with warnings.catch_warnings(record=True) as warned, contextlib.redirect_stdout(stdout):
                warnings.simplefilter("always")
                serial.TOUCHED[0] = False
                if stubs:
                    _watch_calls(True)
                try:
                    outcome = ("ret", fn(*args, **kwargs))
                except Exception as e:  # noqa: BLE001
                    outcome = ("exc", type(e).__module__, type(e).__qualname__, str(e))
                finally:
                    if stubs:
                        _watch_calls(False)
        finally:
            rootlog.removeHandler(handler)
        if serial.TOUCHED[0]:
            raise serial.ElidedAccess()  # an elided container was used: the call is not ok
        cpu = time.process_time() - t0
        digest = serial.digest_cmp((tuple(vals), outcome))
        side = (handler.got, [(w.category.__name__, str(w.message)) for w in warned], stdout.getvalue())
        return digest, side, cpu

    n_blobs = n_ok = n_late = n_bytes = n_retry = 0
    for key, entry, old_code, new_code, module, subst in plan:
        info = {"state": "ok", "phases": {}}
        out["codes"][key] = info
        if entry is None:
            info["state"] = "nocalls"
            continue
        dead, fileno = entry
        if dead:
            info["state"] = f"dead:{dead}"
            continue
        if old_code.co_flags & GEN_FLAGS:
            info["state"] = "dead:generator"
            continue
        rec = pickle.loads(store.read(f"c/{fileno}.pkl"))
        live_old = next((c for fn, c, _ in live if c.co_qualname == old_code.co_qualname
                         and c.co_firstlineno == old_code.co_firstlineno), old_code)
        verdict = []
        overtime = False
        for data, recorded, status in rec["blobs"]:
            n_blobs += 1
            n_bytes += len(data)
            if overtime or status != "ok" or recorded is None or time.process_time() > deadline:
                n_late += status == "ok" and recorded is not None
                verdict.append(False)
                continue
            # A timing verdict (new run > 5x old, or over its 10x cap) is re-measured once with
            # a fresh old / new pair, min of each: a page-reclaim or SMT-contention spike in one
            # run must not flip the blob (moon-17). Digests are never re-tried.
            cpu_olds, cpu_news = [], []
            ok = False
            for attempt in (0, 1):
                timing = False
                try:
                    set_mode(False)
                    with _cpu_limit(max(0.0, deadline - time.process_time()) + 0.01):
                        d_old, side_old, cpu_old = run(live_old, module, data)
                    cpu_olds.append(cpu_old)
                    set_mode(True)
                    cap = min(max(0.0, deadline - time.process_time()), 10 * min(cpu_olds) + 0.2)
                    try:
                        with _cpu_limit(cap + 0.01):
                            d_new, side_new, cpu_new = run(new_code, module, data, subst)
                    except _OverTime:
                        if attempt == 0 and time.process_time() < deadline:
                            n_retry += 1
                            continue  # re-measure once
                        raise
                    cpu_news.append(cpu_new)
                    d_old, d_new = d_old[:len(recorded)], d_new[:len(recorded)]
                    same = d_old == recorded == d_new and side_old == side_new
                    timing = min(cpu_news) <= 5 * min(cpu_olds) + 0.05
                    ok = same and timing
                    if DEBUG and not ok:
                        print(f"[diffexec] {key}: recorded==old {d_old == recorded} old==new {d_old == d_new} "
                              f"side {side_old == side_new} cpu {cpu_old:.4f}/{cpu_new:.4f}", file=sys.stderr)
                    if same and not timing and attempt == 0:
                        n_retry += 1
                        continue
                    break
                except BaseException as e:  # noqa: BLE001  (includes RecursionError, unpicklable output)
                    if DEBUG:
                        print(f"[diffexec] {key}: error {type(e).__name__}: {e}", file=sys.stderr)
                        if os.environ.get("DIFFEXEC_DEBUG_TB"):
                            import traceback
                            traceback.print_exc(limit=-3)
                    ok = False
                    overtime = isinstance(e, _OverTime)  # likely a loop that never ends: stop here
                    break
                finally:
                    set_mode(False)
            n_ok += ok
            verdict.append(ok)
        for phase, idxs in rec["uses"].items():
            if idxs is None:
                v = "incomplete"
            else:
                v = "ok" if all(verdict[i] for i in idxs) else "bad"
            info["phases"][phase_name(phase, tests)] = v
    out["stats"] = {"blobs": n_blobs, "ok": n_ok, "late": n_late, "retry": n_retry, "bytes": n_bytes, "cpu_s": round(time.process_time() - t_start, 3)}
    return out


if __name__ == "__main__":
    main()
