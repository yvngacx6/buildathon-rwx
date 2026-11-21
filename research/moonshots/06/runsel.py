"""Run the selector on a patch dir in shards; save {commit: {workers, reason}} from stdout/stderr."""
import json, os, shutil, subprocess, sys, tempfile, re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

checkout, patches, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
shards = int(sys.argv[4]) if len(sys.argv) > 4 else 10
PY = "/home/aditya/rwx-research-robust/venv/bin/python"
SCR = "/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m06"
ids = sorted(p.stem for p in (patches / "commits").glob("*.patch"))
tmp = Path(tempfile.mkdtemp(dir=SCR))
LINE = re.compile(r"^\[(\S+)\] (\d+) tests, (\d+) workers, ([\d.]+)s :: (.*)$")


def one(k):
    sh = tmp / f"s{k}"
    (sh / "commits").mkdir(parents=True)
    shutil.copy(patches / "tests.json", sh / "tests.json")
    for i in ids[k::shards]:
        shutil.copy(patches / "commits" / f"{i}.patch", sh / "commits")
    env = dict(os.environ, BR_DATA=str(sh), BR_SQLGLOT="/home/aditya/rwx-research-robust/sqlglot")
    p = subprocess.run([PY, "-m", "selector"], cwd=checkout, env=env, capture_output=True, text=True)
    res = {}
    for l in p.stdout.splitlines():
        d = json.loads(l)
        res[d["commit"]] = {"workers": d["workers"]}
    for l in p.stderr.splitlines():
        m = LINE.match(l)
        if m and m[1] in res:
            res[m[1]]["reason"] = m[5]
    return res


with ThreadPoolExecutor(shards) as ex:
    allr = {}
    for r in ex.map(one, range(shards)):
        allr.update(r)
shutil.rmtree(tmp)
out.write_text(json.dumps(allr))
print(len(allr), "of", len(ids))
