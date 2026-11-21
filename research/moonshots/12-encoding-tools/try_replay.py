"""Replay a patch against a store (diffexec.replay child), print tpc-phase verdicts + CPU.

usage: python try_replay.py <store.zip> <sqlglot root> <patch> [<patch> ...]
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

store, root = sys.argv[1], sys.argv[2]
W = os.environ["W"]
for p in sys.argv[3:]:
    tmp = tempfile.mkdtemp()
    shutil.copytree(os.path.join(root, "sqlglot"), os.path.join(tmp, "sqlglot"))
    r = subprocess.run(["patch", "-p1", "-s", "-d", tmp, "-i", os.path.abspath(p)], capture_output=True, text=True)
    if r.returncode:
        print(p, "patch failed", r.stdout, r.stderr)
        continue
    files = []
    for line in open(p):
        if line.startswith("+++ b/sqlglot/"):
            rel = line[6:].strip()
            files.append({"path": rel, "new_text": open(os.path.join(tmp, rel)).read()})
    shutil.rmtree(tmp)
    req = {"root": root, "store": store, "deadline_s": 600, "files": files}
    env = dict(os.environ, PYTHONHASHSEED="0", PYTHONPATH=f"{root}:{W}")
    out = subprocess.run([sys.executable, "-m", "diffexec.replay"], input=json.dumps(req), env=env,
                         capture_output=True, text=True, cwd=W)
    try:
        ans = json.loads(out.stdout)
    except Exception:
        print(p, "replay failed", out.stderr[-2000:])
        continue
    print(os.path.basename(p), "bail", ans.get("bail"), "stats", ans.get("stats"))
    for k, info in ans.get("codes", {}).items():
        ph = {kk.split("::")[-1]: v for kk, v in info.get("phases", {}).items() if "tpc" in kk}
        nb = sum(1 for v in info.get("phases", {}).values() if v == "bad")
        print("   ", k.split("::")[1], info["state"], ph, f"bad phases {nb}/{len(info.get('phases', {}))}")
