"""Load the maps in maps/ (format: maps/SCHEMA.md) and answer lookups as test numbers.

Each map file is loaded lazily, on first use, and each key is resolved to test numbers only
when it is asked for, so a run that never needs a map never pays for it.
Test ids are translated to numbers through the run's own tests.json; ids the run does not
know are ignored.

The image build compiles the maps (`python -m selector.maps build maps index/maps`) into
marshal files, with the line-keyed maps split per source file, so a run reads only the
files a commit touches instead of decompressing and decoding every map. The compiled form
holds exactly the same keys and index lists; when it is missing or was built from another
map file (CRC of the .json.gz), the selector reads the .json.gz itself.
"""

from __future__ import annotations

import gzip
import json
import marshal
import sys
import zlib
from array import array
from pathlib import Path

MAP_FILES = ("lines", "classes", "classes_run", "reads", "test_imports", "import_time", "imports")
REQUIRED = ("lines", "classes", "reads", "test_imports", "import_time")
SHARDED = ("lines", "import_time")  # keys "<path>:<line>": one compiled file per path
ISOLATION_FAILURE = "<isolation-failure>"  # imports map: tests whose record may be incomplete
COMPILED = Path(__file__).resolve().parent.parent / "index" / "maps"
WHOLE_FILE = "*"  # in a compiled shard: every index recorded for any line of the file


class _Table:
    """The raw content of one map file: its "tests" list, its other top-level fields
    ("extra") and key → indices into "tests"."""

    def __init__(self, source: Path, compiled: Path | None) -> None:
        self.source = source
        self._compiled = compiled
        self._meta: dict | None = None
        self._shards: dict[str, dict[str, bytes]] = {}

    def _load(self) -> dict:
        if self._meta is None:
            meta = None
            if self._compiled is not None and self.source.is_file():
                try:
                    meta = marshal.loads((self._compiled / "meta.marshal").read_bytes())
                    if meta["source_crc"] != _crc(self.source):
                        meta = None
                except (OSError, EOFError, ValueError, TypeError, KeyError):
                    meta = None
            if meta is None:  # no usable compiled form: the .json.gz itself
                self._compiled = None
                data = {"tests": [], "map": {}}
                if self.source.is_file():
                    data = json.loads(gzip.decompress(self.source.read_bytes()))
                meta = {"tests": data["tests"], "map": data["map"], "extra": _extra(data)}
            self._meta = meta
        return self._meta

    @property
    def tests(self) -> list[str]:
        return self._load()["tests"]

    @property
    def extra(self) -> dict:
        return self._load()["extra"]

    def get(self, key: str):
        """Indices recorded for `key` (empty when absent)."""
        meta = self._load()
        if self._compiled is None:
            return meta["map"].get(key, ())
        if "map" in meta:
            packed = meta["map"].get(key)
        else:
            path, _, line = key.rpartition(":")
            packed = self._shard(path).get(line)
        return _unpack(packed)

    def in_file(self, path: str):
        """Indices recorded for any key `<path>:<line>`."""
        meta = self._load()
        if self._compiled is not None:
            return _unpack(self._shard(path).get(WHOLE_FILE))
        out: set[int] = set()
        for key, idx in meta["map"].items():
            if key.rpartition(":")[0] == path:
                out.update(idx)
        return out

    def _shard(self, path: str) -> dict[str, bytes]:
        shard = self._shards.get(path)
        if shard is None:
            number = self._load()["shards"].get(path)
            shard = self._shards[path] = {} if number is None else marshal.loads(
                (self._compiled / f"{number}.marshal").read_bytes())
        return shard


def _extra(data: dict) -> dict:
    return {k: v for k, v in data.items() if k not in ("tests", "map")}


def _crc(path: Path) -> int:
    return zlib.crc32(path.read_bytes())


def _pack(indices) -> bytes:
    return array("I", indices).tobytes()


def _unpack(packed: bytes | None):
    return () if packed is None else memoryview(packed).cast("I")


def compile_maps(source: Path, out: Path) -> None:
    """Build step: write the compiled form of every map in `source` under `out`."""
    for name in MAP_FILES:
        src = source / f"{name}.json.gz"
        if not src.is_file():
            continue
        data = json.loads(gzip.decompress(src.read_bytes()))
        meta = {"source_crc": _crc(src), "tests": data["tests"], "extra": _extra(data)}
        folder = out / name
        folder.mkdir(parents=True, exist_ok=True)
        if name in SHARDED:
            shards: dict[str, dict[str, list[int]]] = {}
            for key, idx in data["map"].items():
                path, _, line = key.rpartition(":")
                shards.setdefault(path, {})[line] = idx
            meta["shards"] = {}
            for number, (path, shard) in enumerate(sorted(shards.items())):
                packed = {line: _pack(idx) for line, idx in shard.items()}
                packed[WHOLE_FILE] = _pack(sorted({i for idx in shard.values() for i in idx}))
                (folder / f"{number}.marshal").write_bytes(marshal.dumps(packed))
                meta["shards"][path] = number
        else:
            meta["map"] = {key: _pack(idx) for key, idx in data["map"].items()}
        (folder / "meta.marshal").write_bytes(marshal.dumps(meta))
        print(f"map {name}: {len(data['map'])} keys -> {folder}")


class Map:
    """One map file: key → frozenset of test numbers."""

    def __init__(self, path: Path, number_of: dict[str, int], compiled: Path | None) -> None:
        self.path = path
        self._table = _Table(path, compiled)
        self._number_of = number_of
        self._numbers: list[int | None] | None = None
        self._cache: dict[str, frozenset[int]] = {}

    def _translate(self, idx) -> frozenset[int]:
        if self._numbers is None:
            self._numbers = [self._number_of.get(t) for t in self._table.tests]
        numbers = self._numbers
        return frozenset(n for n in (numbers[i] for i in idx) if n is not None)

    def get(self, key: str) -> frozenset[int]:
        """Tests recorded for `key` (empty when the key is absent)."""
        hit = self._cache.get(key)
        if hit is None:
            hit = self._cache[key] = self._translate(self._table.get(key))
        return hit

    def in_file(self, path: str) -> frozenset[int]:
        """Tests recorded for any key `<path>:<line>`."""
        return self._translate(self._table.in_file(path))

    @property
    def extra(self) -> dict:
        """Top-level fields of the map file other than "tests" and "map"."""
        return self._table.extra

    def traced_ids(self) -> list[str]:
        return self._table.tests


class AttrsMap:
    """maps/attrs.json.gz (mapbuild/attrs_map.py), compact form: "classes" {class key: {name:
    list id}}, "lists" (delta-encoded sorted indices into "tests", shared by every key with the
    same tests), plus "tracked" / "untracked" / "replaced" / "failed". Loaded on first use."""

    def __init__(self, path: Path, number_of: dict[str, int]) -> None:
        self.path = path
        self._number_of = number_of
        self._data: dict | None = None
        self._numbers: list[int | None] = []
        self._lists: list[frozenset[int]] = []

    def _load(self) -> dict:
        if self._data is None:
            data = json.loads(gzip.decompress(self.path.read_bytes()))
            self._numbers = numbers = [self._number_of.get(t) for t in data["tests"]]
            lists = []
            for deltas in data["lists"]:
                idx, at = [], 0
                for d in deltas:
                    at += d
                    idx.append(at)
                lists.append(frozenset(n for n in (numbers[i] for i in idx) if n is not None))
            self._lists = lists
            self._data = data
        return self._data

    def _translate(self, idx) -> frozenset[int]:
        if self._data is None:
            self._load()
        return frozenset(n for n in (self._numbers[i] for i in idx) if n is not None)

    def get(self, key: str) -> frozenset[int]:
        """Tests that looked NAME up through class C or a subclass (key "C NAME")."""
        cls, _, name = key.partition(" ")
        lid = self._load()["classes"].get(cls, {}).get(name)
        return self._lists[lid] if lid is not None else frozenset()

    @property
    def extra(self) -> dict:
        return self._load()

    def traced_ids(self) -> list[str]:
        return self._load()["tests"]


class Maps:
    """All maps for one run, plus derived lookups the selector needs."""

    def __init__(self, directory: Path, tests: list[dict], compiled: Path | None = None) -> None:
        """`compiled`: the folder `compile_maps` wrote, if any. A missing core map would
        read as empty (no test uses anything → nothing selected): refuse to start instead
        (__main__ then answers ALL for every commit)."""
        missing = [n for n in REQUIRED if not (directory / f"{n}.json.gz").is_file()]
        if missing:
            raise FileNotFoundError(f"maps missing in {directory}: {', '.join(missing)}")
        number_of = {t["id"]: t["number"] for t in tests}
        self._number_of = number_of

        def open_map(name: str) -> Map:
            return Map(directory / f"{name}.json.gz", number_of, compiled and compiled / name)

        self.lines = open_map("lines")
        self.classes = open_map("classes")
        # classes_run: instantiations outside sqlglot module bodies (objects made at import
        # are reached through the state entries that hold them); without it, `classes`
        self.classes_run = (open_map("classes_run")
                            if (directory / "classes_run.json.gz").is_file() else None)
        self.reads = open_map("reads")
        self.test_imports = open_map("test_imports")
        self.import_time = ImportTimeMap(directory / "import_time.json.gz",
                                         compiled and compiled / "import_time")
        self.imports = open_map("imports")
        # moon-19 attrs: class-attribute lookups per test (mapbuild/trace_attrs.py); optional
        self.attrs = (AttrsMap(directory / "attrs.json.gz", number_of)
                      if (directory / "attrs.json.gz").is_file() else None)
        self.directory = directory  # moon-21: maps/keys.json.gz is loaded by select.py
        self._unvouched: frozenset[int] | None = None
        self._file_cache: dict[str, frozenset[int]] = {}

    def span(self, path: str, first: int, last: int) -> set[int]:
        """Tests that executed any line in path:first..last (inclusive)."""
        out: set[int] = set()
        for line in range(first, last + 1):
            out |= self.lines.get(f"{path}:{line}")
        return out

    def whole_file(self, path: str) -> frozenset[int]:
        """Tests that executed any line of `path`."""
        hit = self._file_cache.get(path)
        if hit is None:
            hit = self._file_cache[path] = self.lines.in_file(path)
        return hit

    def module_users(self, module: str, class_names: list[str]) -> set[int]:
        """Tests that used any of the top-level classes `class_names` defined in `module`."""
        out: set[int] = set()
        for name in class_names:
            out |= self.classes.get(f"{module}.{name}")
        return out

    def loaders(self, path: str) -> set[int] | None:
        """Tests that load module `path` (imports map), plus every test the map cannot vouch
        for (failed when run alone, or absent from the trace). None: no imports map."""
        if not self.imports.path.is_file():
            return None
        if self._unvouched is None:
            known = set(self.imports.traced_ids())
            self._unvouched = self.imports.get(ISOLATION_FAILURE) | {
                n for tid, n in self._number_of.items() if tid not in known}
        return set(self.imports.get(path)) | self._unvouched

    def collected(self, path: str, by_import: bool = False) -> bool:
        """Is module `path` loaded while pytest collects the test files (imports map,
        `pytest --collect-only tests`)? by_import: loaded by an import statement (or a
        package), i.e. other collected modules need it, not only pytest itself.
        True when the map does not say (missing map or field): assume the worst."""
        key = "collection_imported" if by_import else "collection"
        paths = self.imports.extra.get(key)
        return paths is None or path in paths

    def untraced(self) -> set[int]:
        """Tests of this run that appear in no map: we know nothing about them."""
        seen: set[str] = set()
        for m in (self.lines, self.classes, self.reads):
            seen.update(m.traced_ids())
        return {n for tid, n in self._number_of.items() if tid not in seen}


class ImportTimeMap:
    """`import_time.json.gz`: line → modules whose import runs it (mapbuild/import_time.py).

    Root 0 is `sqlglot` itself. Missing file → empty map (nothing known to run at import).
    """

    ROOT = "sqlglot"

    def __init__(self, path: Path, compiled: Path | None = None) -> None:
        self.path = path
        self._table = _Table(path, compiled)

    def modules(self, path: str, first: int, last: int, start: int | None = None) -> set[str]:
        """Modules whose import executes any line in path:first..last, or (`start`) calls a
        code object whose first line is `start` (key "<path>:-<line>", PY_START): a lambda's
        own line is skipped (`first` = its line + 1) because defining it runs that line, and a
        one-line lambda called at import emits no LINE event of its own."""
        roots = self._table.tests
        out: set[str] = set()
        for line in range(first, last + 1):
            out.update(roots[i] for i in self._table.get(f"{path}:{line}"))
        if start is not None:
            out.update(roots[i] for i in self._table.get(f"{path}:-{start}"))
        return out


if __name__ == "__main__":
    if sys.argv[1:2] != ["build"] or len(sys.argv) != 4:
        sys.exit("usage: python -m selector.maps build <maps dir> <out dir>")
    compile_maps(Path(sys.argv[2]), Path(sys.argv[3]))
