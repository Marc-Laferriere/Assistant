"""A colleague's Notion tracker as a read-only, secondary source.

A colleague who also detailers a job keeps a Notion page with a table of the RFIs
sent (our number, the subcontractor's number, the GC's number, a status). It is a
better source than guessing from email: it already says which RFI is which and
whether it was sent, answered or cancelled. Everything here goes through
`ReadOnlyNotion`; nothing on that page is ever written.

Configured by `[colleague_page]` in config.toml (git-ignored); with no page_id this
module does nothing.
"""

from __future__ import annotations

import logging

from . import notion_api as N
from .config import SECRET_NOTION_TOKEN, load_config
from .db import now_iso
from .notion_readonly import ReadOnlyNotion
from .secrets_store import get_secret

log = logging.getLogger(__name__)

_DEFAULTS = {
    "rfi_db_title": "RFIs",
    "subject_prop": "Subject",
    "status_prop": "Status",
    "notes_prop": "Notes",
    "number_props": [],
    "raised_statuses": [],      # the RFI has gone out (or beyond) - an issue it covers is dealt with
    "draft_statuses": [],       # written but not sent - note it, do not treat the issue as handled
    "cancelled_statuses": [],   # dropped - never covers anything
}


def settings() -> dict | None:
    cfg = load_config().get("colleague_page") or {}
    if not cfg.get("page_id"):
        return None
    return {**_DEFAULTS, **cfg}


def rfi_state(status: str | None, cfg: dict | None = None) -> str:
    """'raised' | 'draft' | 'cancelled' | 'other' for a status in the colleague's tracker."""
    cfg = cfg or settings() or _DEFAULTS
    s = (status or "").strip().lower()
    for state, key in (("cancelled", "cancelled_statuses"), ("draft", "draft_statuses"),
                       ("raised", "raised_statuses")):
        if s in {x.lower() for x in cfg[key]}:
            return state
    return "other"


def _find_rfi_db(notion: ReadOnlyNotion, cfg: dict) -> str | None:
    want = cfg["rfi_db_title"].strip().lower()
    for b in notion.iter_block_children(cfg["page_id"]):
        if b["type"] == "child_database" and b["child_database"].get("title", "").strip().lower() == want:
            return b["id"]
    return None


def _numbers(props: dict, cfg: dict) -> str:
    out = []
    for name in cfg["number_props"]:
        v = (N.plain(props.get(name)) or "").strip()
        if v:
            out.append(f"{name.rstrip(' #')} {v}" if v.isdigit() else v)
    return " / ".join(out)


def refresh(conn, notion: ReadOnlyNotion | None = None) -> int:
    """Copy the colleague's RFI rows into `colleague_rfis`. Returns the row count, or -1 if
    the page is not configured or not reachable (the old copy is kept)."""
    cfg = settings()
    if cfg is None:
        return -1
    try:
        notion = notion or ReadOnlyNotion(get_secret(SECRET_NOTION_TOKEN))
        db_id = _find_rfi_db(notion, cfg)
        if db_id is None:
            log.warning("colleague page has no database titled %r", cfg["rfi_db_title"])
            return -1
        rows = list(notion.iter_database(db_id))
    except Exception as exc:  # noqa: BLE001 - a secondary source must never block the cycle
        log.warning("colleague tracker not read: %s", exc)
        return -1
    seen = set()
    for r in rows:
        p = r["properties"]
        seen.add(r["id"])
        conn.execute(
            "INSERT INTO colleague_rfis (page_id, subject, status, numbers, notes, refreshed_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(page_id) DO UPDATE SET subject=excluded.subject, "
            "status=excluded.status, numbers=excluded.numbers, notes=excluded.notes, "
            "refreshed_at=excluded.refreshed_at",
            (r["id"], N.plain(p.get(cfg["subject_prop"])) or "", N.plain(p.get(cfg["status_prop"])) or "",
             _numbers(p, cfg), (N.plain(p.get(cfg["notes_prop"])) or "")[:500], now_iso()))
    if seen:
        marks = ",".join("?" * len(seen))
        conn.execute(f"DELETE FROM colleague_rfis WHERE page_id NOT IN ({marks})", tuple(seen))
    conn.commit()
    return len(rows)
