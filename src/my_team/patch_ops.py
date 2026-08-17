"""Unified-diff patch parsing and application (v0.7.0 P1-3, apply_patch).

A minimal, STRICT unified-diff engine for apply_patch:

- Format validation: file headers (--- / +++, /dev/null for new or
  deleted files), @@ hunk headers with counts, hunk body lines
  (context ' ', add '+', remove '-'). Malformed input raises PatchError.
- Conflict detection: context and removed lines must match the target
  content EXACTLY at the hunk position (no fuzz, deterministic).
  A mismatch raises PatchError(conflict=True).
- Application: produces the new full content from the original.

Files are treated as line sequences; the result is '\n'-joined
(no trailing newline). This normalization is acceptable for v0.7.0 —
apply_patch is an editing tool, not a byte-exact archival tool.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HUNK_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"
)


class PatchError(ValueError):
    """Patch format or application error.

    `conflict=True` marks a context-mismatch (the patch does not apply
    to the current content); False is a format error.
    """

    def __init__(self, message: str, conflict: bool = False) -> None:
        super().__init__(message)
        self.conflict = conflict


@dataclass(frozen=True)
class PatchHunk:
    """A parsed hunk: position in old/new content + body lines."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[str, ...]  # each with prefix ' ', '+', or '-'

    @property
    def is_new_file(self) -> bool:
        return self.old_start == 0 and self.old_count == 0


def parse_unified_patch(patch_text: str) -> list[PatchHunk]:
    """Parse unified diff text into hunks. Raises PatchError on any
    format violation."""
    if not patch_text or not patch_text.strip():
        raise PatchError("empty patch")
    lines = patch_text.splitlines()
    hunks: list[PatchHunk] = []
    i = 0

    # Skip file headers (--- / +++ / diff --git / index lines).
    while i < len(lines) and (
        lines[i].startswith(("--- ", "+++ ", "diff --git ", "index "))
    ):
        i += 1

    while i < len(lines):
        line = lines[i]
        if line.startswith("@@"):
            m = _HUNK_RE.match(line)
            if not m:
                raise PatchError(f"malformed hunk header: {line!r}")
            old_start = int(m.group(1))
            old_count = int(m.group(2) or "1")
            new_start = int(m.group(3))
            new_count = int(m.group(4) or "1")
            i += 1
            # Hunk body: context lines count toward BOTH old and new;
            # removed only old, added only new. Read until both counts
            # are satisfied.
            body: list[str] = []
            old_seen = new_seen = 0
            while old_seen < old_count or new_seen < new_count:
                if i >= len(lines):
                    raise PatchError("unexpected end of hunk")
                prefix = lines[i][:1]
                if prefix not in (" ", "+", "-"):
                    raise PatchError(
                        f"malformed hunk line: {lines[i]!r}"
                    )
                if prefix == " ":
                    old_seen += 1
                    new_seen += 1
                elif prefix == "-":
                    old_seen += 1
                    if old_seen > old_count:
                        raise PatchError(
                            f"hunk body exceeds old_count ({old_count})"
                        )
                else:
                    new_seen += 1
                    if new_seen > new_count:
                        raise PatchError(
                            f"hunk body exceeds new_count ({new_count})"
                        )
                body.append(lines[i])
                i += 1
            hunks.append(PatchHunk(
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
                lines=tuple(body),
            ))
        else:
            i += 1  # tolerate stray blank lines between hunks

    if not hunks:
        raise PatchError("no hunks found")
    return hunks


def apply_patch(content: str, patch_text: str) -> str:
    """Apply a unified diff to content, returning the new content.

    Raises PatchError(format) for malformed input and
    PatchError(conflict=True) when context lines do not match the
    current content at the hunk position.
    """
    hunks = parse_unified_patch(patch_text)
    old_lines = content.splitlines()
    out: list[str] = []
    cursor = 0  # index into old_lines already consumed

    for hunk in hunks:
        if hunk.is_new_file:
            if content.strip():
                raise PatchError(
                    f"hunk {hunk} claims a new file but content exists",
                    conflict=True,
                )
            if any(l[0] in (" ", "-") for l in hunk.lines):
                raise PatchError(
                    "new-file hunk contains context/removed lines"
                )
            added = [l[1:] for l in hunk.lines if l[0] == "+"]
            out = added
            continue

        pos = hunk.old_start - 1
        if pos < cursor:
            raise PatchError(
                f"hunks overlap or out of order (hunk at {pos + 1}, "
                f"already consumed through {cursor})"
            )

        # Strict context match: context + removed lines must equal the
        # current content at position.
        idx = pos
        for l in hunk.lines:
            if l[0] in ("-", " "):
                expected = l[1:]
                actual = old_lines[idx] if idx < len(old_lines) else None
                if actual != expected:
                    raise PatchError(
                        f"conflict at hunk {hunk.old_start} (line "
                        f"{idx + 1}): expected {expected!r}, found "
                        f"{actual!r}",
                        conflict=True,
                    )
                idx += 1

        out.extend(old_lines[cursor:pos])
        out.extend(l[1:] for l in hunk.lines if l[0] in (" ", "+"))
        cursor = idx

    out.extend(old_lines[cursor:])
    return "\n".join(out)
