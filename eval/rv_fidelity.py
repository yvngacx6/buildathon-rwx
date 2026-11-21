#!/usr/bin/env python3
"""Check runverify verdicts against labels: usage rv_fidelity.py SETDIR RUN.err RUN.out

  * pass-here / fail-in-labels: a labeled failing test that runverify dropped (= a miss);
  * fail-here / pass-in-labels: a test runverify saw fail that the labels say passes;
  * counts of verified / emptied commits and of skip / keep reasons.
"""

import collections
import json
import re
import sys
from pathlib import Path

data, err, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
tests = json.loads((data / "tests.json").read_text())["tests"]
num = {t["id"]: t["number"] for t in tests}
answers = json.loads((data / "answers.json").read_text())["commits"]
rows = {json.loads(l)["commit"]: {n for w in json.loads(l)["workers"] for n in w}
        for l in out.read_text().splitlines() if l}
LINE = re.compile(r"^\[(\S+)\] (\d+) tests.*?(runverify: [^;]*)")
reasons = collections.Counter()
verified = emptied = fail_here_pass_labels = misses = 0
for line in err.read_text().splitlines():
    m = LINE.match(line)
    if not m:
        continue
    c, note = m[1], m[3]
    kind = re.sub(r"\(.*", "", note).strip()
    if "passed" in note:
        verified += 1
        if not rows.get(c):
            emptied += 1
        for tid in re.findall(r"'([^']+)'", note):
            if num.get(tid) not in answers[c]["failing"]:
                fail_here_pass_labels += 1
                print(f"FAIL-HERE/PASS-IN-LABELS {c} {tid}")
        reasons["verified"] += 1
    else:
        reasons[re.sub(r"[\d.]+s", "Xs", note)[:70]] += 1
    lost = set(answers[c]["failing"]) - rows.get(c, set())
    if lost:
        misses += 1
        print(f"MISS {c} {sorted(lost)[:5]} :: {note}")
print(f"{data.name}: verified {verified}, emptied {emptied}, misses {misses}, "
      f"fail-here/pass-in-labels {fail_here_pass_labels}")
for r, n in reasons.most_common(12):
    print(f"   {n:4d} {r}")
