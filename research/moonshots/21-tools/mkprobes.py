"""Write key-level probe patches (unified diffs vs the pinned sqlglot) into OUT/commits.

usage: python3 mkprobes.py <pinned sqlglot> <out dir>
"""
import difflib
import sys
from pathlib import Path

SG, OUT = Path(sys.argv[1]), Path(sys.argv[2])
P = [
    # tokenizer class-creation hook (_COMMENTS / _KEYWORD_TRIE / _FORMAT_STRINGS)
    ("k01", "sqlglot/tokens.py", '"{#": "#}",  # Ensure', '"{#": "#}", "{%": "%}",  # Ensure'),
    ("k02", "sqlglot/tokens.py", '"{#": "#}",  # Ensure', '"{#": "##}",  # Ensure'),
    ("k03", "sqlglot/tokens.py", 'cls._COMMENTS[cls.HINT_START] = "*/"',
     'cls._COMMENTS[cls.HINT_START] = "*/ "'),
    ("k04", "sqlglot/tokens.py", 'for p in ("n", "N")', 'for p in ("N",)'),
    # base keywords / single tokens
    ("k05", "sqlglot/tokens.py", '"DIV": TokenType.DIV,', '"DIV": TokenType.VAR,'),
    ("k06", "sqlglot/tokens.py", '"MEDIUMINT": TokenType.MEDIUMINT,',
     '"MEDIUMINT": TokenType.MEDIUMINT,\n        "ZQXJ": TokenType.SELECT,'),
    ("k07", "sqlglot/tokens.py", '"#": TokenType.HASH,', '"#": TokenType.PARAMETER,'),
    # dialect keyword tables
    ("k08", "sqlglot/dialects/mysql.py", '"CHARSET": TokenType.CHARACTER_SET,', '"CHARSET": TokenType.VAR,'),
    ("k09", "sqlglot/dialects/duckdb.py", '"//": TokenType.DIV,', '"//": TokenType.SLASH,'),
    ("k10", "sqlglot/dialects/snowflake.py", '"BYTEINT": TokenType.INT,', ''),
    ("k11", "sqlglot/dialects/postgres.py", '"~": TokenType.RLIKE,',
     '"~": TokenType.RLIKE,\n            "FOO BAR": TokenType.VAR,'),
    ("k12", "sqlglot/dialects/bigquery.py", '"BEGIN": TokenType.COMMAND,', '"BEGIN": TokenType.BEGIN,'),
    ("k13", "sqlglot/dialects/tsql.py", '"DATETIME2": TokenType.DATETIME2,', '"DATETIME2": TokenType.DATETIME,'),
    ("k14", "sqlglot/dialects/clickhouse.py", '"ATTACH": TokenType.COMMAND,', '"ATTACH": TokenType.ATTACH,'),
    ("k15", "sqlglot/dialects/teradata.py", '"BYTEINT": TokenType.TINYINT,', '"BYTEINT": TokenType.SMALLINT,'),
    # parser dispatch tables
    ("k16", "sqlglot/parsers/mysql.py", "        TokenType.LOCALTIMESTAMP: exp.Localtimestamp,\n    }",
     "        TokenType.LOCALTIMESTAMP: exp.Localtime,\n    }"),
    ("k17", "sqlglot/parsers/mysql.py", 'format=Dialect["mysql"].format_time(seq_get(args, 1)),',
     'format=Dialect["mysql"].format_time(seq_get(args, 0)),'),
    ("k19", "sqlglot/dialects/duckdb.py", '"//": TokenType.DIV,\n            "**": TokenType.DSTAR,',
     '"**": TokenType.DSTAR,\n            "//": TokenType.DIV,'),
    ("k18", "sqlglot/dialects/sqlite.py", '"ATTACH": TokenType.ATTACH,', '"ATTACH": TokenType.COMMAND,'),
]
(OUT / "commits").mkdir(parents=True, exist_ok=True)
for name, rel, old, new in P:
    text = (SG / rel).read_text()
    assert text.count(old) == 1, (name, text.count(old))
    if new == "":  # delete the whole line
        lines = text.splitlines(True)
        idx = [i for i, line in enumerate(lines) if old in line]
        assert len(idx) == 1
        out = "".join(lines[:idx[0]] + lines[idx[0] + 1:])
    else:
        out = text.replace(old, new)
    d = difflib.unified_diff(text.splitlines(True), out.splitlines(True), f"a/{rel}", f"b/{rel}")
    (OUT / "commits" / f"{name}.patch").write_text(f"diff --git a/{rel} b/{rel}\n" + "".join(d))
print(len(P))
