"""Phase 5: task candidates -> a fast approve/reject review -> Notion.

Two sources feed the same local queue (`task_candidates`):
  - 'email'   new task, proposed from a classified message (`implies_task=1`)
  - 'cleanup' proposed edits to an existing, pre-assistant open Task

Review is a single-keystroke terminal loop, deliberately shaped like quick
capture rather than a list that sits around unread.
"""

from __future__ import annotations

import logging
import re
from datetime import date as date_cls

from . import drawings, meetings, rfis, submittals
from . import notion_api as N
from .config import PROFILE_PATH, SECRET_NOTION_TOKEN, load_config, owner_name
from .db import connect, init_db, log_event, now_iso
from .llm import complete_json
from .mailthread import has_sent_reply
from .projects import ProjectDirectory
from .secrets_store import get_secret

log = logging.getLogger(__name__)

# "1234 - Example PG 88 -" style subjects are duct/work orders already turned
# into "List/ship:" tasks by the Submittal mirror — never queue those twice.
_MIRROR_COVERED_RE = re.compile(r"^\s*\d+\s*-\s*.+?\bPG\b", re.IGNORECASE)

# The same SI/RFI often reaches the user twice (a direct Procore notice + a
# colleague's forward, or an "instruction issued" notice + a follow-up
# "drawings released" notice for that same instruction) — one review candidate
# per work item, not one per email.
_WORKITEM_RE = re.compile(r"\bSI\s*[-#]?\s*(\d+)\b|\bRFI\s*[-#]?\s*(\d+)\b", re.IGNORECASE)


def _workitem_key(*texts: str | None) -> str | None:
    for text in texts:
        m = _WORKITEM_RE.search(text or "")
        if m:
            return f"SI-{m.group(1)}" if m.group(1) else f"RFI-{m.group(2)}"
    return None


def _notion() -> N.Notion:
    return N.Notion(get_secret(SECRET_NOTION_TOKEN))


# --------------------------------------------------------------------------- #
# populate
# --------------------------------------------------------------------------- #
def _is_handled(conn, msg_row) -> str | None:
    """Return an auto-resolve reason if the source message is already dealt
    with, else None. Checks the IMAP flag first (cheap, occasionally right),
    then whether the user has sent a reply in the same thread (see mailthread.py —
    the flag alone proved unreliable against the real mailbox)."""
    if msg_row["is_answered"]:
        return "auto_answered"
    if has_sent_reply(conn, msg_row["thread_key"], msg_row["date_utc"]):
        return "auto_sent_reply"
    return None


def resolve_handled(conn) -> int:
    """Auto-clear queued email candidates that have since been handled."""
    rows = conn.execute(
        "SELECT tc.id, tc.title, m.is_answered, m.thread_key, m.date_utc "
        "FROM task_candidates tc JOIN messages m ON m.message_id = tc.message_id "
        "WHERE tc.status = 'proposed' AND tc.source_type = 'email'"
    ).fetchall()
    resolved = 0
    for r in rows:
        why = _is_handled(conn, r)
        if not why:
            continue
        conn.execute(
            "UPDATE task_candidates SET status = 'rejected', decided_at = ? WHERE id = ?",
            (now_iso(), r["id"]),
        )
        conn.execute(
            "INSERT INTO feedback (candidate_id, source_type, decision, reason, detail, created_at) "
            "VALUES (?, 'email', 'rejected', ?, ?, ?)",
            (r["id"], why, r["title"], now_iso()),
        )
        resolved += 1
    if resolved:
        conn.commit()
        log.info("auto-resolved %d candidate(s) — already handled", resolved)
    return resolved


def populate_from_email(conn) -> int:
    resolve_handled(conn)
    try:
        drawings.process(conn)  # automated new-drawing notices -> one list per project
    except Exception as exc:  # noqa: BLE001 - must not block ordinary candidates
        log.exception("drawing lists failed: %s", exc)
    existing = {
        r[0] for r in conn.execute(
            "SELECT message_id FROM task_candidates WHERE message_id IS NOT NULL"
        )
    }
    covered_keys = {
        _workitem_key(r[0]) for r in conn.execute(
            "SELECT title FROM task_candidates WHERE status != 'rejected'"
        )
    } - {None}

    rows = conn.execute(
        "SELECT m.message_id, m.subject, m.from_addr, m.is_answered, m.thread_key, "
        "m.date_utc, c.importance, c.project, c.suggested_action, c.reason "
        "FROM classifications c JOIN messages m ON m.message_id = c.message_id "
        "WHERE c.implies_task = 1 AND c.hidden = 0 AND c.importance != 'low'"
    ).fetchall()
    added = 0
    for r in rows:
        if r["message_id"] in existing or _MIRROR_COVERED_RE.match(r["subject"] or ""):
            continue
        if drawings.notice_kind(r["subject"], r["from_addr"]):
            continue  # collected into the per-project drawing lists instead
        if rfis.is_rfi_notice(r["subject"], r["from_addr"]):
            continue  # tracked by rfis.py (replies to ours / summaries of the rest)
        if submittals.is_notice(r["subject"], r["from_addr"]):
            continue  # tracked by submittals.py
        if meetings.is_meeting_notice(r["subject"]):
            continue  # summarised by meetings.py, never a task
        if _is_handled(conn, r):
            continue
        priority = "High" if r["importance"] == "high" else "Medium"
        title = r["suggested_action"] or r["subject"] or "(untitled)"

        key = _workitem_key(title, r["subject"])
        if key and key in covered_keys:
            log.info("skip %r — %s already has a live candidate/task", title, key)
            continue

        detail = f"From {r['from_addr']}: {r['subject']}"
        conn.execute(
            "INSERT INTO task_candidates (source_type, message_id, title, detail, "
            "priority, project, horizon, reason, status, created_at) VALUES "
            "('email', ?, ?, ?, ?, ?, 'Active', ?, 'proposed', ?)",
            (r["message_id"], title, detail, priority, r["project"], r["reason"], now_iso()),
        )
        if key:
            covered_keys.add(key)
        added += 1
    conn.commit()
    return added


_CLEANUP_SCHEMA = {
    "type": "object",
    "properties": {
        "project": {"type": "string", "nullable": True},
        "priority": {"type": "string", "enum": ["Low", "Medium", "High"]},
        "horizon": {"type": "string", "enum": ["Active", "Someday"]},
        "due": {"type": "string", "nullable": True},
        "reason": {"type": "string"},
    },
    "required": ["priority", "horizon", "reason"],
}

_CLEANUP_SYSTEM = """You help tidy {owner}'s Notion task list. Use this profile as ground truth:

{profile}

For the existing task given, propose:
- project: the matching active project name from the profile's Active Projects
  table, or null if none applies / unclear.
- priority: Low, Medium, or High.
- horizon: "Active" if it has a real, current next step; "Someday" if it is
  backlog, low priority, or "not sure how to approach it yet".
- due: an ISO date (YYYY-MM-DD) ONLY if the task text implies a real one,
  else null. Do not invent a date.
- reason: one short sentence.
Today's date is {today}."""


def populate_cleanup(conn, *, limit: int | None = None) -> int:
    cfg = load_config()
    notion = _notion()
    profile = PROFILE_PATH.read_text(encoding="utf-8")
    model = cfg["ai"]["reasoning_model"]
    existing = {
        r[0] for r in conn.execute(
            "SELECT notion_page_id FROM task_candidates WHERE source_type = 'cleanup'"
        )
    }
    flt = {"and": [
        {"property": "Added by assistant", "checkbox": {"equals": False}},
        {"property": "Status", "status": {"does_not_equal": "Done"}},
        {"property": "Status", "status": {"does_not_equal": "Archived"}},
    ]}
    added = 0
    for page in notion.iter_database(cfg["notion"]["tasks_db_id"], flt=flt):
        if page["id"] in existing:
            continue
        title = N.plain(page["properties"].get("Task name")) or ""
        if not title:
            continue
        notes = N.plain(page["properties"].get("Notes")) or ""
        cur_due = N.plain(page["properties"].get("Due"))
        cur_prio = N.plain(page["properties"].get("Priority"))
        user = (f"Title: {title}\nNotes: {notes}\n"
                f"Current due: {cur_due or '-'}\nCurrent priority: {cur_prio or '-'}")
        try:
            data = complete_json(
                model,
                _CLEANUP_SYSTEM.format(owner=owner_name(), profile=profile,
                                       today=date_cls.today().isoformat()),
                user, schema=_CLEANUP_SCHEMA, max_tokens=500, thinking_budget=0,
            )
        except Exception as exc:  # noqa: BLE001 - one bad proposal shouldn't stop the pass
            log.error("cleanup proposal failed for %r: %s", title, exc)
            continue
        due = (data.get("due") or "").strip()[:10] or None
        conn.execute(
            "INSERT INTO task_candidates (source_type, notion_page_id, title, due, "
            "priority, project, horizon, reason, status, created_at) VALUES "
            "('cleanup', ?, ?, ?, ?, ?, ?, ?, 'proposed', ?)",
            (page["id"], title, due, data.get("priority", "Medium"),
             data.get("project") or None, data.get("horizon", "Active"),
             data.get("reason", ""), now_iso()),
        )
        added += 1
        if limit and added >= limit:
            break
    conn.commit()
    return added


# --------------------------------------------------------------------------- #
# review
# --------------------------------------------------------------------------- #
_REJECT_REASONS = {"d": "already_done", "i": "irrelevant", "w": "wrong_call", "o": "other"}


def _log_feedback(conn, row: dict, decision: str, reason: str | None = None) -> None:
    conn.execute(
        "INSERT INTO feedback (candidate_id, source_type, decision, reason, detail, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (row["id"], row["source_type"], decision, reason, row["title"], now_iso()),
    )


def _reject(conn, notion, row: dict) -> None:
    print("  why? [d]one already  [i]rrelevant  [w]rong call  [o]ther  (Enter=skip reason)")
    while True:
        choice = input("  > ").strip().lower()
        if choice == "":
            reason = None
            break
        if choice in _REJECT_REASONS:
            reason = _REJECT_REASONS[choice]
            break
        print("  ?  d/i/w/o or Enter")

    if reason == "already_done" and row["source_type"] == "cleanup" and row.get("notion_page_id"):
        try:
            notion.update_page(row["notion_page_id"], {"Status": N.status("Done")})
            print("  -> marked Done in Notion (so it's not just dropped)")
        except Exception as exc:  # noqa: BLE001
            log.error("could not mark %r Done: %s", row["title"], exc)

    conn.execute(
        "UPDATE task_candidates SET status='rejected', decided_at=? WHERE id=?",
        (now_iso(), row["id"]),
    )
    _log_feedback(conn, row, "rejected", reason)
    conn.commit()


def _push(conn, notion, directory, tasks_db_id, row: dict) -> None:
    proj_name, proj_id = directory.resolve(row.get("project"))
    props: dict = {
        "Task name": N.title(row["title"]),
        "Priority": N.select(row.get("priority") or "Medium"),
        "Horizon": N.select(row.get("horizon") or "Active"),
    }
    if row.get("due"):
        props["Due"] = N.date(row["due"])
    if proj_id:
        props["Project"] = N.relation([proj_id])

    if row["source_type"] in ("email", "drawings"):
        props["Status"] = N.status("Not started")
        props["Added by assistant"] = N.checkbox(True)
        if row.get("detail"):
            props["Origin"] = N.rich_text(row["detail"])
        page = notion.create_page(tasks_db_id, props)
        notion_page_id = page["id"]
        if row["source_type"] == "drawings":
            conn.execute("UPDATE task_candidates SET notion_page_id=? WHERE id=?",
                         (notion_page_id, row["id"]))
            conn.commit()
            try:
                n = drawings.publish_to_page(conn, notion, notion_page_id, row["id"], intro=True)
                print(f"  -> sheet list added ({n} sheet(s))")
            except Exception as exc:  # noqa: BLE001 - the hourly run retries the body
                log.error("task created but sheet list not written yet: %s", exc)
        print(f"  -> created{'  [' + proj_name + ']' if proj_name else ''}")
    else:
        notion.update_page(row["notion_page_id"], props)
        notion_page_id = row["notion_page_id"]
        print(f"  -> updated{'  [' + proj_name + ']' if proj_name else ''}")

    conn.execute(
        "UPDATE task_candidates SET status='pushed', notion_page_id=?, decided_at=? WHERE id=?",
        (notion_page_id, now_iso(), row["id"]),
    )
    log_event(conn, "task_review_push", notion_task_id=notion_page_id, detail=row["title"])
    _log_feedback(conn, row, "accepted")
    conn.commit()


def _print_candidate(i: int, total: int, row: dict, conn=None) -> None:
    print(f"\n[{i}/{total}] {row['source_type']}")
    print(f"  {row['title']}")
    if row["source_type"] == "drawings" and conn is not None:
        if row.get("detail"):
            print(f"  {row['detail']}")
        for line in drawings.describe(conn, row["id"]):
            print(line)
    if row.get("detail") and row["source_type"] in ("email", "rfi_link"):
        print(f"  {row['detail']}")
    if row.get("reason"):
        print(f"  why: {row['reason']}")
    print(f"  project={row.get('project') or '-'}  priority={row.get('priority') or '-'}  "
          f"horizon={row.get('horizon')}  due={row.get('due') or '-'}")


def run_interactive(*, populate: bool = True) -> None:
    cfg = load_config()
    with connect() as conn:
        init_db(conn)
        if populate:
            n = populate_from_email(conn)
            if n:
                print(f"({n} new from email)")
        else:
            resolve_handled(conn)
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM task_candidates WHERE status = 'proposed' ORDER BY id"
        ).fetchall()]
        if not rows:
            print("Nothing to review.")
            return

        notion = _notion()
        directory = ProjectDirectory(notion, cfg["notion"]["projects_db_id"])
        tasks_db_id = cfg["notion"]["tasks_db_id"]

        print(f"{len(rows)} candidate(s). [Enter/a]ccept  [e]dit  [r]eject  [s]kip  [q]uit")
        for i, row in enumerate(rows, 1):
            while True:
                _print_candidate(i, len(rows), row, conn)
                choice = input("  > ").strip().lower()
                if row["source_type"] == "rfi_link" and choice in ("a", "", "r"):
                    accepted = choice in ("a", "")
                    picked = None
                    options = rfis.link_choices(conn, row["id"]) if accepted else None
                    if options:
                        for n, o in enumerate(options, 1):
                            print(f"    {n}) {o['project'] or '-'} · {o['label']} {o['title']}")
                        pick = input("  which one of yours? number, or Enter = none of them > ").strip()
                        if pick.isdigit() and 1 <= int(pick) <= len(options):
                            picked = options[int(pick) - 1]["id"]
                        else:
                            accepted = False
                    rfis.resolve_link(conn, row["id"], accepted, picked)
                    conn.execute("UPDATE task_candidates SET status=?, decided_at=? WHERE id=?",
                                 ("pushed" if accepted else "rejected", now_iso(), row["id"]))
                    _log_feedback(conn, row, "accepted" if accepted else "rejected",
                                  None if accepted else "not_mine")
                    conn.commit()
                    print("  -> linked to your RFI" if accepted else "  -> marked as someone else's RFI")
                    break
                if choice in ("a", ""):
                    _push(conn, notion, directory, tasks_db_id, row)
                    break
                if choice == "e":
                    t = input(f"  title [{row['title']}]: ").strip()
                    if t:
                        row["title"] = t
                    d = input(f"  due YYYY-MM-DD [{row.get('due') or ''}]: ").strip()
                    if d:
                        row["due"] = d
                    p = input(f"  priority Low/Medium/High [{row.get('priority') or ''}]: ").strip()
                    if p:
                        row["priority"] = p.title()
                    pr = input(f"  project [{row.get('project') or ''}]: ").strip()
                    if pr:
                        row["project"] = pr
                    continue
                if choice == "r":
                    _reject(conn, notion, row)
                    break
                if choice == "s":
                    break
                if choice == "q":
                    print("Stopping — remaining candidates stay queued for next time.")
                    return
                print("  ?  a=accept  e=edit  r=reject  s=skip  q=quit")
    print("\nDone.")


def pending_count() -> int:
    with connect() as conn:
        init_db(conn)
        return conn.execute(
            "SELECT COUNT(*) FROM task_candidates WHERE status = 'proposed'"
        ).fetchone()[0]
