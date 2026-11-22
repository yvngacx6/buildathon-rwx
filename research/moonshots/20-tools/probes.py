"""Tokenizer-setting probes: make patches, label them with the full suite.

  python3 probes.py make  SQLGLOT OUTDIR       # OUTDIR/commits/*.patch + tests.json copy
  python3 probes.py label SQLGLOT OUTDIR NAME  # (in the pinned image) full suite on one probe
  python3 probes.py answers OUTDIR             # OUTDIR/answers.json from OUTDIR/labels/*.log
"""
import difflib, json, re, shutil, subprocess, sys
from pathlib import Path

D = "sqlglot/dialects/"
PROBES = {  # name: (file, old line, new line)
    "t01_base_nested_comments": ("sqlglot/tokens.py", '    NESTED_COMMENTS = True', '    NESTED_COMMENTS = False'),
    "t02_base_drop_unknown": ("sqlglot/tokens.py", '    DROP_UNKNOWN_ESCAPES = False', '    DROP_UNKNOWN_ESCAPES = True'),
    "t03_base_raw_escapes": ("sqlglot/tokens.py", '    STRING_ESCAPES_ALLOWED_IN_RAW_STRINGS = True', '    STRING_ESCAPES_ALLOWED_IN_RAW_STRINGS = False'),
    "t04_base_newline_only": ("sqlglot/tokens.py", '    COMMENTS_TERMINATE_AT_NEWLINE_ONLY = False', '    COMMENTS_TERMINATE_AT_NEWLINE_ONLY = True'),
    "t05_base_hint_start": ("sqlglot/tokens.py", '    HINT_START = "/*+"', '    HINT_START = "/*!"'),
    "t06_base_identifiers": ("sqlglot/tokens.py", """    IDENTIFIERS: t.ClassVar[list[str | tuple[str, str]]] = ['"']""", """    IDENTIFIERS: t.ClassVar[list[str | tuple[str, str]]] = ['"', "`"]"""),
    "t07_pg_bit_strings": (D + "postgres.py", """        BIT_STRINGS = [("b'", "'"), ("B'", "'")]""", """        BIT_STRINGS = [("b'", "'")]"""),
    "t08_pg_hex_strings_empty": (D + "postgres.py", """        HEX_STRINGS = [("x'", "'"), ("X'", "'")]""", """        HEX_STRINGS = []"""),
    "t09_mysql_drop_unknown": (D + "mysql.py", '        DROP_UNKNOWN_ESCAPES = True', '        DROP_UNKNOWN_ESCAPES = False'),
    "t10_mysql_nested": (D + "mysql.py", '        NESTED_COMMENTS = False', '        NESTED_COMMENTS = True'),
    "t11_hive_octal": (D + "hive.py", '        NUMERIC_ESCAPES = {"u": (16, 4, 4, 0xFFFF), "0": (8, 3, 3, 0x7F)}', '        NUMERIC_ESCAPES = {"u": (16, 4, 4, 0xFFFF), "0": (8, 1, 3, 0x7F)}'),
    "t12_ch_hex_min": (D + "clickhouse.py", '        NUMERIC_ESCAPES = {"x": (16, 2, 2, 0xFF)}', '        NUMERIC_ESCAPES = {"x": (16, 1, 2, 0xFF)}'),
    "t13_sf_nested": (D + "snowflake.py", '        NESTED_COMMENTS = False', '        NESTED_COMMENTS = True'),
    "t14_sqlite_hex": (D + "sqlite.py", """        HEX_STRINGS = [("x'", "'"), ("X'", "'"), ("0x", ""), ("0X", "")]""", """        HEX_STRINGS = [("x'", "'"), ("X'", "'"), ("0x", "")]"""),
    "t15_materialize_U": (D + "materialize.py", '        NUMERIC_ESCAPES = {"u": (16, 4, 4, 0xFFFF), "U": (16, 8, 8, 0x10FFFF)}', '        NUMERIC_ESCAPES = {"u": (16, 4, 4, 0xFFFF), "U": (16, 6, 8, 0x10FFFF)}'),
    "t16_ch_ident_escapes": (D + "clickhouse.py", '        IDENTIFIER_ESCAPES = ["\\\\"]', '        IDENTIFIER_ESCAPES = []'),
    "t17_mysql_quotes": (D + "mysql.py", """        QUOTES = ["'", '"']""", """        QUOTES = ["'"]"""),
    "t18_kw_release": ("sqlglot/tokens.py", '        "RELEASE": TokenType.COMMAND,', '        "RELEASE": TokenType.VAR,'),
    "t19_dialect_underscore": (D + "dialect.py", '    NUMBERS_CAN_BE_UNDERSCORE_SEPARATED = False', '    NUMBERS_CAN_BE_UNDERSCORE_SEPARATED = True'),
    "t20_ch_bit_strings_empty": (D + "clickhouse.py", '        BIT_STRINGS = [("0b", "")]', '        BIT_STRINGS = []'),
    "t21_sf_hex_drop_lower": (D + "snowflake.py", """        HEX_STRINGS = [("x'", "'"), ("X'", "'")]""", """        HEX_STRINGS = [("X'", "'")]"""),
    "t22_dax_comments": (D + "dax.py", '        COMMENTS = ["--", "//", ("/*", "*/")]', '        COMMENTS = ["--", ("/*", "*/")]'),
}


def edit(root: Path, name: str) -> tuple[str, str, str]:
    f, old, new = PROBES[name]
    text = (root / f).read_text()
    assert text.count(old + "\n") == 1, (name, text.count(old + "\n"))
    return f, text, text.replace(old + "\n", new + "\n")


def make(sq: Path, out: Path) -> None:
    (out / "commits").mkdir(parents=True, exist_ok=True)
    for name in PROBES:
        f, a, b = edit(sq, name)
        body = "".join(difflib.unified_diff(a.splitlines(True), b.splitlines(True),
                                            f"a/{f}", f"b/{f}", n=3))
        (out / "commits" / f"{name}.patch").write_text(f"diff --git a/{f} b/{f}\n" + body)


def label(sq: Path, out: Path, name: str) -> None:
    w = Path(f"/tmp/lab_{name}")
    shutil.rmtree(w, ignore_errors=True)
    shutil.copytree(sq, w, symlinks=True)
    f, _, b = edit(w, name)
    (w / f).write_text(b)
    (out / "labels").mkdir(exist_ok=True)
    log = out / "labels" / f"{name}.log"
    env = {"SKIP_INTEGRATION": "0", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(w),
           "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "PYTHONHASHSEED": "0"}
    with open(log, "w") as fh:
        subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rfE",
                        "--continue-on-collection-errors", "tests/"], cwd=w, env=env,
                       stdout=fh, stderr=subprocess.STDOUT, timeout=3000)
    shutil.rmtree(w, ignore_errors=True)


def answers(out: Path) -> None:
    tests = json.loads((out / "tests.json").read_text())["tests"]
    by_id = {t["id"]: t["number"] for t in tests}
    by_file: dict[str, list[int]] = {}
    for t in tests:
        by_file.setdefault(t["file"], []).append(t["number"])
    commits = {}
    for log in sorted((out / "labels").glob("*.log")):
        text = log.read_text(errors="replace")
        if not re.search(r"^=+ .*(passed|failed|error).* =+$", text, re.M):
            print("INCOMPLETE", log.stem)
            continue
        failing = set()
        for m in re.finditer(r"^(FAILED|ERROR|SUBFAIL) (\S+)", text, re.M):
            tid = m.group(2).split("[")[0]
            if tid in by_id:
                failing.add(by_id[tid])
            elif "::" not in tid and tid in by_file:  # collection error: the whole file
                failing |= set(by_file[tid])
            else:
                hits = [n for i, n in by_id.items() if i.startswith(tid)]
                if not hits:
                    print("UNMAPPED", log.stem, tid)
                failing |= set(hits)
        commits[log.stem] = {"failing": sorted(failing), "kind": "code"}
        print(log.stem, len(failing), text.strip().splitlines()[-1][:120])
    (out / "answers.json").write_text(json.dumps({"commits": commits}, indent=1))


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "make":
        make(Path(sys.argv[2]), Path(sys.argv[3]))
    elif cmd == "label":
        label(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
    else:
        answers(Path(sys.argv[2]))
