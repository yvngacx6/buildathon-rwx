"""Case-type taxonomy of R23 residual waste on code commits (labeled) / spend (unlabeled)."""
import json, re, sys
from collections import Counter, defaultdict
sys.path.insert(0, '/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m16')
from an1 import cost, ms, SLOW, ALL, load, eff
from selector import diff

DATA = '/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m16/data'
SC = '/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad'


def patch_of(name, c):
    d = f'{SC}/{name}' if name in ('P', 'Q') else ('/home/aditya/Desktop/hackathons/buildathon-rwx/sample' if name == 'sample' else f'{DATA}/{name}')
    return open(f'{d}/commits/{c}.patch').read()


def defline(ptxt):
    """changed - line is inside a def signature (parameter default)"""
    for L in ptxt.splitlines():
        if L.startswith('-') and not L.startswith('---'):
            s = L[1:].strip()
            if s.startswith('def ') or re.match(r'^\w+\s*:\s*[^=]+=\s*\S', s) and s.endswith(','):
                return True
            if re.match(r'^\w+\s*=\s*[^=]', s) and s.endswith(',') and '(' not in s.split('=')[0]:
                return True
    return False


def case(r, ptxt):
    w = r['why']
    if r['isall'] or r['nearall']:
        if '__init_subclass__' in w:
            return 'ALL: tokenizer class-creation hook'
        return 'ALL: ' + (re.sub(r'\[.*', '', w)[:60])
    if defline(ptxt) and 'class ' not in w.split(':')[1][:8]:
        return 'default-arg / signature edit'
    if '__init_subclass__' in w:
        return 'tokenizer class-creation hook'
    if 'runs at import: +' in w and 'readers of changed import state' in w:
        return 'function runs at import, import state changed'
    if re.search(r'runs at import: \+\d', w):
        return 'function runs at import (import-time map)'
    if 'state:' in w or 'no state narrowing' in w:
        return 'class/module body: state-diff readers'
    if 'names ' in w and 'reader sites' in w:
        return 'class/module body: name readers'
    if 'definition-time' in w or 'module ' in w or 'class ' in w:
        return 'class/module body: conservative'
    if 'def ' in w:
        return 'function body'
    return 'other: ' + w[:40]


if __name__ == '__main__':
    for name in sys.argv[1:]:
        n = load(f'{name}.r23.jsonl')
        tot = Counter(); cnt = Counter(); slow = Counter(); ex = defaultdict(list); N = 0
        for k, r in n.items():
            if r['kind'] != 'code':
                continue
            N += 1
            F = set(r['failing'] or [])
            sel = eff(r)
            g = cost(sel) - cost(F)
            gs = cost(sel) - cost(sel - (SLOW - F))  # part removable by dropping wasted slow tests
            cs = case(r, patch_of(name, k))
            tot[cs] += g; cnt[cs] += 1; slow[cs] += gs
            ex[cs].append((round(g, 1), k))
        print(f'== {name}: {N} code commits, R23 {"waste" if name not in ("P","Q") else "spend"} {sum(tot.values()):.1f} s'
              f'  (x24/N per-40: {24*sum(tot.values())/N:.1f})')
        for cs, v in tot.most_common():
            print(f'  {v:7.1f}  per40 {24*v/N:5.1f}  slow-part {slow[cs]:6.1f}  n={cnt[cs]:3d}  {cs}   e.g. {sorted(ex[cs], reverse=True)[:4]}')
