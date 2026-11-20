"""Which library template did the user pick? Pure, no I/O (system-design § 9)."""

import re

from doc_intel.chat.router import ChatMessage
from doc_intel.chat.summary import md
from doc_intel.db.models import LibraryTemplate


def _last(messages: list[ChatMessage], role: str) -> str:
    return next((m.content for m in reversed(messages) if m.role == role), "")


def library_choice(
    library: list[LibraryTemplate], messages: list[ChatMessage], *, by_name: bool = True
) -> LibraryTemplate | None:
    """A template named in the last user message (the longest name wins, a tie picks nothing), else
    the one a number reply selects from the numbered list the assistant showed last.

    The number is resolved through the rendered line, so the list's order or filtering cannot
    shift it. `by_name=False` skips the name match (a confirmed uploaded template wins over it).
    """
    last = _last(messages, "user")
    named = [
        t
        for t in library
        if by_name and re.search(rf"(?<!\w){re.escape(t.name)}(?!\w)", last, re.IGNORECASE)
    ]
    if named:
        longest = max(len(t.name) for t in named)
        top = [t for t in named if len(t.name) == longest]
        return top[0] if len(top) == 1 else None
    number = re.search(r"\d+", last)
    if number is None:
        return None
    shown = _last(messages, "assistant").splitlines()
    for t in library:
        prefix = f"{int(number.group())}. {md(t.name)} ({t.format})"
        if any(line.startswith(prefix) for line in shown):
            return t
    return None
