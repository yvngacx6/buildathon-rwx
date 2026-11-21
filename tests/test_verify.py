"""selector/verify.py: outcome rules, overlay, run/keep split. Run: python -m pytest tests/"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from selector import verify  # noqa: E402

A, B = "tests/test_a.py::T::test_a", "tests/test_a.py::T::test_b"


def raw(*reports, exit=0, **kw):
    return dict(reports=list(reports), collerr=[], exit=exit, foreign=[], spawned=[], **kw)


def test_all_pass():
    assert verify.judge([A, B], raw((A, "call", "passed"), (B, "call", "passed"))) == (set(), "")


def test_failure_in_any_phase_or_subtest_fails():
    r = raw((A, "setup", "passed"), (A, "call", "passed"), (A + " [sub 1]", "call", "failed"),
            (B, "call", "passed"), (B, "teardown", "failed"), exit=1)
    assert verify.judge([A, B], r) == ({A, B}, "")


def test_anomalies_fall_back():
    assert verify.judge([A, B], raw((A, "call", "passed")))[1].startswith("no report")
    assert verify.judge([A], raw((A, "setup", "skipped")))[1].startswith("skipped")
    assert verify.judge([A], raw((A, "call", "passed"), exit=4))[1].startswith("pytest exit")
    assert verify.judge([A], raw((A, "call", "passed"), exit=None))[1].startswith("pytest exit")
    assert verify.judge([A], dict(raw((A, "call", "passed")), collerr=["tests/x.py"]))[1]
    assert verify.judge([A], dict(raw((A, "call", "passed")), foreign=["sqlglot from x"]))[1]
    assert verify.judge([A], dict(raw((A, "call", "passed")), spawned=["Pool"]))[1]
    assert verify.judge([A], raw((A, "call", "passed"), (B, "call", "failed"), exit=1))[1]
    assert verify.judge([A], None)[1]
    assert verify.judge([A], {"error": "killed"})[1]


def test_overlay(tmp_path):
    src, root = tmp_path / "src", tmp_path / "root"
    (src / "pkg" / "sub").mkdir(parents=True)
    (src / "pkg" / "a.py").write_text("a")
    (src / "pkg" / "sub" / "b.py").write_text("b")
    (src / "pkg" / "sub" / "c.py").write_text("c")
    (src / "top.txt").write_text("t")
    verify._overlay(str(root), str(src), {"pkg/sub/b.py": "B2", "pkg/sub/c.py": None,
                                          "pkg/new/d.py": "d"})
    assert (root / "top.txt").is_symlink() and (root / "top.txt").read_text() == "t"
    assert not (root / "pkg").is_symlink() and (root / "pkg" / "a.py").is_symlink()
    assert (root / "pkg" / "sub" / "b.py").read_text() == "B2"
    assert not (root / "pkg" / "sub" / "b.py").is_symlink()
    assert not os.path.lexists(root / "pkg" / "sub" / "c.py")
    assert (root / "pkg" / "new" / "d.py").read_text() == "d"
    assert (src / "pkg" / "sub" / "b.py").read_text() == "b"  # source untouched


def test_plan_splits_slow_and_pool_tests(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("x = 1\n")
    (tmp_path / "tests" / "test_pool.py").write_text("from concurrent.futures import ProcessPoolExecutor\n")
    tests = [dict(number=0, id=A, file="tests/test_a.py", seconds=0.1),
             dict(number=1, id=B, file="tests/test_a.py", seconds=6.0),
             dict(number=2, id="tests/test_pool.py::T::t", file="tests/test_pool.py", seconds=0.1)]
    patch = "--- a/sqlglot/x.py\n+++ b/sqlglot/x.py\n@@ -1 +1 @@\n-a\n+b\n"
    plan = verify.Planner(tests, tmp_path).plan("c", patch, {0, 1, 2})
    assert plan.run == [0] and plan.keep == {1, 2}
    assert verify.Planner(tests, tmp_path).plan("c", patch, {1, 2}) is None
