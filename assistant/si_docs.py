"""Procore Site Instruction notices -> the instruction PDF and its attachments, read.

A Procore SI email carries no attachment. It has two kinds of link:

  * "View PDF" (storage.procore.com, signed): the 1-2 page instruction cover sheet;
  * the "Attachments:" row: one link per attached file (revised sheets, sketches),
    on the project's own Procore site, which redirects to a signed storage.procore.com
    download. No login is involved.

For each recent SI notice this module reads the raw email from the mail server
(read-only), follows those links under strict rules, saves the PDFs under
data/attachments/, and has the AI summarise them: what changed, the sheets, anything
touching ductwork, and the response deadline stated on the cover sheet. The summary
goes onto the task proposed for the notice (see task_review.py), including its
Notion page body.

Download rules: https only; the first host must be procore.com or a subdomain, redirects
may stay on *.procore.com or go to Procore's own S3 storage buckets, and the file must start with "%PDF-" and fit
the size cap. Signed links are never logged. Nothing is ever opened or executed.
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import html
import http.client
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

from . import notion_api as N
from .config import DATA_DIR, SECRET_IMAP_PASSWORD, load_config, owner_name
from .db import now_iso
from .llm import complete_json
from .secrets_store import get_secret

log = logging.getLogger(__name__)

ATTACH_DIR = DATA_DIR / "attachments"
_SUBJECT_RE = re.compile(r"instruction\s*#|site instruction", re.I)
_MAX_REDIRECTS = 3
_DEFAULT_LOOKBACK_DAYS = 14
_DEFAULT_MAX_FILE_MB = 25
_AI_BUDGET_BYTES = 14_000_000      # inline PDF data per request; the rest is saved, not read

_SCHEMA = {
    "type": "object",
    "properties": {
        "task_title": {"type": "string"},
        "what_changed": {"type": "array", "items": {"type": "string"}},
        "sheets": {"type": "array", "items": {"type": "string"}},
        "duct_impact": {"type": "array", "items": {"type": "string"}},
        "response_requested": {"type": "string"},
        "action_needed": {"type": "boolean"},
    },
    "required": ["task_title", "what_changed", "sheets", "duct_impact",
                 "response_requested", "action_needed"],
}


def is_si_notice(subject: str | None, from_addr: str | None) -> bool:
    return "procoretech.com" in (from_addr or "").lower() and bool(_SUBJECT_RE.search(subject or ""))


# --------------------------------------------------------------------------- #
# reading the links out of the raw email
# --------------------------------------------------------------------------- #
class _Anchors(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._href: list[str] = []
        self._text: list[str] = []
        self.found: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href.append(dict(attrs).get("href") or "")
            self._text = []

    def handle_data(self, data):
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            self.found.append((self._href.pop(), " ".join("".join(self._text).split())))


def _is_procore(host: str) -> bool:
    return host == "procore.com" or host.endswith(".procore.com")


# Older links hand the download on to Procore's own storage buckets on Amazon S3
# (procore-pdf-ca01..., prostore-ca01...). Only reachable by redirect from procore.com.
_PROCORE_BUCKET = re.compile(r"^(procore|prostore)[a-z0-9-]*\.s3[a-z0-9.-]*\.amazonaws\.com$")


def _may_fetch(host: str, redirected: bool) -> bool:
    return _is_procore(host) or (redirected and bool(_PROCORE_BUCKET.match(host)))


def extract_links(html_src: str) -> list[tuple[str, str]]:
    """(label, url) for the cover sheet and each attachment, de-duplicated.

    The cover sheet is the signed storage.procore.com "View PDF" link; attachments are
    links on the project's Procore site whose path contains "attachment".
    """
    parser = _Anchors()
    parser.feed(html_src)
    out, seen = [], set()
    for href, label in parser.found:
        url = html.unescape(href).strip()
        p = urlparse(url)
        if p.scheme != "https" or not _is_procore(p.netloc):
            continue
        cover = p.netloc == "storage.procore.com" and label.lower().startswith("view pdf")
        attach = "attachment" in p.path.lower() and p.netloc != "storage.procore.com"
        if not (cover or attach) or url in seen:
            continue
        seen.add(url)
        out.append((label or "document", url))
    return out


def _safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip(" .") or "document.pdf"
    return name if name.lower().endswith(".pdf") else name + ".pdf"


def _filename(headers_value: str | None, fallback: str) -> str:
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', headers_value or "", re.I)
    return _safe_name(m.group(1) if m else fallback)


def _download(url: str, label: str, max_bytes: int) -> tuple[str, bytes] | None:
    """Follow at most a few redirects, every hop on procore.com. None = not a usable PDF."""
    for hop in range(_MAX_REDIRECTS + 1):
        p = urlparse(url)
        if p.scheme != "https" or not _may_fetch(p.netloc, redirected=hop > 0):
            log.warning("si_docs: refusing a link off procore.com (host %s)", p.netloc)
            return None
        conn = http.client.HTTPSConnection(p.netloc, timeout=45)
        try:
            conn.request("GET", p.path + (f"?{p.query}" if p.query else ""),
                         headers={"User-Agent": "Mozilla/5.0"})
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308):
                loc = resp.getheader("Location")
                if not loc:
                    return None
                url = urljoin(url, loc)
                continue
            if resp.status != 200:
                log.warning("si_docs: %s answered HTTP %d", p.netloc, resp.status)
                return None
            data = resp.read(max_bytes + 1)
            disp = resp.getheader("Content-Disposition")
        finally:
            conn.close()
        if len(data) > max_bytes:
            log.warning("si_docs: %r is over the size cap, skipped", label)
            return None
        if not data.startswith(b"%PDF-"):
            log.warning("si_docs: %r is not a PDF (a login page, perhaps), skipped", label)
            return None
        return _filename(disp, label), data
    log.warning("si_docs: too many redirects for %r", label)
    return None


# --------------------------------------------------------------------------- #
# the raw email
# --------------------------------------------------------------------------- #
def _fetch_html(row) -> tuple[str | None, bool]:
    """(html body, retry_later). Read-only. (None, False) = the email is gone."""
    from imapclient import IMAPClient

    cfg = load_config()["imap"]
    try:
        c = IMAPClient(cfg["host"], port=int(cfg.get("port", 993)), ssl=True)
        try:
            c.login(cfg["username"], get_secret(SECRET_IMAP_PASSWORD))
            info = c.select_folder(row["folder"], readonly=True)
            uid = row["uid"] if int(info[b"UIDVALIDITY"]) == row["uidvalidity"] else None
            if uid is None or uid not in c.search(["UID", str(uid)]):
                hits = c.search(["HEADER", "Message-ID", row["message_id"]])
                uid = hits[0] if hits else None
            if uid is None:
                return None, False
            raw = c.fetch([uid], ["BODY.PEEK[]"]).get(uid, {}).get(b"BODY[]")
        finally:
            try:
                c.logout()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        log.warning("si_docs: could not read %r from the server: %s", row["subject"], exc)
        return None, True
    if not raw:
        return None, False
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    body = msg.get_body(preferencelist=("html",))
    return (body.get_content() if body else ""), False


# --------------------------------------------------------------------------- #
# summarising
# --------------------------------------------------------------------------- #
def _summarise(subject: str, files: list[tuple[str, bytes]]) -> dict:
    owner = owner_name()
    system = (
        f"You read a construction Site Instruction (SI) for {owner}, a duct detailer at a "
        "sheet-metal fabricator. The first PDF is normally the instruction cover sheet; any "
        "others are its attachments (revised drawings, sketches, specifications). Return:\n"
        "- task_title: one imperative line for a to-do, starting with the SI number "
        "(e.g. 'SI-259: review fireplace revision and send cost/schedule impact').\n"
        "- what_changed: up to 5 short bullets on what the instruction changes.\n"
        "- sheets: drawing/sheet numbers named or shown (e.g. A207, M3.10); empty if none.\n"
        "- duct_impact: bullets for anything in the documents that touches ductwork, HVAC "
        "equipment, grilles, dampers, shafts or mechanical rooms. State only what the "
        "documents show; do NOT guess what such a change typically affects. Empty if none.\n"
        "- response_requested: the response or deadline the instruction asks for, quoted "
        "closely (e.g. 'Review extras, labor, material and schedule impact within 5 calendar "
        "days'); empty string if none.\n"
        "- action_needed: true if the instruction asks for a response, or changes something "
        "a duct detailer must update; false if it only withdraws or notes something.")
    prompt = (f"Email subject: {subject}\nThere are {len(files)} PDF(s) attached, "
              "in this order: " + ", ".join(n for n, _ in files))
    data = complete_json(load_config()["ai"]["triage_model"], system, prompt, schema=_SCHEMA,
                         max_tokens=2000, thinking_budget=0,
                         files=[("application/pdf", b) for _, b in files])
    clean = lambda xs: [str(x).strip() for x in (xs or []) if str(x).strip()]  # noqa: E731
    return {
        "task_title": str(data.get("task_title") or "").strip(),
        "what_changed": clean(data.get("what_changed")),
        "sheets": clean(data.get("sheets")),
        "duct_impact": clean(data.get("duct_impact")),
        "response_requested": str(data.get("response_requested") or "").strip(),
        "action_needed": bool(data.get("action_needed")),
    }


# --------------------------------------------------------------------------- #
# the cycle step
# --------------------------------------------------------------------------- #
def process(conn) -> dict:
    scfg = load_config().get("si_documents", {})
    if not scfg.get("enabled", True):
        return {}
    days = int(scfg.get("lookback_days", _DEFAULT_LOOKBACK_DAYS))
    max_bytes = int(float(scfg.get("max_file_mb", _DEFAULT_MAX_FILE_MB)) * 1_000_000)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = [r for r in conn.execute(
        "SELECT m.* FROM messages m WHERE m.folder != 'Sent' AND m.from_addr LIKE '%procoretech.com%' "
        "AND m.date_utc >= ? AND NOT EXISTS (SELECT 1 FROM si_docs s WHERE s.message_id = m.message_id) "
        "ORDER BY m.date_utc", (cutoff,)).fetchall() if is_si_notice(r["subject"], r["from_addr"])]
    out = {"read": 0, "no_pdf": 0, "gone": 0, "retry": 0}

    def finish(row, status, title=None, summary=None, files=None):
        conn.execute(
            "INSERT OR REPLACE INTO si_docs (message_id, status, task_title, summary, files, fetched_at) "
            "VALUES (?,?,?,?,?,?)",
            (row["message_id"], status, title, json.dumps(summary) if summary else None,
             json.dumps(files) if files is not None else None, now_iso()))
        conn.commit()

    for m in rows:
        src, retry = _fetch_html(m)
        if retry:
            out["retry"] += 1
            continue
        if src is None:
            finish(m, "gone")
            out["gone"] += 1
            continue
        links = extract_links(src)
        got: list[tuple[str, bytes, str]] = []
        for label, url in links:
            res = _download(url, label, max_bytes)
            if res:
                got.append((res[0], res[1], label))
        if not got:
            finish(m, "no_pdf")
            out["no_pdf"] += 1
            continue

        folder = ATTACH_DIR / (re.sub(r"[^A-Za-z0-9._-]+", "_", (m["subject"] or "SI"))[:60]
                               + "_" + hashlib.sha1(m["message_id"].encode()).hexdigest()[:6])
        folder.mkdir(parents=True, exist_ok=True)
        files_meta, to_read, used = [], [], 0
        for name, data, _label in got:
            target = folder / name
            target.write_bytes(data)
            read = used + len(data) <= _AI_BUDGET_BYTES
            if read:
                to_read.append((name, data))
                used += len(data)
            files_meta.append({"name": name, "path": str(target), "read": read})
        try:
            summary = _summarise(m["subject"] or "", to_read)
        except Exception as exc:  # noqa: BLE001 - retried next cycle
            log.error("si_docs: summary failed for %r: %s", m["subject"], exc)
            out["retry"] += 1
            continue
        finish(m, "done", summary["task_title"] or None, summary, files_meta)
        out["read"] += 1
        log.info("si_docs: read %d PDF(s) for %r", len(got), m["subject"])
    return out


# --------------------------------------------------------------------------- #
# using the result
# --------------------------------------------------------------------------- #
def for_message(conn, message_id: str | None) -> dict | None:
    """{task_title, summary, files} for a notice that was read, else None."""
    if not message_id:
        return None
    r = conn.execute("SELECT * FROM si_docs WHERE message_id = ? AND status = 'done'",
                     (message_id,)).fetchone()
    if not r or not r["summary"]:
        return None
    return {"task_title": r["task_title"], "summary": json.loads(r["summary"]),
            "files": json.loads(r["files"] or "[]")}


def origin_text(base: str, doc: dict) -> str:
    """Short text for the task's Origin property (Notion allows 2000 characters)."""
    s = doc["summary"]
    parts = [base]
    if s["response_requested"]:
        parts.append("Response: " + s["response_requested"])
    if s["duct_impact"]:
        parts.append("Ducts: " + "; ".join(s["duct_impact"]))
    return "\n".join(parts)[:1900]


def _block(kind: str, text: str) -> dict:
    return {"object": "block", "type": kind,
            kind: {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]}}


def publish_to_page(notion: N.Notion, page_id: str, doc: dict) -> int:
    s = doc["summary"]
    blocks = [_block("heading_3", "Site Instruction summary")]
    if s["response_requested"]:
        blocks.append(_block("paragraph", "Response requested: " + s["response_requested"]))
    if s["what_changed"]:
        blocks.append(_block("paragraph", "What changed"))
        blocks += [_block("bulleted_list_item", t) for t in s["what_changed"]]
    if s["duct_impact"]:
        blocks.append(_block("paragraph", "Touches ductwork / mechanical"))
        blocks += [_block("bulleted_list_item", t) for t in s["duct_impact"]]
    if s["sheets"]:
        blocks.append(_block("paragraph", "Sheets: " + ", ".join(s["sheets"])))
    for f in doc["files"]:
        note = "" if f["read"] else " (saved, too large to read)"
        blocks.append(_block("bulleted_list_item", f"{f['name']}{note}  -  {f['path']}"))
    notion.append_blocks(page_id, blocks)
    return len(blocks)
