"""Review queue: "add to" - fold one queued item into a related task.

Two email chains about the same job often produce two tasks. Rather than reject one
and lose what only it mentions, the user adds it to the other:

  * the target can be another queued item or a task already in Notion;
  * the AI writes a one-line note of what the new item adds, which the user confirms;
  * the note is appended to the target's page body (for a queued target, when that
    target is accepted); the higher priority and earlier due date carry over;
  * a queued target that is later rejected hands its merged items back to the queue.

The assistant also suggests a target when it sees one (merge_suggestions), but never
merges on its own.
"""

from __future__ import annotations

import logging
import re
from datetime import date

from . import notion_api as N
from .config import load_config
from .db import now_iso
from .llm import complete_json
from .projects import ProjectDirectory

log = logging.getLogger(__name__)

_PRIORITY_RANK = {"Low": 0, "Medium": 1, "High": 2}   # Urgent is the user's own flag: never touched
_STOP = {
    "the", "and", "for", "with", "from", "that", "this", "are", "was", "has", "have", "any", "all",
    "per", "via", "into", "onto", "re", "fw", "fwd", "review", "check", "confirm", "update", "send",
    "reply", "email", "request", "new", "current", "latest", "regarding", "about", "please",
}
_MIN_SCORE = 2.0       # topic score a target needs before the AI is even asked about it
_TOP_K = 6             # how many ranked targets are shown / judged

_SUGGEST_SCHEMA = {"type": "object", "properties": {
    "match": {"type": "integer", "nullable": True}, "adds": {"type": "string"}},
    "required": ["adds"]}
_SUGGEST_SYSTEM = (
    "A new to-do was proposed from an email. Decide whether it is part of the SAME piece of "
    "work as one of the existing items listed - the same subject, location or element (for "
    "example two email chains about the same penetrations on the same floor) - and not merely "
    "the same project. Return `match` as the number of that existing item, or null if none "
    "fits. `adds` is ONE short line saying what the new item contains that the existing item "
    "does not (a different detail, extra element, new deadline). Plain words, no preamble.")
_ADDS_SYSTEM = (
    "A new to-do was proposed from an email, and the user wants to add it to an existing item. "
    "Return `adds`: ONE short line saying what the new item contains that the existing item "
    "does not (a different detail, extra element, new deadline). Plain words, no preamble.")


# --------------------------------------------------------------------------- #
# how alike are two items?
# --------------------------------------------------------------------------- #
def _tokens(text: str | None) -> set[str]:
    words = re.findall(r"[a-z0-9][a-z0-9\-\.]*", (text or "").lower())
    return {w.strip("-.") for w in words if len(w.strip("-.")) >= 2 and w.strip("-.") not in _STOP}


# Words that turn up in half of all duct-detailing tasks and so say nothing about *which* job
# two items are about. Add to this list as new ones prove noisy.
_FILLER = {"duct", "ductwork", "layout", "coordination", "model", "drawing", "drawings"}


def token_weight(token: str) -> float:
    """How much one shared word says that two items are about the same thing.

    Anything with a digit (a level like l12, a tag like ef-5, an RFI/SI number) pins down the
    subject and counts triple; filler words count nothing; every other word counts once.
    """
    if token in _FILLER:
        return 0.0
    if any(ch.isdigit() for ch in token):
        return 3.0
    return 1.0


def topic_score(cand_text: str, target_text: str, same_project: bool) -> float:
    shared = _tokens(cand_text) & _tokens(target_text)
    return sum(token_weight(t) for t in shared) + (1.0 if same_project else 0.0)


# --------------------------------------------------------------------------- #
# the pool of things an item could be added to
# --------------------------------------------------------------------------- #
_notion_cache: dict[str, list[dict]] = {}


def open_notion_tasks(notion, directory: ProjectDirectory, tasks_db_id: str) -> list[dict]:
    if tasks_db_id in _notion_cache:
        return _notion_cache[tasks_db_id]
    flt = {"and": [
        {"property": "Status", "status": {"does_not_equal": "Done"}},
        {"property": "Status", "status": {"does_not_equal": "Archived"}},
        {"property": "Horizon", "select": {"equals": "Active"}},
    ]}
    out = []
    for page in notion.iter_database(tasks_db_id, flt=flt):
        p = page["properties"]
        rel = p.get("Project", {}).get("relation") or []
        out.append({
            "kind": "notion", "ref": page["id"],
            "title": N.plain(p.get("Task name")) or "(untitled)",
            "project": directory.name_for_id(rel[0]["id"]) if rel else None,
            "priority": N.plain(p.get("Priority")), "due": N.plain(p.get("Due")),
            "extra": " ".join(filter(None, [N.plain(p.get("Origin")), N.plain(p.get("Notes"))])),
        })
    _notion_cache[tasks_db_id] = out
    return out


def ranked_targets(conn, notion, directory, cand: dict, *, limit: int = _TOP_K) -> list[dict]:
    cfg = load_config()
    pool = [{"kind": "candidate", "ref": str(r["id"]), "title": r["title"], "project": r["project"],
             "priority": r["priority"], "due": r["due"], "extra": r["reason"] or ""}
            for r in conn.execute(
                "SELECT id, title, project, priority, due, reason FROM task_candidates "
                "WHERE status='proposed' AND source_type='email' AND id != ?", (cand["id"],))]
    pool += [t for t in open_notion_tasks(notion, directory, cfg["notion"]["tasks_db_id"])
             if t["title"] != cand["title"]]
    c_proj, _ = directory.resolve(cand.get("project"))
    c_text = f"{cand['title']} {cand.get('reason') or ''} {cand.get('subject') or ''}"
    for t in pool:
        t_proj, _ = directory.resolve(t.get("project"))
        t["score"] = topic_score(c_text, f"{t['title']} {t.get('extra') or ''}",
                                 bool(c_proj and c_proj == t_proj))
    pool.sort(key=lambda t: t["score"], reverse=True)
    return pool[:limit]


# --------------------------------------------------------------------------- #
# suggestions (made once, when an item enters the queue)
# --------------------------------------------------------------------------- #
def suggest(conn, notion=None, directory=None) -> int:
    rows = conn.execute(
        "SELECT tc.*, m.subject FROM task_candidates tc LEFT JOIN messages m ON m.message_id = tc.message_id "
        "WHERE tc.status='proposed' AND tc.source_type='email' AND NOT EXISTS "
        "(SELECT 1 FROM merge_suggestions s WHERE s.candidate_id = tc.id)").fetchall()
    if not rows:
        return 0
    from .config import SECRET_NOTION_TOKEN
    from .secrets_store import get_secret
    notion = notion or N.Notion(get_secret(SECRET_NOTION_TOKEN))
    directory = directory or ProjectDirectory(notion, load_config()["notion"]["projects_db_id"])
    model = load_config()["ai"]["triage_model"]
    made = 0
    for r in rows:
        cand = dict(r)
        near = [t for t in ranked_targets(conn, notion, directory, cand) if t["score"] >= _MIN_SCORE]
        target, note = None, ""
        if near:
            listing = "\n".join(f"{i}: {t['title']}" for i, t in enumerate(near, 1))
            try:
                data = complete_json(
                    model, _SUGGEST_SYSTEM,
                    f"New item: {cand['title']}\nWhy: {cand.get('reason') or ''}\n"
                    f"From: {cand.get('detail') or ''}\n\nExisting items:\n{listing}",
                    schema=_SUGGEST_SCHEMA, max_tokens=300, thinking_budget=0)
            except Exception as exc:  # noqa: BLE001 - asked again next cycle
                log.error("merge suggestion failed for %r: %s", cand["title"], exc)
                continue
            idx = data.get("match")
            if isinstance(idx, int) and 1 <= idx <= len(near):
                target, note = near[idx - 1], (data.get("adds") or "").strip()
        conn.execute(
            "INSERT OR REPLACE INTO merge_suggestions (candidate_id, target_kind, target_ref, target_title, "
            "note, created_at) VALUES (?,?,?,?,?,?)",
            (cand["id"], target["kind"] if target else None, target["ref"] if target else None,
             target["title"] if target else None, note, now_iso()))
        made += 1
    conn.commit()
    return made


def suggestion_line(conn, candidate_id: int) -> str | None:
    s = conn.execute("SELECT target_title FROM merge_suggestions WHERE candidate_id=? AND target_ref IS NOT NULL",
                     (candidate_id,)).fetchone()
    return f"looks like part of: {s['target_title']}   (press t to add it)" if s else None


# --------------------------------------------------------------------------- #
# the merge itself
# --------------------------------------------------------------------------- #
def _adds_note(cand: dict, target: dict) -> str:
    try:
        data = complete_json(
            load_config()["ai"]["triage_model"], _ADDS_SYSTEM,
            f"New item: {cand['title']}\nWhy: {cand.get('reason') or ''}\nFrom: {cand.get('detail') or ''}\n\n"
            f"Existing item: {target['title']}", schema=_SUGGEST_SCHEMA, max_tokens=200, thinking_budget=0)
        return (data.get("adds") or "").strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("could not draft the note: %s", exc)
        return ""


def carry_over(cand: dict, cur_priority: str | None, cur_due: str | None) -> dict[str, str | None]:
    """The higher priority and the earlier due date win. Urgent is never touched."""
    out: dict[str, str | None] = {}
    c_pri = cand.get("priority")
    if (cur_priority != "Urgent" and c_pri in _PRIORITY_RANK
            and _PRIORITY_RANK[c_pri] > _PRIORITY_RANK.get(cur_priority, -1)):
        out["priority"] = c_pri
    c_due = (cand.get("due") or "")[:10]
    if c_due and (not cur_due or c_due < cur_due[:10]):
        out["due"] = c_due
    return out


def _note_blocks(note: str, detail: str) -> list[dict]:
    blocks = [N.paragraph_block(f"Also from email ({date.today().isoformat()}): {note}".strip())]
    if detail:
        blocks.append(N.paragraph_block(detail))
    return blocks


def apply_merge(conn, notion, cand: dict, target: dict, note: str) -> dict:
    """Fold `cand` into `target`. Returns the priority/due changes that were applied."""
    if target["kind"] == "notion":
        live = notion.get_page(target["ref"])["properties"]
        changes = carry_over(cand, N.plain(live.get("Priority")), N.plain(live.get("Due")))
        notion.append_blocks(target["ref"], _note_blocks(note, cand.get("detail") or ""))
        props = {}
        if "priority" in changes:
            props["Priority"] = N.select(changes["priority"])
        if "due" in changes:
            props["Due"] = N.date(changes["due"])
        if props:
            notion.update_page(target["ref"], props)
    else:
        tgt = conn.execute("SELECT priority, due FROM task_candidates WHERE id=?", (int(target["ref"]),)).fetchone()
        changes = carry_over(cand, tgt["priority"], tgt["due"])
        for col in ("priority", "due"):
            if col in changes:
                conn.execute(f"UPDATE task_candidates SET {col}=? WHERE id=?", (changes[col], int(target["ref"])))
        conn.execute(
            "INSERT OR REPLACE INTO candidate_merges (candidate_id, target_ref, note, detail, created_at) "
            "VALUES (?,?,?,?,?)", (cand["id"], target["ref"], note, cand.get("detail") or "", now_iso()))
    conn.execute("UPDATE task_candidates SET status='merged', decided_at=? WHERE id=?", (now_iso(), cand["id"]))
    conn.execute(
        "INSERT INTO feedback (candidate_id, source_type, decision, reason, detail, created_at) "
        "VALUES (?, ?, 'merged', 'merged_into', ?, ?)",
        (cand["id"], cand["source_type"], f"{cand['title']} -> {target['title']}", now_iso()))
    conn.commit()
    return changes


def apply_pending_merges(conn, notion, target_candidate_id: int, page_id: str) -> int:
    """When a queued target is accepted, write the notes merged into it onto its new page."""
    rows = conn.execute("SELECT * FROM candidate_merges WHERE target_ref=? AND applied=0",
                        (str(target_candidate_id),)).fetchall()
    for r in rows:
        notion.append_blocks(page_id, _note_blocks(r["note"], r["detail"] or ""))
        conn.execute("UPDATE candidate_merges SET applied=1 WHERE candidate_id=?", (r["candidate_id"],))
    conn.commit()
    return len(rows)


def unmerge(conn, target_candidate_id: int) -> int:
    """A queued target was rejected: give the items merged into it back to the queue."""
    rows = conn.execute("SELECT candidate_id FROM candidate_merges WHERE target_ref=? AND applied=0",
                        (str(target_candidate_id),)).fetchall()
    for r in rows:
        conn.execute("UPDATE task_candidates SET status='proposed', decided_at=NULL WHERE id=?", (r[0],))
        conn.execute("DELETE FROM candidate_merges WHERE candidate_id=?", (r[0],))
    conn.commit()
    return len(rows)


# --------------------------------------------------------------------------- #
# the [t] prompt
# --------------------------------------------------------------------------- #
def interactive_merge(conn, notion, directory, row: dict) -> bool:
    """Walk the user through adding `row` to another item. True if it was merged."""
    cand = dict(row)
    cand["subject"] = (conn.execute("SELECT subject FROM messages WHERE message_id=?",
                                    (row.get("message_id"),)).fetchone() or {"subject": ""})["subject"]
    sug = conn.execute("SELECT * FROM merge_suggestions WHERE candidate_id=? AND target_ref IS NOT NULL",
                       (row["id"],)).fetchone()
    ranked = ranked_targets(conn, notion, directory, cand, limit=8)
    target, note = None, ""

    if sug:
        match = next((t for t in ranked if t["ref"] == sug["target_ref"] and t["kind"] == sug["target_kind"]), None)
        if match:
            print(f"  add to: {match['title']}")
            ans = input("  [Enter]=yes  [l]ist others  [n]o > ").strip().lower()
            if ans == "":
                target, note = match, sug["note"] or ""
            elif ans == "n":
                return False
    if target is None:
        if not ranked:
            print("  nothing to add this to.")
            return False
        for n, t in enumerate(ranked, 1):
            where = "queue" if t["kind"] == "candidate" else "Notion"
            print(f"    {n}) [{where}] {t['project'] or '-'} · {t['title'][:90]}")
        pick = input("  add to which? number, or Enter = cancel > ").strip()
        if not (pick.isdigit() and 1 <= int(pick) <= len(ranked)):
            return False
        target = ranked[int(pick) - 1]

    if not note:
        note = _adds_note(cand, target)
    typed = input(f"  note to add [{note or 'none drafted'}]\n  Enter = keep, or type your own > ").strip()
    note = typed or note or cand["title"]
    changes = apply_merge(conn, notion, cand, target, note)
    extra = "".join(f"; {k} -> {v}" for k, v in changes.items())
    print(f"  -> added to: {target['title'][:80]}{extra}")
    return True
