"""For each dm/m*.json mutant: apply to a fresh copy, run rule ∪ dropped (non-slow) tests,
report failures outside the rule (=> R-DEF unsound)."""
import json, os, re, shutil, subprocess, sys, glob
M = '/w/m16/dm'
SG = '/w/sg'
res = {}
for fn in sorted(glob.glob(f'{M}/m*.json'), key=lambda s: int(re.findall(r'm(\d+)', s)[-1])):
    m = json.load(open(fn))
    tag = os.path.basename(fn)[:-5]
    work = f'/w/sgm'
    if os.path.exists(work):
        shutil.rmtree(work)
    shutil.copytree(SG, work, symlinks=True)
    open(f"{work}/{m['path']}", 'w').write(m['new'])
    ids = open(f'{M}/{tag}.ids').read().split()
    p = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '-p', 'no:randomly',
                        '-rf', '-o', 'addopts=', '--no-header'] + ids, cwd=work, capture_output=True, text=True,
                       env=dict(os.environ, PYTHONHASHSEED='0', PYTHONDONTWRITEBYTECODE='1'), timeout=1500)
    failed = set(re.findall(r'^FAILED (\S+?)(?: - |$)', p.stdout, re.M))
    failed = {f.split('[')[0] for f in failed}
    tests = json.load(open('/w/repo/sample/tests.json'))['tests']
    num = {t['id']: t['number'] for t in tests}
    fnums = {num.get(f, f) for f in failed}
    outside = sorted(str(x) for x in fnums - set(m['rule']))
    res[tag] = dict(func=m['func'], path=m['path'], rule=len(m['rule']), dropped=len(m['dropped']),
                    failed=sorted(map(str, fnums)), outside_rule=outside, tail=p.stdout.strip().splitlines()[-1:])
    print(tag, json.dumps(res[tag]), flush=True)
json.dump(res, open(f'{M}/verify.json', 'w'), indent=1)
