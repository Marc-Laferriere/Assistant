"""Phase 3: read-only IMAP ingestion.

Pulls messages from configured folders into the local ``messages`` table,
deduplicated by Message-ID. Strictly read-only: EXAMINE (readonly select) +
BODY.PEEK — never sets \\Seen, never moves or deletes. Incremental by UID per
folder, with UIDVALIDITY guarding against server-side renumbering.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import timezone
from html.parser import HTMLParser

from imapclient import IMAPClient

from .config import SECRET_IMAP_PASSWORD, load_config
from .db import connect, init_db, now_iso
from .mailthread import normalize_subject
from .secrets_store import get_secret

log = logging.getLogger(__name__)

FETCH_BATCH = 100
_BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "blockquote", "table"}
_DROP_TAGS = {"script", "style", "head", "title"}


# --------------------------------------------------------------------------- #
# HTML -> text (small, dependency-free)
# --------------------------------------------------------------------------- #
class _Stripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._out: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _DROP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self._out.append("\n")

    def handle_endtag(self, tag):
        if tag in _DROP_TAGS and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self._out.append(data)

    def text(self) -> str:
        joined = "".join(self._out)
        return re.sub(r"\n{3,}", "\n\n", joined).strip()


def html_to_text(html: str) -> str:
    parser = _Stripper()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 - malformed HTML shouldn't kill ingestion
        pass
    return parser.text()


# --------------------------------------------------------------------------- #
# message parsing
# --------------------------------------------------------------------------- #
@dataclass
class ParsedMessage:
    message_id: str
    thread_refs: str
    date_utc: str | None
    from_name: str
    from_addr: str
    to_addrs: str
    cc_addrs: str
    subject: str
    is_bulk: bool
    has_attach: bool
    size_bytes: int
    body_text: str
    snippet: str
    thread_key: str


def _addr_list(msg, header: str) -> str:
    out = []
    for name, addr in email.utils.getaddresses(msg.get_all(header, [])):
        if addr:
            out.append(f"{name} <{addr}>" if name else addr)
    return ", ".join(out)


def _body_text(msg, max_chars: int) -> str:
    text = ""
    try:
        body = msg.get_body(preferencelist=("plain", "html"))
    except Exception:  # noqa: BLE001
        body = None
    if body is not None:
        try:
            content = body.get_content()
        except Exception:  # noqa: BLE001
            content = ""
        text = html_to_text(content) if body.get_content_subtype() == "html" else content
    if not text:
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    text = part.get_content()
                    break
                except Exception:  # noqa: BLE001
                    continue
    return (text or "").strip()[:max_chars]


def parse_message(raw: bytes, size_hint: int, max_chars: int) -> ParsedMessage:
    msg = email.message_from_bytes(raw, policy=email.policy.default)

    mid = (msg.get("Message-ID") or "").strip().strip("<>").strip()
    refs = " ".join(
        f"{msg.get('References', '')} {msg.get('In-Reply-To', '')}".split()
    )

    date_utc = None
    try:
        dt = email.utils.parsedate_to_datetime(msg.get("Date"))
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            date_utc = dt.astimezone(timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        pass

    from_name, from_addr = "", ""
    parsed_from = email.utils.getaddresses(msg.get_all("From", []))
    if parsed_from:
        from_name, from_addr = parsed_from[0]

    subject = " ".join(str(msg.get("Subject", "")).split())
    is_bulk = bool(msg.get("List-Id") or msg.get("List-Unsubscribe")) or (
        str(msg.get("Precedence", "")).lower() in ("bulk", "list", "junk")
    )
    try:
        has_attach = any(True for _ in msg.iter_attachments())
    except Exception:  # noqa: BLE001
        has_attach = False

    body = _body_text(msg, max_chars)
    snippet = " ".join(body.split())[:280]

    if not mid:
        digest = hashlib.sha256(
            f"{subject}|{from_addr}|{date_utc}|{body[:200]}".encode()
        ).hexdigest()[:24]
        mid = f"synthetic-{digest}"

    return ParsedMessage(
        message_id=mid,
        thread_refs=refs,
        date_utc=date_utc,
        from_name=from_name,
        from_addr=from_addr.lower(),
        to_addrs=_addr_list(msg, "To"),
        cc_addrs=_addr_list(msg, "Cc"),
        subject=subject,
        is_bulk=is_bulk,
        has_attach=has_attach,
        size_bytes=size_hint or len(raw),
        body_text=body,
        snippet=snippet,
        thread_key=normalize_subject(subject),
    )


# --------------------------------------------------------------------------- #
# ingestion
# --------------------------------------------------------------------------- #
class ImapIngest:
    def __init__(self, cfg: dict, conn):
        ic = cfg["imap"]
        self.host: str = ic["host"]
        self.port: int = int(ic.get("port", 993))
        self.username: str = ic["username"]
        self.folder_patterns: list[str] = list(ic.get("folders", ["INBOX"]))
        self.max_backfill: int = int(ic.get("max_backfill_per_folder", 2000))
        self.body_max: int = int(ic.get("body_max_chars", 20000))
        self.conn = conn
        self._client: IMAPClient | None = None

    def __enter__(self) -> "ImapIngest":
        self._client = IMAPClient(self.host, port=self.port, ssl=True)
        self._client.login(self.username, get_secret(SECRET_IMAP_PASSWORD))
        log.info("IMAP connected: %s@%s:%d", self.username, self.host, self.port)
        return self

    def __exit__(self, *_exc) -> None:
        try:
            if self._client is not None:
                self._client.logout()
        except Exception:  # noqa: BLE001
            pass

    # -- folders ----------------------------------------------------------
    def list_folders(self) -> list[str]:
        return [name for (_flags, _sep, name) in self._client.list_folders()]

    def resolve_folders(self) -> list[str]:
        available = self.list_folders()
        resolved: list[str] = []
        for pat in self.folder_patterns:
            if pat.endswith("/*"):
                base = pat[:-2]
                matches = [f for f in available if f == base or f.startswith(base + "/")]
                if not matches:
                    log.warning("no folders match pattern %r", pat)
                resolved += matches
            elif pat in available:
                resolved.append(pat)
            else:
                log.warning("configured folder not found on server: %r", pat)
        seen: set[str] = set()
        return [f for f in resolved if not (f in seen or seen.add(f))]

    # -- state ----------------------------------------------------------
    def _folder_state(self, folder: str):
        return self.conn.execute(
            "SELECT uidvalidity, last_uid FROM imap_folder_state WHERE folder = ?",
            (folder,),
        ).fetchone()

    def _save_state(self, folder: str, uidvalidity: int, last_uid: int) -> None:
        self.conn.execute(
            "INSERT INTO imap_folder_state (folder, uidvalidity, last_uid, last_sync_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(folder) DO UPDATE SET "
            "uidvalidity=excluded.uidvalidity, last_uid=excluded.last_uid, "
            "last_sync_at=excluded.last_sync_at",
            (folder, uidvalidity, last_uid, now_iso()),
        )
        self.conn.commit()

    # -- upsert ----------------------------------------------------------
    def _upsert(self, pm: ParsedMessage, folder: str, uid: int, uidvalidity: int,
                is_answered: bool) -> bool:
        # INSERT OR IGNORE is atomic, so a concurrent writer can't slip in between a
        # lookup and the insert; rowcount says whether this call created the row.
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO messages (message_id, folder, uid, uidvalidity, thread_refs, "
            "date_utc, from_name, from_addr, to_addrs, cc_addrs, subject, is_bulk, "
            "has_attach, is_answered, thread_key, size_bytes, body_text, snippet, "
            "fetched_at, first_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pm.message_id, folder, uid, uidvalidity, pm.thread_refs, pm.date_utc,
             pm.from_name, pm.from_addr, pm.to_addrs, pm.cc_addrs, pm.subject,
             int(pm.is_bulk), int(pm.has_attach), int(is_answered), pm.thread_key,
             pm.size_bytes, pm.body_text, pm.snippet, now_iso(), now_iso()),
        )
        if cur.rowcount == 1:
            return True
        self.conn.execute(
            "UPDATE messages SET folder=?, uid=?, uidvalidity=?, is_answered=?, "
            "thread_key=?, fetched_at=? WHERE message_id=?",
            (folder, uid, uidvalidity, int(is_answered), pm.thread_key,
             now_iso(), pm.message_id),
        )
        return False

    # -- flags -----------------------------------------------------------
    def refresh_flags(self, folder: str) -> int:
        """Re-check \\Answered on previously-ingested messages in this folder.

        A message's flags can change (the user replies) long after we stored its
        body, and the incremental UID sync only looks at *new* mail — so this
        is a separate, cheap FLAGS-only pass over everything we already have.
        """
        info = self._client.select_folder(folder, readonly=True)
        uidvalidity = int(info[b"UIDVALIDITY"])
        rows = self.conn.execute(
            "SELECT uid, message_id, is_answered FROM messages "
            "WHERE folder = ? AND uidvalidity = ?", (folder, uidvalidity)
        ).fetchall()
        if not rows:
            return 0
        by_uid = {r["uid"]: r for r in rows}
        uids = sorted(by_uid)
        changed = 0
        for start in range(0, len(uids), FETCH_BATCH):
            chunk = uids[start:start + FETCH_BATCH]
            resp = self._client.fetch(chunk, ["FLAGS"])
            for uid in chunk:
                data = resp.get(uid)
                if not data:
                    continue
                answered = int(b"\\Answered" in data.get(b"FLAGS", ()))
                if answered != int(by_uid[uid]["is_answered"]):
                    self.conn.execute(
                        "UPDATE messages SET is_answered = ? WHERE message_id = ?",
                        (answered, by_uid[uid]["message_id"]),
                    )
                    changed += 1
        self.conn.commit()
        if changed:
            log.info("%-30s flags refreshed: %d changed", folder, changed)
        return changed

    # -- sync ----------------------------------------------------------
    def sync_folder(self, folder: str, *, full: bool = False) -> int:
        info = self._client.select_folder(folder, readonly=True)
        uidvalidity = int(info[b"UIDVALIDITY"])
        exists = int(info.get(b"EXISTS", 0))

        state = self._folder_state(folder)
        last_uid = 0
        if state and int(state["uidvalidity"] or 0) == uidvalidity and not full:
            last_uid = int(state["last_uid"] or 0)
        elif state and int(state["uidvalidity"] or 0) != uidvalidity:
            log.warning("%s: UIDVALIDITY changed (%s -> %s) — full re-scan",
                        folder, state["uidvalidity"], uidvalidity)

        if last_uid:
            uids = [u for u in self._client.search(["UID", f"{last_uid + 1}:*"]) if u > last_uid]
        else:
            uids = list(self._client.search(["ALL"]))
            if len(uids) > self.max_backfill:
                uids = sorted(uids)[-self.max_backfill:]
                log.info("%s: capping first sync to newest %d of %d",
                         folder, self.max_backfill, exists)

        uids = sorted(uids)
        if not uids:
            self._save_state(folder, uidvalidity, last_uid)
            log.info("%-30s up to date", folder)
            return 0

        added = 0
        for start in range(0, len(uids), FETCH_BATCH):
            chunk = uids[start:start + FETCH_BATCH]
            resp = self._client.fetch(chunk, ["RFC822.SIZE", "BODY.PEEK[]", "FLAGS"])
            for uid in chunk:
                data = resp.get(uid)
                if not data or b"BODY[]" not in data:
                    continue
                raw = data[b"BODY[]"]
                answered = b"\\Answered" in data.get(b"FLAGS", ())
                pm = parse_message(raw, int(data.get(b"RFC822.SIZE", len(raw))), self.body_max)
                if self._upsert(pm, folder, uid, uidvalidity, answered):
                    added += 1
            self.conn.commit()
            last_uid = max(last_uid, max(chunk))
            self._save_state(folder, uidvalidity, last_uid)

        log.info("%-30s +%d new (through UID %d)", folder, added, last_uid)
        return added

    def run(self, *, full: bool = False) -> int:
        folders = self.resolve_folders()
        if not folders:
            log.warning("no folders to ingest")
            return 0
        log.info("ingesting %d folder(s): %s", len(folders), ", ".join(folders))
        total = 0
        for f in folders:
            total += self.sync_folder(f, full=full)
            self.refresh_flags(f)
        log.info("ingest done: %d new message(s)", total)
        return total


# --------------------------------------------------------------------------- #
# entry points
# --------------------------------------------------------------------------- #
def ingest(*, full: bool = False, only_folder: str | None = None) -> int:
    cfg = load_config()
    with connect() as conn:
        init_db(conn)
        with ImapIngest(cfg, conn) as ing:
            if only_folder:
                ing.folder_patterns = [only_folder]
            return ing.run(full=full)


def list_server_folders() -> None:
    cfg = load_config()
    with connect() as conn:
        with ImapIngest(cfg, conn) as ing:
            for name in sorted(ing.list_folders()):
                print(name)
