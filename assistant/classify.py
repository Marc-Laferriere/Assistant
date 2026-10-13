"""Phase 4: classify ingested email — rules funnel first, Gemini for the rest.

Writes one row per message to ``classifications``. Idempotent: already-classified
messages are skipped unless ``--reclassify``.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import rules
from .config import PROFILE_PATH, load_config, owner_name
from .db import connect, init_db, now_iso
from .llm import LLMError, complete_json

log = logging.getLogger(__name__)

_VALID_IMPORTANCE = {"high", "normal", "low"}

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "importance": {"type": "string", "enum": ["high", "normal", "low"]},
        "category": {"type": "string"},
        "project": {"type": "string", "nullable": True},
        "reason": {"type": "string"},
        "implies_task": {"type": "boolean"},
        "suggested_action": {"type": "string", "nullable": True},
    },
    "required": ["importance", "category", "reason", "implies_task"],
}

_SYSTEM = """You triage the work email of {owner}. Use the profile below as ground truth
about their role, projects, key people, and what counts as noise.

{profile}

For the email the user gives you, return JSON:
- importance: "high" = needs their attention or action soon (a direct request or
  question to them, an RFI/SI/new drawings/stop-work on an active project, a
  coordination or clash issue, a real deadline). "normal" = relevant to their
  work but not urgent (FYI on an active project, minutes with nothing assigned to
  them, vendor/admin). "low" = newsletters, marketing, automated notifications,
  or anything unrelated to their work.
- category: short slug, e.g. project-rfi, project-si, project-drawings,
  project-minutes, coordination, internal, vendor-admin, notification,
  newsletter, personal, spam.
- project: the active project it concerns (from the profile), else null.
- reason: one short sentence, plain, why you chose that importance.
- implies_task: true only if there is a concrete thing THEY need to do.
- suggested_action: if implies_task, a short imperative ("Reply to Sam re
  delivery schedule"); else null.

{floor_note}"""

_USER = """From: {frm}
To: {to}
Cc: {cc}
Date: {date}
Subject: {subject}

{body}"""


@dataclass
class Classification:
    importance: str
    category: str
    project: str | None
    reason: str
    implies_task: bool
    suggested_action: str | None
    source: str
    matched_rule: str | None
    model: str | None


def _coerce(data: dict, *, floor_normal: bool) -> Classification:
    imp = str(data.get("importance", "")).lower().strip()
    if imp not in _VALID_IMPORTANCE:
        imp = "normal"
    if floor_normal and imp == "low":
        imp = "normal"
    project = (data.get("project") or "").strip() or None
    if project and project.lower() in ("none", "null", "n/a"):
        project = None
    action = (data.get("suggested_action") or "").strip() or None
    return Classification(
        importance=imp,
        category=(data.get("category") or "").strip() or "uncategorized",
        project=project,
        reason=(data.get("reason") or "").strip(),
        implies_task=bool(data.get("implies_task")),
        suggested_action=action,
        source="ai",
        matched_rule=None,
        model=None,
    )


def _save(conn, message_id: str, c: Classification, prompt_version: str, hidden: bool = False):
    conn.execute(
        "INSERT INTO classifications (message_id, importance, hidden, category, project, "
        "reason, implies_task, suggested_action, source, matched_rule, model, "
        "prompt_version, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(message_id) DO UPDATE SET importance=excluded.importance, "
        "hidden=excluded.hidden, category=excluded.category, project=excluded.project, "
        "reason=excluded.reason, implies_task=excluded.implies_task, "
        "suggested_action=excluded.suggested_action, source=excluded.source, "
        "matched_rule=excluded.matched_rule, model=excluded.model, "
        "prompt_version=excluded.prompt_version, created_at=excluded.created_at",
        (message_id, c.importance, int(hidden), c.category, c.project, c.reason,
         int(c.implies_task), c.suggested_action, c.source, c.matched_rule, c.model,
         prompt_version, now_iso()),
    )


def _classify_one(row, profile: str, owner: str, model: str, important: bool) -> Classification:
    floor_note = (
        "The sender is a known work contact — do not rate this below \"normal\" "
        "unless it is clearly automated or marketing."
        if important else ""
    )
    system = _SYSTEM.format(owner=owner, profile=profile, floor_note=floor_note)
    body = (row["body_text"] or "")[: _MAX_BODY]
    user = _USER.format(
        frm=f"{row['from_name']} <{row['from_addr']}>",
        to=row["to_addrs"] or "", cc=row["cc_addrs"] or "",
        date=row["date_utc"] or "", subject=row["subject"] or "", body=body,
    )
    data = complete_json(model, system, user, schema=CLASSIFY_SCHEMA,
                         max_tokens=800, thinking_budget=0)
    c = _coerce(data, floor_normal=important)
    c.model = model
    return c


_MAX_BODY = 4000  # overwritten from config at run()


def run(*, limit: int | None = None, reclassify: bool = False) -> dict:
    global _MAX_BODY
    cfg = load_config()
    rcfg = cfg.get("rules", {})
    aicfg = cfg["ai"]
    _MAX_BODY = int(aicfg.get("max_body_chars", 4000))
    model = aicfg["triage_model"]
    pv = aicfg.get("prompt_version", "v1")
    owner = owner_name()
    profile = PROFILE_PATH.read_text(encoding="utf-8")

    counts = {"rules_low": 0, "rules_skip": 0, "ai": 0, "ai_failed": 0}
    with connect() as conn:
        init_db(conn)
        # Sent is ingested only for the "did the user already reply" thread check
        # (assistant/mailthread.py) - never classified or shown in the brief.
        if reclassify:
            q = "SELECT * FROM messages WHERE folder != 'Sent'"
        else:
            q = ("SELECT m.* FROM messages m "
                 "LEFT JOIN classifications c ON c.message_id = m.message_id "
                 "WHERE c.message_id IS NULL AND m.folder != 'Sent'")
        q += " ORDER BY COALESCE(date_utc, first_seen_at) DESC"
        if limit:
            q += f" LIMIT {int(limit)}"
        rows = conn.execute(q).fetchall()
        log.info("classifying %d message(s)", len(rows))

        ai_jobs = []
        for row in rows:
            verdict = rules.evaluate(row, rcfg)
            if verdict and verdict.bucket == "skip":
                _save(conn, row["message_id"],
                      Classification("low", "filtered", None, verdict.reason, False,
                                     None, "rules", verdict.rule, None), pv, hidden=True)
                counts["rules_skip"] += 1
            elif verdict and verdict.bucket == "low":
                _save(conn, row["message_id"],
                      Classification("low", "filtered", None, verdict.reason, False,
                                     None, "rules", verdict.rule, None), pv)
                counts["rules_low"] += 1
            else:
                ai_jobs.append((row, bool(verdict and verdict.important_sender)))
        conn.commit()

        if ai_jobs:
            log.info("  %d -> Gemini (%s)", len(ai_jobs), model)
            workers = max(1, int(aicfg.get("max_workers", 4)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {
                    pool.submit(_classify_one, row, profile, owner, model, imp): row
                    for row, imp in ai_jobs
                }
                for fut in futs:
                    row = futs[fut]
                    try:
                        c = fut.result()
                        _save(conn, row["message_id"], c, pv)
                        counts["ai"] += 1
                    except LLMError as exc:
                        log.error("classify failed for %r: %s", row["subject"], exc)
                        counts["ai_failed"] += 1
                    conn.commit()

    log.info("classify done: %d rules-low, %d rules-skip, %d ai, %d failed",
             counts["rules_low"], counts["rules_skip"], counts["ai"], counts["ai_failed"])
    return counts
