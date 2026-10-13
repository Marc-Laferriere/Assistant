"""Lightweight "has the user already replied in this thread" check.

Subject-normalized grouping rather than walking References/In-Reply-To chains
— simpler, and enough at this mailbox's volume. Used because the IMAP
\\Answered flag turned out not to be reliably set (checked against the live
server: the mail client had not marked it on messages the user had already
replied to).
"""

from __future__ import annotations

import re

_PREFIX_RE = re.compile(r"^\s*(re|fw|fwd)\s*:\s*", re.IGNORECASE)


def normalize_subject(subject: str | None) -> str:
    s = subject or ""
    while True:
        new = _PREFIX_RE.sub("", s)
        if new == s:
            break
        s = new
    return s.strip().lower()


def has_sent_reply(conn, thread_key: str | None, after_date: str | None) -> bool:
    """True if the Sent folder holds a message in the same thread, dated after
    ``after_date`` (or any time, if the source message has no date)."""
    if not thread_key:
        return False
    row = conn.execute(
        "SELECT 1 FROM messages WHERE folder = 'Sent' AND thread_key = ? "
        "AND (? IS NULL OR date_utc IS NULL OR date_utc > ?) LIMIT 1",
        (thread_key, after_date, after_date),
    ).fetchone()
    return row is not None
