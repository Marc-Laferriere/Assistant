"""New-drawing notices -> one reviewable list per project.

Autodesk Forma ("New sheets were published/added") and Procore ("New drawings")
emails are fully automated and would otherwise each become their own task. They
are parsed deterministically (no AI) into `drawing_items`, then collected into one
`task_candidates` row per project (source_type 'drawings'). Accepting that
candidate creates a single Notion task whose body is a checklist of the sheets,
grouped by drawing set. Later notices for the same project are appended to that
task automatically while it is still open; once it is Done/Archived the next
notice starts a fresh candidate.

Architectural (A), Mechanical (M) and Structural (S) sheets each get a checkbox;
every other discipline is listed plainly on one line.
"""

from __future__ import annotations

import html
import logging
import re
from collections import defaultdict
from dataclasses import dataclass

from . import notion_api as N
from .config import SECRET_NOTION_TOKEN, load_config
from .db import now_iso
from .projects import ProjectDirectory
from .secrets_store import get_secret

log = logging.getLogger(__name__)

_FORMA_SUBJ = re.compile(r"new sheets were (published|added)", re.I)
_PROCORE_SUBJ = re.compile(r"new drawings\b", re.I)
_DOWNLOAD_SUBJ = re.compile(r"drawings download", re.I)
_REVIEW_KINDS = ("A", "M", "S")
_BLOCK_CHUNK = 90  # Notion allows 100 children per request
_CLOSED_STATUSES = {"Done", "Archived"}


# --------------------------------------------------------------------------- #
# recognising and parsing notices
# --------------------------------------------------------------------------- #
def notice_kind(subject: str | None, from_addr: str | None) -> str | None:
    """'forma' | 'procore' | 'download' for the automated drawing emails, else None."""
    subj, frm = subject or "", (from_addr or "").lower()
    if "forma.autodesk.com" in frm and _FORMA_SUBJ.search(subj):
        return "forma"
    if "procoretech.com" in frm:
        if _DOWNLOAD_SUBJ.search(subj):
            return "download"
        if _PROCORE_SUBJ.search(subj):
            return "procore"
    return None


@dataclass
class Sheet:
    project_text: str
    set_name: str
    sheet: str
    title: str = ""
    rev: str = ""
    discipline: str = ""
    published: str = ""


def _collapse(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _strip_tags(text: str) -> str:
    return _collapse(html.unescape(re.sub(r"<[^>]+>", " ", text)))


def _parse_forma(body: str) -> tuple[list[Sheet], str | None]:
    t = _collapse(body)
    pm = re.search(r"•\s*(.+?)\s+New sheets were", t)
    project = pm.group(1).strip() if pm else ""
    sheets: list[Sheet] = []
    note = None
    if re.search(r"New sheets were published", t):
        sm = re.search(r"Version set (.+?) Publish date (.+?\d\d?:\d\d (?:AM|PM))", t)
        set_name = sm.group(1).strip() if sm else ""
        pub = sm.group(2).strip() if sm else ""
        seg = re.search(r"Sheet Status (.+?)(?: \d+ more sheets?| View sheets)", t)
        for name in re.findall(r"(\S+)\s+(?:Published|Updated|Revised|Added)\b",
                               seg.group(1) if seg else ""):
            sheets.append(Sheet(project, set_name, name, published=pub))
        more = re.search(r"(\d+) more sheets?", t)
        if more:
            note = f"{more.group(1)} more sheet(s) not listed in the email"
    else:
        seg = re.search(r"Sheet Version Set Status (.+?) View Bridge details", t)
        for chunk in re.split(r"\s+Added\b", seg.group(1) if seg else ""):
            chunk = chunk.strip()
            if chunk:
                name, _, set_name = chunk.partition(" ")
                sheets.append(Sheet(project, set_name.strip(), name))
    return sheets, note


def _parse_procore(body: str) -> tuple[list[Sheet], str | None]:
    pm = re.search(r"PROJECT:\s*(.+?)\s+-{5,}", _collapse(body))
    project = pm.group(1).strip() if pm else ""
    sheets: list[Sheet] = []
    for row in re.findall(r"<tr>(.*?)</tr>", body or "", re.S | re.I):
        cells = [_strip_tags(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)]
        if len(cells) >= 7:
            drawing, title, rev, area, set_name, discipline, date = cells[:7]
            sheets.append(Sheet(f"{project} {area}".strip(), set_name, drawing, title=title,
                                rev=rev, discipline=discipline, published=date))
    return sheets, None


def parse_notice(kind: str, body: str | None) -> tuple[list[Sheet], str | None]:
    if kind == "forma":
        return _parse_forma(body or "")
    if kind == "procore":
        return _parse_procore(body or "")
    return [], None


def sheet_kind(sheet: str, discipline: str = "") -> str:
    """'A' / 'M' / 'S' for the disciplines that get a checkbox, else 'other'."""
    d = discipline.lower()
    if d:
        if "arch" in d:
            return "A"
        if "mech" in d or "hvac" in d:
            return "M"
        if "struct" in d:
            return "S"
        return "other"
    m = re.match(r"([AMS])[-\s.]?\d", sheet.upper())
    return m.group(1) if m else "other"


# --------------------------------------------------------------------------- #
# storing, grouping into candidates
# --------------------------------------------------------------------------- #
def _notion() -> N.Notion:
    return N.Notion(get_secret(SECRET_NOTION_TOKEN))


def _project_label(directory: ProjectDirectory | None, text: str, set_name: str = "") -> str:
    if directory is not None:
        name, _ = directory.resolve(text)
        if name:
            return name
    return re.sub(r"^\s*\d+\s*-\s*", "", text).strip() or "Unknown project"


def _ingest_notice(conn, directory, msg) -> int:
    kind = notice_kind(msg["subject"], msg["from_addr"])
    sheets, note = parse_notice(kind, msg["body_text"]) if kind in ("forma", "procore") else ([], None)
    if kind in ("forma", "procore") and not sheets:
        # Format drifted or the body was cut off: keep a pointer so nothing vanishes.
        sheets = [Sheet(_project_from_subject(msg["subject"]), "", "(see email)",
                        title=(msg["subject"] or "")[:160])]
        note = "could not read the sheet list from this notice"
    n = 0
    for s in sheets:
        project = _project_label(directory, s.project_text)
        cur = conn.execute(
            "INSERT OR IGNORE INTO drawing_items (message_id, project, set_name, sheet, title, "
            "rev, discipline, kind, notice_date) VALUES (?,?,?,?,?,?,?,?,?)",
            (msg["message_id"], project, s.set_name, s.sheet, s.title, s.rev, s.discipline,
             sheet_kind(s.sheet, s.discipline), s.published or (msg["date_utc"] or "")[:10]),
        )
        n += cur.rowcount
    conn.execute(
        "INSERT OR REPLACE INTO drawing_notices (message_id, parsed_at, n_items, extra) "
        "VALUES (?,?,?,?)", (msg["message_id"], now_iso(), n, note),
    )
    return n


def _project_from_subject(subject: str | None) -> str:
    return re.split(r"\s+-\s+New sheets|:\s*New drawings", subject or "", maxsplit=1)[0]


def fold_pending(conn, directory) -> int:
    """One-time: absorb already-queued notice candidates into the drawing lists."""
    rows = conn.execute(
        "SELECT tc.id AS cid, tc.title, m.* FROM task_candidates tc "
        "JOIN messages m ON m.message_id = tc.message_id "
        "WHERE tc.status = 'proposed' AND tc.source_type = 'email'"
    ).fetchall()
    folded = 0
    for r in rows:
        kind = notice_kind(r["subject"], r["from_addr"])
        if not kind:
            continue
        if kind != "download":
            _ingest_notice(conn, directory, r)
        conn.execute("UPDATE task_candidates SET status='rejected', decided_at=? WHERE id=?",
                     (now_iso(), r["cid"]))
        conn.execute(
            "INSERT INTO feedback (candidate_id, source_type, decision, reason, detail, created_at) "
            "VALUES (?, 'email', 'rejected', ?, ?, ?)",
            (r["cid"], "drawing_download_ignored" if kind == "download" else "merged_into_drawing_list",
             r["title"], now_iso()),
        )
        folded += 1
    conn.commit()
    return folded


def _page_open(notion: N.Notion, page_id: str, cache: dict) -> bool:
    if page_id in cache:
        return cache[page_id]
    try:
        page = notion.get_page(page_id)
    except N.NotionError as exc:
        if exc.status == 404:
            cache[page_id] = False
            return False
        raise
    status = N.plain(page["properties"].get("Status"))
    cache[page_id] = not (page.get("archived") or page.get("in_trash") or status in _CLOSED_STATUSES)
    return cache[page_id]


def _items(conn, candidate_id: int, *, only_unsynced: bool = False):
    q = "SELECT * FROM drawing_items WHERE candidate_id = ?"
    if only_unsynced:
        q += " AND in_notion = 0"
    return conn.execute(q + " ORDER BY notice_date, set_name, sheet", (candidate_id,)).fetchall()


def _refresh_candidate(conn, cid: int) -> None:
    items = _items(conn, cid)
    sets = {i["set_name"] for i in items}
    counts = {k: sum(1 for i in items if i["kind"] == k) for k in (*_REVIEW_KINDS, "other")}
    detail = (f"{len(items)} sheet(s) in {len(sets)} set(s): "
              f"{counts['M']} mech, {counts['A']} arch, {counts['S']} struct, {counts['other']} other")
    conn.execute("UPDATE task_candidates SET detail=?, reason=? WHERE id=?",
                 (detail, "Automated new-drawing notices collected for this project", cid))


# --------------------------------------------------------------------------- #
# Notion body
# --------------------------------------------------------------------------- #
def _heading(text: str) -> dict:
    return {"object": "block", "type": "heading_3",
            "heading_3": {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]}}


def _todo(text: str) -> dict:
    return {"object": "block", "type": "to_do",
            "to_do": {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}],
                      "checked": False}}


def _label(i) -> str:
    out = i["sheet"]
    if i["title"]:
        out += f" - {i['title']}"
    if i["rev"]:
        out += f" (rev {i['rev']})"
    return out


def _groups(items) -> list[tuple[tuple[str, str], list]]:
    grouped: dict[tuple[str, str], list] = defaultdict(list)
    for i in items:
        grouped[(i["set_name"] or "Unnamed set", i["notice_date"] or "")].append(i)
    return list(grouped.items())


def build_blocks(conn, items, *, intro: bool = False) -> list[dict]:
    blocks: list[dict] = []
    if intro:
        blocks.append(N.paragraph_block(
            "Collected automatically from Forma / Procore new-drawing notices. "
            "Tick each architectural, mechanical and structural sheet as you review it."))
    extras = {r[0]: r[1] for r in conn.execute("SELECT message_id, extra FROM drawing_notices")}
    for (set_name, date), group in _groups(items):
        blocks.append(_heading(f"{set_name}  ({date})" if date else set_name))
        for i in group:
            if i["kind"] in _REVIEW_KINDS:
                blocks.append(_todo(_label(i)))
        others = [i["sheet"] for i in group if i["kind"] not in _REVIEW_KINDS]
        if others:
            blocks.append(N.paragraph_block("Other sheets: " + ", ".join(others)))
        notes = sorted({extras[i["message_id"]] for i in group if extras.get(i["message_id"])})
        for n in notes:
            blocks.append(N.paragraph_block(f"Note: {n}"))
    return blocks


def publish_to_page(conn, notion: N.Notion, page_id: str, candidate_id: int, *,
                    intro: bool = False) -> int:
    """Append this candidate's not-yet-synced sheets to a Notion page."""
    items = _items(conn, candidate_id, only_unsynced=True)
    if not items:
        return 0
    blocks = build_blocks(conn, items, intro=intro)
    for start in range(0, len(blocks), _BLOCK_CHUNK):
        notion.append_blocks(page_id, blocks[start:start + _BLOCK_CHUNK])
    conn.execute("UPDATE drawing_items SET in_notion = 1 WHERE candidate_id = ? AND in_notion = 0",
                 (candidate_id,))
    conn.commit()
    return len(items)


def describe(conn, candidate_id: int) -> list[str]:
    """Console lines for the review prompt."""
    lines = []
    for (set_name, date), group in _groups(_items(conn, candidate_id)):
        review = [i["sheet"] for i in group if i["kind"] in _REVIEW_KINDS]
        others = [i["sheet"] for i in group if i["kind"] not in _REVIEW_KINDS]
        lines.append(f"  - {set_name} ({date})")
        if review:
            lines.append(f"      check: {', '.join(review)}")
        if others:
            lines.append(f"      other: {', '.join(others)}")
    return lines


# --------------------------------------------------------------------------- #
# the pass that runs every cycle
# --------------------------------------------------------------------------- #
def process(conn, notion: N.Notion | None = None) -> int:
    """Parse new notices, route their sheets to a per-project candidate, and top up
    open Notion tasks. Returns the number of newly parsed sheets."""
    try:
        notion = notion or _notion()
        directory = ProjectDirectory(notion, load_config()["notion"]["projects_db_id"])
    except Exception as exc:  # noqa: BLE001 - never block the cycle; retried next run
        log.error("drawing lists skipped (Notion unavailable): %s", exc)
        return 0

    since_row = conn.execute("SELECT value FROM sync_state WHERE key='drawings_since'").fetchone()
    if since_row is None:
        folded = fold_pending(conn, directory)
        since = now_iso()
        conn.execute("INSERT INTO sync_state (key, value) VALUES ('drawings_since', ?)", (since,))
        conn.commit()
        log.info("drawing lists enabled: folded %d queued notice candidate(s)", folded)
    else:
        since = since_row[0]

    parsed = 0
    for msg in conn.execute(
        "SELECT * FROM messages m WHERE m.first_seen_at >= ? AND m.folder != 'Sent' "
        "AND NOT EXISTS (SELECT 1 FROM drawing_notices d WHERE d.message_id = m.message_id)",
        (since,),
    ).fetchall():
        kind = notice_kind(msg["subject"], msg["from_addr"])
        if kind in ("forma", "procore"):
            parsed += _ingest_notice(conn, directory, msg)
    conn.commit()

    cache: dict[str, bool] = {}
    projects = [r[0] for r in conn.execute(
        "SELECT DISTINCT project FROM drawing_items WHERE candidate_id IS NULL")]
    for project in projects:
        target = conn.execute(
            "SELECT id, status, notion_page_id FROM task_candidates "
            "WHERE source_type='drawings' AND project=? ORDER BY id DESC LIMIT 1", (project,),
        ).fetchone()
        cid = None
        if target and target["status"] == "proposed":
            cid = target["id"]
        elif target and target["status"] == "pushed" and target["notion_page_id"] \
                and _page_open(notion, target["notion_page_id"], cache):
            cid = target["id"]
        if cid is None:
            cur = conn.execute(
                "INSERT INTO task_candidates (source_type, title, project, priority, horizon, "
                "status, created_at) VALUES ('drawings', ?, ?, 'Medium', 'Active', 'proposed', ?)",
                (f"Review new drawings: {project}", project, now_iso()),
            )
            cid = cur.lastrowid
        conn.execute("UPDATE drawing_items SET candidate_id=? WHERE project=? AND candidate_id IS NULL",
                     (cid, project))
        _refresh_candidate(conn, cid)
    conn.commit()

    for cand in conn.execute(
        "SELECT id, notion_page_id FROM task_candidates WHERE source_type='drawings' "
        "AND status='pushed' AND notion_page_id IS NOT NULL AND EXISTS "
        "(SELECT 1 FROM drawing_items d WHERE d.candidate_id = task_candidates.id AND d.in_notion = 0)"
    ).fetchall():
        try:
            if _page_open(notion, cand["notion_page_id"], cache):
                n = publish_to_page(conn, notion, cand["notion_page_id"], cand["id"])
                log.info("added %d sheet(s) to open drawing-review task %s", n, cand["notion_page_id"])
        except Exception as exc:  # noqa: BLE001 - retried next cycle
            log.error("could not update drawing-review task: %s", exc)
    return parsed
