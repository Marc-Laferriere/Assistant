"""Phase 2: quick task capture.

Lines typed into ``capture.txt`` (via the "Add Task" launcher, or by editing the
file directly) become tasks in the Notion Tasks database. See PROPOSAL.md sec 4a.

Deliberately captured lines go straight to Notion — no review queue (that is for
the less-certain email-extracted tasks in Phase 5).

Line syntax (all optional, trailing — put `#project` last if you use it):
    buy duct tape                     -> task "buy duct tape"
    call supplier re PG 90 !          -> High priority
    send RFI @tomorrow                -> due tomorrow
    site walk @2026-09-15             -> due that date
    order flanges @friday !!          -> due Friday, High priority
    list level 3 ductwork #main tower   -> filed under the "Main Tower" project
Lines that are blank or start with '#' are ignored.

The interactive launcher (``--add``) sends each line to Notion the moment you
press Enter — nothing is deferred until you type a blank line, so closing the
window right after typing a task can't strand it unsent.
"""

from __future__ import annotations

import logging
import os
import re
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta

from . import notion_api as N
from .config import (
    CAPTURE_PATH,
    DATA_DIR,
    PROJECT_ROOT,
    SECRET_NOTION_TOKEN,
    load_config,
)
from .db import connect, init_db, log_event, now_iso
from .projects import ProjectDirectory
from .secrets_store import get_secret

log = logging.getLogger(__name__)

ARCHIVE_PATH = PROJECT_ROOT / "capture.archive.txt"
LOCK_PATH = DATA_DIR / "capture.lock"

_PRIO_RE = re.compile(r"\s+(!{1,2})$")
_DUE_RE = re.compile(r"\s+@(\S+)$")
_PROJECT_RE = re.compile(r"\s+#(.+)$")
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _parse_due(token: str) -> str | None:
    t = token.strip().lower()
    today = date.today()
    if t in ("today", "tod"):
        return today.isoformat()
    if t in ("tomorrow", "tmrw", "tom"):
        return (today + timedelta(days=1)).isoformat()
    if t in _WEEKDAYS:
        ahead = (_WEEKDAYS.index(t) - today.weekday()) % 7 or 7
        return (today + timedelta(days=ahead)).isoformat()
    for fmt in ("%Y-%m-%d", "%m-%d", "%m/%d", "%d-%b", "%d%b"):
        try:
            dt = datetime.strptime(t, fmt).date()
        except ValueError:
            continue
        if "%Y" not in fmt:
            dt = dt.replace(year=today.year)
            if dt < today:
                dt = dt.replace(year=today.year + 1)
        return dt.isoformat()
    return None


def parse_line(text: str) -> tuple[str, str | None, str | None, str | None]:
    """Return ``(task_name, due_iso_or_None, priority_or_None, project_hint_or_None)``."""
    name = text.strip()

    project_hint: str | None = None
    m = _PROJECT_RE.search(name)
    if m:
        project_hint = m.group(1).strip()
        name = name[: m.start()].rstrip()

    due: str | None = None
    priority: str | None = None
    changed = True
    while changed:
        changed = False
        m = _PRIO_RE.search(name)
        if m:
            priority = "High"
            name = name[: m.start()].rstrip()
            changed = True
        m = _DUE_RE.search(name)
        if m:
            parsed = _parse_due(m.group(1))
            if parsed:
                due = parsed
                name = name[: m.start()].rstrip()
                changed = True
            else:
                break
    return name, due, priority, project_hint


@contextmanager
def _lock():
    """Best-effort cross-process lock so the hourly run and a manual add don't
    both process the file at once. Stale locks (>5 min) are stolen."""
    DATA_DIR.mkdir(exist_ok=True)
    try:
        if LOCK_PATH.exists() and time.time() - LOCK_PATH.stat().st_mtime > 300:
            LOCK_PATH.unlink(missing_ok=True)
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        log.info("capture: another run holds the lock, skipping")
        yield False
        return
    try:
        yield True
    finally:
        LOCK_PATH.unlink(missing_ok=True)


def create_task(notion: N.Notion, tasks_db_id: str, name: str, due: str | None,
                priority: str | None, project_id: str | None) -> dict:
    """Shared by capture and notes.py's "new task" fallback."""
    props = {
        "Task name": N.title(name),
        "Status": N.status("Not started"),
        "Horizon": N.select("Active"),
    }
    if due:
        props["Due"] = N.date(due)
    if priority:
        props["Priority"] = N.select(priority)
    if project_id:
        props["Project"] = N.relation([project_id])
    return notion.create_page(tasks_db_id, props)


def _record(conn, raw_line: str, notion_page_id: str, name: str) -> None:
    conn.execute(
        "INSERT INTO capture_lines (raw_text, captured_at, notion_page_id, processed_at) "
        "VALUES (?, ?, ?, ?)",
        (raw_line, now_iso(), notion_page_id, now_iso()),
    )
    log_event(conn, "capture", notion_task_id=notion_page_id, detail=name)


def process(*, dry_run: bool = False, notion: N.Notion | None = None) -> int:
    """Batch path — for lines added by editing capture.txt directly, and the
    hourly job's retry of anything the interactive path couldn't send."""
    if not CAPTURE_PATH.exists() or not CAPTURE_PATH.read_text(encoding="utf-8").strip():
        log.debug("capture: nothing pending")
        return 0

    with _lock() as got:
        if not got:
            return 0

        lines = CAPTURE_PATH.read_text(encoding="utf-8").splitlines()
        pending = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
        if not pending:
            CAPTURE_PATH.write_text("", encoding="utf-8")
            return 0

        if dry_run:
            for ln in pending:
                name, due, prio, proj = parse_line(ln)
                log.info("CREATE task %r  due=%s  priority=%s  project=%s",
                         name, due or "-", prio or "-", proj or "-")
            return len(pending)

        cfg = load_config()
        notion = notion or N.Notion(get_secret(SECRET_NOTION_TOKEN))
        tasks_db_id = cfg["notion"]["tasks_db_id"]
        directory = ProjectDirectory(notion, cfg["notion"]["projects_db_id"])
        remaining = list(pending)
        done: list[str] = []
        try:
            with connect() as conn:
                init_db(conn)
                for ln in list(remaining):
                    name, due, prio, proj_hint = parse_line(ln)
                    if not name:
                        remaining.remove(ln)
                        continue
                    _, proj_id = directory.resolve(proj_hint)
                    page = create_task(notion, tasks_db_id, name, due, prio, proj_id)
                    _record(conn, ln, page["id"], name)
                    conn.commit()
                    done.append(ln)
                    remaining.remove(ln)
                    log.info("captured -> %s", name)
        finally:
            if done:
                with ARCHIVE_PATH.open("a", encoding="utf-8") as fh:
                    for ln in done:
                        fh.write(f"{now_iso()}\t{ln}\n")
            CAPTURE_PATH.write_text(
                "\n".join(remaining) + ("\n" if remaining else ""), encoding="utf-8"
            )
        return len(done)


def add_interactive() -> int:
    """Interactive launcher path. Each line is sent to Notion immediately, so
    closing the window after any successful line never loses it. If sending
    fails (offline, API hiccup), the line falls back to capture.txt for the
    hourly job to retry."""
    print("Add tasks to Notion — one per line, sent right away. Blank line (or Ctrl+C) to stop.")
    print("  hints:  trailing !  = High priority     @tomorrow / @2026-09-15 / @friday = due date")
    print("          #project (put it last)         = file under that project\n")

    notion: N.Notion | None = None
    tasks_db_id: str | None = None
    directory: ProjectDirectory | None = None
    n = 0

    while True:
        try:
            line = input("task> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            break

        name, due, prio, proj_hint = parse_line(line)
        if not name:
            continue

        try:
            if notion is None:
                cfg = load_config()
                notion = N.Notion(get_secret(SECRET_NOTION_TOKEN))
                tasks_db_id = cfg["notion"]["tasks_db_id"]
                directory = ProjectDirectory(notion, cfg["notion"]["projects_db_id"])

            proj_name, proj_id = directory.resolve(proj_hint)
            page = create_task(notion, tasks_db_id, name, due, prio, proj_id)
            with connect() as conn:
                init_db(conn)
                _record(conn, line, page["id"], name)
                conn.commit()

            bits = []
            if proj_name:
                bits.append(proj_name)
            if due:
                bits.append(f"due {due}")
            if prio:
                bits.append(prio)
            if proj_hint and not proj_id:
                bits.append(f"project {proj_hint!r} not recognized — add it by hand")
            suffix = f"  ({', '.join(bits)})" if bits else ""
            print(f"  -> sent to Notion{suffix}")
            n += 1
        except Exception as exc:  # noqa: BLE001 - never lose the line over a transient error
            log.error("capture: could not send %r right now: %s", line, exc)
            with CAPTURE_PATH.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            print(f"  ! couldn't reach Notion ({exc}) — saved, will retry automatically.")

    print("\nNothing added." if n == 0 else f"\nDone — {n} task(s) sent.")
    return n
