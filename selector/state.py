"""What a patch changes in the state sqlglot builds at import (round 2, idea C / I22).

Class bodies are often consumed when the class is created: metaclass `__new__`
(`_Dialect`), `__init_subclass__` (`_TokenizerBase`, `Expr`), import-time calls handed the
class (`type("Tokenizer", (base,), {})`), call-valued table entries (`_show_parser(...)`).
The static name scan (names.py) cannot follow those consumers, so it fell back to the wide
rule. Here we follow them by running them:

    import every sqlglot module (old source) → canonical snapshot of all state
    import every sqlglot module (patched)    → canonical snapshot
    diff                                     → the (owner, attribute) values that differ

The snapshot covers every module global and every class `__dict__` entry, reached from the
modules (classes reached only through attributes, like the dialects' generated Tokenizer
classes, are named by the path that reaches them). Values are reduced to digests of a
canonical form: containers in order (sets sorted), functions by qualified name, defaults,
closure cells and attributes (plus code for lambdas and closures; a named def's code is a
function region, handled by the line map), objects by class and attributes. A class value
is a reference; its own entries are compared separately.

The old snapshot is built at image build (`python -m selector.nameindex build` writes it next
to the name index); the patched one runs in a subprocess at run time (~0.8 s container CPU on
the loaded EC2 box, ~0.35 s on a laptop, with the build-time bytecode cache: about 1/3 import,
1/2 digest). Any import error, timeout or surprise → None (caller falls back).

Runs in a fresh interpreter, so the selector process never imports sqlglot (`_run`):
    PYTHONHASHSEED=0 python -P -s -S selector/state.py snap <sqlglot root> <out.pkl>
    PYTHONHASHSEED=0 python -P -s -S selector/state.py diff <sqlglot root> <old.pkl>
        (patched sources as JSON on stdin)
(-P keeps selector/ off sys.path: its select.py would shadow the stdlib module. Not -I: it
ignores PYTHONHASHSEED, and digests and set orders need the fixed seed.) `_run` imports this
module instead of running the file, so the build's bytecode cache spares compiling it.
"""

from __future__ import annotations

import enum
import functools
import importlib
import importlib.abc
import importlib.machinery
import json
import os
import pickle
import re
import sys
import types
from pathlib import Path

SKIP_MODULES = {"sqlglot.__main__"}  # runs a CLI on import
SKIP_CLASS_ATTRS = {"__dict__", "__weakref__", "__doc__", "__module__", "__qualname__",
                    "__firstlineno__", "__static_attributes__", "__annotations__",
                    "__annotate__", "__annotate_func__", "__annotations_cache__",
                    "__parameters__", "__orig_bases__"}
SKIP_GLOBALS = {"__builtins__", "__loader__", "__spec__", "__file__", "__cached__", "__doc__",
                "__name__", "__package__", "__path__", "__annotations__", "__annotate__",
                "__conditional_annotations__"}
_SCALARS = {type(None), bool, int, float, complex}
_PLAIN = _SCALARS | {str, bytes, dict, list, tuple, set, frozenset}
_ADDRESS = re.compile(r" at 0x[0-9a-fA-F]+")
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "index" / "state.pkl"


def _h(*parts: object) -> str:
    """A 64-bit digest of digests / plain values. Python's own hash: the subprocess always
    runs with PYTHONHASHSEED=0, so it is the same in every run (and ~2x faster than a
    cryptographic hash of the repr)."""
    return format(hash(tuple([tuple(p) if isinstance(p, list) else p for p in parts]))
                  & 0xFFFFFFFFFFFFFFFF, "x")


class Snapshot:
    """Digest of every module global and class attribute of the imported package."""

    def __init__(self, package: str, ordered: bool = True) -> None:
        self.package = package
        self.ordered = ordered  # False: dicts digested as unordered (see `build`)
        self.memo: dict[int, str] = {}
        self.path: dict[int, int] = {}  # objects being digested → depth on the current path
        self.low = 1 << 30
        self.keep: list[object] = []  # keep digested objects alive (stable ids)
        self.class_key: dict[int, str] = {}
        self.classes: list[tuple[str, type]] = []
        self.instances: dict[str, int] = {}  # class key → objects of it found in the state
        self.entries: dict[str, str] = {}
        # track: which entries hold objects of which classes (build-time snapshot only)
        self.track = False
        self.inside: list[set[str]] = []  # per object being digested: instance keys within
        self.inst_in: dict[int, frozenset[str]] = {}  # memoized object → instance keys within
        self.held: dict[str, set[str]] = {}  # entry → keys of classes it holds objects of

    def _register(self, cls: type, path: str) -> str:
        key = self.class_key.get(id(cls))
        if key is None:
            real = self._real_key(cls)
            key = real or f"{path}#"
            self.class_key[id(cls)] = key
            self.keep.append(cls)
            self.classes.append((key, cls))
        return key

    @staticmethod
    def _real_key(cls: type) -> str | None:
        """`module:Qual.Name` when that name really leads to the class."""
        mod = sys.modules.get(getattr(cls, "__module__", None) or "")
        qual = getattr(cls, "__qualname__", "")
        if mod is None or "<" in qual:
            return None
        obj = mod
        for part in qual.split("."):
            obj = getattr(obj, part, None)
            if obj is None:
                return None
        return f"{cls.__module__}:{qual}" if obj is cls else None

    def _ours(self, cls: type) -> bool:
        return (getattr(cls, "__module__", "") or "").split(".")[0] == self.package

    def fp(self, v: object, depth: int = 0) -> str:
        # Fast paths first, each giving what the checks below would: a plain str or scalar
        # of exactly that type, or an object already digested (memoized objects are kept
        # alive, so a live `v` with a memoized id is that object; plain values never are).
        t = type(v)
        if t is str:
            return f"str:{v!r}" if len(v) < 64 else _h("str", v)
        hit = self.memo.get(id(v))
        if hit is not None:
            if self.track and self.inside:
                self.inside[-1] |= self.inst_in.get(id(v), frozenset())
            return hit
        if t in _SCALARS:
            return f"{t.__name__}:{v!r}"
        if v is None or isinstance(v, (bool, int, float, complex)):
            return f"{t.__name__}:{v!r}"
        if isinstance(v, (str, bytes)):
            return f"{t.__name__}:{v!r}" if len(v) < 64 else _h(t.__name__, v)
        pos = self.path.get(id(v))
        if pos is not None:  # a cycle: cut it here; digests that depend on the cut are not
            self.low = min(self.low, pos)  # memoized (they depend on the entry point)
            return f"<cycle:{len(self.path) - pos}>"
        pos = self.path[id(v)] = len(self.path)
        saved, self.low = self.low, pos
        if self.track:
            self.inside.append(set())
            try:
                out = self._fp(v, depth)
            finally:
                within = self.inside.pop()
            if self.inside:
                self.inside[-1] |= within
        else:
            out = self._fp(v, depth)
        del self.path[id(v)]
        if self.low >= pos:
            self.memo[id(v)] = out
            self.keep.append(v)
            if self.track and within:
                self.inst_in[id(v)] = frozenset(within)
        self.low = min(saved, self.low)
        return out

    def _fp(self, v: object, depth: int) -> str:
        f = self.fp
        if type(v) is dict:  # the most common container: the dict branch below, sooner
            items = [(f(k), f(x)) for k, x in v.items()]
            return _h("dict", "dict", items if self.ordered else sorted(items))
        if isinstance(v, type):
            if self._ours(v):
                return "class:" + self._register(v, f"<anon>.{v.__module__}.{v.__qualname__}")
            return f"extclass:{v.__module__}.{v.__qualname__}"
        if isinstance(v, enum.Enum):
            return _h("enum", f(type(v)), v.name)
        if isinstance(v, dict):
            items = [(f(k), f(x)) for k, x in v.items()]
            return _h("dict", type(v).__name__, items if self.ordered else sorted(items))
        if isinstance(v, (list, tuple)):
            items = [f(x) for x in v]
            return _h(type(v).__name__, items if self.ordered else sorted(items))
        if isinstance(v, (set, frozenset)):
            return _h(type(v).__name__, sorted(f(x) for x in v))
        if isinstance(v, types.FunctionType):
            code = ""
            if v.__closure__ or "<" in v.__qualname__:  # lambda / closure: code is data here
                code = self._code(v.__code__)
            cells = []
            for c in v.__closure__ or ():
                try:
                    cells.append(f(c.cell_contents))
                except ValueError:
                    cells.append("<empty>")
            return _h("fn", v.__module__, v.__qualname__, code, f(v.__defaults__),
                      f(v.__kwdefaults__), cells, f(dict(v.__dict__)))
        if isinstance(v, (classmethod, staticmethod)):
            return _h(type(v).__name__, f(v.__func__))
        if isinstance(v, property):
            return _h("property", f(v.fget), f(v.fset), f(v.fdel))
        if isinstance(v, functools.cached_property):
            return _h("cached_property", f(v.func))
        if isinstance(v, functools.partial):
            return _h("partial", f(v.func), f(v.args), f(v.keywords))
        if isinstance(v, types.MethodType):
            return _h("method", f(v.__self__), v.__func__.__name__, f(v.__func__))
        if isinstance(v, types.ModuleType):
            return f"module:{v.__name__}"
        if isinstance(v, (types.BuiltinFunctionType, types.BuiltinMethodType,
                          types.WrapperDescriptorType, types.MethodDescriptorType,
                          types.GetSetDescriptorType, types.MemberDescriptorType)):
            owner = getattr(v, "__self__", None)
            own = f(owner) if isinstance(owner, type) else type(owner).__name__
            return _h("builtin", getattr(v, "__qualname__", repr(v)), own)
        if isinstance(v, re.Pattern):
            return _h("re", v.pattern, v.flags)
        if isinstance(v, types.CodeType):
            return self._code(v)
        cls = type(v)
        if self._ours(cls):
            key = self._register(cls, f"<anon>.{cls.__module__}.{cls.__qualname__}")
            self.instances[key] = self.instances.get(key, 0) + 1
            if self.track:
                self.inside[-1].add(key)
        state = []
        d = getattr(v, "__dict__", None)
        if isinstance(d, dict):
            state.append(f(dict(d)))
        for k in cls.__mro__:
            for s in k.__dict__.get("__slots__", ()) if isinstance(
                    k.__dict__.get("__slots__", ()), (tuple, list)) else ():
                if s in ("__dict__", "__weakref__"):
                    continue
                state.append((s, f(getattr(v, s, "<unset>"))))
        if not state and not self._ours(cls):
            return _h("repr", f(cls), _ADDRESS.sub("", repr(v)))
        return _h("obj", f(cls), state)

    def _code(self, c: types.CodeType) -> str:
        consts = [self._code(k) if isinstance(k, types.CodeType) else self.fp(k)
                  for k in c.co_consts]
        return _h("code", c.co_code, consts, c.co_names, c.co_varnames, c.co_freevars,
                  c.co_argcount, c.co_kwonlyargcount, c.co_flags)

    def proj(self, v: object) -> tuple[bool | None, str | None]:
        """What a read that only tests truth (`if X:`) or membership (`k in X`) can see of
        `v`: (bool(v), digest of the set of keys / elements); None where not computed
        (only exact builtin containers and scalars, whose `bool` / `in` run no repo code)."""
        t = type(v)
        truth = bool(v) if t in _PLAIN else None
        keys = None
        if t in (dict, set, frozenset, list, tuple):
            keys = _h(sorted({self.fp(k) for k in v}))
        return truth, keys

    def value(self, key: str) -> object:
        """The value behind an entry key of `take`."""
        kind, owner, name = key.split(" ")
        if kind == "M":
            return vars(sys.modules[owner])[name]
        cls = dict(self.classes)[owner]
        return cls.__dict__[name]

    def take(self) -> None:
        mods = sorted(n for n in sys.modules
                      if (n == self.package or n.startswith(self.package + ".")))
        for name in mods:
            mod = sys.modules[name]
            for g, v in list(vars(mod).items()):
                if g in SKIP_GLOBALS:
                    continue
                if isinstance(v, type) and self._ours(v) and v.__module__ == name:
                    self._register(v, f"{name}:{g}")
                self.entries[f"M {name} {g}"] = self._entry(f"M {name} {g}", v)
        i = 0
        while i < len(self.classes):  # classes found while digesting join the queue
            key, cls = self.classes[i]
            i += 1
            for a, v in list(cls.__dict__.items()):
                if a in SKIP_CLASS_ATTRS:
                    continue
                if isinstance(v, type) and self._ours(v):
                    self._register(v, f"{key}.{a}")
                self.entries[f"C {key} {a}"] = self._entry(f"C {key} {a}", v)
            self.entries[f"C {key} __mro__"] = _h([self.fp(b) for b in cls.__mro__])

    def _entry(self, entry: str, v: object) -> str:
        if not self.track:
            return self.fp(v)
        self.inside = [set()]
        out = self.fp(v)
        if self.inside[0]:
            self.held[entry] = self.inside[0]
        self.inside = []
        return out

    def holders(self) -> dict[str, list[str]]:
        """Class key → the entries whose value holds an object of that class or of a
        subclass (objects built at import that tests reach through those entries)."""
        mro = {key: [key] + [self.class_key.get(id(b)) for b in cls.__mro__[1:]]
               for key, cls in self.classes}
        out: dict[str, set[str]] = {}
        for entry, keys in self.held.items():
            for key in keys:
                for k in mro.get(key, [key]):
                    if k:
                        out.setdefault(k, set()).add(entry)
        return {k: sorted(v) for k, v in out.items()}

    def instance_mro(self) -> list[str]:
        """Keys of every class that some object in the state (built at import) is an
        instance of: tests can reach those objects without creating one themselves."""
        found = {k for key, cls in self.classes if key in self.instances
                 for k in [key] + [self.class_key.get(id(b)) for b in cls.__mro__[1:]] if k}
        return sorted(found)

    def info(self, keys: set[str] | None = None) -> dict[str, dict]:
        """Per class key (all, or those in `keys`): source file, qualname, whether the name
        leads to it, and the keys of its ancestors."""
        out = {}
        for key, cls in self.classes:
            if keys is not None and key not in keys:
                continue
            mod = sys.modules.get(cls.__module__)
            out[key] = {
                "module": cls.__module__,
                "qualname": cls.__qualname__,
                "file": getattr(mod, "__file__", None),
                "real": not key.endswith("#"),
                "mro": [self.class_key.get(id(b)) for b in cls.__mro__[1:]],
            }
        return out


class _PatchedFinder(importlib.abc.MetaPathFinder):
    """Serve the patched source for the files a commit changes."""

    def __init__(self, patched: dict[str, str]) -> None:
        self.patched = {os.path.realpath(p): s for p, s in patched.items()}

    def find_spec(self, fullname, path, target=None):
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or not spec.origin:
            return spec
        source = self.patched.get(os.path.realpath(spec.origin))
        if source is not None:
            spec.loader = _PatchedLoader(fullname, spec.origin, source)
        return spec


class _PatchedLoader(importlib.machinery.SourceFileLoader):
    def __init__(self, fullname: str, path: str, source: str) -> None:
        super().__init__(fullname, path)
        self._source = source

    def get_code(self, fullname):
        return compile(self._source, self.path, "exec", dont_inherit=True)


def take(root: str, patched: dict[str, str], track: bool = False) -> Snapshot:
    import pkgutil
    import warnings

    sys.path.insert(0, root)
    sys.setrecursionlimit(20000)
    if patched:
        sys.meta_path.insert(0, _PatchedFinder({os.path.join(root, p): s
                                                for p, s in patched.items()}))
    warnings.simplefilter("ignore")
    # Import as under the test runner: sqlglot checks `"unittest" in sys.modules` at import
    # (expressions.core.UNITTEST: stricter validation, e.g. of templates parsed at import).
    # Without it, a patch that breaks an import-time parse only under pytest looks harmless.
    import unittest  # noqa: F401
    package = "sqlglot"
    pkg = importlib.import_module(package)
    names = sorted(m.name for m in pkgutil.walk_packages(pkg.__path__, package + ".")
                   if m.name not in SKIP_MODULES)
    for name in names:
        importlib.import_module(name)
    snap = Snapshot(package)
    snap.track = track
    snap.take()
    return snap


def unordered(snap: Snapshot, keys) -> dict[str, str | None]:
    """Order-insensitive digests of some entries (dicts, lists and tuples sorted)."""
    other = Snapshot(snap.package, ordered=False)
    other.class_key, other.classes = snap.class_key, snap.classes
    out: dict[str, str | None] = {}
    for k in keys:
        try:
            out[k] = other.fp(snap.value(k))
        except (KeyError, ValueError):
            out[k] = None
    return out


def projections(snap: Snapshot, keys) -> dict[str, tuple[bool | None, str | None]]:
    """`Snapshot.proj` of some entries (missing / pseudo entries left out)."""
    out = {}
    for k in keys:
        try:
            value = snap.value(k)
        except (KeyError, ValueError):
            continue
        out[k] = snap.proj(value)
    return out


def _main(argv: list[str]) -> None:
    cmd, root = argv[0], argv[1]
    if cmd == "snap":  # snap <root> <out>
        snap = take(root, {}, track=True)
        Path(argv[2]).write_bytes(pickle.dumps(
            {"entries": snap.entries, "info": snap.info(), "instances": snap.instance_mro(),
             "holders": snap.holders(), "proj": projections(snap, snap.entries),
             "unordered": unordered(snap, snap.entries)}, pickle.HIGHEST_PROTOCOL))
        return
    if cmd == "diff":  # diff <root> <old>; stdin: {"path": patched source}
        old = pickle.loads(Path(argv[2]).read_bytes())
        if old.get("files") != source_files(Path(root)):
            sys.exit("the snapshot describes other source")
        patched = json.loads(sys.stdin.read())
        snap = take(root, patched)
        print(json.dumps(compare(snap, old, patched)))
        return
    sys.exit("usage: state.py snap <root> <out> | diff <root> <old>")


def compare(snap: Snapshot, old: dict, patched: dict[str, str]) -> dict:
    """The entries whose value differs between the build-time snapshot `old` and `snap`.

    An entry whose order varies between fresh imports ("loose", see `build`) is compared
    without order. Any other entry whose only difference is order counts only when it
    belongs to a patched module: an edit there can reorder a literal, while elsewhere an
    order-only difference is the address-hash noise the build samples happened to miss."""
    new, before, loose = snap.entries, old["entries"], set(old["loose"])
    modules = {p[:-3].replace("/", ".").removesuffix(".__init__") for p in patched}
    candidates = [k for k in new.keys() | before.keys() if k not in old["volatile"]
                  and new.get(k) != before.get(k)]
    now = unordered(snap, [k for k in candidates if k in new])
    changed = []
    for k in candidates:
        if k not in new or k not in before or now[k] != old["unordered"].get(k):
            changed.append(k)
        elif k not in loose and _module_of_key(k) in modules:
            changed.append(k)
    keys = {k.split(" ")[1] for k in changed if k.startswith("C ")}
    info = snap.info(keys)
    info = {k: info.get(k) or old["info"].get(k) for k in keys}
    held = old.get("holders")
    wanted = keys | {m for k in keys for m in (info.get(k) or {}).get("mro", []) if m}
    # per changed entry, what reads that only test its truth / membership still see the same
    before_proj = old.get("proj") or {}
    now_proj = projections(snap, [k for k in changed if k in new and k in before_proj])
    same = {}
    for k, (truth, keys) in now_proj.items():
        was_truth, was_keys = before_proj[k]
        kept = (["truth"] if truth is not None and truth == was_truth else []) + (
            ["keys"] if keys is not None and keys == was_keys else [])
        if kept:
            same[k] = kept
    return {"changed": sorted(changed),
            "same": same,
            "info": info,
            "instances": sorted(set(snap.instance_mro()) | set(old["instances"])),
            "holders": None if held is None else {k: held[k] for k in wanted if k in held}}


def _module_of_key(key: str) -> str:
    owner = key.split(" ")[1]
    return owner.removeprefix("<anon>.").split(":")[0]


# ── selector side ─────────────────────────────────────────────────────────────────────────


BUILD_SAMPLES = 16


def build(root: Path, out: Path, samples: int = BUILD_SAMPLES) -> bool:
    """Old-source snapshot, at image build.

    Some import-time state differs from one fresh import of the same source to the next:
    sets of classes hash by address, so the order of a dict or tuple built from one varies,
    and a loop variable left in a class body (`for target_type in {...}`) is any member.
    `samples` imports find those entries. For them ("loose") only an order-insensitive digest
    (dicts, lists and tuples sorted) is compared; an entry whose value varies even so is
    "volatile" and never compared: no test can rely on its value (it would fail at random).
    Rare variation the samples miss is handled in `compare`."""
    runs = []
    for i in range(samples):
        data = _snap(root, out.with_suffix(f".{i}.tmp"))
        if data is None:
            return False
        runs.append(data)
    base = runs[0]

    def varies(field: str) -> set[str]:
        return {k for r in runs[1:] for k in r[field].keys() | base[field].keys()
                if r[field].get(k) != base[field].get(k)}

    volatile = varies("unordered")
    base["loose"] = sorted(varies("entries") - volatile)
    base["volatile"] = sorted(volatile)
    base["files"] = source_files(root)
    holders: dict[str, set[str]] = {}
    for r in runs:  # every sample's holders: an entry found holding an object in any import
        for k, v in r.get("holders", {}).items():
            holders.setdefault(k, set()).update(v)
    base["holders"] = {k: sorted(v) for k, v in holders.items()}
    out.write_bytes(pickle.dumps(base, pickle.HIGHEST_PROTOCOL))
    return True


def source_files(root: Path) -> list[tuple[str, int]]:
    """Which source a snapshot describes: path and size of every package file."""
    return sorted((p.relative_to(root).as_posix(), p.stat().st_size)
                  for p in (root / "sqlglot").rglob("*.py"))


def _snap(root: Path, part: Path) -> dict | None:
    if _run(["snap", str(root), str(part)], None) is None:
        return None
    data = pickle.loads(part.read_bytes())
    part.unlink()
    return data


def diff(root: Path, old: Path, patched: dict[str, str]) -> dict | None:
    """Changed state entries for the patched sources, or None if that cannot be computed."""
    if not old.is_file():
        return None
    out = _run(["diff", str(root), str(old)], json.dumps(patched))
    if out is None:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


_RUN = (f"import sys; sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r}); "
        "from selector.state import _main; _main(sys.argv[1:])")


def _run(args: list[str], stdin: str | None) -> str | None:
    import subprocess  # here: the interpreter that `_run` starts does not need it

    env = {"PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1", "PATH": os.environ.get(
        "PATH", "/usr/bin:/bin")}
    if os.environ.get("BR_STATE_PYCACHE"):
        env["PYTHONPYCACHEPREFIX"] = os.environ["BR_STATE_PYCACHE"]
    try:
        proc = subprocess.run([sys.executable, "-P", "-s", "-S", "-c", _RUN, *args],
                              input=stdin, capture_output=True, text=True, env=env, timeout=60,
                              cwd="/")
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


if __name__ == "__main__":
    _main(sys.argv[1:])
