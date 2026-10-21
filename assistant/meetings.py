"""Trade-meeting minutes -> one short summary per meeting, shown in the brief.

Minutes arrive in two shapes:
  * Procore "communication" emails, with the minutes in the body (after a long
    recipient list);
  * ordinary emails whose minutes and schedule are PDF attachments.
For the second kind the PDFs are fetched read-only from the mail server and read by
the AI directly. The same meeting often arrives several times (forwards, re-sends),
so a meeting is keyed by project + date + area and summarised once.

Never creates tasks. The summary is general; the schedule list picks out trade starts
and slab-pour days, and anything naming the user's company is called out separately.
"""

from __future__ import annotations

import email
import email.policy
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone

from . import notion_api as N
from .config import SECRET_IMAP_PASSWORD, SECRET_NOTION_TOKEN, load_config, owner_name
from .db import now_iso
from .llm import complete_json
from .projects import ProjectDirectory
from .secrets_store import get_secret

log = logging.getLogger(__name__)

_LOOKBACK_DAYS = 10          # older minutes are not worth summarising
_MAX_PDF_BYTES = 12_000_000
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

_SCHEMA = {"type": "object", "properties": {
    "points": {"type": "array", "items": {"type": "string"}},
    "schedule": {"type": "array", "items": {"type": "string"}},
    "company_items": {"type": "array", "items": {"type": "string"}},
    "pours": {"type": "array", "items": {"type": "object", "properties": {
        "tower": {"type": "string"}, "what": {"type": "string"}, "date": {"type": "string"},
        "trade_days": {"type": "array", "items": {"type": "object", "properties": {
            "date": {"type": "string"}, "note": {"type": "string"}}, "required": ["date"]}}},
        "required": ["what", "date"]}}},
    "required": ["points", "schedule", "company_items", "pours"]}


def _company_terms() -> str:
    """How the user's company is named in minutes: [meetings] company_names, else the
    first label of the mail domain."""
    cfg = load_config()
    names = cfg.get("meetings", {}).get("company_names") or [
        cfg["imap"]["username"].split("@")[-1].split(".")[0]]
    return " / ".join(f"'{n}'" for n in names)


def meeting_date(subject: str | None) -> str | None:
    s = subject or ""
    m = re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", s)
    if m and m.group(1)[:3].lower() in _MONTHS:
        try:
            return date(int(m.group(3)), _MONTHS[m.group(1)[:3].lower()], int(m.group(2))).isoformat()
        except ValueError:
            return None
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", s)  # month first, as the GC writes it
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            return None
    return None


def is_meeting_notice(subject: str | None) -> bool:
    return "trade meeting" in (subject or "").lower() and meeting_date(subject) is not None


def _project_and_area(directory, folder: str, subject: str, body: str) -> tuple[str, str]:
    area_m = re.search(r"\b(Tower|Midrise)\b", subject or "", re.I)
    area = area_m.group(1).title() if area_m else ""
    blob = f"{folder} {subject}"
    # [[meetings.project_override]] match = "keyword", name = "Project": one meeting covering a
    # job that the Projects database splits into parts (e.g. two towers).
    for rule in load_config().get("meetings", {}).get("project_override", []):
        key = str(rule.get("match", "")).lower()
        if key and key in blob.lower():
            return str(rule["name"]), ""
    name, _ = directory.resolve(blob)
    return name or (folder.split("/")[0] if folder and folder.upper() != "INBOX" else "Unknown project"), area


def _procore_minutes(body: str) -> str:
    """The minutes follow a very long 'Sent to' list; start at the minutes' own heading."""
    hits = list(re.finditer(r"Trade Meeting Minutes\s*-\s*[A-Za-z]+\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}", body))
    text = body[hits[-1].start():] if hits else body
    return text[:14000]


def _fetch_pdfs(msg_row) -> list[tuple[str, bytes]] | None:
    """PDF attachments of one message, read-only. None = could not fetch (retry later)."""
    from imapclient import IMAPClient

    cfg = load_config()["imap"]
    try:
        c = IMAPClient(cfg["host"], port=int(cfg.get("port", 993)), ssl=True)
        try:
            c.login(cfg["username"], get_secret(SECRET_IMAP_PASSWORD))
            info = c.select_folder(msg_row["folder"], readonly=True)
            if int(info[b"UIDVALIDITY"]) != msg_row["uidvalidity"]:
                return None
            got = c.fetch([msg_row["uid"]], ["BODY.PEEK[]"])
            raw = got.get(msg_row["uid"], {}).get(b"BODY[]")
        finally:
            try:
                c.logout()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        log.warning("could not fetch attachments for %r: %s", msg_row["subject"], exc)
        return None
    if not raw:
        return None
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    pdfs, total = [], 0
    for part in msg.walk():
        name = (part.get_filename() or "").lower()
        if part.get_content_type() == "application/pdf" or name.endswith(".pdf"):
            data = part.get_payload(decode=True) or b""
            if data and total + len(data) <= _MAX_PDF_BYTES:
                pdfs.append(("application/pdf", data))
                total += len(data)
    return pdfs


def _summarise(project: str, area: str, when: str, text: str | None,
               files: list[tuple[str, bytes]] | None) -> dict:
    owner = owner_name()
    system = (
        f"You summarise construction trade-meeting minutes for {owner}, a duct detailer at a "
        f"sheet-metal fabricator (the company appears in the minutes as {_company_terms()}). "
        "Return:\n"
        "- points: 5 to 10 short bullets giving a general summary of EVERYTHING important in the "
        "meeting (progress, access and close-offs, inspections, deadlines, safety, coordination). "
        "Plain language, no filler.\n"
        "- schedule: other dated entries that matter (trade starts/finishes, inspections, deliveries "
        "that affect trades). Include the date. Do NOT repeat concrete pours here. Empty if none.\n"
        "- pours: for every concrete pour in the schedule (slab, core, column, ring beam) give the "
        "tower or area, what is poured (e.g. 'L23 slab'), the date, and trade_days: the dates of "
        "the trade days in the cycle immediately BEFORE that pour - days when the trades get "
        "access to the floor (entries called 'Trade day', 'Trade day 1/2', 'Trades/Steel', "
        "'Trades' etc.), each with its label as `note`. Leave trade_days empty if the schedule "
        "shows none. Dates are ISO (YYYY-MM-DD) using the meeting's year. Empty list if no pours.\n"
        "- company_items: every item that names the company above, duct, ductwork, HVAC, sheet metal, or "
        "the mechanical contractor's work that touches ductwork. Empty list if none.")
    parts = f"Project: {project}{' ' + area if area else ''}\nMeeting date: {when}\n"
    if files:
        parts += ("The attached PDFs are the meeting minutes and/or the project calendar "
                  "schedule. Summarise the minutes, and read the schedule for trade starts and slab pours.")
    else:
        parts += "\nMinutes:\n" + (text or "")
    data = complete_json(load_config()["ai"]["triage_model"], system, parts, schema=_SCHEMA,
                         max_tokens=6000, thinking_budget=0, files=files)
    clean = lambda xs: [str(x).strip() for x in (xs or []) if str(x).strip()]  # noqa: E731
    out = {k: clean(data.get(k)) for k in ("points", "schedule", "company_items")}
    out["pours"] = [p for p in (data.get("pours") or []) if isinstance(p, dict) and p.get("date")]
    return out


def process(conn, notion: N.Notion | None = None) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_LOOKBACK_DAYS)).isoformat()
    rows = [r for r in conn.execute(
        "SELECT m.* FROM messages m WHERE lower(m.subject) LIKE '%trade meeting%' AND m.folder != 'Sent' "
        "AND m.date_utc >= ? AND NOT EXISTS (SELECT 1 FROM meeting_msgs x WHERE x.message_id = m.message_id) "
        "ORDER BY m.has_attach DESC, m.date_utc", (cutoff,)).fetchall() if is_meeting_notice(r["subject"])]
    # Hide the raw emails from the ordinary lists whatever happens below.
    for r in conn.execute("SELECT message_id FROM messages WHERE lower(subject) LIKE '%trade meeting%'"):
        conn.execute("UPDATE classifications SET hidden = 1 WHERE message_id = ? AND hidden = 0", (r[0],))
    out = {"summarised": 0, "duplicate": 0, "skipped": 0}
    if not rows:
        conn.commit()
        return out
    notion = notion or N.Notion(get_secret(SECRET_NOTION_TOKEN))
    directory = ProjectDirectory(notion, load_config()["notion"]["projects_db_id"])

    for m in rows:
        when = meeting_date(m["subject"])
        project, area = _project_and_area(directory, m["folder"], m["subject"], m["body_text"] or "")
        existing = conn.execute("SELECT id FROM meetings WHERE project=? AND meeting_date=? AND area=?",
                                (project, when, area)).fetchone()
        if existing:
            conn.execute("INSERT OR REPLACE INTO meeting_msgs VALUES (?, 'duplicate', ?)",
                         (m["message_id"], existing["id"]))
            out["duplicate"] += 1
            continue
        procore = "procoretech.com" in (m["from_addr"] or "").lower()
        files = None
        text = None
        if procore:
            text = _procore_minutes(m["body_text"] or "")
        else:
            files = _fetch_pdfs(m) if m["has_attach"] else []
            if files is None:
                continue  # transient; try again next cycle
            if not files:
                conn.execute("INSERT OR REPLACE INTO meeting_msgs VALUES (?, 'no_content', NULL)",
                             (m["message_id"],))
                out["skipped"] += 1
                continue
        try:
            s = _summarise(project, area, when, text, files)
        except Exception as exc:  # noqa: BLE001 - retried next cycle
            log.error("meeting summary failed for %r: %s", m["subject"], exc)
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO meetings (project, meeting_date, area, message_id, points, schedule, "
            "company_items, pours, first_seen_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (project, when, area, m["message_id"], json.dumps(s["points"]), json.dumps(s["schedule"]),
             json.dumps(s["company_items"]), json.dumps(s["pours"]), m["first_seen_at"] or now_iso()))
        conn.execute("INSERT OR REPLACE INTO meeting_msgs VALUES (?, 'summarised', ?)",
                     (m["message_id"], cur.lastrowid))
        out["summarised"] += 1
        conn.commit()
        log.info("summarised %s %s %s", project, area, when)
    conn.commit()
    return out


_SCHEDULE_SHOWN = 8


def _upcoming(items: list[str]) -> list[str]:
    """Schedule lines that are still ahead of us, slab/column pours first, capped."""
    today = date.today().isoformat()
    keep = []
    for x in items:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", x)
        if not m or m.group(1) >= today:
            keep.append(x)
    return keep[:_SCHEDULE_SHOWN]


def _short(iso: str) -> str:
    try:
        return date.fromisoformat(iso[:10]).strftime("%a %b %d").replace(" 0", " ")
    except ValueError:
        return iso


def _upcoming_pours(pours: list[dict]) -> list[dict]:
    """Pours still ahead, soonest first, each with its lead-up trade days as one line."""
    today = date.today().isoformat()
    out = []
    for p in sorted(pours, key=lambda p: p.get("date", "")):
        if (p.get("date") or "") < today:
            continue
        days = sorted(p.get("trade_days") or [], key=lambda d: d.get("date", ""))
        trade = ", ".join(_short(d["date"]) + (f" ({d['note']})" if d.get("note") else "")
                          for d in days if d.get("date"))
        who = f"{p['tower']} · " if p.get("tower") else ""
        what = p["what"].strip()
        if "pour" not in what.lower():
            what += " pour"
        out.append({"line": f"{who}{what}: {_short(p['date'])}", "trade_days": trade})
    return out[:8]


def brief_data(conn, hours: int = 96) -> list[dict]:
    cut = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    groups: dict[tuple[str, str], dict] = {}
    for r in conn.execute("SELECT * FROM meetings WHERE first_seen_at >= ? "
                          "ORDER BY meeting_date DESC, project, area", (cut,)):
        g = groups.setdefault((r["project"], r["meeting_date"]),
                              {"project": r["project"], "date": r["meeting_date"], "areas": []})
        g["areas"].append({"area": r["area"], "points": json.loads(r["points"] or "[]"),
                           "schedule": _upcoming(json.loads(r["schedule"] or "[]")),
                           "pours": _upcoming_pours(json.loads(r["pours"] or "[]")),
                           "company_items": json.loads(r["company_items"] or "[]")})
    out = list(groups.values())
    for g in out:
        g["company_items"] = [(a["area"], x) for a in g["areas"] for x in a["company_items"]]
    return out
