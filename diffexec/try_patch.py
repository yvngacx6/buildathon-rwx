"""Debug helper: run the replay child on one patch and print its answer.

    DIFFEXEC_DEBUG=1 python -m diffexec.try_patch <sqlglot root> <store> <patch file>
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from selector import diff  # noqa: E402


def main() -> None:
    root, store, patch = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    files = []
    for fp in diff.parse(patch.read_text(errors="replace")):
        if fp.path.startswith("sqlglot/") and fp.path.endswith(".py") and fp.old_path and fp.new_path:
            files.append({"path": fp.path, "new_text": diff.apply((root / fp.path).read_text(), fp)})
    req = {"root": str(root), "store": store, "deadline_s": 60, "files": files}
    env = dict(os.environ, PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, "-m", "diffexec.replay"], input=json.dumps(req), text=True,
                          capture_output=True, env=env, cwd=Path(__file__).resolve().parent.parent)
    sys.stderr.write(proc.stderr[-5000:])
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    for k, c in got.get("codes", {}).items():
        bad = {p: v for p, v in c["phases"].items() if v != "ok"}
        print(k, c["state"], f"phases={len(c['phases'])} not-ok={len(bad)}", list(bad.items())[:4])
    print("bail", got.get("bail"), "stats", got.get("stats"))


if __name__ == "__main__":
    main()
