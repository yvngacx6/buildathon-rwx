"""Run tests/test_generator.py::TestGenerator::test_generator_memory's body on the patched
package (round 20).

The test (6.7 s on the board) is selected only through the wide routes: class bodies,
definition-time / import-time code, import bounds (a dialect module the test loads). Its
body, per member of `Dialects`:

    dialect = Dialect.get_or_raise(name)
    100 x dialect.generator(); gc.collect()
    tracemalloc.start(); 1000 x dialect.generator(); gc.collect()
    assert traced growth < 1024 bytes

That is cheap outside the test worker (~15 ms per dialect on a laptop: the 6.7 s is the
worker's large heap under gc.collect), so we run it, exactly, in a fresh interpreter that
imports the patched sources (the same `_PatchedFinder` as selector/state.py):

  * every member of the patched `Dialects` is loaded through `Dialect.get_or_raise`, in the
    test's order, cold (lazy dialect imports run as in a worker that loads them first);
  * the full warm-up + measured loop runs for every dialect the patch can reach (below), with
    a stricter threshold (GROWTH_LIMIT < 1024: margin for the worker's environment);
  * any exception, a growth at or over the limit, a CPU budget overrun → keep the test.

Which dialects the patch can reach, when every patched file is a dialect-family module
(sqlglot/{dialects,generators,parsers,tokenizers,typing}/<name>.py, not their __init__ /
dialects/dialect.py): a dialect is reached when a patched module is among the modules of
every class in the MRO of the dialect class and of every class-valued attribute of it
(generator_class, tokenizer_class, …), closed over what those family modules import (any
module object, or object with `__module__`, in their globals); or when a patched file's code
runs during one traced `generator()` call. Any other patched file → every dialect.

Selector side (`GenMem.prune`): only when the selection is not ALL, the test is selected only
through sqlglot/ .py files that exist before and after the patch (no new / deleted /
renamed files), dialects/__init__.py is untouched and no hunk of dialects/dialect.py touches
the lazy-import code (`_Dialect`, `get_or_raise`): a worker that already loaded the dialect
takes another path there than our cold load. And only when dropping the test saves clearly
more than the check costs on the scorer.
"""

from __future__ import annotations

import ast
import json
import os
import sys
import time
from pathlib import Path

TEST = "tests/test_generator.py::TestGenerator::test_generator_memory"
GROWTH_LIMIT = 256  # the test asserts < 1024; the pinned source shows +32 B per dialect
WARMUP, MEASURED = 100, 1000  # exactly the test's
FAMILY = ("sqlglot.dialects.", "sqlglot.generators.", "sqlglot.parsers.",
          "sqlglot.tokenizers.", "sqlglot.typing.")
NOT_FAMILY = {"sqlglot.dialects.dialect"}
BUDGET_S = float(os.environ.get("BR_GENMEM_S", "6"))  # child CPU cap (scorer seconds)
STARTUP_S = 0.25  # laptop CPU of the child without the loops (spawn, import, dialect loads)
PER_DIALECT_S = 0.03  # laptop CPU of one dialect's loops and container scan
SCORER_SLOWDOWN = 2.0
SHARE = 0.5  # spend at most this share of what dropping saves


def module_of(path: str) -> str:
    return path[:-3].replace("/", ".").removesuffix(".__init__")


def is_family(mod: str) -> bool:
    return mod.startswith(FAMILY) and mod not in NOT_FAMILY


# ── child ─────────────────────────────────────────────────────────────────────

def _globals(m: str, memo: dict) -> tuple[set[str], set[int]]:
    """(sqlglot modules that module `m`'s globals come from, ids of its other non-scalar
    globals: constants it defines or imported)."""
    import types
    got = memo.get(m)
    if got is None:
        deps, ids = set(), set()
        mod = sys.modules.get(m)
        for k, v in (vars(mod).items() if mod is not None else ()):
            if k.startswith("__") and k.endswith("__"):
                continue
            if isinstance(v, types.ModuleType):
                deps.add(v.__name__)
                continue
            owner = getattr(v, "__module__", None)
            if isinstance(owner, str) and not isinstance(v, (str, bytes)):
                deps.add(owner)
            if (owner is None or owner == m or isinstance(v, (str, bytes))) and not (
                    v is None or isinstance(v, (bool, int, float))):
                ids.add(id(v))
        got = memo[m] = ({d for d in deps if d.startswith("sqlglot")}, ids)
    return got


def _reached(dialect_cls, patched_mods: set[str], memo: dict) -> bool:
    """A patched module among the modules the dialect's classes come from, closed over the
    dialect-family modules' imports (a constant shared with a patched module counts)."""
    classes = set()
    for klass in dialect_cls.__mro__:
        classes.add(klass)
        for v in vars(klass).values():
            if isinstance(v, type):
                classes.update(v.__mro__)
    shared: set[int] = set()
    for pm in patched_mods:
        if pm in sys.modules:
            shared |= _globals(pm, memo)[1]
    seen, todo = set(), [getattr(c, "__module__", None) or "" for c in classes]
    while todo:
        m = todo.pop()
        if m in seen or not m.startswith("sqlglot"):
            continue
        seen.add(m)
        if m in patched_mods:
            return True
        if not is_family(m):
            continue
        deps, ids = _globals(m, memo)
        if ids & shared:
            return True
        todo.extend(deps - seen)
    return False


_CONTAINERS = (list, dict, set, bytearray)


def _sizes() -> dict[int, tuple[object, int]]:
    """Every list / dict / set held by a sqlglot module global or class attribute (classes
    reached from the modules, nested classes included), one level of nesting deeper, and
    the namespaces themselves: id → (object, len)."""
    import types
    out: dict[int, tuple[object, int]] = {}
    seen: set[int] = set()

    def add(v, depth):
        if isinstance(v, _CONTAINERS) and id(v) not in out:
            out[id(v)] = (v, len(v))
            if depth < 1:
                for x in (v.values() if isinstance(v, dict) else
                          v if isinstance(v, (list, set)) else ()):
                    add(x, depth + 1)

    def klass(c):
        if id(c) in seen:
            return
        seen.add(id(c))
        ns = vars(c)
        out[id(ns) ^ 1] = (c, len(ns))  # attributes added to the class
        for v in list(ns.values()):
            if isinstance(v, type):
                if (getattr(v, "__module__", "") or "").startswith("sqlglot"):
                    klass(v)
            else:
                add(v, 0)

    for name, mod in list(sys.modules.items()):
        if not (name == "sqlglot" or name.startswith("sqlglot.")) or mod is None:
            continue
        ns = vars(mod)
        out[id(ns)] = (ns, len(ns))
        for v in list(ns.values()):
            if isinstance(v, type) and not isinstance(v, types.GenericAlias):
                if (getattr(v, "__module__", "") or "").startswith("sqlglot"):
                    klass(v)
            elif not isinstance(v, types.ModuleType):
                add(v, 0)
    return out


def _grew(sizes: dict[int, tuple[object, int]]) -> bool:
    for obj, n in sizes.values():
        now = len(vars(obj)) if isinstance(obj, type) else len(obj)
        if now > n:
            return True
    return False


def check(root: str, patched: dict[str, str], everything: bool) -> dict:
    import gc
    import importlib
    import tracemalloc
    import warnings

    sys.path.insert(0, root)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from selector.state import _PatchedFinder

    sys.meta_path.insert(0, _PatchedFinder({os.path.join(root, p): s
                                            for p, s in patched.items()}))
    warnings.simplefilter("ignore")
    import unittest  # noqa: F401  (sqlglot checks it at import, as under the test runner)
    importlib.import_module("sqlglot")
    from sqlglot.dialects.dialect import Dialect, Dialects

    files = {os.path.realpath(os.path.join(root, p)) for p in patched}
    patched_mods = {module_of(p) for p in patched}
    memo: dict = {}
    ran, worst = [], 0
    for name in Dialects:
        dialect = Dialect.get_or_raise(name)
        reach = everything or _reached(type(dialect), patched_mods, memo)
        if not reach:
            hit = []

            def prof(frame, event, arg):
                if event == "call" and os.path.realpath(frame.f_code.co_filename) in files:
                    hit.append(1)

            sys.setprofile(prof)
            try:
                dialect.generator()
            finally:
                sys.setprofile(None)
            reach = bool(hit)
        if not reach:
            continue
        for _ in range(WARMUP):
            dialect.generator()
        gc.collect()
        sizes = _sizes()
        tracemalloc.start()
        try:
            before = tracemalloc.get_traced_memory()[0]
            for _ in range(MEASURED):
                dialect.generator()
            gc.collect()
            after = tracemalloc.get_traced_memory()[0]
        finally:
            tracemalloc.stop()
        growth = after - before
        worst = max(worst, growth)
        ran.append(str(name.value) or "<base>")
        if _grew(sizes):
            return {"ok": False, "why": f"{name.value}: a module / class container grew",
                    "ran": ran}
        if growth >= GROWTH_LIMIT:
            return {"ok": False, "why": f"{name.value}: +{growth} B", "ran": ran}
    return {"ok": True, "ran": ran, "worst": worst}


def _main(argv: list[str]) -> None:
    import signal
    root, everything, budget = argv[0], argv[1] == "1", float(argv[2])
    signal.setitimer(signal.ITIMER_PROF, budget)  # SIGPROF's default action ends the process
    patched = json.loads(sys.stdin.read())
    try:
        out = check(root, patched, everything)
    except BaseException as exc:  # noqa: BLE001
        out = {"ok": False, "why": f"{type(exc).__name__}: {exc}"[:200]}
    signal.setitimer(signal.ITIMER_PROF, 0)
    print(json.dumps(out))


_RUN = (f"import sys; sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r}); "
        "from selector.genmem import _main; _main(sys.argv[1:])")


def run_child(root: str, patched: dict[str, str], everything: bool,
              budget: float) -> tuple[dict | None, float]:
    """(child's verdict or None, child CPU seconds)."""
    import subprocess
    env = {"PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1",
           "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    t0 = os.times()
    try:
        proc = subprocess.run([sys.executable, "-P", "-s", "-S", "-c", _RUN, root,
                               "1" if everything else "0", str(budget)],
                              input=json.dumps(patched), capture_output=True, text=True,
                              env=env, timeout=budget * 2 + 5, cwd="/")
    except (OSError, subprocess.SubprocessError):
        return None, 0.0
    finally:
        t1 = os.times()
    cpu = (t1.children_user - t0.children_user) + (t1.children_system - t0.children_system)
    if proc.returncode != 0:
        return None, cpu
    try:
        return json.loads(proc.stdout), cpu
    except ValueError:
        return None, cpu


# ── selector side ─────────────────────────────────────────────────────────────

def _lazy_spans(text: str) -> list[tuple[int, int]]:
    """Old-file line spans of the dialect lazy-import code in dialects/dialect.py."""
    spans = []
    for node in ast.parse(text).body:
        if isinstance(node, ast.ClassDef) and node.name == "_Dialect":
            spans.append((node.lineno - len(node.decorator_list), node.end_lineno))
        if isinstance(node, ast.ClassDef) and node.name == "Dialect":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "get_or_raise":
                    spans.append((item.lineno - len(item.decorator_list), item.end_lineno))
    return spans


def _hunk_span(h) -> tuple[int, int]:
    n = sum(1 for line in h.lines if line[:1] in (" ", "-"))
    start = h.old_at + 1 if h.old_at else h.old_start
    return start, start + max(n, 1) - 1


class GenMem:
    def __init__(self, selector, number: int):
        self.sel = selector
        self.number = number
        self.spent = 0.0

    @classmethod
    def create(cls, selector):
        if os.environ.get("BR_GENMEM", "1") == "0":
            return None
        nums = [t["number"] for t in selector.tests if t["id"] == TEST]
        return cls(selector, nums[0]) if nums else None

    def prune(self, files, per_file, benefit_of) -> tuple[bool, str]:
        """(drop the test?, note)."""
        from . import diff
        n = self.number
        if not any(n in got for _, got in per_file):
            return False, ""
        for path, got in per_file:
            if n in got and not (path.startswith("sqlglot/") and path.endswith(".py")):
                return False, "genmem: keep (selected by a non-sqlglot file)"
        patched = {}
        for fp in files:
            p = fp.path
            if not (p.startswith("sqlglot/") and p.endswith(".py")):
                continue
            if fp.old_path != fp.new_path or fp.old_path is None:
                return False, "genmem: keep (new / deleted / renamed module)"
            if p == "sqlglot/dialects/__init__.py":
                return False, "genmem: keep (lazy dialect import)"
            old = self.sel._old_text(p)
            try:
                patched[p] = diff.apply(old, fp)
            except diff.PatchError:
                return False, "genmem: keep (patch)"
            if p == "sqlglot/dialects/dialect.py":
                spans = _lazy_spans(old)
                for h in fp.hunks:
                    a, b = _hunk_span(h)
                    if any(a <= e and s <= b for s, e in spans):
                        return False, "genmem: keep (lazy dialect import)"
        if not patched:
            return False, "genmem: keep (no sqlglot source)"
        everything = not all(is_family(module_of(p)) for p in patched)
        est = STARTUP_S + PER_DIALECT_S * (40 if everything else 4)
        gain = benefit_of({n}) / 1000
        if gain <= 0 or SCORER_SLOWDOWN * est > SHARE * gain:
            return False, f"genmem: keep (not worth it: {gain:.1f}s)"
        root = str(self.sel.sqlglot)
        out, cpu = run_child(root, patched, everything, BUDGET_S / SCORER_SLOWDOWN)
        self.spent += cpu
        if not out or not out.get("ok"):
            why = (out or {}).get("why", "child failed")
            return False, f"genmem: keep ({why}, {cpu:.2f}s)"
        return True, (f"genmem: test_generator_memory dropped ({len(out['ran'])} dialects"
                      f"{' (all)' if everything else ''}, max +{out['worst']} B, {cpu:.2f}s)")
