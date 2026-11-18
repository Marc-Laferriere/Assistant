"""RFIs we wrote, beyond Procore notices: their own PDFs, replies by email, review chatter.

* **Draft PDFs.** An internal RFI email usually carries its question in an attached PDF, so
  the email alone gives a thin title. The PDF is fetched read-only and read by the AI.
* **Replies by email.** A reply can reach the user as an ordinary email - from the consultant,
  or forwarded by a colleague - instead of a Procore notice (for example when the user is not
  yet on the mechanical contractor's Procore distribution). The AI decides whether such an
  email answers one of the user's open RFIs, and reads the attached response PDF.
* **One task per RFI.** Review comments on a draft come as several emails; they share a
  `group_key` so the review queue folds them into one task per RFI.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import notion_api as N
from .config import load_config
from .db import log_event, now_iso
from .drawings import _heading, _page_open
from .llm import complete_json
from .meetings import _fetch_pdfs
from .notify import toast

log = logging.getLogger(__name__)

_RECENT_DAYS = 60   # how far back an email can be and still be judged as a reply
_STALE_DAYS = 3     # an older reply is history: it updates the RFI's status but raises no alert
_TEXT_BLOCK = 1900

_PDF_SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}, "question": {"type": "string"}},
               "required": ["title", "question"]}
_PDF_SYSTEM = (
    "You read a draft RFI (Request For Information) written by a sheet-metal / HVAC fabricator "
    "to send to the general contractor. Return `title`: a short topic title with no RFI number, "
    "and `question`: one or two sentences saying what is being asked.")

_REPLY_SCHEMA = {"type": "object", "properties": {
    "is_response": {"type": "boolean"}, "match": {"type": "integer", "nullable": True},
    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    "summary": {"type": "string"}}, "required": ["is_response", "confidence", "summary"]}
_REPLY_SYSTEM = (
    "A sheet-metal fabricator has sent RFIs (Requests For Information) through its general "
    "contractor. This email may be the RESPONSE to one of them - from the consultant or "
    "contractor, possibly forwarded by a colleague. Numbers differ between systems, so match by "
    "subject matter, not by number. Return is_response=true only if it answers one of the open "
    "RFIs listed, `match` = that RFI's number in the list, and confidence high only if sure. "
    "`summary`: what the response says, in one to three plain sentences (read the attached "
    "PDF if there is one). If it is not a response to one of them, is_response=false.")


def _model() -> str:
    return load_config()["ai"]["triage_model"]


# --------------------------------------------------------------------------- #
# 1. the RFI's own PDF -> a real title and question
# --------------------------------------------------------------------------- #
def enrich_from_pdfs(conn) -> int:
    rows = conn.execute(
        "SELECT o.id AS rfi_id, m.* FROM our_rfis o "
        "JOIN our_rfi_threads t ON t.our_rfi_id = o.id AND t.checked = 1 "
        "JOIN messages m ON m.thread_key = t.thread_key AND m.has_attach = 1 AND m.folder != 'Sent' "
        "WHERE o.pdf_read = 0 ORDER BY o.id, m.date_utc").fetchall()
    done, seen = 0, set()
    for m in rows:
        if m["rfi_id"] in seen:
            continue
        seen.add(m["rfi_id"])
        files = _fetch_pdfs(m)
        if files is None:
            continue  # could not reach the mail server; try again next cycle
        if files:
            try:
                data = complete_json(_model(), _PDF_SYSTEM, f"Email subject: {m['subject']}",
                                     schema=_PDF_SCHEMA, max_tokens=500, thinking_budget=0, files=files)
            except Exception as exc:  # noqa: BLE001 - retried next cycle
                log.error("could not read RFI PDF for %r: %s", m["subject"], exc)
                continue
            title, question = (data.get("title") or "").strip(), (data.get("question") or "").strip()
            conn.execute("UPDATE our_rfis SET title=COALESCE(NULLIF(?,''), title), "
                         "question=COALESCE(NULLIF(?,''), question) WHERE id=?", (title, question, m["rfi_id"]))
            log.info("read the PDF for our RFI %s: %r", m["rfi_id"], title)
        conn.execute("UPDATE our_rfis SET pdf_read = 1 WHERE id = ?", (m["rfi_id"],))
        done += 1
    conn.commit()
    return done


# --------------------------------------------------------------------------- #
# 2. replies that arrive as ordinary email
# --------------------------------------------------------------------------- #
def looks_like_rfi_reply(subject: str, body: str) -> bool:
    """Cheap filter, so the AI is only asked about emails that could be a reply."""
    # TODO(human): list the wording your consultants and contractors use when they answer an
    # RFI - "response", "answered", "reply", "clarification", "per your RFI" ... Return True
    # when the subject or body suggests this email is an answer. A wider net costs a few
    # more AI calls; a narrower one can miss a reply.
    return "response" in (subject or "").lower()


def detect(conn, since: str) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_RECENT_DAYS)).isoformat()
    open_rfis = conn.execute(
        "SELECT * FROM our_rfis WHERE status IN ('internal','submitted') ORDER BY id").fetchall()
    if not open_rfis:
        return 0
    listing = "\n".join(f"{i}: ({o['project']}) {o['label']} - {o['title']}: {o['question']}"
                        for i, o in enumerate(open_rfis, 1))
    rows = conn.execute(
        "SELECT m.* FROM messages m WHERE m.folder != 'Sent' AND m.subject LIKE '%RFI%' "
        "AND lower(m.from_addr) NOT LIKE '%procoretech.com' AND m.date_utc >= ? "
        "AND NOT EXISTS (SELECT 1 FROM rfi_email_events e WHERE e.message_id = m.message_id) "
        "AND NOT EXISTS (SELECT 1 FROM our_rfi_threads t WHERE t.thread_key = m.thread_key AND t.checked = 1) "
        "ORDER BY m.date_utc", (cutoff,)).fetchall()
    stale_cut = (datetime.now(timezone.utc) - timedelta(days=_STALE_DAYS)).isoformat()
    found = 0
    for m in rows:
        if not looks_like_rfi_reply(m["subject"], m["body_text"] or ""):
            continue
        files = _fetch_pdfs(m) if m["has_attach"] else []
        if files is None:
            continue  # transient; try again next cycle
        try:
            data = complete_json(
                _model(), _REPLY_SYSTEM,
                f"Open RFIs:\n{listing}\n\nEmail subject: {m['subject']}\nFrom: {m['from_addr']}\n\n"
                f"{(m['body_text'] or '')[:3500]}",
                schema=_REPLY_SCHEMA, max_tokens=500, thinking_budget=0, files=files or None)
        except Exception as exc:  # noqa: BLE001 - retried next cycle
            log.error("RFI reply check failed for %r: %s", m["subject"], exc)
            continue
        idx = data.get("match")
        target = None
        if (data.get("is_response") and data.get("confidence") == "high"
                and isinstance(idx, int) and 1 <= idx <= len(open_rfis)):
            target = open_rfis[idx - 1]
        when = m["date_utc"] or m["first_seen_at"] or ""
        stale = (m["first_seen_at"] or "") < stale_cut
        conn.execute(
            "INSERT OR IGNORE INTO rfi_email_events (message_id, our_rfi_id, response_text, occurred_at, handled) "
            "VALUES (?,?,?,?,?)",
            (m["message_id"], target["id"] if target else None, (data.get("summary") or "").strip(),
             when, 1 if (stale or not target) else 0))
        if target and stale:
            # History: keep the waiting list honest without raising an alert for old news.
            conn.execute("UPDATE our_rfis SET status='answered', last_event_at=? WHERE id=? AND status IN "
                         "('internal','submitted')", (when, target["id"]))
        if target:
            found += 1
            open_rfis = [o for o in open_rfis if o["id"] != target["id"]] if not stale else open_rfis
            log.info("email reply to our RFI %s: %s", target["id"], m["subject"][:70])
        conn.commit()
    return found


def handle(conn, notion, directory, notify: bool) -> int:
    tasks_db = load_config()["notion"]["tasks_db_id"]
    cache: dict[str, bool] = {}
    done = 0
    for e in conn.execute(
        "SELECT e.*, o.project, o.label, o.title, o.question, o.task_page_id FROM rfi_email_events e "
        "JOIN our_rfis o ON o.id = e.our_rfi_id WHERE e.handled = 0 ORDER BY e.occurred_at").fetchall():
        label = f"{e['label']} {e['title']}".strip()
        blocks = [_heading(f"Response received by email ({(e['occurred_at'] or '')[:10]})")]
        text = (e["response_text"] or "(see the email)").strip()
        blocks += [N.paragraph_block(text[i:i + _TEXT_BLOCK]) for i in range(0, len(text), _TEXT_BLOCK)]
        try:
            page_id = e["task_page_id"]
            if page_id and _page_open(notion, page_id, cache):
                notion.append_blocks(page_id, blocks)
            else:
                props = {
                    "Task name": N.title(f"RFI reply: {label} (by email)"[:200]),
                    "Priority": N.select("High"), "Horizon": N.select("Active"),
                    "Status": N.status("Not started"), "Added by assistant": N.checkbox(True),
                    "Origin": N.rich_text(f"Reply by email to your RFI: {label}"),
                }
                _, pid = directory.resolve(e["project"])
                if pid:
                    props["Project"] = N.relation([pid])
                page_id = notion.create_page(tasks_db, props)["id"]
                intro = f"Your RFI \"{label}\" has been answered."
                if e["question"]:
                    intro += f" You asked: {e['question']}"
                notion.append_blocks(page_id, [N.paragraph_block(intro)] + blocks)
                conn.execute("UPDATE our_rfis SET task_page_id=? WHERE id=?", (page_id, e["our_rfi_id"]))
                log_event(conn, "rfi_email_task_created", notion_task_id=page_id, detail=label)
        except Exception as exc:  # noqa: BLE001 - retried next run
            log.error("could not write the email-reply task for %r: %s", label, exc)
            continue
        conn.execute("UPDATE our_rfis SET status='answered', last_event_at=? WHERE id=?",
                     (e["occurred_at"], e["our_rfi_id"]))
        conn.execute("UPDATE rfi_email_events SET handled=1 WHERE message_id=?", (e["message_id"],))
        conn.commit()
        done += 1
        if notify:
            toast("Your RFI was answered (by email)", f"{e['project'] or ''} {label}: {text[:100]}")
    return done


# --------------------------------------------------------------------------- #
# 3. one task per RFI, and not twice for a reply
# --------------------------------------------------------------------------- #
def group_key_for_thread(conn, thread_key: str | None) -> str | None:
    if not thread_key:
        return None
    r = conn.execute("SELECT our_rfi_id FROM our_rfi_threads WHERE thread_key=? AND checked=1 "
                     "AND our_rfi_id > 0", (thread_key,)).fetchone()
    return f"ourrfi:{r[0]}" if r else None


def reply_message_ids(conn) -> set[str]:
    """Emails already recognised as replies to our RFIs - they must not also become tasks."""
    return {r[0] for r in conn.execute("SELECT message_id FROM rfi_email_events WHERE our_rfi_id IS NOT NULL")}
