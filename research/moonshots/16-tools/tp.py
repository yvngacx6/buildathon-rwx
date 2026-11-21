import sys, json
sys.path.insert(0, '/home/aditya/Desktop/hackathons/buildathon-rwx/.claude/worktrees/agent-ab99fdbafdbe08bd4')
sys.path.insert(0, '/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m16')
from selector import diff
import idiff
SG = '/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m16/sg'


def spec(patch_text):
    res = {}
    for fp in diff.parse(patch_text):
        p = fp.path
        if not (p.startswith('sqlglot/') and p.endswith('.py')):
            continue
        old = open(f'{SG}/{p}').read()
        new = diff.apply(old, fp)
        try:
            res[p] = {f'{q}|{l}': (v if v == 'WHOLE' else sorted(v))
                      for (q, l), v in idiff.diff_source(old, new, f'{SG}/{p}').items()}
        except SyntaxError:
            res[p] = 'SYNTAX'
    return res


if __name__ == '__main__':
    for f in sys.argv[1:]:
        print(f.rsplit('/', 1)[-1], json.dumps(spec(open(f).read())))
