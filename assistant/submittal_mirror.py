"""Phase 1: one-way mirror of the Submittal Inbox into the Tasks database.

Rules:
  * Source rows in an "open" status get a live Tasks row.
  * When a source row reaches a "closed" status (Ready To Ship) or disappears,
    the mirror Tasks row is set to Done + Date Completed.
  * Keyed on the source page id (stored locally and in the Tasks "Source" URL).
  * Idempotent; safe to run repeatedly. Never deletes anything.
  * Status is only (re)written while it still matches what we last set, so a
    manual status change by the user is not clobbered.
"""

from __future__ import annotations

import hashlib
import logging
import re
import urllib.parse
from dataclasses import dataclass
from datetime import date as date_cls
from typing import Any

from . import notion_api as N
from .db import log_event, now_iso

log = logging.getLogger(__name__)

# "<number>-<project> PG <pages>"  e.g. "1234-Example Tower PG 83-84"
_NAME_RE = re.compile(r"^\s*(\d+)\s*-\s*(.+?)\s*PG\s*(.+?)\s*$", re.IGNORECASE)
# "<number>-<rest>"  e.g. "5678-Example Annex Plenum Base"
_NUM_PREFIX_RE = re.compile(r"^\s*(\d+)\s*-\s*(.+?)\s*$")


def _int(value: Any) -> Any:
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _num(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _match_known(text: str, known_projects: list[str]) -> str | None:
    low = (text or "").lower()
    for name in known_projects:
        if low.startswith(name.lower()):
            return name
    return None


def _parse_name(name: str, known_projects: list[str]) -> tuple[str | None, str | None, str | None]:
    """Return ``(project, pages, description)``; any element may be None."""
    m = _NAME_RE.match(name or "")
    if m:
        return m.group(2).strip(), m.group(3).strip(), None
    m = _NUM_PREFIX_RE.match(name or "")
    if m:
        rest = m.group(2).strip()
        return _match_known(rest, known_projects), None, rest
    if (name or "").strip():
        return _match_known(name, known_projects), None, name.strip()
    return None, None, None


@dataclass
class MirrorConfig:
    tasks_db_id: str
    submittal_db_id: str
    title_template: str
    desc_title_template: str
    horizon_for_received: str
    horizon_for_listed: str
    known_projects: list[str]
    open_source_statuses: set[str]
    closed_source_statuses: set[str]
    task_status_for_received: str
    task_status_for_listed: str
    task_status_on_close: str
    skip_project_numbers: set[int]
    skip_name_contains: list[str]
    skip_notes_equals: list[str]
    project_page_ids: dict[str, str]

    @classmethod
    def from_toml(cls, cfg: dict[str, Any]) -> "MirrorConfig":
        n = cfg["notion"]
        m = cfg["submittal_mirror"]
        return cls(
            tasks_db_id=n["tasks_db_id"],
            submittal_db_id=n["submittal_db_id"],
            title_template=m["title_template"],
            desc_title_template=m["desc_title_template"],
            horizon_for_received=m["horizon_for_received"],
            horizon_for_listed=m["horizon_for_listed"],
            known_projects=list(m.get("known_projects", [])),
            open_source_statuses=set(m["open_source_statuses"]),
            closed_source_statuses=set(m["closed_source_statuses"]),
            task_status_for_received=m["task_status_for_received"],
            task_status_for_listed=m["task_status_for_listed"],
            task_status_on_close=m["task_status_on_close"],
            skip_project_numbers=set(m.get("skip_project_numbers", [])),
            skip_name_contains=[s.lower() for s in m.get("skip_name_contains", [])],
            skip_notes_equals=[s.strip().lower() for s in m.get("skip_notes_equals", [])],
            project_page_ids={str(k): v for k, v in m.get("project_page_ids", {}).items()},
        )


@dataclass
class SourceRow:
    page_id: str
    url: str
    name: str
    status: str | None
    due: str | None
    notes: str
    project_number: Any
    project: str | None
    pages: str | None
    description: str | None

    @classmethod
    def from_page(cls, page: dict[str, Any], known_projects: list[str]) -> "SourceRow":
        p = page["properties"]
        name = N.plain(p.get("Name")) or ""
        proj_num = N.plain(p.get("Project Number"))
        first = N.plain(p.get("First Page"))
        last = N.plain(p.get("Last Page"))
        project, pages, description = _parse_name(name, known_projects)
        if pages is None and first not in (None, ""):
            pages = f"{_num(first)}-{_num(last)}" if last not in (None, "") else _num(first)
        return cls(
            page_id=page["id"],
            url=page.get("url", ""),
            name=name,
            status=N.plain(p.get("Status")),
            due=(N.plain(p.get("Due Date")) or None),
            notes=N.plain(p.get("Notes")) or "",
            project_number=proj_num,
            project=project,
            pages=pages,
            description=description,
        )


class SubmittalMirror:
    def __init__(self, notion: N.Notion, conn, cfg: MirrorConfig, *, dry_run: bool):
        self.notion = notion
        self.conn = conn
        self.cfg = cfg
        self.dry_run = dry_run
        self.counts = {"create": 0, "update": 0, "close": 0, "skip": 0, "noop": 0}

    # -- derived values ------------------------------------------------
    def _resolve_project(self, row: SourceRow) -> str | None:
        return row.project or None

    def _project_page_id(self, row: SourceRow) -> str | None:
        name = self._resolve_project(row)
        return self.cfg.project_page_ids.get(name) if name else None

    def _title(self, row: SourceRow) -> str:
        project = self._resolve_project(row)
        if row.pages:
            label = project or (
                f"Project {_int(row.project_number)}" if row.project_number else "Submittal"
            )
            return self.cfg.title_template.format(project=label, pages=row.pages)
        if row.description:
            return self.cfg.desc_title_template.format(description=row.description)
        return self.cfg.desc_title_template.format(description=project or row.name or "submittal")

    def _task_status_for(self, source_status: str | None) -> str:
        if source_status == "Listed":
            return self.cfg.task_status_for_listed
        return self.cfg.task_status_for_received

    def _horizon_for(self, source_status: str | None) -> str:
        # Received = the user's part of the work; Listed = handed to the next
        # stage, still tracked but not theirs to act on - Parked skips both
        # Today's Plan and the Someday rotation (nothing to do until it closes).
        if source_status == "Listed":
            return self.cfg.horizon_for_listed
        return self.cfg.horizon_for_received

    def _should_skip(self, row: SourceRow) -> bool:
        num = _int(row.project_number)
        if isinstance(num, int) and num in self.cfg.skip_project_numbers:
            return True
        low = row.name.lower()
        if any(frag in low for frag in self.cfg.skip_name_contains):
            return True
        note = row.notes.strip().lower()
        if note and note in self.cfg.skip_notes_equals:
            return True
        return False

    def _signature(self, row: SourceRow, task_status: str) -> str:
        parts = [
            self._title(row),
            row.due or "",
            task_status,
            self._horizon_for(row.status),
            self._project_page_id(row) or self._resolve_project(row) or "",
            row.notes,
        ]
        return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:16]

    def _mapped_properties(self, row: SourceRow, task_status: str) -> dict[str, Any]:
        props = {
            "Task name": N.title(self._title(row)),
            "Due": N.date(row.due[:10] if row.due else None),
            "Status": N.status(task_status),
            "Horizon": N.select(self._horizon_for(row.status)),
            "Source": N.url(row.url),
            "Added by assistant": N.checkbox(True),
        }
        page_id = self._project_page_id(row)
        if page_id:
            props["Project"] = N.relation([page_id])
        elif self._resolve_project(row):
            log.warning(
                "no Projects page mapped for %r — leaving %r unlinked",
                self._resolve_project(row), self._title(row),
            )
        if row.notes:
            props["Notes"] = N.rich_text(row.notes)
        return props

    def _get_mirror(self, source_page_id: str):
        return self.conn.execute(
            "SELECT * FROM submittal_mirror WHERE source_page_id = ?", (source_page_id,)
        ).fetchone()

    def _adopt_orphans(self, source_by_dashless: dict[str, dict]) -> int:
        """Rebuild local mappings from Notion when the state DB has been lost.

        Matches existing assistant-created Tasks rows back to their source
        submittal via the trailing page id in the "Source" URL, so a wiped
        ``data/assistant.db`` does not cause duplicate task creation.
        """
        known = {
            r["source_page_id"]
            for r in self.conn.execute("SELECT source_page_id FROM submittal_mirror")
        }
        adopted = 0
        flt = {"property": "Added by assistant", "checkbox": {"equals": True}}
        for page in self.notion.iter_database(self.cfg.tasks_db_id, flt=flt):
            src = N.plain(page["properties"].get("Source")) or ""
            m = re.search(r"([0-9a-fA-F]{32})(?:[?#/]|$)", urllib.parse.urlparse(src).path)
            if not m:
                continue
            source_page = source_by_dashless.get(m.group(1).lower())
            if source_page is None or source_page["id"] in known:
                continue
            status_now = N.plain(page["properties"].get("Status"))
            self.conn.execute(
                "INSERT OR IGNORE INTO submittal_mirror (source_page_id, source_name, "
                "notion_task_id, last_source_status, last_task_status, signature, closed, "
                "created_at, last_synced_at) VALUES (?, ?, ?, ?, ?, '', ?, ?, ?)",
                (source_page["id"], N.plain(source_page["properties"].get("Name")) or "",
                 page["id"], None, status_now,
                 1 if status_now in ("Done", "Archived") else 0,
                 now_iso(), now_iso()),
            )
            known.add(source_page["id"])
            adopted += 1
        if adopted:
            self.conn.commit()
            log.info("adopted %d existing mirror task(s) from Notion (state DB was incomplete)", adopted)
        return adopted

    # -- actions -----------------------------------------------------
    def _create(self, row: SourceRow) -> None:
        task_status = self._task_status_for(row.status)
        title = self._title(row)
        if self.dry_run:
            log.info("CREATE  %-40s due %-10s status %s", title, row.due or "-", task_status)
            self.counts["create"] += 1
            return
        page = self.notion.create_page(
            self.cfg.tasks_db_id, self._mapped_properties(row, task_status)
        )
        self.conn.execute(
            "INSERT INTO submittal_mirror (source_page_id, source_name, notion_task_id, "
            "last_source_status, last_task_status, signature, closed, created_at, last_synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (row.page_id, row.name, page["id"], row.status, task_status,
             self._signature(row, task_status), now_iso(), now_iso()),
        )
        log_event(self.conn, "create", source_page_id=row.page_id,
                  notion_task_id=page["id"], detail=title)
        self.conn.commit()
        log.info("created  %s", title)
        self.counts["create"] += 1

    def _update(self, row: SourceRow, mirror) -> None:
        task_status = self._task_status_for(row.status)
        new_sig = self._signature(row, task_status)
        if new_sig == mirror["signature"]:
            self.counts["noop"] += 1
            return
        props = self._mapped_properties(row, task_status)
        # Leave Status alone only if the user actually changed it in Notion since
        # our last write - compare against Notion's *live* value, not the
        # newly computed target. Comparing to the target used to mistake the
        # mirror's own legitimate Received -> Listed progression for a manual
        # edit and froze Status forever the first time a row advanced.
        keep_status = False
        if not self.dry_run and mirror["last_task_status"] is not None:
            current = self.notion.get_page(mirror["notion_task_id"])
            live_status = N.plain(current["properties"].get("Status"))
            keep_status = live_status != mirror["last_task_status"]
        if keep_status:
            props.pop("Status", None)
        elif task_status == self.cfg.task_status_on_close and mirror["last_task_status"] != task_status:
            # First move into the finished status (e.g. Listed -> Done): stamp it once.
            props["Date Completed"] = N.date(date_cls.today().isoformat())
        title = self._title(row)
        if self.dry_run:
            log.info("UPDATE  %-40s due %-10s%s", title, row.due or "-",
                     "" if "Status" in props else "  (status left as-is)")
            self.counts["update"] += 1
            return
        self.notion.update_page(mirror["notion_task_id"], props)
        self.conn.execute(
            "UPDATE submittal_mirror SET source_name=?, last_source_status=?, "
            "last_task_status=?, signature=?, last_synced_at=? WHERE source_page_id=?",
            (row.name, row.status,
             mirror["last_task_status"] if keep_status else task_status,
             new_sig, now_iso(), row.page_id),
        )
        log_event(self.conn, "update", source_page_id=row.page_id,
                  notion_task_id=mirror["notion_task_id"], detail=title)
        self.conn.commit()
        log.info("updated  %s", title)
        self.counts["update"] += 1

    def _close(self, mirror, reason: str) -> None:
        if mirror["closed"]:
            self.counts["noop"] += 1
            return
        if self.dry_run:
            log.info("CLOSE   %-40s (%s)", mirror["source_name"], reason)
            self.counts["close"] += 1
            return
        self.notion.update_page(
            mirror["notion_task_id"],
            {
                "Status": N.status(self.cfg.task_status_on_close),
                "Date Completed": N.date(date_cls.today().isoformat()),
            },
        )
        self.conn.execute(
            "UPDATE submittal_mirror SET closed=1, last_source_status=?, "
            "last_task_status=?, last_synced_at=? WHERE source_page_id=?",
            (reason, self.cfg.task_status_on_close, now_iso(), mirror["source_page_id"]),
        )
        log_event(self.conn, "close", source_page_id=mirror["source_page_id"],
                  notion_task_id=mirror["notion_task_id"], detail=reason)
        self.conn.commit()
        log.info("closed   %s  (%s)", mirror["source_name"], reason)
        self.counts["close"] += 1

    # -- main -------------------------------------------------------
    def run(self) -> dict[str, int]:
        log.info("submittal mirror%s starting", " (dry-run)" if self.dry_run else "")
        seen: set[str] = set()

        source_pages = list(self.notion.iter_database(self.cfg.submittal_db_id))
        if not self.dry_run:
            self._adopt_orphans({p["id"].replace("-", ""): p for p in source_pages})

        for page in source_pages:
            row = SourceRow.from_page(page, self.cfg.known_projects)
            seen.add(row.page_id)
            mirror = self._get_mirror(row.page_id)

            if self._should_skip(row):
                if mirror is None:
                    self.counts["skip"] += 1
                    log.debug("skip test row: %s", row.name)
                continue

            is_open = row.status in self.cfg.open_source_statuses
            is_closed = row.status in self.cfg.closed_source_statuses

            if mirror is None:
                if is_open:
                    self._create(row)
                continue

            if is_closed or not is_open:
                self._close(mirror, reason=row.status or "source not open")
            else:
                self._update(row, mirror)

        # Source rows that vanished entirely -> close their mirrors.
        for mirror in self.conn.execute(
            "SELECT * FROM submittal_mirror WHERE closed = 0"
        ).fetchall():
            if mirror["source_page_id"] not in seen:
                self._close(mirror, reason="source row removed")

        log.info(
            "mirror done: %d created, %d updated, %d closed, %d unchanged, %d skipped",
            self.counts["create"], self.counts["update"], self.counts["close"],
            self.counts["noop"], self.counts["skip"],
        )
        return self.counts
