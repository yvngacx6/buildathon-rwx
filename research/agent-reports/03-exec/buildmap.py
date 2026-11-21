# merge xdist coverage outputs into compact map: test index sets per (file,line) and per opened file
import json, sys, glob, collections, pickle
tests = json.load(open(sys.argv[1]))["tests"]
idx = {t["id"]: t["number"] for t in tests}
line_map = collections.defaultdict(set); import_lines = collections.defaultdict(set)
open_map = collections.defaultdict(set); class_users = collections.defaultdict(set)
cls_members = collections.defaultdict(set)
for t in tests:
    cls_members[t["id"].rsplit("::", 1)[0]].add(t["number"])
for fn in glob.glob(sys.argv[2] + "/cov_gw*.json"):
    d = json.load(open(fn))
    for k, (lines, opens, mro) in d.items():
        if "|" not in k:
            for f, l in lines: import_lines[f].add(l)
            continue
        phase, nodeid = k.split("|", 1)
        if nodeid not in idx: continue
        tgt = {idx[nodeid]} if phase == "CALL" else cls_members[nodeid.rsplit("::", 1)[0]]
        for f, l in lines: line_map[(f, l)] |= tgt
        for f in opens: open_map[f] |= tgt
        for c in mro: class_users[c] |= tgt
pickle.dump(dict(line_map=dict(line_map), import_lines=dict(import_lines), open_map=dict(open_map), class_users=dict(class_users)), open(sys.argv[3], "wb"))
print(len(line_map), len(open_map), sum(len(v) for v in import_lines.values()))
