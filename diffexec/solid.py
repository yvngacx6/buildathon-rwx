"""Solid packing of the diffexec store (repo size) and its expansion at image build time.

The run-time store (diffexec/build.py's zip: index.pkl + one c/<n>.pkl per function) compresses
every entry on its own, so the inputs that many functions share (the same token lists, trees and
SQL strings, passed down through the parser and generator) are paid once per function. In the
repo the store is instead ONE xz stream of all entries (functions in source order, big dictionary), which is
about 1.5x smaller for the same content. The Docker build (free CPU) expands it back into the
run-time zip, so the run reads exactly what it read before:

    python -m diffexec.solid pack   <store .zip> <out .xz>
    python -m diffexec.solid unpack <in .xz>     <store .zip>

Format: xz( pickle({"names": [...], "sizes": [...]}) + entry bytes in that order ).
"""

from __future__ import annotations

import io
import lzma
import os
import pickle
import sys
import zipfile

# 192 MiB dictionary (compressor ~2 GB RAM, decompressor 192 MiB).
FILTERS = [{"id": lzma.FILTER_LZMA2, "preset": 6, "dict_size": 192 << 20}]


def pack(zip_path: str, out: str) -> int:
    with zipfile.ZipFile(zip_path) as zf:
        index = pickle.loads(zf.read("index.pkl"))
        # functions of one module next to each other (they see the same inputs)
        order = {f"c/{n}.pkl": key for key, (_, n) in index["functions"].items() if n}
        names = sorted(zf.namelist(), key=lambda nm: (nm in order, order.get(nm, ()), nm))
        datas = [zf.read(n) for n in names]
    head = pickle.dumps({"names": names, "sizes": [len(d) for d in datas]}, protocol=5)
    comp = lzma.LZMACompressor(format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC64, filters=FILTERS)
    tmp = out + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(comp.compress(len(head).to_bytes(8, "big") + head))
        for d in datas:
            fh.write(comp.compress(d))
        fh.write(comp.flush())
    os.replace(tmp, out)
    return os.path.getsize(out)


def unpack(xz_path: str, zip_path: str) -> int:
    with lzma.open(xz_path, "rb") as fh:
        n = int.from_bytes(fh.read(8), "big")
        head = pickle.loads(fh.read(n))
        tmp = zip_path + ".tmp"
        with zipfile.ZipFile(tmp, "w") as zf:
            for name, size in zip(head["names"], head["sizes"]):
                data = fh.read(size)
                if len(data) != size:
                    raise ValueError(f"{xz_path}: truncated at {name}")
                info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                zf.writestr(info, data, compresslevel=1)
        if fh.read(1):
            raise ValueError(f"{xz_path}: trailing data")
    os.replace(tmp, zip_path)
    return len(head["names"])


if __name__ == "__main__":
    cmd, src, dst = sys.argv[1:4]
    print({"pack": pack, "unpack": unpack}[cmd](src, dst))
