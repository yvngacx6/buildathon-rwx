"""Value-profile equivalence check (moonshot 03 prototype).

python vcheck.py PROFDIR SQLGLOT DATA SEL.jsonl [answers.json]
For each commit: find the changed expressions; if every change is a BoolOp / single Compare
rewrite expressible over the old site's operands (+ constants), drop each selected test whose
recorded evaluations of that site all give the same value (same truthiness in a test context,
same object otherwise) and skip only pure operands. Prints per-commit savings and totals.
"""
from __future__ import annotations

import ast
import glob
import json
import os
import operator
import pickle
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from selector import diff as D  # noqa: E402
from selector import pack as P  # noqa: E402

UNK = object()
from collections import Counter
WHY = Counter()


# ---------------------------------------------------------------- profile store
def load_profiles(pdir):
    prof = defaultdict(lambda: defaultdict(set))  # phase -> key -> entries
    outcome = {}
    for f in glob.glob(pdir + '/*.pkl'):
        d = pickle.load(open(f, 'rb'))
        sites = d['sites']
        for phase, m in d['prof'].items():
            pm = prof[phase]
            for sid, s in m.items():
                pm[sites[sid]] |= s
        outcome.update(d['outcome'])
    return prof, outcome


# ---------------------------------------------------------------- AST diff
def parents(tree):
    par = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            par[c] = n
    return par


OPNODE = (ast.cmpop, ast.boolop, ast.operator, ast.unaryop, ast.expr_context)


def diffs(o, n, out):
    """Minimal differing node pairs (o, n)."""
    if isinstance(o, OPNODE) or isinstance(n, OPNODE):
        if type(o) is not type(n):
            out.append(None)
        return
    if type(o) is not type(n):
        out.append((o, n)); return
    if not isinstance(o, ast.AST):
        if o != n:
            out.append(None)
        return
    for f in o._fields:
        a, b = getattr(o, f, None), getattr(n, f, None)
        if isinstance(a, list):
            if not isinstance(b, list) or len(a) != len(b):
                out.append((o, n)); return
            for x, y in zip(a, b):
                if isinstance(x, ast.AST) or isinstance(y, ast.AST):
                    sub = []
                    diffs(x, y, sub)
                    if None in sub:
                        out.append((o, n)); return
                    out.extend(sub)
                elif x != y:
                    out.append((o, n)); return
        elif isinstance(a, ast.AST) or isinstance(b, ast.AST):
            if a is None or b is None:
                out.append((o, n)); return
            sub = []
            diffs(a, b, sub)
            if None in sub:
                out.append((o, n)); return
            out.extend(sub)
        elif a != b:
            out.append((o, n)); return


def dump(x):
    return ast.dump(x, annotate_fields=False)


def path_to(tree, node):
    """Field path from tree to node."""
    stack = [(tree, ())]
    while stack:
        cur, p = stack.pop()
        if cur is node:
            return p
        for f in cur._fields:
            v = getattr(cur, f, None)
            if isinstance(v, list):
                for i, x in enumerate(v):
                    if isinstance(x, ast.AST):
                        stack.append((x, p + ((f, i),)))
            elif isinstance(v, ast.AST):
                stack.append((v, p + ((f, None),)))
    return None


def follow(tree, p):
    cur = tree
    for f, i in p:
        cur = getattr(cur, f)
        if i is not None:
            cur = cur[i]
    return cur


def bool_ctx(node, par):
    p = par.get(node)
    if isinstance(p, (ast.If, ast.While, ast.IfExp, ast.Assert)) and p.test is node:
        return True
    if isinstance(p, ast.UnaryOp) and isinstance(p.op, ast.Not):
        return True
    if isinstance(p, ast.comprehension) and node in p.ifs:
        return True
    if isinstance(p, ast.BoolOp):
        return bool_ctx(p, par)  # an operand's value can be the BoolOp's value
    return False


STRICT = (ast.Name, ast.Constant, ast.Load)
LOOSE = STRICT + (ast.Attribute,)


def pure(node, loose):
    ok = LOOSE if loose else STRICT
    for x in ast.walk(node):
        if isinstance(x, (ast.Compare, ast.UnaryOp, ast.Not, ast.BoolOp, ast.And, ast.Or,
                          ast.cmpop)):
            continue
        if not isinstance(x, ok):
            return False
    return True


# ---------------------------------------------------------------- symbolic evaluation
def ev_bool(node, atoms, vec):
    """-> (state, atom-index or const, evaluated set) ; state in 1..5 ; UNK on unknown."""
    seen = []

    def ev(x):
        k = atoms.get(dump(x))
        if k is not None:
            if k >= len(vec) or vec[k] in (0, 6, 7):
                raise LookupError
            seen.append(k)
            return vec[k], k
        if isinstance(x, ast.Constant):
            v = x.value
            st = 3 if v is None else 4 if v is False else 5 if v is True else (2 if v else 1)
            return st, ('c', repr(v))
        if isinstance(x, ast.BoolOp):
            isand = isinstance(x.op, ast.And)
            for i, y in enumerate(x.values):
                st, who = ev(y)
                truthy = st in (2, 5)
                if i == len(x.values) - 1 or (isand and not truthy) or (not isand and truthy):
                    return st, who
        raise LookupError

    try:
        st, who = ev(node)
    except LookupError:
        return UNK
    return st, who, set(seen)


def check_bool(old, new, entries, ctx, impure_atoms):
    atoms = {dump(v): i for i, v in enumerate(old.values)}
    for vec in entries:
        if vec == 'X' or (vec and vec[-1] == 7):
            WHY['B:raised'] += 1
            return False
        a = ev_bool(old, atoms, vec)
        b = ev_bool(new, atoms, vec)
        if a is UNK or b is UNK:
            WHY['B:new-needs-unevaluated'] += 1
            return False
        if b[2] - a[2]:
            return False  # new evaluates an operand old did not: value unknown
        if (a[2] - b[2]) & impure_atoms:
            WHY['B:skips-impure'] += 1
            return False
        if ctx:
            if (a[0] in (2, 5)) != (b[0] in (2, 5)):
                WHY['B:truth-differs'] += 1
                return False
        else:
            if a[1] != b[1] and not (a[0] == b[0] and a[0] in (3, 4, 5)):
                WHY['B:value-differs'] += 1
                return False
    return True


OPS = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
       ast.Gt: operator.gt, ast.GtE: operator.ge}


def side(x, oldx, rec):
    """Abstract value of a new compare operand: the old operand's record or a constant."""
    if dump(x) == dump(oldx):
        return rec
    if isinstance(x, ast.Constant) and isinstance(x.value, (int, float, str, type(None), bool)):
        return ('v', type(x.value).__name__, x.value)
    return UNK


def cmp_val(op, a, b, isb, eqb):
    """Result of `a op b` from abstract values, or UNK."""
    exact = a is not UNK and b is not UNK and a[0] == 'v' and b[0] == 'v'
    t = type(op)
    if exact:
        ta, tb = a[1], b[1]
        plain = {'int', 'float', 'bool', 'str', 'NoneType'}
        if ta in plain and tb in plain:
            va, vb = a[2], b[2]
            if t in (ast.Is, ast.IsNot):
                if va is None or vb is None or isinstance(va, bool) or isinstance(vb, bool):
                    r = (va is vb) and (type(va) is type(vb))
                elif va != vb or type(va) is not type(vb):
                    r = False
                else:
                    return UNK  # equal ints / strs: identity depends on interning
                return r if t is ast.Is else not r
            if t in OPS:
                try:
                    return OPS[t](va, vb)
                except TypeError:
                    return UNK
            return UNK
        if ta == tb and ta not in plain and t in (ast.Is, ast.IsNot, ast.Eq, ast.NotEq):
            r = a[2] == b[2]  # same enum class: same member name <=> identical <=> equal
            return r if t in (ast.Is, ast.Eq) else not r
        return UNK
    if isb is None:
        return UNK
    if t is ast.Is:
        return isb
    if t is ast.IsNot:
        return not isb
    if t in (ast.Eq, ast.NotEq) and eqb is not None:
        return eqb if t is ast.Eq else not eqb
    return UNK


def check_cmp(old, new, entries):
    for e in entries:
        if e == 'X':
            return False
        aa, bb, isbool, isb, eqb = e
        if not isbool:
            WHY['C:nonbool'] += 1
            return False
        ro = cmp_val(old.ops[0], aa, bb, isb, eqb)
        na, nb = side(new.left, old.left, aa), side(new.comparators[0], old.comparators[0], bb)
        if na is UNK or nb is UNK:
            WHY['C:new-operand-unknown'] += 1
            return False
        # identity / equality bits are only valid for the old operand pair
        same_pair = na is aa and nb is bb
        rn = cmp_val(new.ops[0], na, nb, isb if same_pair else None, eqb if same_pair else None)
        if ro is UNK or rn is UNK:
            WHY['C:unknown'] += 1
            return False
        if ro != rn:
            WHY['C:differs'] += 1
            return False
    return True


# ---------------------------------------------------------------- per-commit analysis
def site_key(rel, node, kind):
    return (rel, node.lineno, node.col_offset, node.end_lineno, node.end_col_offset, kind)


def expressible(x, atoms):
    if dump(x) in atoms:
        return True
    if isinstance(x, ast.Constant):
        return True
    if isinstance(x, ast.BoolOp):
        return all(expressible(y, atoms) for y in x.values)
    return False


def analyse_file(rel, old_src, new_src, loose):
    """-> list of change records, or None if the files do not parse / differ oddly.

    record = {'site': (key, chk) or None, 'guards': [(boolop key, operand index)],
              'op': short label}
    """
    try:
        ot, nt = ast.parse(old_src), ast.parse(new_src)
    except SyntaxError:
        return None
    pairs = []
    diffs(ot, nt, pairs)
    if None in pairs:
        return None
    par = parents(ot)
    out = []
    for o, n in pairs:
        site = None
        guards = []
        cur = o
        guard_ok = True
        while cur is not None and not isinstance(cur, ast.stmt):
            if site is None and isinstance(cur, ast.Compare) and len(cur.ops) == 1:
                try:
                    nn = follow(nt, path_to(ot, cur))
                except Exception:
                    nn = None
                if isinstance(nn, ast.Compare) and len(nn.ops) == 1:
                    site = (site_key(rel, cur, 'C'), ('C', cur, nn))
            if site is None and isinstance(cur, ast.BoolOp):
                try:
                    nn = follow(nt, path_to(ot, cur))
                except Exception:
                    nn = None
                atoms = {dump(v) for v in cur.values}
                if nn is not None and expressible(nn, atoms):
                    imp = {i for i, v in enumerate(cur.values) if not pure(v, loose)}
                    site = (site_key(rel, cur, 'B'), ('B', cur, nn, bool_ctx(cur, par), imp))
            p = par.get(cur)
            if isinstance(cur, (ast.Lambda, ast.GeneratorExp)):
                guard_ok = False  # evaluated later, outside the enclosing operand
            if guard_ok and isinstance(p, ast.BoolOp):
                for i, v in enumerate(p.values):
                    if v is cur:
                        guards.append((site_key(rel, p, 'B'), i))
            cur = p
        out.append({'site': site, 'guards': guards})
    return out


def main():
    pdir, sqlglot, data, sel, metaf = sys.argv[1:6]
    ans = json.load(open(sys.argv[6]))['commits'] if len(sys.argv) > 6 and sys.argv[6] != '-v' else {}
    verbose = '-v' in sys.argv
    meta = json.load(open(metaf)) if metaf != '-' else {}
    loose = True
    prof, outcome = load_profiles(pdir)
    tests = json.load(open(data + '/tests.json'))['tests']
    byno = {t['number']: t for t in tests}
    ms = {t['number']: P.duration_ms(t['seconds']) for t in tests}
    out_phase = prof.get('<out>', {})
    cls_setup = defaultdict(list)
    for ph in prof:
        if ph.endswith('|setup'):
            cls_setup[ph[:-6].rsplit('::', 1)[0]].append(ph)
    INCOMPLETE_FILES = ('tests/test_optimizer.py',)  # spawn pool: children not recorded
    INCOMPLETE = {'tests/test_executor.py::TestExecutor::test_optimized_tpch',
                  'tests/test_executor.py::TestExecutor::test_execute_tpch',
                  'tests/test_executor.py::TestExecutor::test_execute_tpcds',
                  'tests/dialects/test_dialect.py::TestDialect::test_lazy_load'}
    if os.environ.get('VC_SYNC'):  # profile recorded with VPROF_SYNCPOOL: pool jobs in-process
        INCOMPLETE_FILES = ()
        INCOMPLETE = {'tests/dialects/test_dialect.py::TestDialect::test_lazy_load'}
    tot = defaultdict(float)
    bad = defaultdict(int)
    perop = defaultdict(lambda: defaultdict(float))
    for line in open(sel):
        r = json.loads(line)
        c = r['commit']
        desc = meta.get(c, {}).get('desc', '')
        import re
        mm = re.search(r"'(.*)'->'(.*)'", desc)
        op = (meta.get(c, {}).get('kind', '?') + ':' + (mm.group(1) + '->' + mm.group(2) if mm else 'other'))
        sel_set = {t for w in r['workers'] for t in w}
        before = P.cost_ms(r['workers'], ms) / 1000 if sel_set else 0
        tot['before'] += before
        perop[op]['n'] += 1
        perop[op]['before'] += before
        if not sel_set:
            continue
        text = Path(data, 'commits', c + '.patch').read_text()
        try:
            fps = D.parse(text)
        except Exception:
            continue
        recs = []
        ok = True
        for fp in fps:
            path = fp.path
            if not path.endswith('.py'):
                ok = False; break
            if not path.startswith('sqlglot/') or fp.old_path is None or fp.new_path is None:
                ok = False; break
            old = Path(sqlglot, fp.old_path).read_text()
            try:
                new = D.apply(old, fp)
            except Exception:
                ok = False; break
            a = analyse_file(path, old, new, loose)
            if a is None:
                ok = False; break
            recs.extend(a)
        if not ok or not recs:
            perop[op]['unsupported'] += before
            continue
        if any(rec['site'] is None and not rec['guards'] for rec in recs):
            perop[op]['nosite'] += before
            continue
        tot['eligible_commits'] += 1
        perop[op]['eligible'] += before

        def site_equiv(chk, entries):
            if chk[0] == 'C':
                return check_cmp(chk[1], chk[2], entries)
            return check_bool(chk[1], chk[2], entries, chk[3], chk[4])

        def rec_bucket(rec, getent):
            """3 = evaluated, all equivalent; 2 = not evaluated, guarded off; 1 = not
            evaluated, no positive evidence; 0 = keep."""
            if rec['site'] is not None:
                ent = getent(rec['site'][0])
                if ent:
                    return 3 if site_equiv(rec['site'][1], ent) else 0
            for gk, gi in rec['guards']:
                ge = getent(gk)
                if ge:
                    if all(v != 'X' and (gi >= len(v) or v[gi] == 0) and v[-1] != 7 for v in ge):
                        return 2
                    return 0  # operand evaluated, change reached
            return 1 if rec['site'] is not None else 0

        # evaluations outside any test must be harmless, else keep everything
        if min(rec_bucket(rec, lambda k: out_phase.get(k, ())) for rec in recs) == 0:
            tot['out_block'] += 1
            perop[op]['outblock'] += before
            continue
        drops = {1: set(), 2: set(), 3: set()}
        for t in sel_set:
            nid = byno[t]['id']
            if outcome.get(nid) is not True or nid in INCOMPLETE or byno[t]['file'] in INCOMPLETE_FILES:
                continue
            phases = [nid, nid + '|setup', nid + '|teardown'] + cls_setup.get(nid.rsplit('::', 1)[0], [])

            def getent(k):
                s = set()
                for ph in phases:
                    s |= prof.get(ph, {}).get(k, set())
                return s
            b = min(rec_bucket(rec, getent) for rec in recs)
            if b:
                drops[b].add(t)
        f = set(ans.get(c, {}).get('failing', [])) if ans else set()
        line_out = []
        for name, dset in (('iii', drops[3]), ('iii+a', drops[3] | drops[2]),
                           ('all', drops[3] | drops[2] | drops[1])):
            keep = sel_set - dset
            after = P.cost_ms(P.pack(keep, ms), ms) / 1000 if keep else 0
            tot['saved_' + name] += before - after
            perop[op]['saved_' + name] += before - after
            bad[name] += len(dset & f)
            if verbose and dset & f:
                print("  BAD", name, c, [byno[x]["id"] for x in dset & f])
            line_out.append('%s %.1f' % (name, before - after))
        if verbose and (drops[3] or drops[2] or (dset & f)):
            print(c, op, 'before %.1f' % before, ' '.join(line_out),
                  'n3=%d n2=%d n1=%d sel=%d bad=%d' % (len(drops[3]), len(drops[2]), len(drops[1]),
                                                       len(sel_set), len(dset & f)))
    n = perop and sum(v['n'] for v in perop.values())
    for k, v in sorted(perop.items(), key=lambda x: -x[1]['before'])[:25]:
        print('%-22s n=%3d before=%7.1f unsup=%6.1f nosite=%6.1f elig=%6.1f outblk=%6.1f  '
              'save iii=%5.1f iii+a=%5.1f all=%5.1f' % (
                  k, v['n'], v['before'], v['unsupported'], v['nosite'], v['eligible'], v['outblock'],
                  v['saved_iii'], v['saved_iii+a'], v['saved_all']))
    print({k: round(v, 1) for k, v in tot.items()}, 'per40:',
          {k: round(tot[k] * 40 / n, 1) for k in ('before', 'saved_iii', 'saved_iii+a', 'saved_all')},
          'bad', dict(bad))
    print('non-equivalence reasons (per test x site check):', dict(WHY))


if __name__ == '__main__':
    main()
