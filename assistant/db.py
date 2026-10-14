"""Local SQLite state.

Phase 1 tables only. Later phases add messages / classifications /
task_candidates / capture_lines / feedback (see PROPOSAL.md sec 9).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .config import DB_PATH, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS submittal_mirror (
    source_page_id     TEXT PRIMARY KEY,
    source_name        TEXT,
    notion_task_id     TEXT NOT NULL,
    last_source_status TEXT,
    last_task_status   TEXT,          -- what we last set Status to (to detect manual edits)
    signature          TEXT,          -- hash of mapped fields; skip PATCH when unchanged
    closed             INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    last_synced_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS capture_lines (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_text       TEXT NOT NULL,
    captured_at    TEXT NOT NULL,
    notion_page_id TEXT,
    processed_at   TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    message_id    TEXT PRIMARY KEY,
    folder        TEXT,
    uid           INTEGER,
    uidvalidity   INTEGER,
    thread_refs   TEXT,               -- References + In-Reply-To, for threading
    date_utc      TEXT,
    from_name     TEXT,
    from_addr     TEXT,
    to_addrs      TEXT,
    cc_addrs      TEXT,
    subject       TEXT,
    is_bulk       INTEGER NOT NULL DEFAULT 0,   -- List-Id / List-Unsubscribe / Precedence
    has_attach    INTEGER NOT NULL DEFAULT 0,
    is_answered   INTEGER NOT NULL DEFAULT 0,   -- IMAP "answered" flag (unreliable - see mailthread.py)
    thread_key    TEXT,                         -- normalized subject, for the Sent-folder reply check
    size_bytes    INTEGER,
    body_text     TEXT,
    snippet       TEXT,
    fetched_at    TEXT,
    first_seen_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date_utc);
CREATE INDEX IF NOT EXISTS idx_messages_from ON messages(from_addr);

CREATE TABLE IF NOT EXISTS imap_folder_state (
    folder       TEXT PRIMARY KEY,
    uidvalidity  INTEGER,
    last_uid     INTEGER NOT NULL DEFAULT 0,
    last_sync_at TEXT
);

CREATE TABLE IF NOT EXISTS classifications (
    message_id       TEXT PRIMARY KEY,
    importance       TEXT NOT NULL,          -- high | normal | low
    hidden           INTEGER NOT NULL DEFAULT 0,  -- rules 'skip' -> keep out of the brief
    category         TEXT,
    project          TEXT,
    reason           TEXT,
    implies_task     INTEGER NOT NULL DEFAULT 0,
    suggested_action TEXT,
    source           TEXT NOT NULL,          -- rules | ai
    matched_rule     TEXT,
    model            TEXT,
    prompt_version   TEXT,
    created_at       TEXT NOT NULL,
    user_override    TEXT
);
CREATE INDEX IF NOT EXISTS idx_class_importance ON classifications(importance);

CREATE TABLE IF NOT EXISTS task_candidates (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type    TEXT NOT NULL,      -- 'email' (new task) | 'cleanup' (update existing)
    message_id     TEXT,               -- set for source_type='email'
    notion_page_id TEXT,               -- existing page for 'cleanup'; filled in on push for 'email'
    title          TEXT NOT NULL,
    detail         TEXT,               -- human context, e.g. "From sam@example.com: Re: delivery schedule"
    due            TEXT,
    priority       TEXT,
    project        TEXT,
    horizon        TEXT NOT NULL DEFAULT 'Active',
    reason         TEXT,
    status         TEXT NOT NULL DEFAULT 'proposed',  -- proposed|accepted|rejected|pushed
    created_at     TEXT NOT NULL,
    decided_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON task_candidates(status);

CREATE TABLE IF NOT EXISTS drawing_notices (
    message_id TEXT PRIMARY KEY,
    parsed_at  TEXT NOT NULL,
    n_items    INTEGER NOT NULL DEFAULT 0,
    extra      TEXT                       -- e.g. "3 more sheet(s) not listed in the email"
);

CREATE TABLE IF NOT EXISTS drawing_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id   TEXT NOT NULL,
    project      TEXT NOT NULL,           -- canonical Projects-DB name when resolvable
    set_name     TEXT NOT NULL DEFAULT '',
    sheet        TEXT NOT NULL,
    title        TEXT,
    rev          TEXT,
    discipline   TEXT,
    kind         TEXT NOT NULL,           -- A | M | S | other
    notice_date  TEXT,
    candidate_id INTEGER,                 -- the 'drawings' task_candidates row it belongs to
    in_notion    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (message_id, project, set_name, sheet)
);
CREATE INDEX IF NOT EXISTS idx_drawing_items_candidate ON drawing_items(candidate_id);

CREATE TABLE IF NOT EXISTS call_notes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_text       TEXT NOT NULL,
    captured_at    TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',  -- pending | filed
    notion_task_id TEXT,
    filed_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_call_notes_status ON call_notes(status);

CREATE TABLE IF NOT EXISTS someday_rotation (
    notion_page_id TEXT PRIMARY KEY,
    last_shown_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id  INTEGER,            -- task_candidates.id
    source_type   TEXT,               -- 'email' | 'cleanup'
    decision      TEXT NOT NULL,      -- 'accepted' | 'rejected'
    reason        TEXT,               -- rejects: already_done | irrelevant | wrong_call | other
    detail        TEXT,               -- the candidate's title, for later pattern review
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,
    action         TEXT NOT NULL,     -- create | update | close | skip | error
    source_page_id TEXT,
    notion_task_id TEXT,
    detail         TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a table already existed (SQLite's
    CREATE TABLE IF NOT EXISTS won't do this on its own)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    if "is_answered" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN is_answered INTEGER NOT NULL DEFAULT 0")
    if "thread_key" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN thread_key TEXT")
    # Created here (not in SCHEMA) so it works whether thread_key came from a
    # fresh CREATE TABLE or was just added above.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_key)")


def log_event(
    conn: sqlite3.Connection,
    action: str,
    *,
    source_page_id: str | None = None,
    notion_task_id: str | None = None,
    detail: str = "",
) -> None:
    conn.execute(
        "INSERT INTO event_log (ts, action, source_page_id, notion_task_id, detail) "
        "VALUES (?, ?, ?, ?, ?)",
        (now_iso(), action, source_page_id, notion_task_id, detail),
    )
