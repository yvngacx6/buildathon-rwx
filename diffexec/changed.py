"""Measurement helper: the function code objects that each patch of a labeled set changes.

    python -m diffexec.changed <sqlglot root> <set dir>... > only.txt

Prints "path:qualname" lines (input for DIFFEXEC_ONLY), so that the recorder can be run on
just these functions. With the same caps, the recording of a function does not depend on
which other functions are recorded, so this simulates the full recorder exactly for them.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from diffexec import serial  # noqa: E402
from diffexec.replay import _walk  # noqa: E402
from selector import diff  # noqa: E402


def changed(root: Path, patch_text: str) -> set[str]:
    out = set()
    for fp in diff.parse(patch_text):
        if not (fp.path.startswith("sqlglot/") and fp.path.endswith(".py") and fp.old_path and fp.new_path):
            continue
        try:
            old = (root / fp.path).read_text()
            new = diff.apply(old, fp)
            a = _walk(compile(old, fp.path, "exec", dont_inherit=True))
            b = _walk(compile(new, fp.path, "exec", dont_inherit=True))
        except Exception:  # noqa: BLE001
            continue
        for k in a.keys() & b.keys():
            if serial.code_digest(a[k], shallow=True) != serial.code_digest(b[k], shallow=True):
                out.add(f"{fp.path}:{k[0]}")
    return out


def main() -> None:
    root = Path(sys.argv[1])
    names = set()
    for d in sys.argv[2:]:
        for p in sorted(Path(d, "commits").glob("*.patch")):
            names |= changed(root, p.read_text(errors="replace"))
    print("\n".join(sorted(names)))


if __name__ == "__main__":
    main()
