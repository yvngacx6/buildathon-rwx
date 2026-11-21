"""Prototype: finer fixture → readers rule (moonshot 14).

For a fixture FilePatch, run the PINNED tests/helpers.py loaders on old and new text and compare,
per reader, only the part of the loader output that reader consumes.  Unknown reader → keep.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import pickle
import re
import sys
import tempfile
from pathlib import Path

SQLGLOT = Path(os.environ.get('BR_SQLGLOT', str(Path.home() / 'rwx-research-robust' / 'sqlglot')))

# ── consumer table: which fields of the loader output each pinned test consumes ──────────
# loader: 'pairs' (load_sql_fixture_pairs) | 'list' (load_sql_fixtures)
# view:   'load'      only that loading succeeds (setUpClass consumers)
#         'all'       every field (meta, sql, expected) / every item
#         'notitle'   check_file: meta minus 'title' (title only names a subTest), sql, expected
#         'sql_exp'   (sql, expected)          test_optimized_tpch, test_pretty
#         'exec'      (bool(meta.execute), sql) + parse/pickle check for non-executed queries
EX = 'tests/test_executor.py::TestExecutor::'
TPC = {'tests/fixtures/optimizer/tpc-h/tpc-h.sql': 'tpch', 'tests/fixtures/optimizer/tpc-ds/tpc-ds.sql': 'tpcds'}


def build_consumers(readers_of: dict[str, list[str]]) -> dict[tuple[str, str], tuple[str, str]]:
    """(fixture path, test id) → (loader, view), derived from the pinned test sources.
    Anything not derivable is left out (→ keep)."""
    src = (SQLGLOT / 'tests/test_optimizer.py').read_text()
    tree = ast.parse(src)
    bodies = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith('test_'):
            bodies[node.name] = ast.get_source_segment(src, node)
    out = {}
    for path, tests in readers_of.items():
        if not path.startswith('tests/fixtures/'):
            continue
        rel = path[len('tests/fixtures/'):]
        for t in tests:
            meth = t.rsplit('::', 1)[1]
            if t.startswith('tests/test_optimizer.py::TestOptimizer::') and rel.startswith('optimizer/') and rel.endswith('.sql'):
                stem = rel[len('optimizer/'):-4]
                body = bodies.get(meth, '')
                if re.search(r'check_file\(\s*"%s"' % re.escape(stem), body):
                    out[(path, t)] = ('pairs', 'notitle')
                elif f'load_sql_fixture_pairs("{rel}")' in body:
                    out[(path, t)] = ('pairs', 'notitle')  # annotate_*: title is a label
                elif f'load_sql_fixtures("{rel}")' in body:
                    out[(path, t)] = ('list', 'all')
            elif t.startswith(EX) and path in TPC:
                suite = TPC[path]
                if meth == f'test_execute_{suite}':
                    out[(path, t)] = ('pairs', 'exec')
                elif meth == 'test_optimized_tpch' and suite == 'tpch':
                    out[(path, t)] = ('pairs', 'sql_exp')
                else:
                    out[(path, t)] = ('pairs', 'load')
            elif t == 'tests/test_transpile.py::TestTranspile::test_pretty' and rel == 'pretty.sql':
                out[(path, t)] = ('pairs', 'sql_exp')
            elif t in ('tests/test_transpile.py::TestTranspile::test_identity',
                       'tests/test_serde.py::TestSerde::test_serde') and rel == 'identity.sql':
                out[(path, t)] = ('list', 'all')
            elif t == 'tests/test_transpile.py::TestTranspile::test_partial' and rel == 'partial.sql':
                out[(path, t)] = ('list', 'all')
    return out


_helpers = None


def helpers():
    global _helpers
    if _helpers is None:
        spec = importlib.util.spec_from_file_location('_pinned_helpers', SQLGLOT / 'tests/helpers.py')
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _helpers = m
    return _helpers


def load(text_bytes: bytes, rel: str, loader: str):
    """Run the pinned loader on these bytes exactly as the test would (file on disk,
    utf-8 text mode).  Returns ('ok', items) or ('err', repr)."""
    h = helpers()
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text_bytes)
        old = h.FIXTURES_DIR
        h.FIXTURES_DIR = d
        try:
            fn = h.load_sql_fixture_pairs if loader == 'pairs' else h.load_sql_fixtures
            return 'ok', list(fn(rel))
        except Exception as e:  # noqa: BLE001
            return 'err', repr(e)
        finally:
            h.FIXTURES_DIR = old


def view(items, loader, v):
    if v == 'load':
        return None
    if loader == 'list':
        return items
    if v == 'all':
        return items
    if v == 'notitle':
        return [({k: x for k, x in m.items() if k != 'title'}, s, e) for m, s, e in items]
    if v == 'sql_exp':
        return [(s, e) for _, s, e in items]
    if v == 'exec':
        return [(bool(m.get('execute')), s) for m, s, _ in items]
    raise ValueError(v)


def parse_pickle_ok(sql: str) -> bool:
    """What _mp_execute does for a query that is not executed: parse_one + pickle round trip."""
    if str(SQLGLOT) not in sys.path:
        sys.path.insert(0, str(SQLGLOT))
    try:
        from sqlglot import parse_one
        e = parse_one(sql)
        pickle.loads(pickle.dumps(e))
        pickle.loads(pickle.dumps({}))
        return True
    except Exception:  # noqa: BLE001
        return False


def decide(path: str, old_bytes: bytes, new_bytes: bytes, readers: list[str], consumers, code_clean: bool):
    """→ (kept test ids, notes)."""
    rel = path[len('tests/fixtures/'):]
    kept, notes = [], []
    cache = {}
    for t in readers:
        c = consumers.get((path, t))
        if c is None:
            kept.append(t); notes.append(f'{t}: unknown consumer'); continue
        loader, v = c
        if loader not in cache:
            cache[loader] = (load(old_bytes, rel, loader), load(new_bytes, rel, loader))
        (so, io_), (sn, in_) = cache[loader]
        if so != 'ok':
            kept.append(t); notes.append('old load failed?!'); continue
        if sn != 'ok':
            kept.append(t); notes.append(f'{t}: new load raises {in_}'); continue
        vo, vn = view(io_, loader, v), view(in_, loader, v)
        if vo == vn:
            continue
        if v == 'exec' and code_clean and len(vo) == len(vn):
            diff_idx = [i for i in range(len(vo)) if vo[i] != vn[i]]
            if all(not vo[i][0] and not vn[i][0] and parse_pickle_ok(vn[i][1]) for i in diff_idx):
                notes.append(f'{t}: {len(diff_idx)} non-executed queries changed, parse ok → drop')
                continue
        kept.append(t)
    return kept, notes
