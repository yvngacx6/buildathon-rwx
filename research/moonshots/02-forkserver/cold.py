import json, sys
O = "/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m02"
run, selout = sys.argv[1], sys.argv[2]
base = {int(k): v for k, v in json.load(open(f"{O}/base.json"))["commits"]["__base__"]["times"].items()}
tests = {t["number"]: t for t in json.load(open('/home/aditya/Desktop/hackathons/buildathon-rwx/sample/tests.json'))["tests"]}
sel = {}
for l in open(f"{O}/{selout}"):
    r = json.loads(l)
    sel[r["commit"]] = [n for w in r["workers"] for n in w if tests[n]["seconds"] < 3]
j = json.load(open(f"{O}/{run}"))["commits"]
tc = tb = tr = 0
for c, v in sorted(j.items()):
    if "child_cpu" not in v or not sel.get(c):
        continue
    b = sum(base.get(n, 0) for n in sel[c])
    r = sum(tests[n]["seconds"] for n in sel[c])
    tc += v["child_cpu"]; tb += b; tr += r
    print(f"{c} {v.get('how', v.get('fallback'))[:20]:20s} child={v['child_cpu']:6.2f} warm-base={b:6.2f} rec={r:6.2f}")
print(f"TOTAL child={tc:.2f} warm-base={tb:.2f} rec={tr:.2f}")
