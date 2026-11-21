"""Credit a dialect's users to the classes it reaches through parser_class / generator_class /
tokenizer_class / jsonpath_tokenizer_class (and their bases), in maps/classes.json.gz and
maps/classes_run.json.gz.

Usage: python mapbuild/dialect_reach.py <pinned sqlglot checkout> <maps dir>

Why: the traces credit a Parser / Generator / Tokenizer class only to tests that made an
instance of it. A test that uses a dialect but reads only class attributes of its components
(`anonymize()` checks `Dialect.get_or_raise(d).parser_class.FUNCTION_PARSERS`) never makes
one, so a change to `TrinoParser.FUNCTION_PARSERS` did not reach a test that used Athena
(Athena.Parser = AthenaParser(TrinoParser)). Here every test recorded for a dialect class D
(the map is MRO-inclusive: D's key also holds the users of D's subclasses, by-name lookups
included) is also recorded for every sqlglot class in the MRO of D's four component classes,
as the metaclass resolves them (inherited from a parent dialect when D does not set its own).

Only adds tests (over-approximates: a parent dialect's key also holds its subclasses' users,
who then reach the parent's component classes too). Idempotent. Built only from the pinned
source: no traces, labels or patches. classes_run gets classes_run users, so it stays within
classes.
"""

import gzip
import importlib
import json
import os
import pkgutil
import sys

sys.path.insert(0, os.path.dirname(__file__))
from export import write_map  # noqa: E402

COMPONENTS = ("tokenizer_class", "jsonpath_tokenizer_class", "parser_class", "generator_class")


def _key(cls):
    return f"{cls.__module__}.{cls.__qualname__}"


def reach(sqlglot_dir):
    """dialect key -> sorted keys of the sqlglot classes its components reach (MRO)."""
    sys.path.insert(0, os.path.abspath(sqlglot_dir))
    import sqlglot.dialects as pkg
    from sqlglot.dialects.dialect import Dialect

    for info in pkgutil.iter_modules(pkg.__path__):
        importlib.import_module(f"sqlglot.dialects.{info.name}")
    dialects, todo = [], [Dialect]
    while todo:
        d = todo.pop()
        dialects.append(d)
        todo.extend(d.__subclasses__())
    out = {}
    for d in dialects:
        if not d.__module__.startswith("sqlglot."):
            continue
        got = set()
        for attr in COMPONENTS:
            comp = getattr(d, attr, None)
            if isinstance(comp, type):
                got |= {_key(c) for c in comp.__mro__ if c.__module__.startswith("sqlglot.")}
        out[_key(d)] = sorted(got)
    return out


def augment(path, reached):
    data = json.loads(gzip.decompress(open(path, "rb").read()))
    m = {k: set(v) for k, v in data["map"].items()}
    added = 0
    for d, comps in reached.items():
        users = m.get(d)
        if not users:
            continue
        for c in comps:
            row = m.setdefault(c, set())
            before = len(row)
            row |= users
            added += len(row) - before
    write_map(path, data["tests"], m, None)
    return added


def main(sqlglot_dir, maps_dir):
    reached = reach(sqlglot_dir)
    for name in ("classes.json.gz", "classes_run.json.gz"):
        added = augment(os.path.join(maps_dir, name), reached)
        print(f"{name}: {len(reached)} dialects, +{added} (key, test) pairs", file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:3])
