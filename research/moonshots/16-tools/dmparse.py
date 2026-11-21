"""Re-parse dm/m*.out (pytest -rfE incl. SUBFAILED lines): failures outside the R-DEF rule."""
import json, re, glob, os, sys
M = sys.argv[1]
tests = json.load(open(sys.argv[2]))['tests']
num = {t['id']: t['number'] for t in tests}
tot = dict(mutants=0, failing_mutants=0, failing_tests=0, outside=0)
for out in sorted(glob.glob(f'{M}/m*.out'), key=lambda s: int(re.findall(r'm(\d+)', s)[-1])):
    tag = os.path.basename(out)[:-4]
    m = json.load(open(f'{M}/{tag}.json'))
    txt = open(out).read()
    failed = set()
    for line in txt.splitlines():
        if line.startswith(('FAILED', 'SUBFAILED', 'ERROR')):
            for t in re.findall(r'(tests/\S+?\.py::\w+::\w+)', line):
                failed.add(t)
    fn = {num.get(t, t) for t in failed}
    outside = sorted(map(str, fn - set(m['rule'])))
    tot['mutants'] += 1; tot['failing_mutants'] += bool(fn); tot['failing_tests'] += len(fn); tot['outside'] += len(outside)
    print(tag, m['func'], 'rule', len(m['rule']), 'dropped', len(m['dropped']), 'failing', len(fn),
          'OUTSIDE', outside, '|', txt.strip().splitlines()[-1][:90])
print(tot)
