"""Mutation probes: edit a fixture in a scratch sqlglot copy, predict kept readers with fxrule,
run ALL readers for real, check failing ⊆ kept."""
import gzip, json, os, re, subprocess, sys
M = os.path.dirname(os.path.abspath(__file__))
os.environ['BR_SQLGLOT'] = os.environ.get('PROBE_SQ', M + '/sq')
W = '/home/aditya/Desktop/hackathons/buildathon-rwx/.claude/worktrees/agent-ac002a98cc72f28ef'
sys.path.insert(0, M)
import fxrule
PY = '/home/aditya/rwx-research-robust/venv/bin/python'
SQ = fxrule.SQLGLOT
rd = json.load(gzip.open(W + '/maps/reads.json.gz'))
readers_of = {p: [rd['tests'][i] for i in v] for p, v in rd['map'].items()}
CONS = fxrule.build_consumers(readers_of)
H = 'tests/fixtures/optimizer/tpc-h/tpc-h.sql'
DS = 'tests/fixtures/optimizer/tpc-ds/tpc-ds.sql'
SIMP = 'tests/fixtures/optimizer/simplify.sql'
AT = 'tests/fixtures/optimizer/annotate_types.sql'


def sub1(old, new):
    def f(t):
        assert old in t, old
        return t.replace(old, new, 1)
    return f


PROBES = [
    ('tpch expected-only (quoted ident)', H, sub1('  "lineitem"."l_returnflag" AS "l_returnflag",', '  "lineitem"."l_returnflag" AS "l_returnflagx",')),
    ('tpch input edit that parses', H, sub1("date '1998-12-01' - interval '90' day", "date '1998-12-01' - interval '91' day")),
    ('tpch input edit, parse error', H, sub1('        l_returnflag,\n        l_linestatus,\n        sum(', '        l_returnflag,\n        l_linestatus,,\n        sum(')),
    ('tpch extra ; (pair shift)', H, sub1('        l_linestatus;\nSELECT', '        l_linestatus;\n;\nSELECT')),
    ('tpch # line no colon', H, sub1('select\n        l_returnflag,', '# oops\nselect\n        l_returnflag,')),
    ('tpch -- comment change', H, sub1('-- TPC-H 1\n', '-- TPC-H 1 edited\n')),
    ('tpch blank lines added', H, sub1('        l_linestatus;\nSELECT', '        l_linestatus;\n\n\nSELECT')),
    ('tpcds expected-only', DS, lambda t: t.replace('FROM "date_dim" AS "date_dim"', 'FROM "date_dim" AS "dd"', 1)),
    ('tpcds non-exec input parses', DS, sub1('SELECT s_store_name,\n               Sum(ss_net_profit)', 'SELECT s_store_name,\n               Sum(ss_net_profit + 0)')),
    ('tpcds non-exec input parse error', DS, sub1('SELECT s_store_name,\n               Sum(ss_net_profit)', 'SELECT s_store_name,\n               Sum(ss_net_profit))')),
    ('tpcds execute: true -> false', DS, sub1('# execute: true\n', '# execute: false\n')),
    ('tpcds title added', DS, sub1('-- TPC-DS 8\n--------------------------------------\n', '-- TPC-DS 8\n--------------------------------------\n# title: q8\n')),
    ('simplify title edit', SIMP, sub1('# title: postgres preserves IS NOT NULL', '# title: postgres keeps IS NOT NULL')),
    ('simplify leading-space -- line', SIMP, sub1('# title: postgres preserves IS NOT NULL', '  -- hello\n# title: postgres preserves IS NOT NULL')),
    ('annotate_types -- line', AT, lambda t: '-- top comment\n' + t),
]


def run_readers(tests):
    rec = str(SQ) + '.failrec.txt'
    if os.path.exists(rec):
        os.remove(rec)
    env = dict(os.environ, SKIP_INTEGRATION='0', FAILREC=rec, PYTHONPATH=M + '/plug')
    r = subprocess.run([PY, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '-p', 'failrec', '-n', '6', *tests],
                       cwd=SQ, capture_output=True, text=True, env=env)
    out = r.stdout + r.stderr
    failed = set(open(rec).read().split()) if os.path.exists(rec) else set()
    tail = [l for l in out.splitlines() if re.search(r'\d+ (passed|failed)', l)]
    return failed, (tail[-1] if tail else out[-300:])


def random_probes(n, seed=14):
    import random
    rnd = random.Random(seed)
    out = []
    for k in range(n):
        path = rnd.choice([H, DS])
        lines = (SQ / path).read_text().split('\n')
        while True:
            i = rnd.randrange(len(lines))
            if lines[i].strip() and not lines[i].startswith('--'):
                break
        words = lines[i].split(' ')
        j = rnd.randrange(len(words) + 1)
        tok = rnd.choice(['42', 'NULL', 'a', '0', '1', 'x', ';', '# t'])
        new = ' '.join(words[:j] + [tok] + words[j:])

        def fn(t, i=i, new=new):
            ls = t.split('\n')
            ls[i] = new
            return '\n'.join(ls)
        out.append((f'rand{k} {path.split("/")[-1]}:{i + 1} +{tok!r}', path, fn))
    return out


if sys.argv[1:2] == ['random']:
    PROBES = random_probes(int(sys.argv[2]))
    sys.argv = sys.argv[:1]

only = sys.argv[1:]
for name, path, fn in PROBES:
    if only and not any(o in name for o in only):
        continue
    p = SQ / path
    ob = p.read_bytes()
    nb = fn(ob.decode()).encode()
    rs = sorted(set(readers_of[path]))
    kept, notes = fxrule.decide(path, ob, nb, rs, CONS, True)
    p.write_bytes(nb)
    try:
        failed, tail = run_readers(rs)
    finally:
        p.write_bytes(ob)
    miss = failed - set(kept)
    print(f'{name:36s} readers {len(rs):2d} kept {len(kept):2d} failing {len(failed):2d} '
          f'{"MISS " + str(sorted(miss)) if miss else "ok"} | kept={[k.rsplit("::",1)[1] for k in kept][:4]} '
          f'failing={[k.rsplit("::",1)[1] for k in sorted(failed)][:4]} | {tail.strip()[:80]}', flush=True)
