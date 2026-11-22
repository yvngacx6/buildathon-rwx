"""Bytecode-level diff of an old vs new Python source: which OLD instructions differ.

Soundness argument: if old and new code objects are identical except at a set D of old
instructions (same opcodes/args elsewhere, jump targets and exception table consistent under
the alignment, same code-object signature fields), then any execution of the old code that
never executes an instruction in D is step-for-step identical to the new code's execution.
Anything we cannot align cleanly -> WHOLE (every instruction of that code object is in D).
"""
import dis, difflib, sys

SIG = ("co_argcount", "co_posonlyargcount", "co_kwonlyargcount", "co_flags", "co_varnames",
       "co_cellvars", "co_freevars", "co_nlocals")
JUMPS = set(dis.hasjrel) | set(dis.hasjabs)
try:
    from dis import _parse_exception_table as _pet
except ImportError:
    _pet = None


def _key(ins):
    if ins.opcode in JUMPS:
        return (ins.opname, "J")
    if ins.opname in ("LOAD_CONST", "RETURN_CONST") and hasattr(ins.argval, "co_code"):
        return (ins.opname, "code:" + ins.argval.co_qualname)
    return (ins.opname, ins.argrepr, ins.arg if not ins.argrepr else None)


def _nested(code):
    return [c for c in code.co_consts if hasattr(c, "co_code")]


def _insts(code):
    return [i for i in dis.get_instructions(code) if i.opname != "CACHE"]


def diff_code(old, new, out, path=""):
    """Fill out[(qualname, firstlineno)] = set(old offsets) | 'WHOLE'."""
    key = (old.co_qualname, old.co_firstlineno)
    oi, ni = _insts(old), _insts(new)
    D = set()
    whole = any(getattr(old, f) != getattr(new, f) for f in SIG)
    if not whole:
        ok, nk = [_key(i) for i in oi], [_key(i) for i in ni]
        sm = difflib.SequenceMatcher(None, ok, nk, autojunk=False)
        o2n = {}
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                for d in range(i2 - i1):
                    o2n[i1 + d] = j1 + d
            elif tag in ("replace", "delete"):
                D.update(range(i1, i2))
            else:  # insert before old index i1: whoever reaches i1 reaches new code
                D.add(min(i1, len(oi) - 1))
                if i1 > 0:
                    D.add(i1 - 1)  # conservative: the instruction falling into the insert
        ooff = {i.offset: k for k, i in enumerate(oi)}
        noff = {i.offset: k for k, i in enumerate(ni)}
        for k, j in o2n.items():
            a, b = oi[k], ni[j]
            if a.opcode in JUMPS:
                ta, tb = ooff.get(a.argval), noff.get(b.argval)
                if ta is None or tb is None or o2n.get(ta) != tb:
                    D.add(k)
        # exception table must map entry-for-entry
        if _pet is not None:
            try:
                eo, en = _pet(old), _pet(new)
            except Exception:
                whole = True
            else:
                def norm(entries, offs, m=None):
                    r = []
                    for e in entries:
                        s, t = offs.get(e.start), offs.get(e.target)
                        # end is exclusive offset: map the last covered instruction
                        last = max((v for o, v in offs.items() if o < e.end), default=None)
                        r.append((s, last, t, e.depth, e.lasti))
                    return r
                no, nn = norm(eo, ooff), norm(en, noff)
                if len(no) != len(nn):
                    whole = True
                else:
                    for a, b in zip(no, nn):
                        mapped = tuple(o2n.get(x) if isinstance(x, int) and i < 3 else x
                                       for i, x in enumerate(a))
                        if a[0] is None or mapped[:3] != b[:3] or a[3:] != b[3:]:
                            # a try range whose boundaries move: every instruction it guards
                            # and its handler differ
                            whole = True
    if whole:
        out[key] = "WHOLE"
    elif D:
        out[key] = {oi[k].offset for k in D}
    # nested code objects: pair by qualname + order
    on, nn = _nested(old), _nested(new)
    from collections import defaultdict
    bo, bn = defaultdict(list), defaultdict(list)
    for c in on:
        bo[c.co_qualname].append(c)
    for c in nn:
        bn[c.co_qualname].append(c)
    for q in set(bo) | set(bn):
        a, b = bo.get(q, []), bn.get(q, [])
        if len(a) != len(b):
            for c in a:
                out[(c.co_qualname, c.co_firstlineno)] = "WHOLE"
                _all_whole(c, out)
            if not a:  # new nested code with no old counterpart: parent instruction differs
                out.setdefault(key, set())
                if out[key] != "WHOLE":
                    out[key] = "WHOLE"
            continue
        for x, y in zip(a, b):
            diff_code(x, y, out, path)


def _all_whole(code, out):
    for c in _nested(code):
        out[(c.co_qualname, c.co_firstlineno)] = "WHOLE"
        _all_whole(c, out)


def diff_source(old_src, new_src, filename):
    out = {}
    diff_code(compile(old_src, filename, "exec"), compile(new_src, filename, "exec"), out)
    return out


if __name__ == "__main__":
    a, b = open(sys.argv[1]).read(), open(sys.argv[2]).read()
    for k, v in diff_source(a, b, sys.argv[1]).items():
        print(k, v if v == "WHOLE" else sorted(v))
