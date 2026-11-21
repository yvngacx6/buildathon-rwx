"""Parse label outputs into failing test numbers and evaluate proto_select on the private validation set.
usage: eval_val.py tests.json labels_dir patch_dir"""
import json, re, sys, pathlib, glob; sys.path.insert(0, str(pathlib.Path(__file__).parent))
from proto_select import select
tests = json.load(open(sys.argv[1]))["tests"]; num = {t["id"]: t["number"] for t in tests}; all_ids = set(num)
sec = {t["id"]: t["seconds"] for t in tests}
for lab in sorted(glob.glob(sys.argv[2] + "/*.txt")):
    name = pathlib.Path(lab).stem; txt = open(lab).read(); fail = set()
    for l in txt.splitlines():
        if re.match(r"(FAILED|ERROR|SUBFAILED)", l):
            m = re.search(r"(tests/\S+?\.py)(::\S+)?", l)
            if not m: continue
            nid = re.sub(r"\[.*$", "", m.group(0))
            if "::" in nid: fail |= {t for t in all_ids if t == nid or t.startswith(nid + "::")}
            else: fail |= {t for t in all_ids if t.startswith(nid + "::")}
    tail = txt.strip().splitlines()[-1] if txt.strip() else "EMPTY"
    sel, why = select(open(f"{sys.argv[3]}/{name}.patch").read(), all_ids)
    miss = fail - sel
    print(f"{name:18} fail={len(fail):4} sel={len(sel):4} ({sum(sec[t] for t in sel):5.1f}s) MISS={len(miss)} {sorted(miss)[:2]} | {' '.join(why)[:110]} | {tail[-60:]}")
