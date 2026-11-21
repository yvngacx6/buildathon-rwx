#!/usr/bin/env python3
"""Assemble a labeled set in sample/ format from generated patches + labeling outcomes.

usage: assemble.py <gen_dir> <labels.json from parse.py> <tests.json> <dest_dir>

dest gets commits/<id>.patch, tests.json (copied unchanged), answers.json
{"commits": {id: {"failing": [...], "kind": "..."}}}. Only patches that finished labeling
cleanly are included (apply failures and runs that never reached session end are left out
and listed on stderr), so commits/ and answers.json always match.
"""
import json, shutil, sys
from pathlib import Path

gen, labels, tests, dest = Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text()), Path(sys.argv[3]), Path(sys.argv[4])
meta = json.loads((gen / "meta.json").read_text())
tmp = dest.with_name(dest.name + ".tmp")
shutil.rmtree(tmp, ignore_errors=True)
(tmp / "commits").mkdir(parents=True)
shutil.copy(tests, tmp / "tests.json")
answers, skipped = {}, []
for pid in sorted(labels):
    lab = labels[pid]
    if pid not in meta or "failing" not in lab or not lab.get("ended"):
        skipped.append((pid, lab.get("status")))
        continue
    shutil.copy(gen / "commits" / f"{pid}.patch", tmp / "commits" / f"{pid}.patch")
    answers[pid] = {"failing": lab["failing"], "kind": meta[pid]["kind"]}
(tmp / "answers.json").write_text(json.dumps({"commits": answers}, indent=1) + "\n")
shutil.rmtree(dest, ignore_errors=True)
tmp.rename(dest)
print(f"{len(answers)} labeled -> {dest}; left out: {skipped}", file=sys.stderr)
