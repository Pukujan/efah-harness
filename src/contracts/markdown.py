"""Structured reads of ``contract.md`` and the owner documents.

The contract's prose carries several lists that exist nowhere in
``contract.yaml``: the Section 14.4 walking-skeleton trace, the Section 27
evidence package, the Section 8 compiler-output bullets, and AMENDMENT-001's
impact analysis. The compiler reads them from the source document rather than
restating them, so a change to the contract changes the compiled output instead
of silently disagreeing with it (Section 1.2 authority order).
"""

from __future__ import annotations

import re

_FENCE = re.compile(r"^```")


def section(text: str, heading: str) -> str:
    """Return the body of the markdown section whose heading line matches.

    ``heading`` is matched against the heading text after the ``#`` markers.
    The section ends at the next heading of the same or higher level.
    """
    lines = text.splitlines()
    start: int | None = None
    level = 0
    for index, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if not match:
            continue
        if start is None:
            if match.group(2).strip().startswith(heading):
                start = index + 1
                level = len(match.group(1))
            continue
        if len(match.group(1)) <= level:
            return "\n".join(lines[start:index])
    if start is None:
        return ""
    return "\n".join(lines[start:])


def fenced_block(text: str, index: int = 0) -> list[str]:
    """Return the non-empty lines of the *index*-th fenced block in *text*.

    Leading whitespace is preserved: Section 5's repository layout is a tree and
    its indentation is the structure.
    """
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if _FENCE.match(line.strip()):
            if current is None:
                current = []
            else:
                blocks.append(current)
                current = None
            continue
        if current is not None:
            current.append(line.rstrip())
    if not blocks or index >= len(blocks):
        return []
    return [line for line in blocks[index] if line.strip()]


def arrow_steps(lines: list[str]) -> list[str]:
    """Normalise a ``a\\n-> b\\n-> c`` trace block into an ordered step list."""
    steps: list[str] = []
    for line in lines:
        cleaned = line.lstrip("→>-").strip()
        if cleaned:
            steps.append(cleaned)
    return steps


def bullets(text: str) -> list[str]:
    """Top-level ``- `` bullets, joined across their continuation lines."""
    collected: list[str] = []
    current: list[str] = []
    for raw in text.splitlines():
        if raw.startswith("- "):
            if current:
                collected.append(" ".join(current))
            current = [raw[2:].strip()]
        elif current and raw.startswith("  ") and raw.strip():
            current.append(raw.strip())
        elif current and not raw.strip():
            collected.append(" ".join(current))
            current = []
        elif current and not raw.startswith(" "):
            collected.append(" ".join(current))
            current = []
    if current:
        collected.append(" ".join(current))
    return collected


def numbered_items(text: str) -> list[str]:
    """Top-level ``1. `` ordered-list items, in document order."""
    items: list[str] = []
    current: list[str] = []
    for raw in text.splitlines():
        if re.match(r"^\d+\.\s", raw):
            if current:
                items.append(" ".join(current))
            current = [re.sub(r"^\d+\.\s+", "", raw).strip()]
        elif current and raw.startswith("   ") and raw.strip():
            current.append(raw.strip())
        elif current:
            items.append(" ".join(current))
            current = []
    if current:
        items.append(" ".join(current))
    return items
