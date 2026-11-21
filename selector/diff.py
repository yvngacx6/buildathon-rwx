"""Parse unified diffs and apply them in pure Python.

The run image has no `git` and no `patch`, so we apply hunks ourselves.
Any hunk that does not apply raises `PatchError`; the caller then runs ALL tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchError(Exception):
    """The patch is malformed or does not apply to the pinned source."""


@dataclass
class Hunk:
    old_start: int  # 1-based line number in the old file
    lines: list[str] = field(default_factory=list)  # each starts with ' ', '-' or '+'


@dataclass
class FilePatch:
    old_path: str | None  # None when the file is new
    new_path: str | None  # None when the file is deleted
    hunks: list[Hunk] = field(default_factory=list)
    is_binary: bool = False

    @property
    def path(self) -> str:
        """The repo-relative path this patch is about (new path wins)."""
        return self.new_path or self.old_path or ""


def _strip_prefix(raw: str) -> str | None:
    raw = raw.split("\t", 1)[0].strip()
    if raw == "/dev/null":
        return None
    if raw.startswith(("a/", "b/")):
        return raw[2:]
    return raw


def parse(text: str) -> list[FilePatch]:
    """Split a unified diff into one FilePatch per changed file."""
    patches: list[FilePatch] = []
    current: FilePatch | None = None
    hunk: Hunk | None = None
    pending_old: str | None = None
    for line in text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            current, hunk, pending_old = None, None, None
            continue
        if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            m = re.match(r"Binary files (\S+) and (\S+) differ", line)
            fp = FilePatch(_strip_prefix(m.group(1)) if m else None,
                           _strip_prefix(m.group(2)) if m else None, is_binary=True)
            patches.append(fp)
            current, hunk = None, None
            continue
        if line.startswith("--- ") and hunk_done(hunk):
            pending_old = line[4:]
            continue
        if line.startswith("+++ ") and pending_old is not None:
            current = FilePatch(_strip_prefix(pending_old), _strip_prefix(line[4:]))
            patches.append(current)
            pending_old, hunk = None, None
            continue
        m = _HUNK_HEADER.match(line)
        if m and current is not None:
            hunk = Hunk(old_start=int(m.group(1)))
            hunk.old_len = int(m.group(2)) if m.group(2) is not None else 1  # type: ignore[attr-defined]
            hunk.new_len = int(m.group(4)) if m.group(4) is not None else 1  # type: ignore[attr-defined]
            current.hunks.append(hunk)
            continue
        if hunk is not None and line[:1] in (" ", "-", "+"):
            hunk.lines.append(line)
        elif hunk is not None and line.startswith("\\"):
            # "\ No newline at end of file": strip the newline from the previous line.
            if hunk.lines:
                hunk.lines[-1] = hunk.lines[-1].rstrip("\n")
    return patches


def hunk_done(hunk: Hunk | None) -> bool:
    """True when a '--- ' line cannot belong to the current hunk body."""
    if hunk is None:
        return True
    old_seen = sum(1 for l in hunk.lines if l[:1] in (" ", "-"))
    new_seen = sum(1 for l in hunk.lines if l[:1] in (" ", "+"))
    return old_seen >= getattr(hunk, "old_len", 0) and new_seen >= getattr(hunk, "new_len", 0)


def apply(old_text: str, patch: FilePatch) -> str:
    """Return the new file text. Raise PatchError if any hunk does not match."""
    old = old_text.splitlines(keepends=True)
    out: list[str] = []
    cursor = 0  # index into `old` of the next line not yet copied
    for hunk in patch.hunks:
        before = [l[1:] for l in hunk.lines if l[:1] in (" ", "-")]
        after = [l[1:] for l in hunk.lines if l[:1] in (" ", "+")]
        start = _find(old, before, max(hunk.old_start - 1, 0), cursor)
        out.extend(old[cursor:start])
        out.extend(after)
        cursor = start + len(before)
    out.extend(old[cursor:])
    return "".join(out)


def _find(old: list[str], block: list[str], hint: int, floor: int) -> int:
    """Find `block` in `old` at or after `floor`, nearest to `hint`."""
    if not block:
        return max(min(hint, len(old)), floor)
    norm = [b.rstrip("\r\n") for b in block]

    def matches(i: int) -> bool:
        return all(old[i + k].rstrip("\r\n") == norm[k] for k in range(len(block)))

    for delta in range(0, len(old) + 1):
        for i in (hint + delta, hint - delta):
            if floor <= i <= len(old) - len(block) and matches(i):
                return i
    raise PatchError(f"hunk at line {hint + 1} does not apply")
