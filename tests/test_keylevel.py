"""Moonshot 21: key-level import-state diff helpers (selector/state.py)."""

from __future__ import annotations

import enum
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from selector import state  # noqa: E402


def test_kid_equal_keys_share_an_id():
    class S(str, enum.Enum):
        A = "a"

    class I(enum.IntEnum):
        X = 3

    assert state.kid("a") == state.kid(S.A)  # equal keys hit one dict slot
    assert state.kid(1) == state.kid(True) == state.kid(1.0)
    assert state.kid(3) == state.kid(I.X)
    assert state.kid(("a", 1)) == "(s:a,i:1)"
    assert state.kid(object()) is None


def test_changed_keys_nested_paths():
    snap = state.Snapshot("x")
    old = state.key_table(snap, {"A": 1, "F": {"O": {0: True}}, "Z": 2})
    new = state.key_table(snap, {"A": 1, "F": {"O": {0: True}, "X": {0: True}}, "Y": 3})
    got = state.changed_keys(old, new)
    assert ("s:F", "s:X") in got  # added inside a nested dict: its key in that dict
    assert ("", "s:Z") in got and ("", "s:Y") in got  # removed / added at the top
    assert ("", "s:A") not in got and ("", "s:F") not in got


def test_changed_keys_order_only():
    snap = state.Snapshot("x")
    old = state.key_table(snap, {"A": 1, "B": 2})
    new = state.key_table(snap, {"B": 2, "A": 1})
    assert state.changed_keys(old, new) == [("", state.ORDER)]
