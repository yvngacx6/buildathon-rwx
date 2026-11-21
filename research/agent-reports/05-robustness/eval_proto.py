import json, sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).parent))
from proto_select import select
tests = json.load(open(sys.argv[1]))["tests"]; ans = json.load(open(sys.argv[2]))["commits"]
num = {t["id"]: t["number"] for t in tests}; byn = {t["number"]: t for t in tests}; all_ids = set(num)
tot_miss = 0; tot_sel = 0
for pid, a in sorted(ans.items()):
    sel, why = select(pathlib.Path(sys.argv[3], pid + ".patch").read_text(), all_ids)
    seln = {num[t] for t in sel}; miss = set(a["failing"]) - seln; tot_miss += bool(miss)
    secs = sum(byn[n]["seconds"] for n in seln); tot_sel += secs
    print(f"{pid} {a.get('kind','?'):8} fail={len(a['failing']):3} sel={len(seln):4} {secs:6.1f}s MISS={[byn[n]['id'] for n in miss][:3]} {' '.join(why)[:150]}")
print("commits missed", tot_miss, "selected secs", round(tot_sel, 1))
