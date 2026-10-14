"""RFI tracking: know when the RFIs *we* wrote get answered, and summarise the rest.

Flow
----
1. Internal RFI emails (a colleague, or the user, writing/reviewing an RFI before
   it goes to the GC) are registered as `our_rfis`. The user's own numbering never
   matches the GC's, so:
2. Procore notices (new / response / closed / reopened) are parsed into
   `procore_rfis` + `rfi_events`, and each new Procore RFI is matched to one of our
   open RFIs by the AI (title *and* question text, since the GC sometimes retitles).
   A confident match links automatically; a doubtful one is asked in the review
   queue. Once linked, the Procore number is the identity, so later retitling
   doesn't matter.
3. For a linked RFI, a response / reopen / close raises a Windows notification and
   writes (or extends) one Notion task. Every other RFI just gets a short summary
   in the brief.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timedelta, timezone

from . import notion_api as N
from .config import PROFILE_PATH, SECRET_NOTION_TOKEN, load_config, owner_name
from .db import log_event, now_iso
from .drawings import _heading, _page_open
from .llm import complete_json
from .notify import toast
from .projects import ProjectDirectory
from .secrets_store import get_secret

log = logging.getLogger(__name__)

_NOTICE_RE = re.compile(
    r"^(?P<proj>.+?):\s*(?P<kind>New RFI|Response to RFI|RFI closed|RFI reopened)\b"
    r"[,\s]*#?(?P<num>\d+),?\s*(?P<title>.*)$", re.IGNORECASE)
_KINDS = {"new rfi": "new", "response to rfi": "response", "rfi closed": "closed",
          "rfi reopened": "reopened"}
_NUM_RE = re.compile(r"\bRFI\s*[-#]?\s*0*(\d+)\b", re.IGNORECASE)
_RECENT_DAYS = 14          # how far back internal RFIs / unmatched Procore RFIs are considered
_SUMMARY_HOURS = 72
_TEXT_BLOCK = 1900


# --------------------------------------------------------------------------- #
# recognising / parsing Procore notices
# --------------------------------------------------------------------------- #
def parse_notice(subject: str | None, from_addr: str | None, body: str | None) -> dict | None:
    if "procoretech.com" not in (from_addr or "").lower():
        return None
    m = _NOTICE_RE.match((subject or "").strip())
    if not m:
        return None
    proj = m.group("proj").strip()
    key = (re.match(r"\s*(\d+)\s*,", proj) or re.match(r"\s*(\d+)\s*-", proj))
    key = key.group(1) if key else proj
    name = proj.rsplit(" - ", 1)[-1].strip() if " - " in proj else proj
    t = re.sub(r"\s+", " ", html.unescape(body or ""))
    q = re.search(r"Question:\s*(.+?)\s*(?:Attachments:|Additional information)", t)
    responses = re.findall(r"'s response:\s*(.+?)\s*(?:Attachments:|View in Procore)", t)
    due = re.search(r"Due date:\s*([A-Z][a-z]+ \d{1,2}, \d{4})", t)
    asked = re.search(r"Question Asked by:\s*(.+?)\s+Location", t)
    return {
        "kind": _KINDS[m.group("kind").lower()],
        "project_key": key, "project_name": name,
        "number": int(m.group("num")), "title": m.group("title").strip(),
        "question": q.group(1).strip() if q else "",
        "response": "\n\n".join(r.strip() for r in responses),
        "due": due.group(1) if due else "", "asked_by": asked.group(1).strip() if asked else "",
    }


def is_rfi_notice(subject: str | None, from_addr: str | None) -> bool:
    return parse_notice(subject, from_addr, "") is not None


# --------------------------------------------------------------------------- #
# AI helpers
# --------------------------------------------------------------------------- #
_EXTRACT_SCHEMA = {"type": "object", "properties": {
    "is_rfi": {"type": "boolean"}, "title": {"type": "string"}, "question": {"type": "string"}},
    "required": ["is_rfi", "title", "question"]}
_EXTRACT_SYSTEM = (
    "You read internal email at a sheet-metal / HVAC fabricator. Decide whether this email is "
    "about an RFI (Request For Information) that the company itself WROTE and is reviewing or "
    "forwarding for submission to the general contractor. Return is_rfi=false for replies from "
    "the GC, notifications, or emails that merely mention an RFI. If true give a short topic "
    "title (no RFI number) and, in one or two sentences, what is being asked.")

_MATCH_SCHEMA = {"type": "object", "properties": {
    "match_id": {"type": "integer", "nullable": True},
    "confidence": {"type": "string", "enum": ["high", "medium", "low"]}},
    "required": ["confidence"]}
_MATCH_SYSTEM = (
    "A general contractor / mechanical contractor just opened an RFI in Procore. Decide which, "
    "if any, of the company's own internal RFIs it is the formal version of. Match on the "
    "subject matter of the question, not on numbers (numbering differs between the two "
    "systems) and not strictly on wording (titles are sometimes reworded). Return match_id of "
    "the internal RFI, or null if none fits. confidence: high only if you are sure.")

_SUMMARY_SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}},
                   "required": ["summary"]}


def _summary_system(owner: str, profile: str) -> str:
    return (f"You summarise a construction RFI for {owner}, a duct detailer. Use the profile as "
            f"context.\n\n{profile}\n\nIn at most two plain sentences say what was asked, what "
            "the answer is (if there is one), and what it could change for sheet-metal work - "
            "including indirect effects such as a moved wall, a changed fire rating, a changed "
            "equipment location, or a clearance. If it has no plausible effect on duct work, say so.")


def _model() -> str:
    return load_config()["ai"]["triage_model"]


# --------------------------------------------------------------------------- #
# step 1: register our own RFIs from internal email
# --------------------------------------------------------------------------- #
def _own_domain() -> str:
    return load_config()["imap"]["username"].split("@")[-1].lower()


def _resolve_project(directory, *parts: str | None) -> str | None:
    name, _ = directory.resolve(" ".join(p for p in parts if p))
    return name


def _register_internal(conn, directory) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_RECENT_DAYS)).isoformat()
    domain = "@" + _own_domain()
    rows = conn.execute(
        "SELECT m.*, c.project AS cls_project FROM messages m "
        "LEFT JOIN classifications c ON c.message_id = m.message_id "
        "WHERE m.subject LIKE '%RFI%' AND lower(m.from_addr) LIKE ? AND m.date_utc >= ? "
        "AND m.thread_key IS NOT NULL ORDER BY m.date_utc",
        (f"%{domain}", cutoff),
    ).fetchall()
    added = 0
    model = _model()
    seen_threads = {r[0] for r in conn.execute("SELECT thread_key FROM our_rfi_threads")}
    for m in rows:
        if m["thread_key"] in seen_threads:
            continue
        try:
            data = complete_json(
                model, _EXTRACT_SYSTEM,
                f"Subject: {m['subject']}\nFrom: {m['from_addr']}\nTo: {m['to_addrs']}\n\n"
                f"{(m['body_text'] or '')[:3000]}",
                schema=_EXTRACT_SCHEMA, max_tokens=400, thinking_budget=0)
        except Exception as exc:  # noqa: BLE001 - retried next cycle
            log.error("RFI extraction failed for %r: %s", m["subject"], exc)
            continue
        seen_threads.add(m["thread_key"])
        if not data.get("is_rfi"):
            conn.execute("INSERT OR IGNORE INTO our_rfi_threads (thread_key, our_rfi_id, checked) "
                         "VALUES (?, 0, 0)", (m["thread_key"],))
            continue
        project = _resolve_project(directory, m["cls_project"], m["folder"], m["subject"])
        nm = _NUM_RE.search(m["subject"] or "")
        number = int(nm.group(1)) if nm else None
        existing = None
        if number is not None:
            existing = conn.execute("SELECT id FROM our_rfis WHERE number=? AND project IS ?",
                                    (number, project)).fetchone()
        if existing:
            rid = existing["id"]  # same RFI reviewed in a second email thread
        else:
            cur = conn.execute(
                "INSERT INTO our_rfis (project, number, label, title, question, first_message_id, "
                "sent_at, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (project, number, f"RFI-{number:02d}" if number is not None else "RFI",
                 data.get("title", "").strip(), data.get("question", "").strip(),
                 m["message_id"], m["date_utc"], now_iso()))
            rid = cur.lastrowid
            added += 1
            log.info("registered internal RFI %s %r (%s)", f"#{number}" if number else "",
                     data.get("title"), project)
        conn.execute("INSERT OR IGNORE INTO our_rfi_threads (thread_key, our_rfi_id, checked) "
                     "VALUES (?, ?, 1)", (m["thread_key"], rid))
    conn.commit()
    return added


# --------------------------------------------------------------------------- #
# step 2: parse Procore notices into RFIs + events
# --------------------------------------------------------------------------- #
def _ingest_notices(conn, directory, since: str) -> int:
    n = 0
    recent_cut = (datetime.now(timezone.utc) - timedelta(days=_RECENT_DAYS)).isoformat()
    for m in conn.execute(
        "SELECT * FROM messages m WHERE lower(m.from_addr) LIKE '%procoretech.com' "
        "AND m.subject LIKE '%RFI%' AND NOT EXISTS "
        "(SELECT 1 FROM rfi_events e WHERE e.message_id = m.message_id) ORDER BY m.date_utc"
    ).fetchall():
        info = parse_notice(m["subject"], m["from_addr"], m["body_text"])
        if not info:
            continue
        project = _resolve_project(directory, info["project_name"], info["title"])
        row = conn.execute("SELECT * FROM procore_rfis WHERE project_key=? AND number=?",
                           (info["project_key"], info["number"])).fetchone()
        when = m["date_utc"] or m["first_seen_at"] or ""
        if row is None:
            state = "none" if when >= recent_cut else "other"
            cur = conn.execute(
                "INSERT INTO procore_rfis (project_key, project, number, title, question, due, "
                "asked_by, link_state, first_seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (info["project_key"], project, info["number"], info["title"], info["question"],
                 info["due"], info["asked_by"], state, when))
            pid = cur.lastrowid
        else:
            pid = row["id"]
            conn.execute(
                "UPDATE procore_rfis SET title=COALESCE(NULLIF(?,''),title), "
                "question=COALESCE(NULLIF(?,''),question), due=COALESCE(NULLIF(?,''),due) WHERE id=?",
                (info["title"], info["question"], info["due"], pid))
        status = {"new": "open", "response": "answered", "closed": "closed",
                  "reopened": "open"}[info["kind"]]
        conn.execute("UPDATE procore_rfis SET status=? WHERE id=?", (status, pid))
        # Events from before this feature existed are history, not news.
        handled = 1 if (m["first_seen_at"] or "") < since else 0
        conn.execute(
            "INSERT OR IGNORE INTO rfi_events (message_id, procore_rfi_id, kind, response_text, "
            "occurred_at, handled) VALUES (?,?,?,?,?,?)",
            (m["message_id"], pid, info["kind"], info["response"], when, handled))
        conn.execute("UPDATE classifications SET hidden = 1 WHERE message_id = ?", (m["message_id"],))
        n += 1
    conn.commit()
    return n


# --------------------------------------------------------------------------- #
# step 3: match Procore RFIs to ours
# --------------------------------------------------------------------------- #
def _link(conn, procore_id: int, our_id: int, state: str) -> None:
    p = conn.execute("SELECT status FROM procore_rfis WHERE id=?", (procore_id,)).fetchone()
    status = {"open": "submitted", "answered": "answered", "closed": "closed"}[p["status"]]
    conn.execute("UPDATE procore_rfis SET our_rfi_id=?, link_state=? WHERE id=?",
                 (our_id, state, procore_id))
    conn.execute("UPDATE our_rfis SET procore_rfi_id=?, status=? WHERE id=?",
                 (procore_id, status, our_id))


def _match(conn) -> tuple[int, int]:
    linked = asked = 0
    model = _model()
    for p in conn.execute("SELECT * FROM procore_rfis WHERE link_state='none' ORDER BY id").fetchall():
        # Every open, unmatched internal RFI is a candidate: the project is evidence, not a
        # filter, because Procore and our own filing don't always split towers the same way.
        cands = conn.execute(
            "SELECT * FROM our_rfis WHERE procore_rfi_id IS NULL AND status='internal' "
            "ORDER BY id").fetchall()
        if not cands:
            conn.execute("UPDATE procore_rfis SET link_state='other' WHERE id=?", (p["id"],))
            continue
        listing = "\n".join(f"[{c['id']}] ({c['project']}) {c['label']} - {c['title']}: {c['question']}"
                            for c in cands)
        try:
            data = complete_json(
                model, _MATCH_SYSTEM,
                f"Procore RFI #{p['number']} ({p['project']}): {p['title']}\nQuestion: {p['question']}\n\n"
                f"Internal RFIs still waiting to be matched:\n{listing}",
                schema=_MATCH_SCHEMA, max_tokens=200, thinking_budget=0)
        except Exception as exc:  # noqa: BLE001 - retried next cycle
            log.error("RFI match failed for #%s: %s", p["number"], exc)
            continue
        mid = data.get("match_id")
        valid = {c["id"] for c in cands}
        if mid in valid and data.get("confidence") == "high":
            _link(conn, p["id"], mid, "auto")
            linked += 1
            log.info("linked Procore RFI #%s to our RFI %s", p["number"], mid)
        else:
            guess = mid if mid in valid else (cands[0]["id"] if len(cands) == 1 else None)
            cid = conn.execute(
                "INSERT INTO task_candidates (source_type, title, detail, priority, project, horizon, "
                "reason, status, created_at) VALUES ('rfi_link', ?, ?, 'Medium', ?, 'Active', ?, "
                "'proposed', ?)",
                (f"Is Procore RFI #{p['number']} one of yours? {p['title']}",
                 _link_detail(cands, guess),
                 p["project"], "Accept = yes, link it. Reject = someone else's RFI.", now_iso())).lastrowid
            conn.execute("INSERT INTO rfi_link_proposals (candidate_id, procore_rfi_id, our_rfi_id) "
                         "VALUES (?,?,?)", (cid, p["id"], guess))
            conn.execute("UPDATE procore_rfis SET link_state='asked' WHERE id=?", (p["id"],))
            asked += 1
    conn.commit()
    return linked, asked


def _link_detail(cands, guess_id) -> str:
    g = next((c for c in cands if c["id"] == guess_id), None)
    if g:
        return f"Best guess: {g['label']} - {g['title']}"
    return "No clear match. Open internal RFIs: " + "; ".join(
        f"{c['label']} {c['title']}" for c in cands)


def link_choices(conn, candidate_id: int) -> list[dict] | None:
    """None when the proposal already names a best guess; otherwise the open internal
    RFIs the user can pick from."""
    pr = conn.execute("SELECT our_rfi_id FROM rfi_link_proposals WHERE candidate_id=?",
                      (candidate_id,)).fetchone()
    if pr is None or pr["our_rfi_id"]:
        return None
    return [dict(r) for r in conn.execute(
        "SELECT id, project, label, title FROM our_rfis WHERE procore_rfi_id IS NULL "
        "AND status='internal' ORDER BY id")]


def resolve_link(conn, candidate_id: int, accepted: bool, our_rfi_id: int | None = None) -> None:
    """Called from the review queue. `our_rfi_id` overrides the proposal's guess."""
    pr = conn.execute("SELECT * FROM rfi_link_proposals WHERE candidate_id=?", (candidate_id,)).fetchone()
    if not pr:
        return
    target = our_rfi_id or pr["our_rfi_id"]
    if accepted and target:
        _link(conn, pr["procore_rfi_id"], target, "confirmed")
    else:
        conn.execute("UPDATE procore_rfis SET link_state='other' WHERE id=?", (pr["procore_rfi_id"],))
    conn.commit()


# --------------------------------------------------------------------------- #
# step 4: act on events for RFIs that are ours
# --------------------------------------------------------------------------- #
def _para(text: str) -> list[dict]:
    text = text.strip()
    return [N.paragraph_block(text[i:i + _TEXT_BLOCK]) for i in range(0, len(text), _TEXT_BLOCK)] or []


def _event_blocks(events, procore) -> list[dict]:
    blocks: list[dict] = []
    for e in events:
        day = (e["occurred_at"] or "")[:10]
        if e["kind"] == "response":
            blocks.append(_heading(f"Response received ({day})"))
            blocks += _para(e["response_text"] or "(see the Procore notice)")
        elif e["kind"] == "closed":
            blocks.append(_heading(f"RFI closed ({day})"))
        elif e["kind"] == "reopened":
            blocks.append(_heading(f"RFI reopened ({day})"))
    return blocks


def _handle_events(conn, notion, directory, notify: bool) -> int:
    rows = conn.execute(
        "SELECT e.*, p.number, p.title ptitle, p.project, p.question pquestion, p.our_rfi_id "
        "FROM rfi_events e JOIN procore_rfis p ON p.id = e.procore_rfi_id "
        "WHERE e.handled = 0 AND p.our_rfi_id IS NOT NULL ORDER BY e.occurred_at").fetchall()
    by_rfi: dict[int, list] = {}
    for r in rows:
        by_rfi.setdefault(r["our_rfi_id"], []).append(r)
    cache: dict[str, bool] = {}
    tasks_db = load_config()["notion"]["tasks_db_id"]
    done = 0
    for our_id, events in by_rfi.items():
        ours = conn.execute("SELECT * FROM our_rfis WHERE id=?", (our_id,)).fetchone()
        news = [e for e in events if e["kind"] != "new"]
        if news:
            e0 = events[0]
            blocks = _event_blocks(news, e0)
            label = f"{ours['label']} {ours['title']}".strip()
            try:
                page_id = ours["task_page_id"]
                if page_id and _page_open(notion, page_id, cache):
                    notion.append_blocks(page_id, blocks)
                else:
                    props = {
                        "Task name": N.title(f"RFI reply: {label} (Procore #{e0['number']})"),
                        "Priority": N.select("High"), "Horizon": N.select("Active"),
                        "Status": N.status("Not started"), "Added by assistant": N.checkbox(True),
                        "Origin": N.rich_text(f"Reply to your RFI, Procore #{e0['number']}: {e0['ptitle']}"),
                    }
                    _, pid = directory.resolve(ours["project"])
                    if pid:
                        props["Project"] = N.relation([pid])
                    page = notion.create_page(tasks_db, props)
                    page_id = page["id"]
                    intro = f"Your RFI \"{label}\" is Procore RFI #{e0['number']}: {e0['ptitle']}."
                    if ours["question"]:
                        intro += f" You asked: {ours['question']}"
                    notion.append_blocks(page_id, [N.paragraph_block(intro)] + blocks)
                    conn.execute("UPDATE our_rfis SET task_page_id=? WHERE id=?", (page_id, our_id))
                    log_event(conn, "rfi_task_created", notion_task_id=page_id, detail=label)
            except Exception as exc:  # noqa: BLE001 - nothing marked handled; retried next run
                log.error("could not write RFI task for %r: %s", label, exc)
                continue
            kinds = {e["kind"] for e in news}
            if "closed" in kinds and "response" in kinds:
                headline, status = "answered and closed", "closed"
            elif "closed" in kinds:
                headline, status = "closed", "closed"
            elif "reopened" in kinds:
                headline, status = "reopened", "submitted"
            else:
                headline, status = "answered", "answered"
            if "reopened" in kinds and "response" in kinds:
                headline, status = "answered again", "answered"
            conn.execute("UPDATE our_rfis SET status=?, last_event_at=? WHERE id=?",
                         (status, news[-1]["occurred_at"], our_id))
            if notify:
                reply = next((e["response_text"] for e in news if e["response_text"]), "")
                toast(f"Your RFI was {headline}",
                      f"{ours['project'] or ''} #{e0['number']}: {e0['ptitle']}"
                      + (f" - {reply[:90]}" if reply else ""))
        for e in events:
            conn.execute("UPDATE rfi_events SET handled = 1 WHERE message_id = ?", (e["message_id"],))
        conn.commit()
        done += 1
    # Events for RFIs that are not ours need no action.
    conn.execute("UPDATE rfi_events SET handled = 1 WHERE handled = 0 AND procore_rfi_id IN "
                 "(SELECT id FROM procore_rfis WHERE link_state = 'other')")
    conn.commit()
    return done


# --------------------------------------------------------------------------- #
# step 5: summaries of everyone else's RFIs
# --------------------------------------------------------------------------- #
def _summarise_others(conn) -> int:
    cut = (datetime.now(timezone.utc) - timedelta(hours=_SUMMARY_HOURS)).isoformat()
    owner = owner_name()
    profile = PROFILE_PATH.read_text(encoding="utf-8")
    model = _model()
    n = 0
    for p in conn.execute(
        "SELECT p.*, (SELECT max(occurred_at) FROM rfi_events e WHERE e.procore_rfi_id=p.id) last_ev "
        "FROM procore_rfis p WHERE p.link_state='other' AND EXISTS "
        "(SELECT 1 FROM rfi_events e WHERE e.procore_rfi_id=p.id AND e.occurred_at >= ?)", (cut,)
    ).fetchall():
        if p["summary_at"] and p["summary_at"] >= (p["last_ev"] or ""):
            continue
        resp = conn.execute("SELECT response_text FROM rfi_events WHERE procore_rfi_id=? AND "
                            "response_text != '' ORDER BY occurred_at DESC LIMIT 1", (p["id"],)).fetchone()
        try:
            data = complete_json(
                model, _summary_system(owner, profile),
                f"RFI #{p['number']}: {p['title']}\nQuestion: {p['question']}\n"
                f"Status: {p['status']}\nResponse: {(resp['response_text'] if resp else '(none yet)')[:3000]}",
                schema=_SUMMARY_SCHEMA, max_tokens=300, thinking_budget=0)
        except Exception as exc:  # noqa: BLE001
            log.error("RFI summary failed for #%s: %s", p["number"], exc)
            continue
        conn.execute("UPDATE procore_rfis SET summary=?, summary_at=? WHERE id=?",
                     (data.get("summary", "").strip(), now_iso(), p["id"]))
        n += 1
    conn.commit()
    return n


# --------------------------------------------------------------------------- #
# the pass that runs every cycle
# --------------------------------------------------------------------------- #
def process(conn, notion: N.Notion | None = None, *, notify: bool = True) -> dict:
    notion = notion or N.Notion(get_secret(SECRET_NOTION_TOKEN))
    directory = ProjectDirectory(notion, load_config()["notion"]["projects_db_id"])
    row = conn.execute("SELECT value FROM sync_state WHERE key='rfis_since'").fetchone()
    if row is None:
        since = now_iso()
        conn.execute("INSERT INTO sync_state (key, value) VALUES ('rfis_since', ?)", (since,))
        conn.commit()
        log.info("RFI tracking enabled")
    else:
        since = row[0]
    out = {"registered": _register_internal(conn, directory),
           "notices": _ingest_notices(conn, directory, since)}
    out["linked"], out["asked"] = _match(conn)
    out["acted"] = _handle_events(conn, notion, directory, notify)
    out["summaries"] = _summarise_others(conn)
    return out


# --------------------------------------------------------------------------- #
# brief data
# --------------------------------------------------------------------------- #
def brief_data(conn, window_hours: int) -> dict:
    cut = (datetime.now(timezone.utc) - timedelta(hours=max(window_hours, 48))).isoformat()
    answered = conn.execute(
        "SELECT o.*, p.number pnum, p.title ptitle FROM our_rfis o JOIN procore_rfis p ON p.id=o.procore_rfi_id "
        "WHERE o.status IN ('answered','closed') AND o.last_event_at >= ? ORDER BY o.last_event_at DESC",
        (cut,)).fetchall()
    out_answered = []
    for o in answered:
        resp = conn.execute(
            "SELECT response_text FROM rfi_events WHERE procore_rfi_id=? AND response_text != '' "
            "ORDER BY occurred_at DESC LIMIT 1", (o["procore_rfi_id"],)).fetchone()
        out_answered.append(dict(o) | {"response": resp["response_text"] if resp else ""})
    waiting = [dict(r) for r in conn.execute(
        "SELECT o.*, p.number pnum FROM our_rfis o LEFT JOIN procore_rfis p ON p.id=o.procore_rfi_id "
        "WHERE o.status IN ('internal','submitted') ORDER BY o.sent_at").fetchall()]
    today = datetime.now(timezone.utc)
    for w in waiting:
        try:
            w["days"] = (today - datetime.fromisoformat(w["sent_at"])).days
        except (TypeError, ValueError):
            w["days"] = None
    others = [dict(r) for r in conn.execute(
        "SELECT p.* FROM procore_rfis p WHERE p.link_state='other' AND p.summary IS NOT NULL AND "
        "EXISTS (SELECT 1 FROM rfi_events e WHERE e.procore_rfi_id=p.id AND e.occurred_at >= ?) "
        "ORDER BY p.project, p.number", (cut,)).fetchall()]
    return {"answered": out_answered, "waiting": waiting, "others": others}
