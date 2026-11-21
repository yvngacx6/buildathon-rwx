"""Elided stand-ins and effect digests of diffexec/serial.py (round 8). Run: python -m pytest tests/
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from diffexec import replay, serial  # noqa: E402


def _use(expr: str, e) -> bool:
    """Run `expr` on a stand-in `e` the way replay does; True if the use was noticed."""
    serial.TOUCHED[0] = False
    replay._watch_calls(True)
    try:
        try:
            exec(expr, {"e": e})
        except Exception:  # noqa: BLE001  (a swallowed use must still be noticed)
            pass
    except serial.ElidedAccess:
        pass
    finally:
        replay._watch_calls(False)
    return serial.TOUCHED[0]


@pytest.mark.parametrize("expr", [
    "e[0]", "list(e)", "e == []", "[] == e", "hash(e)", "e.__class__", "isinstance(e, list)",
    "str(e)", "repr(e)", "f'{e}'", "e + [1]", "[1] + e", "1 in e", "iter(e)", "e.append(1)",
    "e.x = 1", "type(e)", "id(e)", "sorted(e)", "{**e}", "[*e]", "e * 2", "copy.copy(e)",
    "for x in e: pass", "e < e", "dict.fromkeys(e)", "max(e, e)", "getattr(e, 'pop')",
    "list.__len__(e)", "object.__getattribute__(e, '_n')", "try:\n    e[0]\nexcept BaseException:\n    pass",
])
def test_any_use_beyond_len_and_bool_is_noticed(expr):
    e = serial.make_elided("list", 3, 0)
    if "copy." in expr:
        expr = "import copy\n" + expr
    assert _use(expr, e)


@pytest.mark.parametrize("expr", ["len(e)", "bool(e)", "e is None", "not e", "x = e if e else 0",
                                  "d = {'k': e}", "t = (e, e)"])
def test_len_bool_identity_are_faithful(expr):
    e = serial.make_elided("list", 3, 0)
    assert not _use(expr, e)
    assert len(e) == 3 and bool(e) and not bool(serial.make_elided("dict", 0, 1))


def test_elided_input_roundtrip_keeps_aliasing_and_matches_record_digest():
    rows = [(i, str(i)) for i in range(100)]
    obj = {"a": rows, "b": rows, "small": [1, 2]}
    elided: dict = {}
    data = serial.dumps_in((obj,), elide=32, elided=elided)
    (back,) = serial.loads_in(data)
    assert type(back["a"]) is serial.Elided and back["a"] is back["b"]
    assert back["small"] == [1, 2]
    # record side (real containers + the elided map) == replay side (stand-ins)
    assert serial.digest_cmp((obj,), elided) == serial.digest_cmp((back,))
    # the effect digest still sees the post-call state of everything that was not elided
    back["small"].append(3)
    assert serial.digest_cmp((obj,), elided) != serial.digest_cmp((back,))


def test_digest_ignores_interning_but_sees_mutable_sharing():
    a = "ab"
    b = "".join(["a", "b"])
    assert a is not b
    assert serial.digest_cmp((a, a)) == serial.digest_cmp((a, b))
    lst = [1]
    assert serial.digest_cmp((lst, lst)) != serial.digest_cmp((lst, [1]))
    assert serial.digest_cmp(("x",)) != serial.digest_cmp((b"x",))
    assert serial.digest_cmp((("x",),)) != serial.digest_cmp((frozenset({"x"}),))


def test_pattern_matching_in_patched_code_is_detected():
    # MATCH_SEQUENCE / MATCH_MAPPING read type flags without any hook a stand-in could see, so
    # replay refuses stand-in inputs when changed code uses `match` (diffexec/replay.py).
    src = "def f(x):\n    def g(y):\n        match y:\n            case [*_]:\n                return 1\n    return g(x)\n"
    assert replay.uses_match(compile(src, "<t>", "exec"))
    assert not replay.uses_match(compile("def f(x):\n    return len(x)\n", "<t>", "exec"))
