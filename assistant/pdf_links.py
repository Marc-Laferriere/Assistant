"""Tell the user when a PDF linked into a Revit model needs adding or updating.

The user keeps the PDFs linked into each tower model in one folder per job (on the
company server) and never deletes old revisions. For every architectural /
mechanical / structural sheet in a new-drawings notice (see drawings.py) this
module looks in that job's folder:

  * no PDF for the sheet at all           -> "add it"
  * PDFs for it, but none at the issued revision -> "update it"

and raises one Windows notification plus one Notion task (a checklist) per project.
The folder is only ever *read*. When the right file shows up the entry resolves on
its own, and the task closes once nothing is outstanding.

File names vary by job (drawing name plus a revision tag such as IFC or SI-002), so
the AI reads names into (sheet, tag) pairs and judges whether a file matches an
issued revision.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path

from . import notion_api as N
from .config import SECRET_NOTION_TOKEN, load_config
from .db import log_event, now_iso
from .drawings import _page_open
from .llm import complete_json
from .notify import toast
from .projects import ProjectDirectory
from .secrets_store import get_secret

log = logging.getLogger(__name__)

_PARSE_BATCH = 80
_JUDGE_BATCH = 25

_PARSE_SCHEMA = {"type": "object", "properties": {"files": {"type": "array", "items": {
    "type": "object", "properties": {"i": {"type": "integer"}, "sheet": {"type": "string"},
                                     "tag": {"type": "string"}},
    "required": ["i", "sheet", "tag"]}}}, "required": ["files"]}
_PARSE_SYSTEM = (
    "You read PDF file names from a construction drawing folder. For each numbered file name "
    "return the drawing/sheet number exactly as written in it (for example A207, M3.10, "
    "PMP-N-M501; empty if there is none) and its revision tag (for example IFC, SI-002, "
    "ASI-35, REV 3, a date; empty if there is none). Return one entry per input, using the "
    "same index i.")

_JUDGE_SCHEMA = {"type": "object", "properties": {"results": {"type": "array", "items": {
    "type": "object", "properties": {"n": {"type": "integer"}, "has": {"type": "boolean"},
                                     "reason": {"type": "string"}},
    "required": ["n", "has"]}}}, "required": ["results"]}
_JUDGE_SYSTEM = (
    "A drawing sheet has just been issued in a revision (a named set such as an SI/ASI "
    "instruction, a bulletin, 'IFC', or a revision number). For each numbered sheet you are "
    "given the existing PDF files for that sheet in the user's folder, with the revision tag "
    "read from each file name. Decide whether ANY existing file is the issued revision, "
    "matching tags by meaning (SI-002 = SI#2 = SI 002; the set name or its number identifies "
    "the revision). has=true only if you are confident; if the files are older revisions or "
    "you are unsure, has=false. In `reason` say briefly what the folder has, e.g. 'has SI-046'.")


def _norm(s: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


# --------------------------------------------------------------------------- #
# folder registry
# --------------------------------------------------------------------------- #
def add_folder(conn, project: str, path: str) -> int:
    path = str(Path(path.strip().strip('"')))
    cur = conn.execute("INSERT OR IGNORE INTO pdf_folders (project, path, added_at) VALUES (?,?,?)",
                       (project, path, now_iso()))
    conn.commit()
    return cur.rowcount


def list_folders(conn) -> list:
    return conn.execute("SELECT * FROM pdf_folders ORDER BY project, id").fetchall()


# --------------------------------------------------------------------------- #
# scan and read file names
# --------------------------------------------------------------------------- #
def _parse_names(names: list[str]) -> dict[int, tuple[str, str]]:
    listing = "\n".join(f"{i}: {n}" for i, n in enumerate(names))
    data = complete_json(load_config()["ai"]["triage_model"], _PARSE_SYSTEM, listing,
                         schema=_PARSE_SCHEMA, max_tokens=6000, thinking_budget=0)
    out = {}
    for e in data.get("files", []):
        if isinstance(e.get("i"), int) and 0 <= e["i"] < len(names):
            out[e["i"]] = ((e.get("sheet") or "").strip(), (e.get("tag") or "").strip())
    return out


def scan(conn) -> list[int]:
    """Refresh the cached file list for every registered folder. Returns the ids of the
    folders whose contents changed. An unreachable folder is skipped, never treated as
    empty, so a server hiccup can't cause false alerts."""
    changed: list[int] = []
    for f in list_folders(conn):
        root = Path(f["path"])
        try:
            if not root.is_dir():
                log.warning("PDF folder not reachable, skipping: %s", root)
                continue
            files = {str(p): p for p in root.rglob("*") if p.suffix.lower() == ".pdf" and p.is_file()}
        except OSError as exc:
            log.warning("could not read PDF folder %s: %s", root, exc)
            continue
        known = {r["path"] for r in conn.execute("SELECT path FROM pdf_files WHERE folder_id=?", (f["id"],))}
        gone = known - files.keys()
        if gone:
            conn.executemany("DELETE FROM pdf_files WHERE path=?", [(g,) for g in gone])
        new = [files[p] for p in files if p not in known]
        for p in new:
            try:
                mtime = p.stat().st_mtime
            except OSError:
                mtime = None
            conn.execute("INSERT OR IGNORE INTO pdf_files (path, folder_id, name, mtime) VALUES (?,?,?,?)",
                         (str(p), f["id"], p.name, mtime))
        conn.commit()
        pending = conn.execute("SELECT path, name FROM pdf_files WHERE folder_id=? AND parsed=0",
                               (f["id"],)).fetchall()
        for start in range(0, len(pending), _PARSE_BATCH):
            batch = pending[start:start + _PARSE_BATCH]
            try:
                parsed = _parse_names([b["name"] for b in batch])
            except Exception as exc:  # noqa: BLE001 - retried next cycle
                log.error("could not read file names in %s: %s", root, exc)
                break
            for i, b in enumerate(batch):
                sheet, tag = parsed.get(i, ("", ""))
                conn.execute("UPDATE pdf_files SET sheet=?, tag=?, parsed=1 WHERE path=?",
                             (sheet, tag, b["path"]))
            conn.commit()
        if new or gone:
            changed.append(f["id"])
            log.info("PDF folder %s: %d new, %d removed", root.name, len(new), len(gone))
    return changed


def _candidates(conn, project: str, sheet: str) -> list:
    """Files in this project's folders that are for this sheet."""
    want = _norm(sheet)
    rows = conn.execute(
        "SELECT p.name, p.sheet, p.tag FROM pdf_files p JOIN pdf_folders f ON f.id = p.folder_id "
        "WHERE f.project = ? AND p.parsed = 1", (project,)).fetchall()
    out = []
    boundary = re.compile(r"(?<![A-Za-z0-9])" + re.escape(sheet) + r"(?![0-9])", re.I)
    for r in rows:
        if _norm(r["sheet"]) == want or (not r["sheet"] and boundary.search(r["name"])):
            out.append(r)
    return out


def _sig(files) -> str:
    return hashlib.sha1("|".join(sorted(f["name"] for f in files)).encode()).hexdigest()[:12]


def _judge(project: str, entries: list[dict]) -> dict[int, tuple[bool, str]]:
    """entries: {n, sheet, set_name, rev, files}. Returns n -> (has, reason)."""
    lines = []
    for e in entries:
        have = "; ".join(f"{f['name']} [tag: {f['tag'] or '-'}]" for f in e["files"])
        lines.append(f"{e['n']}: sheet {e['sheet']} issued in set \"{e['set_name']}\""
                     f"{' rev ' + e['rev'] if e['rev'] else ''}\n    existing files: {have}")
    data = complete_json(load_config()["ai"]["triage_model"], _JUDGE_SYSTEM,
                         f"Project: {project}\n\n" + "\n".join(lines),
                         schema=_JUDGE_SCHEMA, max_tokens=2500, thinking_budget=0)
    return {r["n"]: (bool(r.get("has")), (r.get("reason") or "").strip())
            for r in data.get("results", []) if isinstance(r.get("n"), int)}


# --------------------------------------------------------------------------- #
# decide what needs doing
# --------------------------------------------------------------------------- #
def _assess(conn, project: str, rows: list) -> None:
    """Judge drawing items (rows of drawing_items) or open updates against the folder."""
    todo = []
    for r in rows:
        files = _candidates(conn, project, r["sheet"])
        if not files:
            yield_state = ("open", "no PDF for this sheet in the folder", _sig([]))
            _store(conn, r, project, *yield_state)
        else:
            todo.append((r, files))
    for start in range(0, len(todo), _JUDGE_BATCH):
        chunk = todo[start:start + _JUDGE_BATCH]
        entries = [{"n": i, "sheet": r["sheet"], "set_name": r["set_name"] or "", "rev": r["rev"] or "",
                    "files": files} for i, (r, files) in enumerate(chunk)]
        try:
            verdict = _judge(project, entries)
        except Exception as exc:  # noqa: BLE001 - retried next cycle (nothing stored)
            log.error("PDF revision check failed for %s: %s", project, exc)
            continue
        for i, (r, files) in enumerate(chunk):
            has, reason = verdict.get(i, (False, "could not tell"))
            _store(conn, r, project, "ok" if has else "open", reason or "", _sig(files))
    conn.commit()


def _store(conn, item, project: str, state: str, note: str, sig: str) -> None:
    item_id = item["item_id"] if "item_id" in item.keys() else item["id"]
    existing = conn.execute("SELECT id, state FROM pdf_updates WHERE item_id=?", (item_id,)).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO pdf_updates (item_id, project, sheet, state, note, sig, created_at, resolved_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (item_id, project, item["sheet"], state, note, sig, now_iso(),
             now_iso() if state == "ok" else None))
    else:
        conn.execute("UPDATE pdf_updates SET state=?, note=?, sig=?, resolved_at=? WHERE id=?",
                     (state, note, sig, now_iso() if state == "ok" else None, existing["id"]))


def check(conn) -> dict:
    out = {"new": 0, "resolved": 0}
    # New A/M/S sheets from notices that arrived after their project's folder was registered.
    for proj in {r["project"] for r in list_folders(conn)}:
        since = conn.execute("SELECT min(added_at) FROM pdf_folders WHERE project=?", (proj,)).fetchone()[0]
        rows = conn.execute(
            "SELECT di.* FROM drawing_items di JOIN drawing_notices dn ON dn.message_id = di.message_id "
            "WHERE di.project = ? AND di.kind IN ('A','M','S') AND dn.parsed_at >= ? AND NOT EXISTS "
            "(SELECT 1 FROM pdf_updates u WHERE u.item_id = di.id) ORDER BY di.id", (proj, since)).fetchall()
        if rows:
            _assess(conn, proj, rows)
            out["new"] += len(rows)
        # Re-check outstanding ones only when the folder's matching files changed.
        stale = []
        for u in conn.execute("SELECT u.*, di.set_name, di.rev FROM pdf_updates u JOIN drawing_items di "
                              "ON di.id = u.item_id WHERE u.project=? AND u.state='open'", (proj,)):
            if _sig(_candidates(conn, proj, u["sheet"])) != (u["sig"] or ""):
                stale.append(u)
        if stale:
            before = len(stale)
            _assess(conn, proj, stale)
            out["resolved"] += before - conn.execute(
                "SELECT count(*) FROM pdf_updates WHERE state='open' AND id IN (%s)" %
                ",".join(str(u["id"]) for u in stale)).fetchone()[0]
    return out


# --------------------------------------------------------------------------- #
# tell the user
# --------------------------------------------------------------------------- #
def _line(u, di) -> str:
    what = f"{u['sheet']}"
    if di["title"]:
        what += f" - {di['title']}"
    need = (di["set_name"] or "latest issue")[:80]
    return f"{what}: {u['note'] or 'needs update'} (issued: {need})"


def notify_and_task(conn, notion: N.Notion, directory, notify: bool = True) -> int:
    tasks_db = load_config()["notion"]["tasks_db_id"]
    cache: dict[str, bool] = {}
    alerted = 0
    projects = [r[0] for r in conn.execute(
        "SELECT DISTINCT project FROM pdf_updates WHERE state='open' AND notified=0")]
    for proj in projects:
        ups = conn.execute(
            "SELECT u.*, di.title, di.set_name FROM pdf_updates u JOIN drawing_items di ON di.id=u.item_id "
            "WHERE u.project=? AND u.state='open' AND u.notified=0 ORDER BY u.id", (proj,)).fetchall()
        blocks = [{"object": "block", "type": "to_do", "to_do": {
            "rich_text": [{"type": "text", "text": {"content": _line(u, u)[:1900]}}], "checked": False}}
            for u in ups]
        try:
            row = conn.execute("SELECT page_id FROM pdf_tasks WHERE project=?", (proj,)).fetchone()
            first = 90
            if row and _page_open(notion, row["page_id"], cache):
                page_id = row["page_id"]
                notion.append_blocks(page_id, blocks[:first])
            else:
                first = 89
                props = {"Task name": N.title(f"Update linked PDFs: {proj}"),
                         "Priority": N.select("Medium"), "Horizon": N.select("Active"),
                         "Status": N.status("Not started"), "Added by assistant": N.checkbox(True),
                         "Origin": N.rich_text("New drawings issued that are missing or out of date in your PDF folder")}
                _, pid = directory.resolve(proj)
                if pid:
                    props["Project"] = N.relation([pid])
                page_id = notion.create_page(tasks_db, props)["id"]
                notion.append_blocks(page_id, [N.paragraph_block(
                    "Sheets in your linked-PDF folder that need adding or updating. This task closes "
                    "itself once the matching files appear in the folder.")] + blocks[:first])
                conn.execute("INSERT OR REPLACE INTO pdf_tasks (project, page_id) VALUES (?,?)", (proj, page_id))
                log_event(conn, "pdf_task_created", notion_task_id=page_id, detail=proj)
            for start in range(first, len(blocks), 90):
                notion.append_blocks(page_id, blocks[start:start + 90])
        except Exception as exc:  # noqa: BLE001 - retried next cycle
            log.error("could not write PDF-update task for %s: %s", proj, exc)
            continue
        conn.execute("UPDATE pdf_updates SET notified=1, task_page_id=? WHERE id IN (%s)" %
                     ",".join(str(u["id"]) for u in ups), (page_id,))
        conn.commit()
        alerted += 1
        if notify:
            toast(f"{len(ups)} PDF{'s' if len(ups) != 1 else ''} to update: {proj}",
                  ", ".join(u["sheet"] for u in ups)[:180])
    return alerted


def close_finished(conn, notion: N.Notion) -> int:
    closed = 0
    cache: dict[str, bool] = {}
    for t in conn.execute("SELECT project, page_id FROM pdf_tasks").fetchall():
        outstanding = conn.execute("SELECT count(*) FROM pdf_updates WHERE project=? AND state='open'",
                                   (t["project"],)).fetchone()[0]
        if outstanding:
            continue
        try:
            if _page_open(notion, t["page_id"], cache):
                import datetime as _dt
                notion.update_page(t["page_id"], {"Status": N.status("Done"),
                                                  "Date Completed": N.date(_dt.date.today().isoformat())})
                log_event(conn, "pdf_task_autoclosed", notion_task_id=t["page_id"], detail=t["project"])
                closed += 1
            conn.execute("DELETE FROM pdf_tasks WHERE project=?", (t["project"],))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not close PDF task for %s: %s", t["project"], exc)
    conn.commit()
    return closed


def process(conn, notion: N.Notion | None = None, *, notify: bool = True) -> dict:
    if not list_folders(conn):
        return {}
    notion = notion or N.Notion(get_secret(SECRET_NOTION_TOKEN))
    directory = ProjectDirectory(notion, load_config()["notion"]["projects_db_id"])
    out = {"changed_folders": len(scan(conn))}
    out.update(check(conn))
    out["alerts"] = notify_and_task(conn, notion, directory, notify)
    out["closed"] = close_finished(conn, notion)
    return out
