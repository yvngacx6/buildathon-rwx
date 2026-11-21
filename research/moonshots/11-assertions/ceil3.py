import json, sys, re, glob, collections
W="/home/aditya/Desktop/hackathons/buildathon-rwx/.claude/worktrees/agent-a8cb1457210c6ab49"
sys.path.insert(0,W)
from selector import pack
O="/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m02"
tests={t["number"]:t for t in json.load(open(f"{W}/sample/tests.json"))["tests"]}
ms={n:pack.duration_ms(t["seconds"]) for n,t in tests.items()}
def cost(s):
    s=set(s); return pack.cost_ms(pack.pack(s,ms),ms)/1000 if s else 0.0
SLOW=3.0
def elig_map(errf):
    d={}
    for l in open(errf):
        m=re.match(r"\[(\w+)\] ",l)
        if m: d[m.group(1)]="diffexec:" in l
    return d
def grp(n):
    f=tests[n]["file"]
    return "dialects" if f.startswith("tests/dialects/") else f.split("/")[-1]
for name,ansf in [("sample",f"{W}/sample/answers.json"),("priv","/home/aditya/Desktop/hackathons/buildathon-rwx/eval/data/private/answers.json")]:
    ans={c:set(v["failing"]) for c,v in json.load(open(ansf))["commits"].items()}
    rows=[json.loads(l) for l in open(f"{O}/{name}.out")]
    el=elig_map(f"{O}/{name}.err"); k=40/len(rows)
    for only_elig in (True,False):
        g=collections.Counter(); kinds=collections.Counter(); per=collections.Counter()
        for r in rows:
            c=r["commit"]
            if only_elig and not el.get(c): continue
            sel={x for w in r["workers"] for x in w}; f=ans[c]
            full=cost(sel)
            dro={x for x in sel if tests[x]["seconds"]<SLOW and x not in f}
            per[c]=full-cost(sel-dro)
            for G in {grp(x) for x in dro}:
                g[G]+=full-cost(sel-{x for x in dro if grp(x)==G})
        print(name, "elig" if only_elig else "all", "marginal gain per group /40:", [(G,round(v*k,1)) for G,v in g.most_common(8)])
        print("   top commits:", [(c,round(v,1)) for c,v in per.most_common(8)])
