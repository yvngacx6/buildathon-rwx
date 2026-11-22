"""Print the key-level part of the import-state diff for patches (run in the pinned image).

usage: PYTHONHASHSEED=0 python keydiff.py <patch>...   (cwd: repo root with index/ built)
"""
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.getcwd())
from selector import diff, state  # noqa: E402

SG = Path(os.environ["BR_SQLGLOT"])
for p in sys.argv[1:]:
    patched = {}
    for fp in diff.parse(Path(p).read_text()):
        if fp.path.startswith("sqlglot/") and fp.path.endswith(".py") and fp.old_path:
            patched[fp.path] = diff.apply((SG / fp.path).read_text(), fp)
    found = state.diff(SG, Path("index/state.pkl").resolve(), patched)
    if not found or "changed" not in found:
        print("==", Path(p).stem, "no diff", found)
        continue
    keys = found.get("keys", {})
    unkeyed = [k for k in found["changed"] if k not in keys]
    ck = Counter(tuple(x) for v in keys.values() for x in v)
    print("==", Path(p).stem, f"changed={len(found['changed'])} keyed={len(keys)}",
          "unkeyed:", Counter(k.split(" ")[2] for k in unkeyed).most_common(6))
    print("   keys:", [(pth.replace(state.KEY_SEP, "/"), k, n) for (pth, k), n in ck.most_common(6)])
