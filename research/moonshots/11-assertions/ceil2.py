import json, sys, re, glob
W="/home/aditya/Desktop/hackathons/buildathon-rwx/.claude/worktrees/agent-a8cb1457210c6ab49"
sys.path.insert(0,W)
from selector import pack
O="/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m02"
M="/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m11"
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
def dial(n): return tests[n]["file"].startswith("tests/dialects/")
def run(name, rows, fail, elig):
    k=40/len(rows); res={}
    for lab,pred in [("all cheap, any commit",lambda c,n:True),("all cheap, func-body commits",lambda c,n:elig.get(c,False)),
                     ("dialect tests, func-body commits",lambda c,n:elig.get(c,False) and dial(n))]:
        T=Tc=0; z=0
        for r in rows:
            c=r["commit"]
            if c not in fail: continue
            sel={x for w in r["workers"] for x in w}; f=fail[c]
            keep={x for x in sel if tests[x]["seconds"]>=SLOW or x in f or not pred(c,x)}
            T+=cost(sel); Tc+=cost(keep)
            if sel and not keep: z+=1
        print(f"  {name} [{lab}]: gain {(T-Tc)*k:.1f}/40 of {T*k:.1f}; ->[] {z*k:.1f}/40")
for name,ansf in [("sample",f"{W}/sample/answers.json"),("priv","/home/aditya/Desktop/hackathons/buildathon-rwx/eval/data/private/answers.json")]:
    ans={c:set(v["failing"]) for c,v in json.load(open(ansf))["commits"].items()}
    rows=[json.loads(l) for l in open(f"{O}/{name}.out")]
    el=elig_map(f"{O}/{name}.err")
    print(name, "func-body (diffexec) commits:", sum(el.values()), "/", len(rows))
    run(name, rows, ans, el)
# P: oracle verdicts where available
runs={}
for f in glob.glob(f"{O}/or_P_*.json")+glob.glob(f"{M}/orP10_*.json"):
    try: runs.update(json.load(open(f))["commits"])
    except Exception: pass
rows=[json.loads(l) for l in open(f"{O}/P.out")]
el=elig_map(f"{O}/P.err")
fail={c:set(v["failed"])|set(v.get("untrusted",[])) for c,v in runs.items() if "failed" in v}
print("P oracled", len(fail), "of", len(rows), "; func-body", sum(el.values()))
rows2=[r for r in rows if r["commit"] in fail]
k=40/len(rows)
# report per 600-commit normalisation (unoracled commits counted as zero gain)
def runP(lab,pred):
    T=Tc=0; z=0
    for r in rows2:
        c=r["commit"]; sel={x for w in r["workers"] for x in w}; f=fail[c]
        keep={x for x in sel if tests[x]["seconds"]>=SLOW or x in f or not pred(c,x)}
        T+=cost(sel); Tc+=cost(keep); z+= bool(sel and not keep)
    print(f"  P [{lab}]: gain {(T-Tc)*k:.1f}/40 (over all 600); ->[] {z*k:.1f}/40")
runP("all cheap, any",lambda c,n:True)
runP("all cheap, func-body",lambda c,n:el.get(c,False))
runP("dialect, func-body",lambda c,n:el.get(c,False) and dial(n))
