"""I13 side measurement: does the patch leave every code object's bytecode unchanged?

    python -m diffexec.i13_bytecode <sqlglot root> <set dir>...

For each commit, each patched .py file is compiled before and after; code objects are
compared by bytecode, names, constants and flags, ignoring line numbers. Prints the
commits where every .py change is bytecode-identical (and whether the AST filter already
caught them).
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from diffexec import serial  # noqa: E402
from diffexec.replay import _walk  # noqa: E402
from selector import classify, diff  # noqa: E402


def same_bytecode(old: str, new: str, filename: str) -> bool:
    a = _walk(compile(old, filename, "exec", dont_inherit=True))
    b = _walk(compile(new, filename, "exec", dont_inherit=True))
    return a.keys() == b.keys() and all(serial.code_digest(a[k]) == serial.code_digest(b[k]) for k in a)


def same_ast(old: str, new: str) -> bool:
    x, y = ast.parse(old), ast.parse(new)
    classify.strip_docstrings(x)
    classify.strip_docstrings(y)
    return ast.dump(x) == ast.dump(y)


def main() -> None:
    root = Path(sys.argv[1])
    for d in sys.argv[2:]:
        d = Path(d)
        answers = json.loads((d / "answers.json").read_text())["commits"]
        for patch in sorted((d / "commits").glob("*.patch")):
            files = diff.parse(patch.read_text(errors="replace"))
            pys = [f for f in files if f.path.endswith(".py") and f.old_path and f.new_path]
            if not pys or len(pys) != len([f for f in files if not f.path.startswith("tests/fixtures/")]):
                continue
            verdicts = []
            for f in pys:
                try:
                    old = (root / f.path).read_text()
                    new = diff.apply(old, f)
                    verdicts.append((same_ast(old, new), same_bytecode(old, new, str(root / f.path))))
                except Exception as e:  # noqa: BLE001
                    verdicts.append((False, False))
            nfail = len(answers[patch.stem]["failing"])
            print(f"{d.name:10s} {patch.stem:18s} fails={nfail:<5d} ast_equal={all(v[0] for v in verdicts)!s:5s} "
                  f"bytecode_equal={all(v[1] for v in verdicts)}")


if __name__ == "__main__":
    main()
