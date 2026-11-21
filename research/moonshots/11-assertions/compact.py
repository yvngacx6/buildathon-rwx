"""Compact the raw unit stores: lines -> array('I') of (file_idx << 16 | line)."""
import pickle, glob, sys, array, zlib, lzma
M = "/tmp/claude-1000/-home-aditya-Desktop/917d9e69-42b9-4bef-9e78-d244939af294/scratchpad/m11"
files = {}
out = {}
for f in sorted(glob.glob(f"{M}/rec_dial_*.pkl") + glob.glob(f"{M}/rec_opt_*.pkl")):
    d = pickle.load(open(f, "rb"))
    for n, r in d.items():
        units = []
        for key, disc, sig, uc, ok, lines in r.get("units", []):
            enc = None
            if lines is not None:
                enc = array.array("I", sorted((files.setdefault(fn, len(files)) << 16) | ln for fn, ln in lines))
            units.append([tuple(key), disc, sig, uc, ok, enc])
        r = dict(r)
        r["units"] = units
        out[n] = r
    del d
store = {"files": files, "tests": out}
raw = pickle.dumps(store, protocol=5)
open(f"{M}/store.pkl", "wb").write(raw)
print("files", len(files), "tests", len(out), "pickle MB", len(raw) / 1e6, "lzma MB", len(lzma.compress(raw, preset=6)) / 1e6)
