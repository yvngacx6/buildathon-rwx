"""Model 'practice-leader-style' selectors on our labeled sets, per kind.

N1 cover:   changed old lines in sqlglot/*.py -> tests covering them (lines map, test phase);
            pure insertions -> neighbour lines; fixtures/data -> readers; tests/*.py -> that file's tests;
            anything else -> nothing.
N2 cover+imp: N1, plus changed lines nobody covers at test time (defs, class bodies, module level)
            -> every test whose run loaded that module (imports map). New/deleted files -> importers / ALL.
N3 safe:    N2, plus syntax-invalid new file / deleted file / removed def name -> ALL.
"""
import ast, gzip, json, re, sys, collections
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from selector import pack

REPO = Path(sys.argv[1])
SQLGLOT = Path(sys.argv[2])
sets = sys.argv[3:]


def load(n):
    d = json.load(gzip.open(REPO / 'maps' / f'{n}.json.gz'))
    return d


L = load('lines'); R = load('reads'); I = load('imports'); TI = load('test_imports')


def parse(patch):
    files = []  # (old, new, removed_old_lines, added_after_old_lines, new_text_lines, deleted, created)
    cur = None
    for line in patch.splitlines():
        if line.startswith('diff --git'):
            m = re.match(r'diff --git a/(\S+) b/(\S+)', line)
            cur = dict(old=m.group(1), new=m.group(2), rem=set(), ins=set(), added=[], deleted=False, created=False)
            files.append(cur)
        elif cur is None:
            continue
        elif line.startswith('deleted file'):
            cur['deleted'] = True
        elif line.startswith('new file'):
            cur['created'] = True
        elif line.startswith('@@'):
            m = re.match(r'@@ -(\d+)(?:,(\d+))? \+(\d+)', line)
            o = int(m.group(1)); cur['o'] = o
        elif line.startswith('---') or line.startswith('+++'):
            continue
        elif 'o' in cur:
            if line.startswith('-'):
                cur['rem'].add(cur['o']); cur['o'] += 1
            elif line.startswith('+'):
                cur['ins'].add(cur['o']); cur['added'].append(line[1:])
            else:
                cur['o'] += 1
    return files


def run(setdir):
    tests = json.load(open(setdir / 'tests.json'))['tests']
    ids = {t['id']: t['number'] for t in tests}
    ms = {t['number']: pack.duration_ms(t['seconds']) for t in tests}
    by_file = collections.defaultdict(set)
    for t in tests: by_file[t['file']].add(t['number'])
    ALLN = set(ms)
    ans = json.load(open(setdir / 'answers.json'))['commits']

    def m(mp, key):
        return {ids[mp['tests'][i]] for i in mp['map'].get(key, []) if mp['tests'][i] in ids}

    out = {}
    for f in sorted((setdir / 'commits').glob('*.patch')):
        c = f.stem
        sel = {k: set() for k in ('N1', 'N2', 'N3')}
        all3 = False
        for fp in parse(f.read_text(errors='replace')):
            p = fp['old'] if not fp['created'] else fp['new']
            lines = set(fp['rem']) | {x for o in fp['ins'] for x in (o - 1, o)}
            if p.startswith('sqlglot/') and p.endswith('.py'):
                cov = set().union(*[m(L, f'{p}:{x}') for x in lines]) if lines else set()
                sel['N1'] |= cov
                uncovered = any(not L['map'].get(f'{p}:{x}') for x in fp['rem']) or not fp['rem']
                imp = m(I, p)
                n2 = cov | (imp if uncovered else set())
                if fp['created']:
                    n2 = set()
                sel['N2'] |= n2; sel['N3'] |= n2
                # N3: validity checks on new text
                try:
                    newsrc = (SQLGLOT / p).read_text() if not fp['deleted'] else ''
                except FileNotFoundError:
                    newsrc = ''
                if fp['deleted'] or fp['old'] != fp['new']:
                    all3 = True
                elif any(re.match(r'\s*(def|class)\s', a) for a in fp['added']) and fp['rem']:
                    all3 = True  # def/class header rewritten (rename) -> ALL
                if any(a.lstrip().startswith(('import ', 'from ')) for a in fp['added']):
                    all3 = True
            elif p.startswith('tests/') and p.endswith('.py'):
                t = by_file.get(p, set())
                if not t:  # helper module: importers
                    t = m(TI, p) or m(I, p) or set()
                for k in sel: sel[k] |= t
                if fp['deleted'] or fp['created'] and not t:
                    pass
            elif p.startswith('tests/') or p.startswith('sqlglot/'):
                r = m(R, p)
                for k in sel: sel[k] |= r
        if all3:
            sel['N3'] = set(ALLN)
        res = {}
        fail = set(ans[c]['failing'])
        for k, s in sel.items():
            w = pack.pack(s, ms) if s else []
            cost = pack.cost_ms(w, ms) if w else 0
            miss = bool(fail - s)
            res[k] = (cost / 1000, miss)
        out[c] = (ans[c].get('kind', '?'), res, len(fail))
    return out


for name in sets:
    d = Path(name)
    res = run(d)
    json.dump(res, open(Path(__file__).parent / f'naive_{d.name}.json', 'w'))
    agg = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, 0.0, 0]))
    for c, (kind, r, nf) in res.items():
        for k, (cost, miss) in r.items():
            a = agg[kind][k]; a[0] += 1; a[1] += cost; a[2] += miss
    print(f'== {d.name}')
    for kind, r in sorted(agg.items()):
        print(f'  {kind:12s} n={r["N1"][0]:3d} ' + '  '.join(f'{k}: {v[1]:7.1f}s {v[2]}m' for k, v in sorted(r.items())))
    tot = {k: (sum(r[k][1] for r in agg.values()), sum(r[k][2] for r in agg.values())) for k in ('N1', 'N2', 'N3')}
    print('  TOTAL', tot)
