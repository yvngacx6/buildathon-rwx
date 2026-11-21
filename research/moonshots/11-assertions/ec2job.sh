#!/bin/bash
# runs inside the container: /w = ~/work/m11
set -x
A=/w/research/moonshots/11-assertions/asrv.py
SQ=/shared/sqlglot
cat /w/sel/t_dial.txt <(echo) /w/sel/t_opt.txt | grep . > /w/sel/t_all.txt
if [ ! -f /w/out2/store.pkl ]; then
  mkdir -p /w/out2
  for i in 0 1; do
    python $A $SQ /shared/PQ/P x /w/out2/rec_$i.pkl --do record --tests /w/sel/t_all.txt --shard $i/2 > /w/out2/rec_$i.log 2>&1 &
  done
  wait
  python - <<'EOF'
import pickle, array
files, out = {}, {}
for i in (0, 1):
    d = pickle.load(open(f"/w/out2/rec_{i}.pkl", "rb"))
    for n, r in d.items():
        units = []
        for key, disc, sig, uc, ok, lines in r.get("units", []):
            enc = None if lines is None else array.array("I", sorted((files.setdefault(fn, len(files)) << 16) | ln for fn, ln in lines))
            units.append([tuple(key), disc, sig, uc, ok, enc])
        r = dict(r); r["units"] = units; out[n] = r
pickle.dump({"files": files, "tests": out}, open("/w/out2/store.pkl", "wb"), protocol=5)
print("store", len(out))
EOF
fi
run() {  # set data sel
  for i in 0 1; do
    python $A $SQ $2 $3 /w/out2/sl_$1_$i.json --do run --line-only-store --store /w/out2/store.pkl --shard $i/2 > /w/out2/sl_$1_$i.log 2>&1 &
  done
  wait
}
run sample /w/sample /w/sel/sample.out
run priv /shared/eval_data/private /w/sel/priv.out
run P /shared/PQ/P /w/sel/P.out
run Q /shared/PQ/Q /w/sel/Q300.out
echo ALLDONE
