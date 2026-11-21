"""Round-trip + size + decode-CPU measurement of hcons on a diffexec recording.

usage: python analyze.py rec_exec.pkl [phase-substring ...]
"""
import collections
import lzma
import pickle
import sys
import time
import zlib

sys.setrecursionlimit(100000)
from diffexec import serial
from diffexec.build import _records
import hcons
from sqlglot.expressions.core import Expr

path = sys.argv[1]
want = sys.argv[2:] or ["tpc"]
head, it = _records(path)
tab = hcons.Table()
fails = collections.Counter()
per_fn = {}
raw_all = []
enc_all = []
t_load = t_dec = t_dig = 0.0
n_ok = n_tot = 0
bytes_ok = bytes_tot = 0
exact_bytes = 0
for key, rec in it:
    if rec["dead"]:
        continue
    uses = rec["uses"]
    hs = set()
    phases = {}
    for ph, s in uses.items():
        if isinstance(ph, tuple) and any(w in ph[1] for w in want):
            phases[ph[1].split("::")[-1]] = None if s is None else len(s)
            if s:
                hs |= s
    if not phases:
        continue
    st = per_fn.setdefault(key[1], {"phases": phases, "blobs": 0, "raw": 0, "enc": 0, "fb": 0,
                                    "load_s": 0.0, "dec_s": 0.0, "nodes0": len(tab.nodes)})
    for h in hs:
        blob = rec["blobs"][h][0]
        n_tot += 1
        bytes_tot += len(blob)
        st["blobs"] += 1
        st["raw"] += len(blob)
        raw_all.append(blob)
        t0 = time.perf_counter()
        v = serial.loads_in(blob)
        t1 = time.perf_counter()
        t_load += t1 - t0
        st["load_s"] += t1 - t0
        if serial.MADE[0]:
            pass
        try:
            d0 = serial.digest_cmp(v)
        except BaseException as e:
            fails["orig digest: " + type(e).__name__] += 1
            enc_all.append(blob); st["fb"] += 1; st["enc"] += len(blob)
            continue
        snap = (len(tab.nodes), dict(tab.ix) if False else None)
        try:
            enc = hcons.encode(tab, v, Expr)
        except hcons.NotCanonical as e:
            fails[str(e)] += 1
            enc_all.append(blob); st["fb"] += 1; st["enc"] += len(blob)
            continue
        except serial.Unrecordable as e:
            fails["unrecordable " + str(e)] += 1
            enc_all.append(blob); st["fb"] += 1; st["enc"] += len(blob)
            continue
        dec = tab._dec if hasattr(tab, "_dec") else hcons.Decoder(tab)
        tab._dec = dec
        dec.refresh()
        t2 = time.perf_counter()
        try:
            v2 = hcons.decode(dec, enc)
        except Exception as e:
            fails["decode " + type(e).__name__ + str(e)[:40]] += 1
            enc_all.append(blob); st["fb"] += 1; st["enc"] += len(blob)
            continue
        t3 = time.perf_counter()
        t_dec += t3 - t2
        st["dec_s"] += t3 - t2
        t4 = time.perf_counter()
        d1 = serial.digest_cmp(v2)
        t_dig += time.perf_counter() - t4
        if d1 != d0:
            fails["digest mismatch"] += 1
            enc_all.append(blob); st["fb"] += 1; st["enc"] += len(blob)
            continue
        if serial.dumps_in(v2) == blob:
            exact_bytes += 1
        n_ok += 1
        bytes_ok += len(blob)
        enc_all.append(enc)
        st["enc"] += len(enc)
    st["nodes1"] = len(tab.nodes)

print(f"blobs {n_tot}  round-trip ok {n_ok} ({100*n_ok/max(1,n_tot):.1f}%), bytes ok "
      f"{100*bytes_ok/max(1,bytes_tot):.1f}%, byte-identical re-pickle {exact_bytes}")
print("fallback reasons:", dict(fails))
print(f"load(raw pickle) {t_load:.2f}s  decode(hcons) {t_dec:.2f}s  digest(decoded) {t_dig:.2f}s")
raw = b"".join(raw_all)
nodes_b = pickle.dumps((tab.classes, tab.nodes, tab.sigs), protocol=5)
enc = b"".join(enc_all)
print(f"raw distinct-input pickles: {len(raw)/1e6:.1f} MB; per-blob zlib sum "
      f"{sum(len(zlib.compress(b, 6)) for b in raw_all)/1e6:.2f} MB")
t0 = time.time()
xraw = len(lzma.compress(raw, preset=6))
print(f"  solid xz of raw: {xraw/1e6:.2f} MB ({time.time()-t0:.0f}s)")
print(f"hcons: {len(tab.nodes)} nodes (refs {tab.refs}), node table pickle {len(nodes_b)/1e6:.2f} MB, "
      f"residual inputs {len(enc)/1e6:.2f} MB")
xn = len(lzma.compress(nodes_b, preset=6))
xe = len(lzma.compress(enc, preset=6))
print(f"  xz: node table {xn/1e6:.2f} MB + residual {xe/1e6:.2f} MB = {(xn+xe)/1e6:.2f} MB")
print()
print(f"{'function':55s} {'blobs':>6s} {'rawMB':>7s} {'encKB':>7s} {'fb':>4s} {'newnodes':>8s} {'load_s':>6s} {'dec_s':>6s}  phases")
for k, st in sorted(per_fn.items(), key=lambda x: -x[1]["raw"]):
    print(f"{k[:55]:55s} {st['blobs']:6d} {st['raw']/1e6:7.2f} {st['enc']/1e3:7.1f} {st['fb']:4d} "
          f"{st.get('nodes1', 0)-st['nodes0']:8d} {st['load_s']:6.2f} {st['dec_s']:6.2f}  {st['phases']}")
pickle.dump({"per_fn": per_fn}, open(path + ".m12.pkl", "wb"))
