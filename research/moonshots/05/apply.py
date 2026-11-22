"""Apply a unified diff to a tree with the selector's pure-Python patcher: apply.py ROOT PATCH."""
import os, sys
sys.path.insert(0, "/w/sel")
from selector import diff
root, patch = sys.argv[1], sys.argv[2]
for fp in diff.parse(open(patch).read()):
    path = os.path.join(root, fp.path)
    old = open(path).read() if os.path.exists(path) else ""
    new = diff.apply(old, fp)
    with open(path, "w") as fh:
        fh.write(new)
