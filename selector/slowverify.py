"""Phase verification of tests/test_parser.py::TestParser::test_max_nodes (r18).

The test (28.8 s on the board) is six phases:
  P1 parse_one("SELECT 1", max_nodes=1e6)              cheap
  P2 parse_one(500 columns FROM t, max_nodes=1e6)       cheap
  P3 parse_one(10000 x (x=x=...=x), max_nodes=1e6)      ~70 % of the time: must raise
     ParseError, str() containing "Maximum number of AST nodes"
  P4 the same sql, max_nodes=50_000                     ~25 %: ParseError, "(50000)"
  P5 parse_one(100 x=i, max_nodes=5)                    cheap: ParseError
  P6 parse_one("SELECT 1, 2, 3")                        cheap

Build (pinned source, not charged; each of P3 / P4 in its own cold fork, so a memo that an
earlier phase would warm cannot hide lines): the sqlglot lines P3 / P4 run OUTSIDE
sqlglot/errors.py, and every "frontier" call into errors.py (a frame in errors.py whose caller
is not in errors.py: raise_error → highlight_sql, ParseError.new; one each per phase) with its
arguments (pickled at entry) and the canonical value the old code returns for them.

Run, for a commit whose .py edits are all clean function-body edits (the diffexec jobs, as
subtest.py):
  1. a touched statement on a P3 / P4 line outside errors.py → keep.
  2. in a forked child with the changed functions' code swapped in:
       * P1, P2, P5, P6 run exactly as the test runs them (same calls, same assertions);
       * each recorded frontier call is made again with its recorded arguments; its value
         (type-strict canonical form, exceptions by type / args / attributes) and its
         arguments after the call must equal the pinned ones.
     Anything else (exception, difference, timeout) → keep.
Why that is the test's outcome: P3 / P4 under the new code run the same statements as under
the old code (none outside errors.py is touched), from the same state (jobs write no shared
state; Dialect.parser() / tokenizer() are fresh objects per parse_one), until a frontier call;
each frontier call is a function of its arguments and unchanged module state, and gives the
same value without changing its arguments, so execution continues identically to the end of
the phase. The other phases are run for real.
"""

from __future__ import annotations

import gc
import hashlib
import os
import pickle
import sys
import time
import zlib
from pathlib import Path

from .subtest import SCORER_SLOWDOWN, _Cov, _hit, _in_fork, _swap

REPO = Path(__file__).resolve().parent.parent
DEFAULT_STORE = REPO / "index" / "slowverify.pkl"
TEST = "tests/test_parser.py::TestParser::test_max_nodes"
FRONTIER = "sqlglot/errors.py"
BUDGET_S = float(os.environ.get("BR_SLOWVERIFY_S", "4"))


# ── what the test does ────────────────────────────────────────────────────────

def big_sql() -> str:
    width = 10000
    depth = 100
    expr = "x" + "=x" * depth
    cols = [expr for _ in range(width)]
    return f"SELECT {','.join(cols)}"


def phase(name: str) -> None:
    """One phase of test_max_nodes, with the test's assertions (AssertionError on failure)."""
    from sqlglot import exp, parse_one
    from sqlglot.errors import ParseError

    def raises(fn):
        try:
            fn()
        except ParseError as e:
            return e
        raise AssertionError("ParseError not raised")

    if name == "P1":
        assert isinstance(parse_one("SELECT 1", max_nodes=1_000_000), exp.Select)
    elif name == "P2":
        cols = [f"col{i}" for i in range(500)]
        sql = f"SELECT {','.join(cols)} FROM t"
        assert isinstance(parse_one(sql, max_nodes=1_000_000), exp.Select)
    elif name == "P3":
        sql = big_sql()
        e = raises(lambda: parse_one(sql, max_nodes=1_000_000))
        assert "Maximum number of AST nodes" in str(e)
    elif name == "P4":
        sql = big_sql()
        e = raises(lambda: parse_one(sql, max_nodes=50_000))
        assert "(50000)" in str(e)
    elif name == "P5":
        raises(lambda: parse_one("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5))
    elif name == "P6":
        assert isinstance(parse_one("SELECT 1, 2, 3"), exp.Select)
    else:
        raise ValueError(name)


CHEAP = ("P1", "P2", "P5", "P6")
SLOW = ("P3", "P4")


# ── canonical values ──────────────────────────────────────────────────────────

class Unsupported(Exception):
    pass


def canon(v, depth: int = 0):
    """Type-strict canonical form of plain values and exceptions (else Unsupported)."""
    if depth > 20:
        raise Unsupported("deep")
    t = type(v)
    if v is None or t in (bool, int, float, str, bytes):
        return (t.__name__, repr(v))
    if t in (tuple, list):
        return (t.__name__, tuple(canon(x, depth + 1) for x in v))
    if t is dict:
        return ("dict", tuple((canon(k, depth + 1), canon(x, depth + 1)) for k, x in v.items()))
    if isinstance(v, BaseException):
        return ("exc", t.__module__, t.__qualname__, canon(v.args, depth + 1),
                canon(dict(sorted(vars(v).items())), depth + 1), str(v),
                v.__cause__ is None, v.__context__ is None)
    raise Unsupported(t.__name__)


def digest(v) -> str:
    return hashlib.blake2b(repr(canon(v)).encode(), digest_size=16).hexdigest()


def resolve(module, qualname: str):
    """Module-level function or classmethod by qualified name (bound), else Unsupported."""
    parts = qualname.split(".")
    if "<locals>" in parts or len(parts) > 2:
        raise Unsupported(qualname)
    obj = getattr(module, parts[0])
    if len(parts) == 2:
        raw = obj.__dict__.get(parts[1])
        if not isinstance(raw, classmethod):
            raise Unsupported(qualname)
        obj = getattr(obj, parts[1])
    return obj


def load_kwargs(entry: dict) -> dict:
    kwargs = pickle.loads(zlib.decompress(entry["args"]))
    if entry.get("drop_cls"):
        kwargs.pop("cls")  # a classmethod: resolve() returns it bound
    return kwargs


# ── build ─────────────────────────────────────────────────────────────────────

def fingerprint(root: Path) -> str:
    h = hashlib.blake2b(digest_size=16)
    for p in sorted((root / "sqlglot").rglob("*.py")):
        h.update(str(p.relative_to(root)).encode())
        h.update(p.read_bytes())
    h.update((root / "tests" / "test_parser.py").read_bytes())
    return h.hexdigest()


def _test_source_ok(root: Path) -> bool:
    """The test's body is the one `phase` replicates."""
    src = (root / "tests" / "test_parser.py").read_text()
    want = '''    def test_max_nodes(self):
        self.assertIsInstance(parse_one("SELECT 1", max_nodes=1_000_000), exp.Select)

        cols = [f"col{i}" for i in range(500)]
        sql = f"SELECT {','.join(cols)} FROM t"
        self.assertIsInstance(parse_one(sql, max_nodes=1_000_000), exp.Select)

        width = 10000
        depth = 100
        expr = "x" + "=x" * depth
        cols = [expr for _ in range(width)]
        sql = f"SELECT {','.join(cols)}"

        with self.assertRaises(ParseError) as ctx:
            parse_one(sql, max_nodes=1_000_000)
        self.assertIn("Maximum number of AST nodes", str(ctx.exception))

        with self.assertRaises(ParseError) as ctx:
            parse_one(sql, max_nodes=50_000)
        self.assertIn("(50000)", str(ctx.exception))

        # Hitting a limit in one parse doesn't affect the next
        with self.assertRaises(ParseError):
            parse_one("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5)
        self.assertIsInstance(parse_one("SELECT 1, 2, 3"), exp.Select)
'''
    return src.count(want) == 1


def build(root: Path, out: Path) -> None:
    root = root.resolve()
    sys.path.insert(0, str(root))
    import sqlglot  # noqa: F401
    import sqlglot.errors as errors_mod
    from sqlglot import parse_one  # noqa: F401
    prefix = str(root) + "/"
    efile = str(root / FRONTIER)
    meta = {"fingerprint": fingerprint(root), "ok": _test_source_ok(root), "phases": {}}
    if not meta["ok"]:
        print("slowverify: test_max_nodes body differs; store disabled", file=sys.stderr)

    def traced(name):
        def run():
            calls = []

            def prof(frame, event, arg):
                if event != "call" or frame.f_code.co_filename != efile:
                    return
                back = frame.f_back
                if back is not None and back.f_code.co_filename == efile:
                    return
                code = frame.f_code
                n = code.co_argcount + code.co_kwonlyargcount
                names = code.co_varnames[:n]
                args = {k: frame.f_locals[k] for k in names}
                calls.append([code.co_qualname, code.co_flags, code.co_posonlyargcount, names,
                              zlib.compress(pickle.dumps(args, protocol=5), 6)])

            cov = _Cov(prefix)
            cov.start()
            sys.setprofile(prof)
            try:
                phase(name)
            finally:
                sys.setprofile(None)
                lines = cov.stop()
            return lines, calls
        return run

    for name in SLOW:
        t0 = time.process_time()
        lines, calls = _in_fork(traced(name))
        lines.pop(FRONTIER, None)  # errors.py runs only inside the frontier calls
        frontier = []
        for qualname, flags, posonly, names, blob in calls:
            entry = {"qualname": qualname, "args": blob, "ok": False}
            try:
                if flags & 0x0C or posonly:  # *args / **kwargs / positional-only
                    raise Unsupported("signature")
                fn = resolve(errors_mod, qualname)
                if names and names[0] == "self":
                    raise Unsupported("method")
                entry["drop_cls"] = bool(names) and names[0] == "cls"
                kwargs = load_kwargs(entry)
                before = digest(kwargs)
                d1 = digest(fn(**kwargs))
                d2 = digest(fn(**load_kwargs(entry)))
                if d1 != d2 or digest(kwargs) != before:
                    raise Unsupported("not deterministic / mutates its arguments")
                entry.update(ok=True, result=d1, argdigest=before)
            except Exception as e:  # noqa: BLE001
                entry["why"] = f"{type(e).__name__}: {e}"
            frontier.append(entry)
        meta["phases"][name] = {"lines": lines, "frontier": frontier,
                                "cpu": time.process_time() - t0}
        print(f"slowverify {name}: {sum(map(len, lines.values()))} lines,"
              f" frontier {[(f['qualname'], f['ok']) for f in frontier]}", file=sys.stderr)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_bytes(pickle.dumps(meta, protocol=5))
    os.replace(tmp, out)


# ── run time ──────────────────────────────────────────────────────────────────

class SlowVerify:
    def __init__(self, selector, store: Path):
        self.sel = selector
        self.store = store
        self._meta = None
        self.numbers = {t["number"]: t["id"] for t in selector.tests if t["id"] == TEST}
        self.spent = 0.0
        self.last_child = 0.0

    @classmethod
    def create(cls, selector):
        if os.environ.get("BR_SLOWVERIFY", "1") == "0":
            return None
        store = Path(os.environ.get("BR_SLOWVERIFY_STORE", DEFAULT_STORE))
        if not store.is_file():
            return None
        self = cls(selector, store)
        return self if self.numbers else None

    def meta(self):
        if self._meta is None:
            meta = pickle.loads(self.store.read_bytes())
            ok = meta.get("ok") and meta["fingerprint"] == fingerprint(Path(self.sel.sqlglot))
            self._meta = meta if ok else {}
        return self._meta

    def prune(self, jobs, spans, candidates: set[int], benefit_of) -> tuple[set[int], str]:
        cands = candidates & set(self.numbers)
        if not cands or not spans:
            return set(), ""
        try:
            meta = self.meta()
            if not meta:
                return set(), "slowverify: keep (no store)"
            for name in SLOW:
                if _hit(meta["phases"][name]["lines"], spans):
                    return set(), f"slowverify: keep ({name} runs a touched statement)"
            # Replayed always (cheap): an errors.py edit changes what the frontier calls do;
            # without one they are unchanged code, and the lines they run elsewhere are in
            # the phase lines above.
            replay = True
            if not all(f["ok"] for n in SLOW for f in meta["phases"][n]["frontier"]):
                return set(), "slowverify: keep (frontier call not replayable)"
            gain = benefit_of(cands)
            if gain <= 0 or SCORER_SLOWDOWN * 0.5 > min(BUDGET_S, 0.6 * gain / 1000):
                return set(), "slowverify: keep (not worth it)"
            ok = self.verify(jobs, replay)
        except Exception as e:  # noqa: BLE001
            return set(), f"slowverify: keep ({type(e).__name__})"
        if not ok:
            return set(), f"slowverify: keep (verification failed, {self.last_child:.2f}s)"
        return cands, (f"slowverify: test_max_nodes dropped (cheap phases"
                       f"{' + errors.py replay' if replay else ''}, {self.last_child:.2f}s)")

    def verify(self, jobs, replay: bool) -> bool:
        from .subtest import _warm
        root = str(self.sel.sqlglot)
        _warm(root)
        budget = BUDGET_S / SCORER_SLOWDOWN
        rfd, wfd = os.pipe()
        t0 = os.times()
        pid = os.fork()
        if pid == 0:
            code = 1
            try:
                os.close(rfd)
                ok = _child(root, jobs, self.meta(), replay, budget)
                with os.fdopen(wfd, "wb") as fh:
                    fh.write(b"OK" if ok else b"NO")
                code = 0
            except BaseException:  # noqa: BLE001
                if os.environ.get("SUBTEST_DEBUG"):
                    import traceback
                    traceback.print_exc()
            finally:
                os._exit(code)
        os.close(wfd)
        import select as _select
        deadline = time.monotonic() + budget * 2 + 2
        chunks = []
        with os.fdopen(rfd, "rb") as fh:
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                ready, _, _ = _select.select([fh], [], [], left)
                if not ready:
                    break
                chunk = os.read(fh.fileno(), 1 << 16)
                if not chunk:
                    break
                chunks.append(chunk)
        timed_out = time.monotonic() >= deadline
        if timed_out:
            os.kill(pid, 9)
        data = b"" if timed_out else b"".join(chunks)
        _, status = os.waitpid(pid, 0)
        t1 = os.times()
        self.last_child = round((t1.children_user - t0.children_user)
                                + (t1.children_system - t0.children_system), 2)
        self.spent += self.last_child
        return status == 0 and data == b"OK"


def _child(root, jobs, meta, replay: bool, budget: float) -> bool:
    import contextlib
    import io
    import signal
    signal.signal(signal.SIGPROF, signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_PROF, budget)
    gc.enable()
    why = _swap(root, jobs)
    if why:
        if os.environ.get("SUBTEST_DEBUG"):
            print("slowverify bail:", why, file=sys.stderr)
        return False
    import sqlglot.errors as errors_mod
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        for name in ("P1", "P2"):
            phase(name)
        if replay:
            for n in SLOW:
                for f in meta["phases"][n]["frontier"]:
                    fn = resolve(errors_mod, f["qualname"])
                    kwargs = load_kwargs(f)
                    got = digest(fn(**kwargs))
                    if got != f["result"] or digest(kwargs) != f["argdigest"]:
                        if os.environ.get("SUBTEST_DEBUG"):
                            print("slowverify diff:", n, f["qualname"], file=sys.stderr)
                        return False
        for name in ("P5", "P6"):
            phase(name)
    return True


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "build":
        build(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        sys.exit("usage: python -m selector.slowverify build SQLGLOT_ROOT OUT.pkl")
