"""Submittal notices: tell the user when a submittal they care about comes out.

Procore sends "Submittal distributed ..." when a submittal is finalised and
"Approver ... updated their response ..." along the way. For each distributed
submittal the AI decides whether it is one of the user's own kind of equipment
(configured in `[submittals] our_types`), matches an item on the plain-text watch
list (`submittal_watch.txt`), or is just someone else's. Ours and watched ones raise
a desktop notification and one Notion task, whatever the response status; the rest
get a one-line summary in the brief.

The watch list is also applied to ordinary email whose subject mentions
"submittal" (once per thread), so a forwarded approval for a submittal that never
goes through Procore can still be caught.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timedelta, timezone

from . import notion_api as N
from .config import SECRET_NOTION_TOKEN, SUBMITTAL_WATCH_PATH, load_config
from .db import log_event, now_iso
from .drawings import _heading, _page_open
from .llm import complete_json
from .notify import toast
from .projects import ProjectDirectory
from .secrets_store import get_secret

log = logging.getLogger(__name__)

_DIST_RE = re.compile(r"^(?P<proj>.+?):\s*Submittal distributed\s+(?P<rest>.+)$", re.I)
_APPR_RE = re.compile(r"^(?P<proj>.+?):\s*Approver\s+.+?\s+updated their response for submittal\s+"
                      r"(?P<rest>.+)$", re.I)
_RECENT_HOURS = 72
_TEXT_BLOCK = 1900

_SCHEMA = {"type": "object", "properties": {
    "category": {"type": "string", "enum": ["ours", "watch", "other"]},
    "matched": {"type": "string"}, "summary": {"type": "string"}},
    "required": ["category", "summary"]}


def watch_items() -> list[str]:
    try:
        lines = SUBMITTAL_WATCH_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def parse_notice(subject: str | None, from_addr: str | None, body: str | None) -> dict | None:
    if "procoretech.com" not in (from_addr or "").lower():
        return None
    subj = (subject or "").strip()
    m = _DIST_RE.match(subj)
    kind = "distributed"
    if not m:
        m = _APPR_RE.match(subj)
        kind = "approver"
    if not m:
        return None
    proj = m.group("proj").strip()
    pk = re.match(r"\s*(\d+)\s*[,\-]", proj)
    number, _, title = m.group("rest").partition(",")
    t = re.sub(r"\s+", " ", html.unescape(body or ""))
    resp = re.search(r"Response:\s*(.+?)\s+(?:Response:|Attachments|Ball in court|Final due)", t)
    spec = re.search(r"Spec section:\s*(.+?)\s+Type:", t)
    return {"kind": kind, "project_key": pk.group(1) if pk else proj,
            "project_name": proj.rsplit(" - ", 1)[-1].strip() if " - " in proj else proj,
            "number": number.strip(), "title": title.strip(),
            "response": resp.group(1).strip() if resp else "",
            "spec": spec.group(1).strip() if spec else ""}


def is_notice(subject: str | None, from_addr: str | None) -> bool:
    return parse_notice(subject, from_addr, "") is not None


def _model() -> str:
    return load_config()["ai"]["triage_model"]


def _judge(info: dict, watch: list[str]) -> dict:
    cfg = load_config().get("submittals", {})
    ours = cfg.get("our_types", "none specified")
    system = (
        "You help a duct detailer at a sheet-metal fabricator decide which construction "
        "submittals matter to them. Categories:\n"
        f"- ours: a submittal for the kind of equipment the company itself supplies: {ours}\n"
        "- watch: not ours, but it matches an item on the user's watch list (set `matched` to "
        "that item, verbatim)\n"
        "- other: anything else.\n"
        "Match by meaning, not exact words (abbreviations like GRD = grilles/registers/diffusers "
        "and FD = fire dampers are common). `summary` is ONE short line: what the submittal is "
        "and its response/status if given.\n\nWatch list:\n"
        + ("\n".join(f"- {w}" for w in watch) if watch else "(empty)"))
    user = (f"Project: {info['project_name']}\nSubmittal: {info['number']} {info['title']}\n"
            f"Spec section: {info['spec']}\nResponse: {info['response'] or '-'}\n"
            f"Context: {info.get('context', '')[:1500]}")
    data = complete_json(_model(), system, user, schema=_SCHEMA, max_tokens=300, thinking_budget=0)
    cat = data.get("category") if data.get("category") in ("ours", "watch", "other") else "other"
    return {"category": cat, "matched": (data.get("matched") or "").strip(),
            "summary": (data.get("summary") or "").strip()}


def _resolve_project(directory, *parts) -> str | None:
    name, _ = directory.resolve(" ".join(p for p in parts if p))
    return name


# --------------------------------------------------------------------------- #
# ingest
# --------------------------------------------------------------------------- #
def _ingest(conn, directory, since: str) -> int:
    n = 0
    recent_cut = (datetime.now(timezone.utc) - timedelta(hours=_RECENT_HOURS)).isoformat()
    watch = watch_items()

    for m in conn.execute(
        "SELECT * FROM messages m WHERE lower(m.from_addr) LIKE '%procoretech.com' "
        "AND lower(m.subject) LIKE '%submittal%' AND NOT EXISTS "
        "(SELECT 1 FROM submittal_events e WHERE e.message_id = m.message_id) ORDER BY m.date_utc"
    ).fetchall():
        info = parse_notice(m["subject"], m["from_addr"], m["body_text"])
        if not info:
            continue
        key = f"procore:{info['project_key']}:{info['number']}"
        when = m["date_utc"] or m["first_seen_at"] or ""
        project = _resolve_project(directory, info["project_name"], info["title"])
        row = conn.execute("SELECT * FROM submittals WHERE key=?", (key,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO submittals (key, source, project, number, title, spec_section, response, "
                "first_seen_at, last_event_at) VALUES (?, 'procore', ?, ?, ?, ?, ?, ?, ?)",
                (key, project, info["number"], info["title"], info["spec"], info["response"], when, when))
        elif info["response"]:
            conn.execute("UPDATE submittals SET response=?, last_event_at=? WHERE key=?",
                         (info["response"], when, key))
        handled = 1 if (m["first_seen_at"] or "") < since else 0
        if info["kind"] == "approver":
            handled = 1  # progress notices; the distributed notice is what we act on
        conn.execute("INSERT OR IGNORE INTO submittal_events (message_id, key, kind, response, "
                     "occurred_at, handled) VALUES (?,?,?,?,?,?)",
                     (m["message_id"], key, info["kind"], info["response"], when, handled))
        conn.execute("UPDATE classifications SET hidden = 1 WHERE message_id = ?", (m["message_id"],))
        n += 1

    # Ordinary email that mentions a submittal, checked against the watch list.
    if watch:
        for m in conn.execute(
            "SELECT * FROM messages m WHERE lower(m.subject) LIKE '%submittal%' "
            "AND lower(m.from_addr) NOT LIKE '%procoretech.com' AND m.folder != 'Sent' "
            "AND m.first_seen_at >= ? AND m.thread_key IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM submittal_events e WHERE e.message_id = m.message_id) ORDER BY m.date_utc",
            (since,),
        ).fetchall():
            key = f"mail:{m['thread_key']}"
            conn.execute(
                "INSERT OR IGNORE INTO submittals (key, source, project, number, title, first_seen_at, "
                "last_event_at) VALUES (?, 'mail', NULL, '', ?, ?, ?)",
                (key, m["subject"] or "", m["date_utc"] or "", m["date_utc"] or ""))
            conn.execute("INSERT OR IGNORE INTO submittal_events (message_id, key, kind, response, "
                         "occurred_at, handled) VALUES (?,?, 'mail', '', ?, 0)",
                         (m["message_id"], key, m["date_utc"]))
            n += 1
    conn.commit()
    return n


def _judge_pending(conn) -> int:
    """Judge each submittal that has an unhandled or recent distributed/mail event."""
    recent_cut = (datetime.now(timezone.utc) - timedelta(hours=_RECENT_HOURS)).isoformat()
    watch = watch_items()
    done = 0
    for s in conn.execute(
        "SELECT s.* FROM submittals s WHERE s.category IS NULL AND EXISTS "
        "(SELECT 1 FROM submittal_events e WHERE e.key = s.key AND e.kind IN ('distributed','mail') "
        "AND (e.handled = 0 OR e.occurred_at >= ?))", (recent_cut,)
    ).fetchall():
        context = ""
        if s["source"] == "mail":
            ev = conn.execute("SELECT message_id FROM submittal_events WHERE key=? ORDER BY occurred_at "
                              "LIMIT 1", (s["key"],)).fetchone()
            msg = conn.execute("SELECT subject, from_addr, body_text FROM messages WHERE message_id=?",
                               (ev["message_id"],)).fetchone() if ev else None
            if msg:
                context = f"Email from {msg['from_addr']}: {(msg['body_text'] or '')[:1200]}"
        info = {"project_name": s["project"] or "", "number": s["number"] or "", "title": s["title"] or "",
                "spec": s["spec_section"] or "", "response": s["response"] or "", "context": context}
        try:
            res = _judge(info, watch)
        except Exception as exc:  # noqa: BLE001 - retried next cycle
            log.error("submittal judgement failed for %r: %s", s["title"], exc)
            continue
        if s["source"] == "mail" and res["category"] == "ours":
            res["category"] = "watch"  # only the watch list can claim an ordinary email
        conn.execute("UPDATE submittals SET category=?, matched=?, summary=? WHERE key=?",
                     (res["category"], res["matched"], res["summary"], s["key"]))
        done += 1
    # Old, already-handled notices that were never judged are just history.
    conn.execute("UPDATE submittals SET category='history' WHERE category IS NULL AND NOT EXISTS "
                 "(SELECT 1 FROM submittal_events e WHERE e.key = submittals.key AND "
                 "(e.handled = 0 OR e.occurred_at >= ?))", (recent_cut,))
    conn.commit()
    return done


# --------------------------------------------------------------------------- #
# act
# --------------------------------------------------------------------------- #
def _para(text: str) -> list[dict]:
    text = text.strip()
    return [N.paragraph_block(text[i:i + _TEXT_BLOCK]) for i in range(0, len(text), _TEXT_BLOCK)]


def _act(conn, notion, directory, notify: bool) -> int:
    tasks_db = load_config()["notion"]["tasks_db_id"]
    cache: dict[str, bool] = {}
    acted = 0
    for ev in conn.execute(
        "SELECT e.*, s.project, s.number, s.title, s.spec_section, s.category, s.matched, s.summary, "
        "s.notified, s.task_page_id, s.source FROM submittal_events e JOIN submittals s ON s.key = e.key "
        "WHERE e.handled = 0 AND e.kind IN ('distributed','mail') AND s.category IS NOT NULL "
        "ORDER BY e.occurred_at").fetchall():
        if ev["category"] not in ("ours", "watch"):
            conn.execute("UPDATE submittal_events SET handled=1 WHERE message_id=?", (ev["message_id"],))
            continue
        # Re-read: an earlier event in this same pass may have just created the task.
        now_state = conn.execute("SELECT notified, task_page_id FROM submittals WHERE key=?",
                                 (ev["key"],)).fetchone()
        ev = dict(ev) | {"notified": now_state["notified"], "task_page_id": now_state["task_page_id"]}
        if ev["source"] == "mail" and ev["notified"]:
            conn.execute("UPDATE submittal_events SET handled=1 WHERE message_id=?", (ev["message_id"],))
            continue  # one alert per email thread
        label = (f"{ev['number']} {ev['title']}".strip() if ev["source"] == "procore" else ev["title"])
        why = "your equipment" if ev["category"] == "ours" else f"on your watch list ({ev['matched']})"
        status = f" - {ev['response']}" if ev["response"] else ""
        blocks = [_heading(f"Released ({(ev['occurred_at'] or '')[:10]})"),
                  *_para(ev["summary"] or label), *_para(f"Matched because: {why}.")]
        try:
            page_id = ev["task_page_id"]
            if page_id and _page_open(notion, page_id, cache):
                notion.append_blocks(page_id, blocks)
            else:
                props = {
                    "Task name": N.title(f"Submittal released: {label}{status}"[:200]),
                    "Priority": N.select("High"), "Horizon": N.select("Active"),
                    "Status": N.status("Not started"), "Added by assistant": N.checkbox(True),
                    "Origin": N.rich_text(f"Submittal notice - {why}"),
                }
                _, pid = directory.resolve(ev["project"])
                if pid:
                    props["Project"] = N.relation([pid])
                page = notion.create_page(tasks_db, props)
                page_id = page["id"]
                notion.append_blocks(page_id, blocks)
                conn.execute("UPDATE submittals SET task_page_id=? WHERE key=?", (page_id, ev["key"]))
                log_event(conn, "submittal_task_created", notion_task_id=page_id, detail=label)
        except Exception as exc:  # noqa: BLE001 - not marked handled; retried next run
            log.error("could not write submittal task for %r: %s", label, exc)
            continue
        conn.execute("UPDATE submittals SET notified=1 WHERE key=?", (ev["key"],))
        conn.execute("UPDATE submittal_events SET handled=1 WHERE message_id=?", (ev["message_id"],))
        conn.commit()
        acted += 1
        if notify:
            toast("Submittal released" + (f": {ev['response']}" if ev["response"] else ""),
                  f"{ev['project'] or ''} {label} ({why})")
    conn.commit()
    return acted


def process(conn, notion: N.Notion | None = None, *, notify: bool = True) -> dict:
    notion = notion or N.Notion(get_secret(SECRET_NOTION_TOKEN))
    directory = ProjectDirectory(notion, load_config()["notion"]["projects_db_id"])
    row = conn.execute("SELECT value FROM sync_state WHERE key='submittals_since'").fetchone()
    if row is None:
        since = now_iso()
        conn.execute("INSERT INTO sync_state (key, value) VALUES ('submittals_since', ?)", (since,))
        conn.commit()
        log.info("submittal tracking enabled")
    else:
        since = row[0]
    out = {"notices": _ingest(conn, directory, since)}
    out["judged"] = _judge_pending(conn)
    out["acted"] = _act(conn, notion, directory, notify)
    return out


def brief_data(conn, window_hours: int) -> dict:
    cut = (datetime.now(timezone.utc) - timedelta(hours=max(window_hours, 48))).isoformat()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM submittals WHERE last_event_at >= ? AND category IN ('ours','watch','other') "
        "AND EXISTS (SELECT 1 FROM submittal_events e WHERE e.key = submittals.key AND "
        "e.kind IN ('distributed','mail')) ORDER BY last_event_at DESC", (cut,)).fetchall()]
    return {"released": [r for r in rows if r["category"] in ("ours", "watch")],
            "others": [r for r in rows if r["category"] == "other"]}
