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
    revised      INTEGER NOT NULL DEFAULT 0,   -- this sheet was seen in an earlier notice
    prev_note    TEXT,                         -- e.g. "rev 1 -> 2" / "also in: <earlier set>"
    UNIQUE (message_id, project, set_name, sheet)
);
CREATE INDEX IF NOT EXISTS idx_drawing_items_candidate ON drawing_items(candidate_id);

-- RFIs we wrote internally, to be matched against the GC's numbering in Procore.
CREATE TABLE IF NOT EXISTS our_rfis (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project         TEXT,
    number          INTEGER,             -- our own RFI number when the subject has one
    label           TEXT,                -- e.g. "RFI-02"
    title           TEXT,
    question        TEXT,
    first_message_id TEXT,
    sent_at         TEXT,
    status          TEXT NOT NULL DEFAULT 'internal',  -- internal|submitted|answered|closed
    procore_rfi_id  INTEGER,
    task_page_id    TEXT,
    last_event_at   TEXT,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS our_rfi_threads (
    thread_key  TEXT PRIMARY KEY,
    our_rfi_id  INTEGER NOT NULL,
    checked     INTEGER NOT NULL DEFAULT 1   -- 0 = looked at and judged not an RFI
);
-- Every RFI that appears in Procore notices (ours or anyone's).
CREATE TABLE IF NOT EXISTS procore_rfis (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_key   TEXT NOT NULL,
    project       TEXT,
    number        INTEGER NOT NULL,
    title         TEXT,
    question      TEXT,
    due           TEXT,
    asked_by      TEXT,
    status        TEXT NOT NULL DEFAULT 'open',      -- open|answered|closed
    our_rfi_id    INTEGER,
    link_state    TEXT NOT NULL DEFAULT 'none',      -- none|auto|confirmed|asked|other
    summary       TEXT,
    summary_at    TEXT,
    first_seen_at TEXT NOT NULL,
    UNIQUE (project_key, number)
);
CREATE TABLE IF NOT EXISTS rfi_events (
    message_id     TEXT PRIMARY KEY,
    procore_rfi_id INTEGER NOT NULL,
    kind           TEXT NOT NULL,            -- new|response|closed|reopened
    response_text  TEXT,
    occurred_at    TEXT,
    handled        INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS rfi_link_proposals (
    candidate_id   INTEGER PRIMARY KEY,
    procore_rfi_id INTEGER NOT NULL,
    our_rfi_id     INTEGER
);

-- Procore submittal notices (and watched-for submittal emails), one row per submittal.
CREATE TABLE IF NOT EXISTS submittals (
    key           TEXT PRIMARY KEY,        -- procore:<project key>:<number> | mail:<thread key>
    source        TEXT NOT NULL,           -- procore | mail
    project       TEXT,
    number        TEXT,
    title         TEXT,
    spec_section  TEXT,
    response      TEXT,
    category      TEXT,                    -- ours | watch | other | history (NULL = not judged yet)
    matched       TEXT,
    summary       TEXT,
    task_page_id  TEXT,
    notified      INTEGER NOT NULL DEFAULT 0,
    last_event_at TEXT,
    first_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submittal_events (
    message_id TEXT PRIMARY KEY,
    key        TEXT NOT NULL,
    kind       TEXT NOT NULL,              -- distributed | approver | mail
    response   TEXT,
    occurred_at TEXT,
    handled    INTEGER NOT NULL DEFAULT 0
);

-- Folders of PDFs linked into the user's Revit models, one or more per project.
CREATE TABLE IF NOT EXISTS pdf_folders (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    project  TEXT NOT NULL,               -- canonical Projects-DB name
    path     TEXT NOT NULL UNIQUE,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pdf_files (
    path      TEXT PRIMARY KEY,
    folder_id INTEGER NOT NULL,
    name      TEXT NOT NULL,
    mtime     REAL,
    sheet     TEXT,                       -- sheet / drawing number parsed from the file name
    tag       TEXT,                       -- revision tag parsed from the file name (IFC, SI-002, ...)
    parsed    INTEGER NOT NULL DEFAULT 0
);
-- A new-drawings sheet that may need its linked PDF added or updated.
CREATE TABLE IF NOT EXISTS pdf_updates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id      INTEGER NOT NULL UNIQUE,  -- drawing_items.id
    project      TEXT NOT NULL,
    sheet        TEXT NOT NULL,
    state        TEXT NOT NULL,            -- open | ok
    note         TEXT,                     -- what the folder has / lacks
    sig          TEXT,                     -- candidate-file signature at the last judgement
    notified     INTEGER NOT NULL DEFAULT 0,
    task_page_id TEXT,
    created_at   TEXT NOT NULL,
    resolved_at  TEXT
);
CREATE TABLE IF NOT EXISTS pdf_tasks (
    project TEXT PRIMARY KEY,
    page_id TEXT NOT NULL
);

-- Trade-meeting minutes, summarised once per (project, date, area).
CREATE TABLE IF NOT EXISTS meetings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project       TEXT NOT NULL,
    meeting_date  TEXT NOT NULL,           -- ISO date
    area          TEXT NOT NULL DEFAULT '',  -- e.g. Tower / Midrise ('' when the job has one meeting)
    message_id    TEXT NOT NULL,           -- the email the summary was made from
    points        TEXT,                    -- JSON list: general summary
    schedule      TEXT,                    -- JSON list: trade starts / slab pour days
    company_items TEXT,                    -- JSON list: items naming the user's company or trade
    pours         TEXT,                    -- JSON list: {tower, what, date, trade_days[]}
    first_seen_at TEXT NOT NULL,
    UNIQUE (project, meeting_date, area)
);
CREATE TABLE IF NOT EXISTS meeting_msgs (
    message_id TEXT PRIMARY KEY,
    outcome    TEXT NOT NULL,              -- summarised | duplicate | undated | no_content
    meeting_id INTEGER
);

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
    mcols = {r[1] for r in conn.execute("PRAGMA table_info(meetings)")}
    if mcols and "pours" not in mcols:
        conn.execute("ALTER TABLE meetings ADD COLUMN pours TEXT")
    dcols = {r[1] for r in conn.execute("PRAGMA table_info(drawing_items)")}
    if dcols and "revised" not in dcols:
        conn.execute("ALTER TABLE drawing_items ADD COLUMN revised INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE drawing_items ADD COLUMN prev_note TEXT")


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
