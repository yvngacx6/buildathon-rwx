import json, os, sys
from pathlib import Path
from collections import defaultdict
W = '/home/aditya/Desktop/hackathons/buildathon-rwx/.claude/worktrees/agent-ab99fdbafdbe08bd4'
R = '/home/aditya/Desktop/hackathons/buildathon-rwx'
SC = '/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad'
sys.path.insert(0, W); sys.path.insert(0, W + '/ev' + 'al')
os.environ.setdefault("BR_SQLGLOT", str(Path.home() / "rwx-research-robust" / "sqlglot"))
import gap
from selector import pack
from selector.select import ALL_FRACTION
name, kinds = sys.argv[1], set(sys.argv[2].split(','))
if name == 'sample':
    data = Path(W) / 'sample'
elif name in ('P', 'Q'):
    data = Path(SC) / name
else:
    data = Path(R) / ('ev' + 'al') / 'data' / name
tf = data / 'tests.json'
tests = json.loads((tf if tf.exists() else Path(W) / 'sample/tests.json').read_text())["tests"]
answers = json.loads((data / "answers.json").read_text())["commits"] if (data / 'answers.json').exists() else None
meta = json.loads((data / 'meta.json').read_text()) if (data / 'meta.json').exists() else {}
ms = {t["number"]: pack.duration_ms(t["seconds"]) for t in tests}
sel = gap.TracingSelector(tests, Path(os.environ["BR_SQLGLOT"]), Path(W) / "maps")
ev = sel.all_numbers(); allc = pack.cost_ms(pack.pack(ev, ms), ms)
lim = int(sys.argv[3]) if len(sys.argv) > 3 else 10**9
out = open(f'{name}.jsonl', 'w')
done = 0
for p in sorted((data / "commits").glob("*.patch")):
    c = p.stem
    if answers is not None:
        a = answers.get(c, {"failing": [], "kind": "?"}); k = a.get('kind'); F = a['failing']
    else:
        m = meta.get(c, {}); k = m.get('kind', '?') if isinstance(m, dict) else '?'; F = None
    if 'any' not in kinds and k not in kinds:
        continue
    done += 1
    if done > lim:
        break
    sel.credits = []
    try:
        nums, why = sel.select(p.read_text(errors='replace'))
    except Exception as e:
        nums, why = ev, f'ALL (error {e})'
    nums = set(nums)
    owners = defaultdict(set)
    for lab, got in sel.credits:
        for n in got & nums:
            owners[n].add(lab)
    if nums and sel._untraced:
        for n in set(sel._untraced) & nums:
            owners[n].add('untraced')
    nearall = nums != ev and pack.cost_ms(pack.pack(nums, ms), ms) >= ALL_FRACTION * allc
    out.write(json.dumps(dict(commit=c, kind=k, failing=F, sel=sorted(nums), nearall=nearall,
                              isall=nums == ev, why=why,
                              owners={str(n): sorted(v) for n, v in owners.items()})) + '\n')
    out.flush()
