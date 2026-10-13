"""Deterministic pre-filter for email — runs before any AI call.

A verdict of ``low`` or ``skip`` is final (no AI). ``None`` means "let the model
decide"; ``important_sender`` on that verdict tells the classifier to apply a
"not below normal" floor for non-bulk mail.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RuleVerdict:
    bucket: str | None            # 'low' | 'skip' | None
    reason: str
    rule: str
    important_sender: bool = False


def _addr_matches(addr: str | None, patterns: list[str]) -> str | None:
    a = (addr or "").lower().strip()
    if not a:
        return None
    domain = a.split("@", 1)[1] if "@" in a else ""
    for raw in patterns:
        p = raw.lower().strip()
        if not p:
            continue
        if a == p or domain == p or domain.endswith("." + p):
            return p
    return None


def evaluate(msg_row, rcfg: dict) -> RuleVerdict | None:
    addr = msg_row["from_addr"]
    subject = (msg_row["subject"] or "").lower()
    is_bulk = bool(msg_row["is_bulk"])

    m = _addr_matches(addr, rcfg.get("skip_senders", []) + rcfg.get("skip_domains", []))
    if m:
        return RuleVerdict("skip", f"rules: skip sender/domain {m}", "skip_sender")
    for frag in rcfg.get("skip_subject_contains", []):
        if frag.lower() in subject:
            return RuleVerdict("skip", f"rules: skip subject ~{frag!r}", "skip_subject")

    important = _addr_matches(
        addr, rcfg.get("important_domains", []) + rcfg.get("important_senders", [])
    )

    m = _addr_matches(addr, rcfg.get("low_domains", []) + rcfg.get("low_senders", []))
    if m:
        return RuleVerdict("low", f"rules: newsletter/marketing sender {m}", "low_sender")

    if is_bulk and not important:
        return RuleVerdict("low", "rules: bulk mail, sender not on the work list", "bulk")

    if important and not is_bulk:
        return RuleVerdict(None, "", "important_floor", important_sender=True)

    return None
