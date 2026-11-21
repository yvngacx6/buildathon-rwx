"""Evaluate the finer fixture rule on labeled sets (soundness + cost) and unlabeled sets (cost)."""
import gzip, json, os, sys, time
W = '/home/aditya/Desktop/hackathons/buildathon-rwx/.claude/worktrees/agent-ac002a98cc72f28ef'
sys.path.insert(0, W)
sys.path.insert(0, os.path.dirname(__file__))
from selector import pack, diff
import fxrule

D = '/home/aditya/Desktop/hackathons/buildathon-rwx/eval/data'
SP = '/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad'
rd = json.load(gzip.open(W + '/maps/reads.json.gz'))
rids = rd['tests']
readers_of = {p: [rids[i] for i in v] for p, v in rd['map'].items()}
CONS = fxrule.build_consumers(readers_of)
SQ = fxrule.SQLGLOT
tests = json.load(open(D + '/private/tests.json'))['tests']
NUM = {t['id']: t['number'] for t in tests}
MS = {t['number']: pack.duration_ms(t['seconds']) for t in tests}
NAME = {t['number']: t['id'].split('::', 1)[1] for t in tests}


def cost(s):
    return pack.cost_ms(pack.pack(s, MS), MS) / 1000


def run_set(label, p, labeled):
    ans = json.load(open(p + '/answers.json'))['commits'] if labeled else {}
    tot_old = tot_new = tot_or = 0.0
    n = zero = miss = 0
    fo_old = fo_new = 0.0; nfo = 0
    cpu = 0.0
    for c in sorted(os.listdir(p + '/commits')):
        fps = diff.parse(open(f'{p}/commits/{c}').read())
        fx = [f for f in fps if f.path.startswith('tests/fixtures/')]
        if not fx:
            continue
        n += 1
        only = len(fx) == len(fps)
        code_clean = all(f.path.startswith('tests/fixtures/') or not f.path.endswith('.py') for f in fps)
        old_sel, new_sel, notes = set(), set(), []
        t0 = time.process_time()
        for f in fx:
            rs = set()
            for q in {f.old_path, f.new_path} - {None}:
                rs |= set(readers_of.get(q, []))
            old_sel |= {NUM[t] for t in rs if t in NUM}
            simple = (f.old_path == f.new_path and f.path.endswith(('.sql',)) and not f.is_binary)
            if not simple:
                new_sel |= {NUM[t] for t in rs if t in NUM}; notes.append('not simple'); continue
            try:
                ob = (SQ / f.path).read_bytes()
                nb = diff.apply(ob.decode('utf-8'), f).encode('utf-8')
            except Exception as e:  # noqa: BLE001
                new_sel |= {NUM[t] for t in rs if t in NUM}; notes.append(f'apply {e!r}'); continue
            kept, nt = fxrule.decide(f.path, ob, nb, sorted(rs), CONS, code_clean)
            notes += nt
            new_sel |= {NUM[t] for t in kept if t in NUM}
        cpu += time.process_time() - t0
        co, cn = cost(old_sel), cost(new_sel)
        tot_old += co; tot_new += cn
        if only:
            nfo += 1; fo_old += co; fo_new += cn
            if not new_sel:
                zero += 1
        line = f'{label[:7]} {c[:-6]:24s} {"only" if only else "mix "} old {co:5.1f} new {cn:5.1f}'
        if labeled:
            fail = set(ans[c[:-6]]['failing'])
            # failing tests that are fixture readers must stay selected (others belong to the code part)
            need = fail & old_sel
            m = need - new_sel
            orc = cost(fail & old_sel)
            tot_or += orc
            line += f' oracle(readers∩fail) {orc:5.1f}' + (f' MISS {[NAME[x] for x in m]}' if m else '')
            miss += bool(m)
        if co != cn or (labeled and m):
            print(line, [NAME[x] for x in sorted(new_sel)][:4], notes[:2])
    print(f'== {label}: fixture commits {n}, fixture-only {nfo}; cost old {tot_old:.1f} new {tot_new:.1f}'
          f' (fixture-only {fo_old:.1f} → {fo_new:.1f}); fixture-only → [] {zero}; misses {miss}; '
          f'oracle {tot_or:.1f}; rule CPU {cpu:.2f}s')


for s in sys.argv[1:]:
    if s in ('P', 'Q'):
        run_set(s, f'{SP}/{s}', False)
    elif s == 'sample':
        run_set(s, W + '/sample', True)
    else:
        run_set(s, f'{D}/{s}', True)
