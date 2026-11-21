"""r26 probes: run the real slow tests on patched sqlglot copies.

usage (in the mapbuild image, via ~/shared/drun.sh):
  python experiments/r26_probe.py OUT.json TESTS PATCH...
TESTS: comma-separated pytest node ids (relative to the sqlglot root).
Each patch is applied to a fresh copy of $BR_SQLGLOT; result per patch: pass / fail / error.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

SRC = os.environ.get("BR_SQLGLOT", "/shared/sqlglot")
out, tests, patches = sys.argv[1], sys.argv[2].split(","), sys.argv[3:]
PAR = int(os.environ.get("PROBE_PAR", "2"))


def apply_patch(dst, patch):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from selector import diff
    with open(patch, encoding="utf-8") as fh:
        files = diff.parse(fh.read())
    for fp in files:
        old = os.path.join(dst, fp.old_path) if fp.old_path else None
        new = os.path.join(dst, fp.new_path) if fp.new_path else None
        text = open(old, encoding="utf-8").read() if old else ""
        body = diff.apply(text, fp)
        if old and old != new:
            os.remove(old)
        if new:
            os.makedirs(os.path.dirname(new), exist_ok=True)
            with open(new, "w", encoding="utf-8") as fh:
                fh.write(body)
    return bool(files)


def one(patch):
    pid = os.path.basename(patch).removesuffix(".patch")
    tmp = tempfile.mkdtemp(prefix=f"probe_{pid}_")
    dst = os.path.join(tmp, "sg")
    try:
        shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        if not apply_patch(dst, patch):
            return pid, {"status": "apply-failed"}
        env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONPATH=dst)
        p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                            "-rfE", *tests], cwd=dst, capture_output=True, text=True, env=env,
                           timeout=1800)
        tail = p.stdout.strip().splitlines()[-1:] if p.stdout else []
        failed = [ln for ln in p.stdout.splitlines() if ln.startswith(("FAILED", "ERROR"))]
        return pid, {"status": "pass" if p.returncode == 0 else "fail", "rc": p.returncode,
                     "summary": tail, "failed": failed[:10]}
    except Exception as e:  # noqa: BLE001
        return pid, {"status": "error", "err": repr(e)[:300]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


res = {}
with ThreadPoolExecutor(PAR) as ex:
    for pid, r in ex.map(one, patches):
        res[pid] = r
        print(pid, r["status"], r.get("summary") or r.get("err") or "", flush=True)
with open(out, "w") as fh:
    json.dump(res, fh, indent=1)
