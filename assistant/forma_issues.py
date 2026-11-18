"""Forma issue notices -> one checklist task per project.

Autodesk Forma emails a notice for every assignment, watcher change, comment and
status change on an issue. They used to become one task each. Now they are parsed
(no AI) into `forma_issues`, and every project gets a single task (source_type
'issues') whose body is a checklist: one checkbox per issue, rewritten in place as
comments arrive. Accepting the candidate in the review queue creates the task;
afterwards it is kept current automatically while it stays open.

An issue leaves the list - is ticked, with a note saying which RFI - once an RFI of
ours covers it. Matching is by subject matter (AI), against RFIs from two places:
the colleague tracker (`colleague_notion`, read-only) and RFIs we registered from
email (`our_rfis`). A drafted RFI only adds a note; a cancelled one covers nothing,
and if an RFI is cancelled later its issue is un-ticked again. Nothing is deleted
from Notion: ticking is reversible, deleting a block is not.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass

from . import colleague_notion
from . import notion_api as N
from .config import SECRET_NOTION_TOKEN, load_config
from .db import log_event, now_iso
from .drawings import _BLOCK_CHUNK, _page_open, _project_label
from .llm import complete_json
from .projects import ProjectDirectory
from .secrets_store import get_secret

log = logging.getLogger(__name__)

_ISSUE_RE = re.compile(r"\bissue\s*#\s*(\d+)\b", re.I)
_CLOSED = {"closed", "resolved"}
_COMMENT_CHARS = 240


# --------------------------------------------------------------------------- #
# recognising and parsing notices
# --------------------------------------------------------------------------- #
def is_notice(subject: str | None, from_addr: str | None) -> bool:
    return "forma.autodesk.com" in (from_addr or "").lower() and bool(_ISSUE_RE.search(subject or ""))


def _collapse(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


@dataclass
class Notice:
    project_text: str
    number: int
    title: str = ""
    status: str = ""
    assignee: str = ""
    comment: str = ""


def parse_notice(subject: str | None, body: str | None) -> Notice | None:
    m = _ISSUE_RE.search(subject or "") or _ISSUE_RE.search(body or "")
    if not m:
        return None
    number = int(m.group(1))
    t = _collapse(body)
    project = (subject or "").split(" - ", 1)[0].strip() if " - " in (subject or "") else ""
    tm = re.search(rf"Issue\s*#\s*{number}\s+(.+?)\s+(?:What.s changed|Comment|Details)\b", t)
    cm = re.search(r"\bComment (.+?) Details Status\b", t)
    dm = re.search(r"Details Status (.+?) Type .+? Assignee (.+?) Due date", t)
    return Notice(project, number, tm.group(1).strip() if tm else "",
                  dm.group(1).strip().lower() if dm else "",
                  dm.group(2).strip() if dm else "",
                  cm.group(1).strip() if cm else "")


def _ingest(conn, directory) -> int:
    n = 0
    for msg in conn.execute(
        "SELECT * FROM messages m WHERE m.folder != 'Sent' AND NOT EXISTS "
        "(SELECT 1 FROM forma_issue_notices f WHERE f.message_id = m.message_id) ORDER BY m.date_utc"
    ).fetchall():
        if not is_notice(msg["subject"], msg["from_addr"]):
            continue
        conn.execute("INSERT OR IGNORE INTO forma_issue_notices (message_id, parsed_at) VALUES (?,?)",
                     (msg["message_id"], now_iso()))
        note = parse_notice(msg["subject"], msg["body_text"])
        if note is None or not note.project_text:
            continue
        project = _project_label(directory, note.project_text)
        conn.execute(
            "INSERT INTO forma_issues (project, number, title, status, assignee, last_comment, "
            "n_comments, last_notice_at) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(project, number) DO UPDATE SET "
            "title = COALESCE(NULLIF(excluded.title, ''), title), "
            "status = COALESCE(NULLIF(excluded.status, ''), status), "
            "assignee = COALESCE(NULLIF(excluded.assignee, ''), assignee), "
            "last_comment = COALESCE(NULLIF(excluded.last_comment, ''), last_comment), "
            "n_comments = n_comments + excluded.n_comments, last_notice_at = excluded.last_notice_at, "
            # something new happened on an issue that had been written off as handled: look again
            "candidate_id = CASE WHEN candidate_id = 0 THEN NULL ELSE candidate_id END",
            (project, note.number, note.title, note.status, note.assignee, note.comment,
             1 if note.comment else 0, msg["date_utc"]))
        n += 1
    conn.commit()
    return n


# --------------------------------------------------------------------------- #
# which RFI (if any) covers an issue
# --------------------------------------------------------------------------- #
def _rfi_now(conn, rfi_key: str) -> tuple[str, str]:
    """(state, label) of an RFI as it stands now. state: raised | draft | cancelled | other."""
    kind, _, ref = rfi_key.partition(":")
    if kind == "page":
        r = conn.execute("SELECT subject, status, numbers FROM colleague_rfis WHERE page_id = ?",
                         (ref,)).fetchone()
        if r is None:
            return "cancelled", ""
        return colleague_notion.rfi_state(r["status"]), r["numbers"] or r["subject"]
    r = conn.execute("SELECT label, title, status FROM our_rfis WHERE id = ?", (ref,)).fetchone()
    if r is None:
        return "cancelled", ""
    state = "draft" if r["status"] == "internal" else "raised"
    return state, r["label"] or r["title"] or "our RFI"


def _coverage(conn, issue_id: int) -> tuple[str | None, str | None]:
    """(label of an RFI that has gone out, label of one only drafted)."""
    raised = draft = None
    for m in conn.execute("SELECT rfi_key FROM forma_issue_rfis WHERE issue_id = ?", (issue_id,)):
        state, label = _rfi_now(conn, m["rfi_key"])
        if state == "raised" and raised is None:
            raised = label or "RFI"
        elif state == "draft" and draft is None:
            draft = label or "RFI"
    return raised, draft


_MATCH_SCHEMA = {
    "type": "object",
    "properties": {"matches": {"type": "array", "items": {
        "type": "object",
        "properties": {"issue": {"type": "integer"}, "rfi": {"type": "string"}},
        "required": ["issue", "rfi"]}}},
    "required": ["matches"],
}

_MATCH_SYSTEM = """You match design-coordination issues to RFIs for a duct detailer.

An issue is a short item someone logged in Autodesk Forma (a clash, a discrepancy, a missing
detail). An RFI is a formal question sent to the design team. An RFI covers an issue only when it
asks about the same specific thing: the same piece of equipment, shaft, wall or location AND the same
problem. A shared project, trade or general topic is not enough, and neither is a similar-sounding
room or unit number. When unsure, do not match. One RFI may cover several issues. Return only
matches you are confident in, using the issue numbers and RFI keys exactly as given."""


def _candidate_rfis(conn, project: str) -> list[dict]:
    out = []
    cfg = colleague_notion.settings()
    if cfg and (cfg.get("project") or "").strip().lower() == project.strip().lower():
        for r in conn.execute("SELECT page_id, subject, status, numbers, notes FROM colleague_rfis"):
            if colleague_notion.rfi_state(r["status"], cfg) != "cancelled":
                out.append({"key": f"page:{r['page_id']}", "text": f"{r['numbers'] or ''} {r['subject']}".strip(),
                            "notes": r["notes"] or ""})
    for r in conn.execute("SELECT id, label, title, question, status FROM our_rfis"
                          " WHERE lower(project) = lower(?)", (project,)):
        out.append({"key": f"our:{r['id']}", "text": f"{r['label'] or ''} {r['title'] or ''}".strip(),
                    "notes": (r["question"] or "")[:300]})
    return out


def match_rfis(conn) -> int:
    """Ask the model which open issues are covered by an RFI. Skipped when nothing changed."""
    projects = [r[0] for r in conn.execute(
        "SELECT DISTINCT project FROM forma_issues WHERE status NOT IN ('closed','resolved')")]
    added = 0
    for project in projects:
        issues = conn.execute(
            "SELECT id, number, title, last_comment, n_comments FROM forma_issues WHERE project = ? "
            "AND status NOT IN ('closed','resolved') AND NOT EXISTS "
            "(SELECT 1 FROM forma_issue_rfis f WHERE f.issue_id = forma_issues.id)", (project,)).fetchall()
        rfis = _candidate_rfis(conn, project)
        if not issues or not rfis:
            continue
        sig = hashlib.sha1(repr(([(i["id"], i["title"], i["n_comments"]) for i in issues],
                                 [(r["key"], r["text"]) for r in rfis])).encode()).hexdigest()
        state_key = f"issue_rfi_sig:{project}"
        row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (state_key,)).fetchone()
        if row and row[0] == sig:
            continue
        keys = {f"R{n}": r for n, r in enumerate(rfis, 1)}
        user = ("ISSUES:\n" + "\n".join(
            f"#{i['number']}: {i['title']}" + (f" (latest comment: {i['last_comment'][:200]})" if i["last_comment"] else "")
            for i in issues) + "\n\nRFIs:\n" + "\n".join(
            f"{k}: {r['text']}" + (f" - {r['notes'][:200]}" if r["notes"] else "") for k, r in keys.items()))
        try:
            data = complete_json(load_config()["ai"]["reasoning_model"], _MATCH_SYSTEM, user,
                                 schema=_MATCH_SCHEMA, max_tokens=800)
        except Exception as exc:  # noqa: BLE001 - retried next cycle; the list still works without it
            log.warning("issue/RFI matching skipped: %s", exc)
            continue
        by_number = {i["number"]: i for i in issues}
        for m in data.get("matches", []):
            issue, rfi = by_number.get(m.get("issue")), keys.get(str(m.get("rfi", "")).strip())
            if issue is None or rfi is None:
                continue
            cur = conn.execute("INSERT OR IGNORE INTO forma_issue_rfis (issue_id, rfi_key, label, matched_at) "
                               "VALUES (?,?,?,?)", (issue["id"], rfi["key"], rfi["text"][:120], now_iso()))
            added += cur.rowcount
            log.info("issue #%s (%s) matched to RFI %r", issue["number"], project, rfi["text"])
        conn.execute("INSERT INTO sync_state (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (state_key, sig))
    conn.commit()
    return added


# --------------------------------------------------------------------------- #
# the Notion checklist
# --------------------------------------------------------------------------- #
def _render(conn, i) -> tuple[str, bool]:
    """(text, ticked) for an issue's checkbox."""
    raised, draft = _coverage(conn, i["id"])
    status = (i["status"] or "").lower()
    closed = status in _CLOSED
    text = f"#{i['number']} {i['title'] or '(untitled)'}"
    if i["last_comment"]:
        text += f" - latest comment: \"{i['last_comment'][:_COMMENT_CHARS]}\""
    tags = []
    if raised:
        tags.append(f"RFI raised: {raised}")
    elif draft:
        tags.append(f"RFI drafted: {draft}")
    if closed:
        tags.append("closed in Forma")
    elif status and status != "open":
        tags.append(f"Forma status: {status}")
    if tags:
        text += "  [" + "; ".join(tags) + "]"
    return text[:1990], bool(raised or closed)


def _todo(text: str, checked: bool) -> dict:
    return {"object": "block", "type": "to_do",
            "to_do": {"rich_text": [{"type": "text", "text": {"content": text}}], "checked": checked}}


def _issues(conn, cid: int):
    return conn.execute("SELECT * FROM forma_issues WHERE candidate_id = ? ORDER BY number", (cid,)).fetchall()


def sync_candidate(conn, notion: N.Notion, page_id: str, cid: int, *, intro: bool = False) -> int:
    """Bring the Notion checklist up to date: add new issues, rewrite changed ones, tick covered ones."""
    new, changed = [], 0
    for i in _issues(conn, cid):
        if i["block_id"] == "gone":
            continue
        text, checked = _render(conn, i)
        if i["block_id"] is None:
            new.append((i, text, checked))
            continue
        if text == i["synced_text"] and int(checked) == i["synced_checked"]:
            continue
        body: dict = {"to_do": {"rich_text": [{"type": "text", "text": {"content": text}}]}}
        if int(checked) != i["synced_checked"]:
            body["to_do"]["checked"] = checked   # leave a box the user ticked by hand alone otherwise
        try:
            notion.update_block(i["block_id"], body)
        except N.NotionError as exc:
            if exc.status in (400, 404):
                conn.execute("UPDATE forma_issues SET block_id = 'gone' WHERE id = ?", (i["id"],))
                log.warning("checkbox for issue #%s is gone from Notion; leaving it", i["number"])
                continue
            raise
        conn.execute("UPDATE forma_issues SET synced_text = ?, synced_checked = ? WHERE id = ?",
                     (text, int(checked), i["id"]))
        changed += 1
    if new:
        if intro:
            notion.append_blocks(page_id, [N.paragraph_block(
                "Collected automatically from Forma issue notices. An issue is ticked once an RFI of "
                "ours covers it (or Forma closes it); the note on the line says which.")])
        for start in range(0, len(new), _BLOCK_CHUNK):
            chunk = new[start:start + _BLOCK_CHUNK]
            res = notion.append_blocks(page_id, [_todo(t, c) for _, t, c in chunk])
            for (i, text, checked), block in zip(chunk, res.get("results", [])):
                conn.execute("UPDATE forma_issues SET block_id = ?, synced_text = ?, synced_checked = ? "
                             "WHERE id = ?", (block["id"], text, int(checked), i["id"]))
    conn.commit()
    return len(new) + changed


def describe(conn, cid: int) -> list[str]:
    lines = []
    for i in _issues(conn, cid):
        raised, draft = _coverage(conn, i["id"])
        extra = f"   [RFI raised: {raised}]" if raised else (f"   [RFI drafted: {draft}]" if draft else "")
        lines.append(f"  - #{i['number']} {i['title'] or '(untitled)'}{extra}")
    return lines


def _refresh_candidate(conn, cid: int) -> None:
    n = len(_issues(conn, cid))
    conn.execute("UPDATE task_candidates SET detail = ?, reason = ? WHERE id = ?",
                 (f"{n} open issue(s) in Forma", "Automated Forma issue notices collected for this project", cid))


# --------------------------------------------------------------------------- #
# older per-issue tasks
# --------------------------------------------------------------------------- #
def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower().replace("&", "and"))


def _old_tasks(conn, project: str, number: int) -> list:
    """Tasks (queued or created) that came from an earlier Forma notice about this issue."""
    out = []
    for r in conn.execute(
        "SELECT tc.id, tc.status, tc.title, tc.notion_page_id, m.subject, m.from_addr FROM task_candidates tc "
        "JOIN messages m ON m.message_id = tc.message_id WHERE tc.source_type = 'email' "
        "AND tc.status IN ('proposed', 'pushed')"):
        if is_notice(r["subject"], r["from_addr"]):
            m = _ISSUE_RE.search(r["subject"])
            head, want = _norm(r["subject"].split(" - ", 1)[0]), _norm(project)
            if m and int(m.group(1)) == number and head and (head in want or want in head):
                out.append(r)
    return out


def _handled_already(conn, notion, cache, project: str, number: int) -> bool:
    """True when the issue had earlier tasks and every one of them is closed - already dealt with."""
    old = [t for t in _old_tasks(conn, project, number) if t["status"] == "pushed" and t["notion_page_id"]]
    if not old:
        return False
    return not any(_page_open(notion, t["notion_page_id"], cache) for t in old)


def fold_queued(conn) -> int:
    """Queued (not yet accepted) per-issue candidates are absorbed into the list."""
    n = 0
    for r in conn.execute(
        "SELECT tc.id, tc.title, m.subject, m.from_addr FROM task_candidates tc "
        "JOIN messages m ON m.message_id = tc.message_id WHERE tc.status = 'proposed' "
        "AND tc.source_type = 'email'").fetchall():
        if not is_notice(r["subject"], r["from_addr"]):
            continue
        conn.execute("UPDATE task_candidates SET status='rejected', decided_at=? WHERE id=?", (now_iso(), r["id"]))
        conn.execute("INSERT INTO feedback (candidate_id, source_type, decision, reason, detail, created_at) "
                     "VALUES (?, 'email', 'rejected', 'merged_into_issue_list', ?, ?)", (r["id"], r["title"], now_iso()))
        n += 1
    conn.commit()
    return n


def retire_old_tasks(conn, notion, cache: dict) -> int:
    """Once a project's list exists, the per-issue tasks it replaces are set to Done (status only)."""
    from datetime import date as date_cls
    done = 0
    projects = {r[0] for r in conn.execute(
        "SELECT p.project FROM forma_issues p JOIN task_candidates tc ON tc.id = p.candidate_id "
        "WHERE tc.source_type = 'issues' AND tc.status = 'pushed'")}
    for project in projects:
        for i in conn.execute("SELECT number FROM forma_issues WHERE project = ?", (project,)).fetchall():
            for t in _old_tasks(conn, project, i["number"]):
                if t["status"] != "pushed" or not t["notion_page_id"]:
                    continue
                key = f"issue_task_retired:{t['id']}"
                if conn.execute("SELECT 1 FROM sync_state WHERE key = ?", (key,)).fetchone():
                    continue
                try:
                    if _page_open(notion, t["notion_page_id"], cache):
                        notion.update_page(t["notion_page_id"], {
                            "Status": N.status("Done"), "Date Completed": N.date(date_cls.today().isoformat())})
                        cache[t["notion_page_id"]] = False
                        log_event(conn, "issue_task_retired", notion_task_id=t["notion_page_id"], detail=t["title"])
                        done += 1
                    conn.execute("INSERT INTO sync_state (key, value) VALUES (?, ?)", (key, now_iso()))
                except Exception as exc:  # noqa: BLE001 - retried next cycle
                    log.warning("could not retire old issue task %r: %s", t["title"], exc)
    conn.commit()
    return done


# --------------------------------------------------------------------------- #
# the pass that runs every cycle
# --------------------------------------------------------------------------- #
def process(conn, notion: N.Notion | None = None) -> dict:
    res = {"parsed": 0, "matched": 0, "synced": 0, "retired": 0}
    try:
        notion = notion or N.Notion(get_secret(SECRET_NOTION_TOKEN))
        directory = ProjectDirectory(notion, load_config()["notion"]["projects_db_id"])
    except Exception as exc:  # noqa: BLE001
        log.error("Forma issue lists skipped (Notion unavailable): %s", exc)
        return res

    fold_queued(conn)
    res["parsed"] = _ingest(conn, directory)
    colleague_notion.refresh(conn)
    res["matched"] = match_rfis(conn)

    cache: dict[str, bool] = {}
    for project in [r[0] for r in conn.execute(
            "SELECT DISTINCT project FROM forma_issues WHERE candidate_id IS NULL")]:
        ready = []
        for i in conn.execute("SELECT * FROM forma_issues WHERE project = ? AND candidate_id IS NULL",
                              (project,)).fetchall():
            raised, _ = _coverage(conn, i["id"])
            if (i["status"] or "") in _CLOSED or raised:
                continue                      # nothing to do about it; reconsidered if that changes
            if _handled_already(conn, notion, cache, project, i["number"]):
                conn.execute("UPDATE forma_issues SET candidate_id = 0 WHERE id = ?", (i["id"],))
                continue                      # its earlier tasks were all closed by hand
            ready.append(i["id"])
        if not ready:
            continue
        target = conn.execute("SELECT id, status, notion_page_id FROM task_candidates "
                              "WHERE source_type='issues' AND project=? ORDER BY id DESC LIMIT 1",
                              (project,)).fetchone()
        cid = None
        if target and target["status"] == "proposed":
            cid = target["id"]
        elif target and target["status"] == "pushed" and target["notion_page_id"] \
                and _page_open(notion, target["notion_page_id"], cache):
            cid = target["id"]
        if cid is None:
            cid = conn.execute(
                "INSERT INTO task_candidates (source_type, title, project, priority, horizon, status, created_at) "
                "VALUES ('issues', ?, ?, 'Medium', 'Active', 'proposed', ?)",
                (f"Forma issues: {project}", project, now_iso())).lastrowid
        for iid in ready:
            conn.execute("UPDATE forma_issues SET candidate_id = ? WHERE id = ?", (cid, iid))
        _refresh_candidate(conn, cid)
    conn.commit()

    for cand in conn.execute("SELECT id, notion_page_id FROM task_candidates WHERE source_type='issues' "
                             "AND status='pushed' AND notion_page_id IS NOT NULL").fetchall():
        try:
            if _page_open(notion, cand["notion_page_id"], cache):
                res["synced"] += sync_candidate(conn, notion, cand["notion_page_id"], cand["id"])
        except Exception as exc:  # noqa: BLE001 - retried next cycle
            log.error("could not update the Forma issues task: %s", exc)
    from .drawings import auto_close
    auto_close(conn, notion, cache, "issues")
    res["retired"] = retire_old_tasks(conn, notion, cache)
    return res
