"""The run-time side of idea I08: answer "who can read this name?" from a prebuilt index.

`selector/names.py` scans the pinned sqlglot source (AST walks, import-time call analysis,
getattr scan). That takes ~2 s of CPU, and the build is not charged, so the Dockerfile runs

    python -m selector.nameindex build /opt/sqlglot /app/index/names.pkl

and a run only unpickles the tables (and, per name, the occurrences it asks about). When the
index is missing or describes other source (fingerprint of file paths and sizes), the
selector builds it in memory from the source instead: same answers, more CPU.
(A stale index is detected by re-checking the size of every file it scanned.)
"""

from __future__ import annotations

import pickle
import re
import sys
import zlib
from pathlib import Path

from . import state
from .names import HARMLESS_BASES, Fallback, Index, Site

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "index" / "names.pkl"


def read_sources(sqlglot: Path) -> dict[str, str]:
    sources = {}
    for root in ("sqlglot", "tests"):
        for p in (sqlglot / root).rglob("*.py"):
            sources[p.relative_to(sqlglot).as_posix()] = p.read_text(errors="replace")
    return sources


def file_sizes(sqlglot: Path) -> list[tuple[str, int]]:
    """Which source an index describes: every scanned file's path and size."""
    return sorted((p.relative_to(sqlglot).as_posix(), p.stat().st_size)
                  for root in ("sqlglot", "tests") for p in (sqlglot / root).rglob("*.py"))


def build(sources: dict[str, str], files: list[tuple[str, int]]) -> dict:
    """The stored form of `Index.export()`. Occurrences are pickled per name, so a run only
    unpickles the names it asks about."""
    data = Index(sources).export()
    data["occ"] = {k: zlib.compress(pickle.dumps(v, pickle.HIGHEST_PROTOCOL), 1)
                   for k, v in data["occ"].items()}
    data["files"] = files
    return data


def load(path: Path, sqlglot: Path) -> NameIndex | None:
    """The index built for this source, or None (missing / stale / unreadable).

    Checks that every file the index scanned still has its size (a stat per file; no
    directory walk), which catches a different checkout or a fake test repo."""
    try:
        data = pickle.loads(path.read_bytes())
        if not all((sqlglot / rel).stat().st_size == size for rel, size in data["files"]):
            return None
    except (OSError, pickle.UnpicklingError, EOFError, AttributeError, ValueError,
            ImportError, KeyError, TypeError):
        return None
    return NameIndex(data)


class NameIndex:
    """Answers the selector's questions from the stored tables:

      occ         identifier → zlib(pickle([(path, "attr" | "str" | "name", scope kind,
                  scope chain, Site)])): every load of `x.NAME`, string "NAME", bare `NAME`
      imports     name → [(file, local alias)] for every `from … import name [as alias]`
      stars       [(file, module)] for every `from module import *`
      dynamic     [(Site, name pattern | None, dir()-only, module pattern | None)]
      classes     class name → [(path, chain, base / metaclass names, dotted bases,
                  [(decorator, is a call)])]
      class_refs  class name → names of classes whose body binds it (`Parser = MySQLParser`)
      calls       [(path, line, bare callee | None, [(slot, handed names, strings)])]
                  for every call that runs at import
      functions   function name → [(params, kw-only params, names it loads,
                  is `return <param>`, is `return lambda x: x`)]
    """

    def __init__(self, data: dict) -> None:
        self.data = data
        self._occ: dict[str, list] = {}

    def _occurrences(self, key: str) -> list:
        hit = self._occ.get(key)
        if hit is None:
            blob = self.data["occ"].get(key)
            hit = self._occ[key] = pickle.loads(zlib.decompress(blob)) if blob else []
        return hit

    def sites(self, name: str, owner_path: str, owner_chain: tuple[str, ...],
              new_name: bool) -> list[Site]:
        """Every place that can read `name` as defined by the owner (class chain, or module)."""
        out: list[Site] = []
        module = owner_path[:-3].replace("/", ".").removesuffix(".__init__")
        aliases: dict[str, set[str]] = {}  # file → local names bound to the module's name
        if owner_chain == ():  # module names reach other files only through imports
            leaf = module.rsplit(".", 1)[-1]
            for path, star_module in self.data["stars"]:
                if star_module.endswith(leaf):
                    raise Fallback(f"{path} star-imports {module}")
            aliases.setdefault(owner_path, set()).add(name)
            for path, alias in self.data["imports"].get(name, ()):
                aliases.setdefault(path, set()).add(alias)
        keys = {name} | {a for local in aliases.values() for a in local}
        for key in keys:
            for path, kind, scope, chain, site in self._occurrences(key):
                if kind in ("attr", "str"):
                    hit = key == name
                elif owner_chain:  # a class attribute is a bare name only in its own body
                    hit = (key == name and path == owner_path and scope == "class"
                           and tuple(chain) == owner_chain)
                else:
                    hit = key in aliases.get(path, ())
                if hit:
                    out.append(site)
        for site, allowed, names_only, on_module in self.data["dynamic"]:
            if names_only and not new_name:
                continue  # dir(): sees which names exist, not their values
            if on_module is not None and (owner_chain or not re.fullmatch(on_module, module,
                                                                          re.S)):
                continue  # reads attributes of other modules; a class attribute is not one
            if allowed is None or re.fullmatch(allowed, name, re.S):
                out.append(site)
        return out

    def mentions(self, name: str) -> list[Site]:
        """Every place that names `name` (attribute, bare name, string, or an import alias of
        it), whatever it binds to."""
        out = [site for *_, site in self._occurrences(name)]
        for path, alias in self.data["imports"].get(name, ()):
            if alias != name:
                out += [site for p, *_, site in self._occurrences(alias) if p == path]
        return out

    def holders(self, name: str) -> list[tuple[str, tuple[str, ...]]]:
        """Classes whose body binds class `name` (`class MySQL(Dialect): Parser = MySQLParser`),
        as (path, chain)."""
        out = []
        for holder in self.data["class_refs"].get(name, ()):
            out += [(p, tuple(ch)) for p, ch, *_ in self.data["classes"].get(holder, ())]
        return out

    def check_class(self, path: str, chain: tuple[str, ...]) -> list[tuple[str, tuple[str, ...]]]:
        """Raise Fallback when class machinery outside our view may consume its attributes
        (an ancestor outside the repo, a decorator that is not the identity).

        Returns the class's family: itself and every subclass (by spelled base), as
        (path, chain).
        """
        classes = self.data["classes"]
        start = [e for e in classes.get(chain[-1], ()) if e[0] == path and tuple(e[1]) == chain]
        if not start:
            raise Fallback(f"class {'.'.join(chain)} not found")
        seen: set[tuple] = set()
        todo = list(start)
        while todo:  # every ancestor must be a repo class or a harmless builtin base
            p, ch, base_names, _, decorators = todo.pop()
            if (p, tuple(ch)) in seen:
                continue
            seen.add((p, tuple(ch)))
            for deco, is_call in decorators:
                if not self._identity(deco, is_call):
                    raise Fallback(f"class {ch[-1]} is decorated with {deco}")
            for base in base_names:
                found = classes.get(base, [])
                if not found and base not in HARMLESS_BASES and base != "type":
                    raise Fallback(f"class {ch[-1]} derives from {base}")
                todo.extend(found)
        family = [(path, chain)]
        todo_chains = [chain]
        while todo_chains:  # subclasses: a base spelled `...Name` (top level) / `...Outer.Inner`
            want = ".".join(todo_chains.pop())
            for entries in classes.values():
                for p, ch, _, dotted, _ in entries:
                    ch = tuple(ch)
                    if (p, ch) in family:
                        continue
                    if any(s == want or s.endswith("." + want) for s in dotted):
                        family.append((p, ch))
                        todo_chains.append(ch[-1:] if len(ch) == 1 else ch)
        return family

    def _identity(self, name: str, call: bool) -> bool:
        """Every repo function called `name` returns the decorated object untouched
        (`return f`; for a decorator factory, `return lambda f: f`)."""
        found = self.data["functions"].get(name, [])
        return bool(found) and all((f[4] if call else f[3]) for f in found)

    def handles(self, family: list[tuple[str, tuple[str, ...]]]) -> str | None:
        """A definition-time call that is handed one of these classes (or names its dialect).

        Such a call may read any attribute of the class at import; we cannot follow it.
        """
        names = {ch[-1] for _, ch in family}
        # Classes are also found by registry name: the lower-cased name of the class, or of a
        # class whose body holds it (`class MySQL(Dialect): parser_class = MySQLParser`).
        holders = set(names)
        for n in names:
            holders |= self.data["class_refs"].get(n, set())
        tags = {n.lower() for n in holders}
        for path, line, callee, slots in self.data["calls"]:
            for slot, handed, strings in slots:
                hit = sorted(handed & names) or [repr(s) for s in sorted(strings & tags)]
                if hit and not self._captured_only(callee, slot):
                    return f"{path}:{line} hands {hit[0]} to a call at import"
        return None

    def _captured_only(self, callee: str | None, slot: int | str) -> bool:
        """The callee (a repo function, found by name) only stores this argument in a closure
        (`def build(kind): def _builder(args): ... kind ...; return _builder`): nothing reads
        it at import, and the closure's own reads are ordinary run-time sites."""
        if callee is None or slot == "receiver":
            return False
        found = self.data["functions"].get(callee, [])
        if not found:
            return False
        for params, kwonly, used, _, _ in found:
            if isinstance(slot, int):
                if slot >= len(params):
                    return False
                param = params[slot]
            else:
                if slot not in params and slot not in kwonly:
                    return False
                param = slot
            if param in used:
                return False
        return True


def main(argv: list[str]) -> None:
    """usage: python -m selector.nameindex build <sqlglot checkout> <out file>"""
    if len(argv) != 3 or argv[0] != "build":
        sys.exit(main.__doc__)
    sqlglot, out = Path(argv[1]), Path(argv[2])
    data = build(read_sources(sqlglot), file_sizes(sqlglot))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(pickle.dumps(data, pickle.HIGHEST_PROTOCOL))
    print(f"name index: {len(data['occ'])} names, {out.stat().st_size // 1024} KiB -> {out}")
    # The old-source import-state snapshot (selector/state.py) lives next to it.
    snap = out.with_name("state.pkl")
    if state.build(sqlglot.resolve(), snap.resolve()):
        print(f"state snapshot: {snap.stat().st_size // 1024} KiB -> {snap}")
    else:
        print("state snapshot: FAILED (state narrowing disabled)", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1:])
