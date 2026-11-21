"""The entry point's safety net (selector/__main__.py, research/moonshots/09-final.md R1/R4):
one line per commit file on every path out — normal, Selector failure, time budget, SIGTERM.
Run: python -m pytest tests/
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_select import FILES, T, edit, make_selector  # noqa: E402

ALL = set(range(len(T)))
# A comment, a code edit, a docs edit; one commit id with a quote, a space and a newline.
COMMITS = {
    "a1": edit("sqlglot/lib.py", "LIMIT = 10\n", "LIMIT = 10  # cap\n"),
    'b "2\nx': edit("sqlglot/lib.py", "return sql\n", "return sql +\n"),
    "c3": "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-hello\n+hi\n",
}


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    make_selector(tmp_path)  # writes the fake repo and maps
    data = tmp_path / "data"
    (data / "commits").mkdir(parents=True)
    for name, text in COMMITS.items():
        (data / "commits" / f"{name}.patch").write_text(text)
    tests = [{"number": i, "id": t, "file": t.split("::")[0], "seconds": 1.0}
             for i, t in enumerate(T)]
    (data / "tests.json").write_text(json.dumps({"tests": tests}))
    none = str(tmp_path / "none")
    return dict(os.environ, BR_DATA=str(data), BR_SQLGLOT=str(tmp_path / "sqlglot"),
                BR_MAPS=str(tmp_path / "maps"), BR_MAPS_COMPILED=none, BR_INDEX=none,
                BR_STATE=none, BR_DIFFEXEC="0", BR_SUBTEST="0", BR_SLOWVERIFY="0",
                BR_GENMEM="0", PYTHONPATH=str(ROOT))


def lines_of(stdout: str) -> dict[str, set[int]]:
    rows = [json.loads(line) for line in stdout.splitlines()]
    assert len(rows) == len(COMMITS) and stdout.endswith("\n")
    return {r["commit"]: {n for w in r["workers"] for n in w} for r in rows}


def run_main(env: dict[str, str], prelude: str = "", **kw) -> subprocess.CompletedProcess:
    code = prelude + "from selector.__main__ import main; main()"
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                          text=True, timeout=60, **kw)


def test_normal_run_one_line_per_commit_in_order(env):
    proc = run_main(env)
    got = lines_of(proc.stdout)
    assert list(got) == sorted(COMMITS)  # file order, weird id intact
    assert got == {"a1": set(), 'b "2\nx': ALL, "c3": set()}


def test_selector_failure_answers_all(env):
    proc = run_main(dict(env, BR_MAPS=env["BR_MAPS"] + "-missing"))
    assert "selector unavailable" in proc.stderr
    assert set(map(frozenset, lines_of(proc.stdout).values())) == {frozenset(ALL)}


def test_corrupt_tests_json_still_one_line_per_commit(env):
    Path(env["BR_DATA"], "tests.json").write_text("{")
    proc = run_main(env)
    assert len(proc.stdout.splitlines()) == len(COMMITS)


def test_budget_spent_answers_all(env):
    proc = run_main(dict(env, BR_DEADLINE_SCALE="0"))
    assert set(map(frozenset, lines_of(proc.stdout).values())) == {frozenset(ALL)}


def test_verify_phase_degrades_the_selector(env):
    # used share ≥ 0.40 but < 0.70 from the first commit on: Selector.degrade() runs
    prelude = ("import selector.__main__ as m, selector.select as s\n"
               "m.Budget.used = lambda self: 0.5\n"
               "orig = s.Selector.degrade\n"
               "def degrade(self):\n    print('DEGRADED', file=__import__('sys').stderr)\n"
               "    orig(self)\n"
               "s.Selector.degrade = degrade\n")
    proc = run_main(env, prelude)
    assert proc.stderr.count("DEGRADED") == 1
    assert lines_of(proc.stdout) == {"a1": set(), 'b "2\nx': ALL, "c3": set()}


# A selector whose second commit hangs in a child process: the wall timer must interrupt it,
# kill the child and answer ALL for the rest.
HANG = """\
import os, subprocess, time
import selector.select as s
orig = s.Selector.select
def select(self, text):
    if "return sql +" in text:
        child = subprocess.Popen(["sleep", "300"])
        open(os.environ["PIDFILE"], "w").write(str(child.pid))
        time.sleep(300)
    return orig(self, text)
s.Selector.select = select
"""


def _dead(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True
    return stat.rsplit(")", 1)[1].split()[0] == "Z"


def test_deadline_interrupts_a_hung_commit_and_kills_children(env, tmp_path):
    pidfile = tmp_path / "pid"
    # 3 commits × 4.5 s × 0.2 = 2.7 s budget: ALL phase after ~1.9 s
    t = time.monotonic()
    proc = run_main(dict(env, BR_DEADLINE_SCALE="0.2", PIDFILE=str(pidfile)), HANG)
    assert time.monotonic() - t < 20
    got = lines_of(proc.stdout)
    assert got["a1"] == set() and got['b "2\nx'] == ALL and got["c3"] == ALL
    assert "Deadline" in proc.stderr
    assert _dead(int(pidfile.read_text()))


def test_sigterm_prints_finished_lines_and_all_for_the_rest(env, tmp_path):
    pidfile = tmp_path / "pid"
    p = subprocess.Popen([sys.executable, "-c", HANG + "from selector.__main__ import main; main()"],
                         env=dict(env, PIDFILE=str(pidfile)), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    for _ in range(200):
        if pidfile.exists() and pidfile.read_text():
            break
        time.sleep(0.05)
    p.send_signal(signal.SIGTERM)
    out, _ = p.communicate(timeout=20)
    got = lines_of(out)
    assert got["a1"] == set() and got['b "2\nx'] == ALL and got["c3"] == ALL
    assert _dead(int(pidfile.read_text()))
