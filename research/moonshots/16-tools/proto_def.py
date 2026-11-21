"""Prototype R-DEF: literal default-value edit -> tests that ran a call site that leaves the
parameter to its default (statement level), else the function-level tests.

Sound under: (1) a default value is observed only by a call that does not supply the
parameter (or by introspection of __defaults__/signature defaults; sqlglot's two inspect
uses read parameter NAMES only); (2) every call that runs f executes f's body (function-level
fallback); (3) direct call sites are found by name over sqlglot/ + tests/ (over-approximate:
any call with that attribute / bare name); escaping references (name used as a value) or
dynamic getattr dispatch (`*_sql`) -> function-level fallback; call sites / references that
run at definition time (module / class body, decorator, default expr) -> no refinement.
"""
import ast, gzip, json, os, re, sys
from collections import defaultdict
W = '/home/aditya/Desktop/hackathons/buildathon-rwx/.claude/worktrees/agent-ab99fdbafdbe08bd4'
M16 = '/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m16'
sys.path.insert(0, W); sys.path.insert(0, M16)
from selector import diff
from selector.maps import Maps  # noqa
SG = M16 + '/sg'

_files = None


def files():
    global _files
    if _files is None:
        _files = {}
        for root in ('sqlglot', 'tests'):
            for dp, dn, fn in os.walk(f'{SG}/{root}'):
                for f in fn:
                    if f.endswith('.py'):
                        p = os.path.join(dp, f)[len(SG) + 1:]
                        try:
                            _files[p] = ast.parse(open(f'{SG}/{p}').read())
                        except SyntaxError:
                            pass
    return _files


def default_edits(old_src, new_src):
    """[(qualname, funcnode, param, index_with_self)] if the edit only changes default exprs
    of def signatures; None if anything else differs."""
    a, b = ast.parse(old_src), ast.parse(new_src)
    out = []

    def strip(n):
        n2 = ast.parse(ast.unparse(n)).body[0] if False else n
        return n2

    fa = [n for n in ast.walk(a)]
    fb = [n for n in ast.walk(b)]
    if len(fa) != len(fb):
        return None
    for x, y in zip(fa, fb):
        if type(x) is not type(y):
            return None
    # find differing top-most nodes
    diffs = []

    def walk(x, y, parents):
        if ast.dump(x) == ast.dump(y):
            return
        if type(x) is not type(y):
            diffs.append((x, parents)); return
        fx = list(ast.iter_fields(x)); fy = list(ast.iter_fields(y))
        leaf = True
        for (k, vx), (_, vy) in zip(fx, fy):
            if isinstance(vx, ast.AST):
                if ast.dump(vx) != ast.dump(vy):
                    leaf = False; walk(vx, vy, parents + [(x, k)])
            elif isinstance(vx, list):
                if len(vx) != len(vy):
                    diffs.append((x, parents)); return
                for i, (ex, ey) in enumerate(zip(vx, vy)):
                    if isinstance(ex, ast.AST) and ast.dump(ex) != ast.dump(ey):
                        leaf = False; walk(ex, ey, parents + [(x, (k, i))])
                    elif not isinstance(ex, ast.AST) and ex != ey:
                        diffs.append((x, parents)); return
            elif vx != vy:
                diffs.append((x, parents)); return
    walk(a, b, [])
    for node, parents in diffs:
        # need a parent chain ... FunctionDef -> args -> defaults[i] / kw_defaults[i]
        fidx = None
        for j in range(len(parents) - 1, -1, -1):
            if isinstance(parents[j][0], (ast.FunctionDef, ast.AsyncFunctionDef)):
                fidx = j; break
        if fidx is None:
            return None
        f, key = parents[fidx]
        if key != 'args' or fidx + 1 >= len(parents):
            return None
        argsnode, k2 = parents[fidx + 1]
        if not isinstance(k2, tuple) or k2[0] not in ('defaults', 'kw_defaults'):
            return None
        i = k2[1]
        A = f.args
        if k2[0] == 'defaults':
            pos = A.posonlyargs + A.args
            p = pos[len(pos) - len(A.defaults) + i]
            idx = pos.index(p)
            out.append((f, p.arg, idx, False))
        else:
            out.append((f, A.kwonlyargs[i].arg, None, True))
    return out or None


class Parented(ast.NodeVisitor):
    pass


def sites(fname):
    """call sites and other references of `fname` over sqlglot/ + tests/:
    yields (path, stmt_first, stmt_last, kind, node, deftime)"""
    for p, tree in files().items():
        stack = []

        def visit(n, in_func, stmt):
            if isinstance(n, ast.stmt):
                stmt = n
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in n.decorator_list + n.args.defaults + [x for x in n.args.kw_defaults if x]:
                    visit(d, in_func, stmt)
                for s in n.body:
                    visit(s, True, s)
                return
            if isinstance(n, ast.Lambda):
                for d in n.args.defaults:
                    visit(d, in_func, stmt)
                visit(n.body, True, stmt)
                return
            if isinstance(n, ast.Call):
                fn = n.func
                nm = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else None
                if nm == fname:
                    yield_.append((p, stmt, 'call', n, not in_func))
                    if isinstance(fn, ast.Attribute):
                        visit(fn.value, in_func, stmt)
                    for x in n.args + [k.value for k in n.keywords]:
                        visit(x, in_func, stmt)
                    return
            if isinstance(n, ast.Attribute) and n.attr == fname or isinstance(n, ast.Name) and n.id == fname:
                yield_.append((p, stmt, 'ref', n, not in_func))
            if isinstance(n, ast.alias) and n.name.rsplit('.', 1)[-1] == fname and n.asname and n.asname != fname:
                yield_.append((p, stmt, 'alias', n, not in_func))  # `import f as g`: calls of g unseen
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value == fname:
                yield_.append((p, stmt, 'str', n, not in_func))
            for c in ast.iter_child_nodes(n):
                visit(c, in_func, stmt)
        yield_ = []
        for s in tree.body:
            visit(s, False, s)
        for y in yield_:
            yield y


def supplies(call, param, idx, kwonly):
    if any(k.arg is None for k in call.keywords) or any(isinstance(a, ast.Starred) for a in call.args):
        return False
    if any(k.arg == param for k in call.keywords):
        return True
    if kwonly or idx is None:
        return False
    # positional: idx counts `self` for methods; treat as supplied only if enough positional
    # args even for an unbound call (conservative)
    return len(call.args) >= idx + 1


def stmt_span(stmt):
    """lines of the statement's own header (compound) or whole simple statement"""
    if isinstance(stmt, (ast.If, ast.While)):
        return stmt.lineno, stmt.test.end_lineno
    if isinstance(stmt, (ast.For, ast.AsyncFor)):
        return stmt.lineno, stmt.iter.end_lineno
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        return stmt.lineno, stmt.items[-1].context_expr.end_lineno
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Try)):
        return stmt.lineno, stmt.lineno
    return stmt.lineno, stmt.end_lineno


def select(maps, path, old_src, new_src):
    """-> (tests, explanation) or None (rule does not apply)"""
    eds = default_edits(old_src, new_src)
    if not eds:
        return None
    tests = set(); why = []
    for f, param, idx, kwonly in eds:
        fname = f.name
        flev = maps.span(path, f.body[0].lineno, f.end_lineno)
        if fname.endswith('_sql') or fname.startswith('__'):
            tests |= flev; why.append(f'{fname}: dynamic dispatch -> function'); continue
        obs, esc, deft = [], [], []
        for p, stmt, kind, node, at_def in sites(fname):
            if kind == 'call':
                if supplies(node, param, idx, kwonly):
                    continue
                (deft if at_def else obs).append((p, stmt))
            else:
                (deft if at_def else esc).append((p, stmt, kind))
        if deft:
            return None  # observed at definition time: import state (not this rule)
        if esc:
            tests |= flev
            why.append(f'{fname}.{param}: {len(esc)} escaping refs -> function ({len(flev)})')
            continue
        got = set()
        for p, stmt in obs:
            a, b = stmt_span(stmt)
            got |= maps.span(p, a, b)
        # a call site can only bind f if f ran, so ∩ function-level is still sound
        tests |= got & flev
        why.append(f'{fname}.{param}: {len(obs)} default-observing call sites -> {len(got & flev)} (function {len(flev)})')
    return tests, '; '.join(why)
