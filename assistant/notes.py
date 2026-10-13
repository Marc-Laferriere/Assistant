"""Quick call-note capture, filed against a specific Task's page body.

Flow: type freely during/after a call — one line per Enter, no per-line
decisions, just get it down. Blank line (or Ctrl+C) ends capture and moves
straight into review: for each line, pick a project, then a task under it (or
create a new one on the spot). The note text is appended to that task's
Notion page body; a short flag is set on the task's "Notes" property (not the
content itself — Notes stays scannable in table view) so it's obvious there's
something to read when you open the task.

Skipped lines stay `pending` for the next review pass — nothing is lost.
"""

from __future__ import annotations

import logging
from datetime import date as date_cls

from . import notion_api as N
from .capture import create_task, parse_line
from .config import SECRET_NOTION_TOKEN, load_config
from .db import connect, init_db, now_iso
from .projects import ProjectDirectory
from .secrets_store import get_secret

log = logging.getLogger(__name__)

_FLAG_DEFAULT = "\U0001f4dd see notes in body"


def _notion() -> N.Notion:
    return N.Notion(get_secret(SECRET_NOTION_TOKEN))


# --------------------------------------------------------------------------- #
# capture
# --------------------------------------------------------------------------- #
def capture_interactive() -> int:
    print('Add call notes — one per line, saved as you go. Blank line (or Ctrl+C) '
          "to finish and file them.\n")
    n = 0
    with connect() as conn:
        init_db(conn)
        while True:
            try:
                line = input("note> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                break
            conn.execute(
                "INSERT INTO call_notes (raw_text, captured_at, status) VALUES (?, ?, 'pending')",
                (line, now_iso()),
            )
            conn.commit()
            n += 1
    if n:
        print(f"\n{n} note(s) captured.")
    else:
        print("\nNothing captured.")
    return n


# --------------------------------------------------------------------------- #
# filing
# --------------------------------------------------------------------------- #
def _active_tasks_for_project(notion: N.Notion, tasks_db_id: str, project_id: str) -> list[dict]:
    flt = {"and": [
        {"property": "Project", "relation": {"contains": project_id}},
        {"property": "Status", "status": {"does_not_equal": "Done"}},
        {"property": "Status", "status": {"does_not_equal": "Archived"}},
    ]}
    out = []
    for page in notion.iter_database(tasks_db_id, flt=flt):
        out.append({"id": page["id"], "title": N.plain(page["properties"].get("Task name")) or "(untitled)"})
    return out


def _file_note(notion: N.Notion, task_id: str, note_text: str, flag_text: str) -> None:
    stamp = date_cls.today().isoformat()
    notion.append_blocks(task_id, [N.paragraph_block(f"{stamp} — {note_text}")])
    page = notion.get_page(task_id)
    current = N.plain(page["properties"].get("Notes")) or ""
    if flag_text not in current:
        notion.update_page(task_id, {"Notes": N.rich_text(f"{current} · {flag_text}" if current else flag_text)})


def _create_new_task_from_note(notion: N.Notion, tasks_db_id: str, raw_line: str,
                               project_id: str, flag_text: str) -> str:
    name, due, prio, _ = parse_line(raw_line)  # project hint (if any) is ignored - already chosen
    if not name:
        name = raw_line[:100]
    page = create_task(notion, tasks_db_id, name, due, prio, project_id)
    stamp = date_cls.today().isoformat()
    notion.append_blocks(page["id"], [N.paragraph_block(f"{stamp} — {raw_line}")])
    notion.update_page(page["id"], {"Notes": N.rich_text(flag_text)})
    return page["id"]


# --------------------------------------------------------------------------- #
# review (interactive pickers)
# --------------------------------------------------------------------------- #
def _pick_project(projects: list[tuple[str, str]]) -> tuple[str, str] | None:
    print("  which project?")
    for i, (name, _pid) in enumerate(projects, 1):
        print(f"    {i}. {name}")
    print("    s. skip for now (project doesn't exist yet? create it in Notion, "
          "then come back — nothing is lost)")
    while True:
        choice = input("  > ").strip().lower()
        if choice == "s":
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(projects):
            return projects[int(choice) - 1]
        for name, pid in projects:
            if choice and choice in name.lower():
                return (name, pid)
        print("    ?  number, or s to skip")


def _pick_task(notion: N.Notion, tasks_db_id: str, project_id: str, project_name: str) -> tuple[str, str | None]:
    """Return ('use', task_id) | ('new', None) | ('skip', None)."""
    tasks = _active_tasks_for_project(notion, tasks_db_id, project_id)
    print(f"  which task under {project_name}?")
    for i, t in enumerate(tasks, 1):
        print(f"    {i}. {t['title']}")
    print("    n. new task")
    print("    s. skip for now")
    while True:
        choice = input("  > ").strip().lower()
        if choice == "s":
            return ("skip", None)
        if choice == "n":
            return ("new", None)
        if choice.isdigit() and 1 <= int(choice) <= len(tasks):
            return ("use", tasks[int(choice) - 1]["id"])
        print("    ?  number, n for new, or s to skip")


def review_interactive() -> int:
    with connect() as conn:
        init_db(conn)
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM call_notes WHERE status = 'pending' ORDER BY id"
        ).fetchall()]
    if not rows:
        print("Nothing to review.")
        return 0

    cfg = load_config()
    flag_text = cfg.get("notes", {}).get("flag_text", _FLAG_DEFAULT)
    notion = _notion()
    tasks_db_id = cfg["notion"]["tasks_db_id"]
    directory = ProjectDirectory(notion, cfg["notion"]["projects_db_id"])
    projects = directory.all()

    print(f"\n{len(rows)} note(s) to file. [number]=project  s=skip  n=new task\n")
    filed = 0
    with connect() as conn:
        for i, row in enumerate(rows, 1):
            print(f"[{i}/{len(rows)}] {row['raw_text']}")
            picked = _pick_project(projects)
            if picked is None:
                print("  -> left for later. Run \"Add Note\" again anytime to pick this back up.")
                continue
            proj_name, proj_id = picked
            action, task_id = _pick_task(notion, tasks_db_id, proj_id, proj_name)
            if action == "skip":
                print("  -> left for later. Run \"Add Note\" again anytime to pick this back up.")
                continue
            if action == "new":
                task_id = _create_new_task_from_note(notion, tasks_db_id, row["raw_text"], proj_id, flag_text)
                print(f"  -> created a new task under {proj_name}")
            else:
                _file_note(notion, task_id, row["raw_text"], flag_text)
                print(f"  -> filed under {proj_name}")
            conn.execute(
                "UPDATE call_notes SET status = 'filed', notion_task_id = ?, filed_at = ? WHERE id = ?",
                (task_id, now_iso(), row["id"]),
            )
            conn.commit()
            filed += 1

    left = len(rows) - filed
    print(f"\nDone — {filed}/{len(rows)} filed." + (f" {left} left for next time." if left else ""))
    return filed
