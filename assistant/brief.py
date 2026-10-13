"""Phase 4: render the morning brief as a local HTML file."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import BRIEF_DIR, load_config
from .db import connect, init_db, now_iso
from .mailthread import has_sent_reply
from .plan import build as build_plan

log = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
    )


def build(*, since_hours: int | None = None, open_after: bool | None = None) -> Path:
    cfg = load_config()
    bcfg = cfg.get("brief", {})
    window = int(since_hours if since_hours is not None else bcfg.get("window_hours", 30))
    low_limit = int(bcfg.get("low_list_limit", 40))
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window)).isoformat()

    with connect() as conn:
        init_db(conn)
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT m.message_id, m.subject, m.from_name, m.from_addr, m.date_utc, "
                "m.folder, m.has_attach, m.is_answered, m.thread_key, c.importance, "
                "c.category, c.project, c.reason, c.implies_task, c.suggested_action, "
                "c.source "
                "FROM messages m JOIN classifications c ON c.message_id = m.message_id "
                "WHERE c.hidden = 0 AND COALESCE(m.first_seen_at, m.date_utc) >= ? "
                "ORDER BY m.date_utc DESC",
                (cutoff,),
            ).fetchall()
        ]

    high = [r for r in rows if r["importance"] == "high"]
    normal = [r for r in rows if r["importance"] == "normal"]
    low = [r for r in rows if r["importance"] == "low"]
    # Already-handled threads (Answered flag, or a Sent reply) aren't a task.
    with connect() as conn2:
        tasks = [
            r for r in rows
            if r["implies_task"] and not r["is_answered"]
            and not has_sent_reply(conn2, r["thread_key"], r["date_utc"])
        ]

    try:
        plan_data = build_plan()
    except Exception as exc:  # noqa: BLE001 - brief must render even if planning fails
        log.exception("planning failed, brief will omit it: %s", exc)
        plan_data = {"plan": [], "note": None, "active_count": 0, "left_out": 0,
                     "someday": [], "someday_count": 0}

    generated = datetime.now().strftime("%A %d %b %Y, %I:%M %p").replace(" 0", " ")
    html = _env().get_template("brief.html.j2").render(
        generated=generated,
        window_hours=window,
        high=high, normal=normal, low=low, tasks=tasks,
        low_limit=low_limit,
        counts={"high": len(high), "normal": len(normal),
                "low": len(low), "tasks": len(tasks)},
        plan=plan_data["plan"], plan_note=plan_data["note"],
        plan_active_count=plan_data["active_count"], plan_left_out=plan_data["left_out"],
        someday=plan_data["someday"], someday_count=plan_data["someday_count"],
    )

    BRIEF_DIR.mkdir(exist_ok=True)
    out = BRIEF_DIR / f"brief-{datetime.now():%Y-%m-%d-%H%M}.html"
    out.write_text(html, encoding="utf-8")
    (BRIEF_DIR / "latest.html").write_text(html, encoding="utf-8")

    with connect() as conn:
        conn.execute(
            "INSERT INTO sync_state (key, value) VALUES ('last_brief_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (now_iso(),),
        )
        conn.commit()

    log.info("brief: %d important, %d can-wait, %d buried, %d task candidate(s) -> %s",
             len(high), len(normal), len(low), len(tasks), out.name)

    do_open = bcfg.get("open_after_build", False) if open_after is None else open_after
    if do_open and os.name == "nt":
        try:
            os.startfile(str(BRIEF_DIR / "latest.html"))  # noqa: S606
        except OSError as exc:
            log.warning("could not open brief: %s", exc)
    return out
