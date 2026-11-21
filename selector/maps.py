"""Load the maps in maps/ (format: maps/SCHEMA.md) and answer lookups as test numbers.

Each map file is loaded lazily, on first use, and each key is resolved to test numbers only
when it is asked for, so a run that never needs a map never pays for it.
Test ids are translated to numbers through the run's own tests.json; ids the run does not
know are ignored.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

MAP_FILES = ("lines", "classes", "reads", "test_imports", "import_time")


class Map:
    """One `<name>.json.gz` file: key → frozenset of test numbers."""

    def __init__(self, path: Path, number_of: dict[str, int]) -> None:
        self.path = path
        self._number_of = number_of
        self._raw: dict[str, list[int]] | None = None
        self._numbers: list[int | None] = []
        self._cache: dict[str, frozenset[int]] = {}

    def _load(self) -> dict[str, list[int]]:
        if self._raw is None:
            if self.path.is_file():
                data = json.loads(gzip.decompress(self.path.read_bytes()))
            else:
                data = {"tests": [], "map": {}}
            self._numbers = [self._number_of.get(t) for t in data["tests"]]
            self._tests = data["tests"]
            self._raw = data["map"]
        return self._raw

    def get(self, key: str) -> frozenset[int]:
        """Tests recorded for `key` (empty when the key is absent)."""
        hit = self._cache.get(key)
        if hit is None:
            idx = self._load().get(key, ())
            hit = frozenset(n for n in (self._numbers[i] for i in idx) if n is not None)
            self._cache[key] = hit
        return hit

    def traced_ids(self) -> list[str]:
        self._load()
        return self._tests


class Maps:
    """All maps for one run, plus derived lookups the selector needs."""

    def __init__(self, directory: Path, tests: list[dict]) -> None:
        number_of = {t["id"]: t["number"] for t in tests}
        self._number_of = number_of
        self.lines = Map(directory / "lines.json.gz", number_of)
        self.classes = Map(directory / "classes.json.gz", number_of)
        self.reads = Map(directory / "reads.json.gz", number_of)
        self.test_imports = Map(directory / "test_imports.json.gz", number_of)
        self.import_time = ImportTimeMap(directory / "import_time.json.gz")
        self._file_index: dict[str, list[str]] | None = None
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
            if self._file_index is None:
                index: dict[str, list[str]] = {}
                for key in self.lines._load():
                    index.setdefault(key.rpartition(":")[0], []).append(key)
                self._file_index = index
            out: set[int] = set()
            for key in self._file_index.get(path, ()):
                out |= self.lines.get(key)
            hit = self._file_cache[path] = frozenset(out)
        return hit

    def module_users(self, module: str, class_names: list[str]) -> set[int]:
        """Tests that used any of the top-level classes `class_names` defined in `module`."""
        out: set[int] = set()
        for name in class_names:
            out |= self.classes.get(f"{module}.{name}")
        return out

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

    def __init__(self, path: Path) -> None:
        self.path = path
        self._roots: list[str] = []
        self._raw: dict[str, list[int]] | None = None

    def _load(self) -> dict[str, list[int]]:
        if self._raw is None:
            data = {"tests": [], "map": {}}
            if self.path.is_file():
                data = json.loads(gzip.decompress(self.path.read_bytes()))
            self._roots, self._raw = data["tests"], data["map"]
        return self._raw

    def modules(self, path: str, first: int, last: int) -> set[str]:
        """Modules whose import executes any line in path:first..last."""
        raw = self._load()
        out: set[str] = set()
        for line in range(first, last + 1):
            out.update(self._roots[i] for i in raw.get(f"{path}:{line}", ()))
        return out
