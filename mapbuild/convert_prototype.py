"""Convert the research prototype maps into the maps/SCHEMA.md format (INTERIM maps).

The definitive maps are built in the pinned image by the map builder. Until they land,
this script turns the laptop-built prototype data into the same contract so the selector
can run today:

  ~/rwx-research-robust/linemap.json   per-line coverage, test_function contexts, inline pool
  ~/rwx-research/covmap2.json.gz       a second, independent per-line coverage map (union)
  ~/rwx-research-robust/usemap.json    per test: classes instantiated (with MRO)
  ~/rwx-research-robust/readmap.json   per test: repo files opened
  <sqlglot>/tests/**/*.py              static import scan → test_imports

Every input was produced by running the *unmodified* pinned sqlglot suite. No labeled patch
is read here.

Usage: python3 mapbuild/convert_prototype.py <tests.json> <sqlglot checkout> <out dir>
"""

from __future__ import annotations

import ast
import gzip
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

HOME = Path.home()
LINEMAP = HOME / "rwx-research-robust" / "linemap.json"
COVMAP2 = HOME / "rwx-research" / "covmap2.json.gz"
USEMAP = HOME / "rwx-research-robust" / "usemap.json"
READMAP = HOME / "rwx-research-robust" / "readmap.json"


def dotted(test_id: str) -> str:
    """tests/x/test_y.py::Cls::fn → tests.x.test_y.Cls.fn (coverage's test_function context)."""
    return test_id.replace(".py::", ".").replace("::", ".").replace("/", ".")


class Builder:
    """Collects {key: set(test id)} maps and writes them in the compact schema format."""

    def __init__(self, known_ids: list[str]) -> None:
        self.known = set(known_ids)
        self.by_dotted = {dotted(t): t for t in known_ids}
        self.dropped: set[str] = set()
        self.seen: set[str] = set()  # ids that appear in at least one trace

    def resolve_ctx(self, ctx: str) -> str | None:
        if not ctx:
            return None  # import / collection time: not attributable to a test
        tid = self.by_dotted.get(ctx)
        if tid is None:
            self.dropped.add(ctx)
        return tid

    def resolve_id(self, tid: str) -> str | None:
        if tid in self.known:
            return tid
        self.dropped.add(tid)
        return None


def write_map(path: Path, mapping: dict[str, set[str]]) -> None:
    tests = sorted({t for ts in mapping.values() for t in ts})
    index = {t: i for i, t in enumerate(tests)}
    out = {"tests": tests,
           "map": {k: sorted(index[t] for t in v) for k, v in sorted(mapping.items()) if v}}
    with gzip.open(path, "wt", compresslevel=9) as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"  {path.name}: {len(out['map'])} keys, {len(tests)} tests, "
          f"{path.stat().st_size / 1e6:.2f} MB", file=sys.stderr)


def build_lines(b: Builder) -> dict[str, set[str]]:
    lines: dict[str, set[str]] = defaultdict(set)
    raw = json.loads(LINEMAP.read_text())
    for file, by_line in raw.items():
        for line, ctxs in by_line.items():
            ids = {t for t in map(b.resolve_ctx, ctxs) if t}
            if ids:
                lines[f"{file}:{line}"] |= ids
    del raw
    if COVMAP2.exists():  # union: an independent trace can only add tests
        cm = json.loads(gzip.open(COVMAP2, "rt").read())
        names = [b.resolve_id(t) for t in cm["tests"]]
        for file, by_line in cm["files"].items():
            for line, idx in by_line.items():
                ids = {names[i] for i in idx if 0 <= i < len(names) and names[i]}
                if ids:
                    lines[f"{file}:{line}"] |= ids
    return lines


def build_classes(b: Builder) -> dict[str, set[str]]:
    classes: dict[str, set[str]] = defaultdict(set)
    for tid, used in json.loads(USEMAP.read_text()).items():
        tid = b.resolve_id(tid)
        if not tid:
            continue
        for entry in used:
            module, _, qual = entry.partition(":")
            if qual:
                classes[f"{module}.{qual}"].add(tid)
    return classes


def _setup_classes(sqlglot: Path) -> set[str]:
    """`tests/x.py::Cls` for every test class that defines setUpClass (static scan)."""
    out = set()
    for path in (sqlglot / "tests").rglob("test_*.py"):
        rel = path.relative_to(sqlglot).as_posix()
        for node in ast.walk(ast.parse(path.read_bytes())):
            if isinstance(node, ast.ClassDef) and any(
                    isinstance(f, ast.FunctionDef) and f.name == "setUpClass" for f in node.body):
                out.add(f"{rel}::{node.name}")
    return out


def build_reads(b: Builder, sqlglot: Path) -> dict[str, set[str]]:
    """Files opened per test. The prototype plugin credits reads made in setUpClass to the
    first test that ran in the class only; SCHEMA.md wants the whole class. So for classes
    that define setUpClass, files read by the class's first test (pytest runs unittest
    methods in name order) are credited to every test of the class."""
    reads: dict[str, set[str]] = defaultdict(set)
    raw = json.loads(READMAP.read_text())
    for tid, files in raw.items():
        tid = b.resolve_id(tid)
        if tid:
            for f in files:
                reads[f].add(tid)
    members: dict[str, list[str]] = defaultdict(list)
    for tid in b.known:
        members[tid.rsplit("::", 1)[0]].append(tid)
    for cls in _setup_classes(sqlglot):
        tests = sorted(members.get(cls, []))
        if tests:
            for f in raw.get(tests[0], ()):
                reads[f] |= set(tests)
    return reads


def _imports_of(path: Path, root: Path) -> set[str]:
    """Repo paths of modules under tests/ that `path` imports (absolute or relative)."""
    try:
        tree = ast.parse(path.read_bytes())
    except SyntaxError:
        return set()
    pkg = path.relative_to(root).parent.parts
    names: set[tuple[str, ...]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {tuple(a.name.split(".")) for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = tuple(node.module.split(".")) if node.module else ()
            if node.level:
                base = pkg[: len(pkg) - node.level + 1] + base
            names.add(base)
            names |= {base + (a.name,) for a in node.names}
    found = set()
    for parts in names:
        if not parts or parts[0] != "tests":
            continue
        for i in range(1, len(parts) + 1):  # every package on the way is imported too
            for cand in (Path(*parts[:i]).with_suffix(".py"), Path(*parts[:i]) / "__init__.py"):
                if (root / cand).is_file():
                    found.add(cand.as_posix())
    return found


def build_test_imports(b: Builder, sqlglot: Path) -> dict[str, set[str]]:
    files = sorted(p for p in (sqlglot / "tests").rglob("*.py"))
    direct = {p.relative_to(sqlglot).as_posix(): _imports_of(p, sqlglot) for p in files}
    tests_in: dict[str, set[str]] = defaultdict(set)
    for tid in b.known:
        tests_in[tid.split("::", 1)[0]].add(tid)
    out: dict[str, set[str]] = defaultdict(set)
    for test_file in direct:
        seen, todo = set(), list(direct[test_file])
        while todo:  # transitive closure
            mod = todo.pop()
            if mod in seen:
                continue
            seen.add(mod)
            todo.extend(direct.get(mod, ()))
        for mod in seen:
            out[mod] |= tests_in.get(test_file, set())
    return out


def main() -> None:
    tests_json, sqlglot, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    known = [t["id"] for t in json.loads(tests_json.read_text())["tests"]]
    b = Builder(known)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    write_map(out / "lines.json.gz", build_lines(b))
    write_map(out / "classes.json.gz", build_classes(b))
    write_map(out / "reads.json.gz", build_reads(b, sqlglot))
    write_map(out / "test_imports.json.gz", build_test_imports(b, sqlglot))
    print(f"  dropped {len(b.dropped)} contexts/ids not in tests.json, e.g. {sorted(b.dropped)[:3]}",
          file=sys.stderr)
    manifest = {
        "status": "INTERIM prototype maps, laptop-built (not the pinned image). Replace with "
                  "the definitive maps from branch `maps` when they land.",
        "sqlglot_sha": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa",
        "sources": [str(p).replace(str(HOME), "~") for p in (LINEMAP, COVMAP2, USEMAP, READMAP)],
        "build_command": "python3 mapbuild/convert_prototype.py sample/tests.json <sqlglot> maps",
        "notes": "lines = union of two coverage runs (test_function contexts, inline process "
                 "pool with pickle round-trip); import-time ('') contexts dropped. classes "
                 "track Dialect/Parser/Generator/Tokenizer/Expression hierarchies only. reads "
                 "made by the first test of a setUpClass class are credited to the whole class. "
                 "test_imports from a static AST scan.",
        "date": time.strftime("%Y-%m-%d"),
    }
    (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"  done in {time.time() - t0:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
