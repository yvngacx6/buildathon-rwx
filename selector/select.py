"""Turn one patch into a set of test numbers.

v0 rules (safe, map-free):
  * every changed file is a .py file whose AST is unchanged  → run nothing
  * anything else                                             → run ALL tests
Later versions add map lookups (see ARCHITECTURE.md).
"""

from __future__ import annotations

import ast
from pathlib import Path

from . import diff


class Selector:
    def __init__(self, tests: list[dict], sqlglot: Path, maps: Path) -> None:
        self.tests = tests
        self.sqlglot = sqlglot
        self.maps = maps
        self._all = {t["number"] for t in tests}

    def all_numbers(self) -> set[int]:
        return set(self._all)

    def select(self, patch_text: str) -> tuple[set[int], str]:
        """Return (test numbers, human-readable reason)."""
        files = diff.parse(patch_text)
        if not files:
            return self.all_numbers(), "ALL (no files parsed)"
        for fp in files:
            if not self._is_noop(fp):
                return self.all_numbers(), f"ALL ({fp.path} changes behaviour)"
        return set(), "NONE (AST unchanged in every file)"

    def _is_noop(self, fp: diff.FilePatch) -> bool:
        """True when a .py file's syntax tree is identical before and after the patch."""
        if fp.is_binary or not fp.old_path or not fp.new_path or fp.old_path != fp.new_path:
            return False
        if not fp.path.endswith(".py"):
            return False
        source = self.sqlglot / fp.old_path
        if not source.is_file():
            return False
        old_bytes = source.read_bytes()
        try:
            new_text = diff.apply(old_bytes.decode("utf-8"), fp)
            old_tree = ast.dump(ast.parse(old_bytes))
            new_tree = ast.dump(ast.parse(new_text.encode("utf-8")))
        except (diff.PatchError, SyntaxError, ValueError, UnicodeDecodeError):
            return False
        return old_tree == new_tree
