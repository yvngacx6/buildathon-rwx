"""Build step: download the sqlglot source tarball at the pinned commit and unpack it.

usage: python fetch_sqlglot.py <sha> <dest dir>

Retries with backoff (a transient network error at the final rebuild would mean no image and
no score) and checks the download against the recorded sha256 inside the retry loop (a
truncated body retries). The tar.gz bytes are what GitHub generated at the time we recorded
them; GitHub has recompressed archives before (2023) with the same tar content, so the
decompressed tar's sha256 is accepted as well. Neither matches after every attempt → the
build fails loudly.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import shutil
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

# sha256 for SQLGLOT_SHA eb11aec4be8cb2122afe713a4816662c3f0fb2fa (recorded 2026-11-21).
EXPECTED_GZ = "93b2445f15f395b81c20556777ece19a6c7507df950f13fa9fac4a3be739af0d"
EXPECTED_TAR = "4e82990be8a92930972718f45fd99a0ee91108013e2cced6fef90bc08ed326e3"
ATTEMPTS = 6


def fetch(sha: str) -> bytes:
    url = f"https://github.com/tobymao/sqlglot/archive/{sha}.tar.gz"
    last = "no attempt"
    for attempt in range(1, ATTEMPTS + 1):
        try:
            data = urllib.request.urlopen(url, timeout=120).read()
            gz = hashlib.sha256(data).hexdigest()
            tar = hashlib.sha256(gzip.decompress(data)).hexdigest()
            if gz == EXPECTED_GZ or tar == EXPECTED_TAR:
                print(f"sqlglot {sha}: {len(data)} bytes, sha256 ok (attempt {attempt})")
                return data
            last = f"sha256 mismatch: tar.gz {gz}, tar {tar}"
        except Exception as exc:  # noqa: BLE001  (network, truncated gzip, …: retry)
            last = f"{type(exc).__name__}: {exc}"
        print(f"attempt {attempt}/{ATTEMPTS} failed: {last}", file=sys.stderr)
        if attempt < ATTEMPTS:
            time.sleep(min(5 * 2 ** (attempt - 1), 60))
    sys.exit(f"FATAL: could not fetch a verified sqlglot {sha} tarball: {last}")


def main() -> None:
    sha, dest = sys.argv[1], Path(sys.argv[2])
    data = fetch(sha)
    tmp = dest.parent / f".sqlglot-{sha}-unpack"
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        tf.extractall(tmp, filter="data")
    shutil.move(str(tmp / f"sqlglot-{sha}"), str(dest))
    shutil.rmtree(tmp)


if __name__ == "__main__":
    main()
