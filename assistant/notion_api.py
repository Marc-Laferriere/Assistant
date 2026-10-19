"""Minimal Notion REST client (stdlib only).

Just the calls Phase 1 needs: query a database, create a page, update a page,
retrieve a database schema. Handles pagination and 429 rate-limiting.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Iterator

log = logging.getLogger(__name__)

API_ROOT = "https://api.notion.com"
NOTION_VERSION = "2022-06-28"


class NotionError(RuntimeError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"Notion API {status} {code}: {message}")
        self.status = status
        self.code = code


class Notion:
    def __init__(self, token: str, *, max_retries: int = 4):
        self._token = token
        self._max_retries = max_retries

    # -- transport --------------------------------------------------------
    def _request(self, method: str, path: str, body: dict | None = None) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        attempt = 0
        while True:
            attempt += 1
            req = urllib.request.Request(
                API_ROOT + path, method=method, data=data, headers=headers
            )
            try:
                with urllib.request.urlopen(req) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as exc:
                payload = exc.read().decode(errors="replace")
                try:
                    parsed = json.loads(payload)
                except ValueError:
                    parsed = {}
                if exc.code == 429 and attempt <= self._max_retries:
                    wait = float(exc.headers.get("Retry-After", "1")) or 1.0
                    log.warning("rate-limited, retrying in %.1fs", wait)
                    time.sleep(wait)
                    continue
                if exc.code >= 500 and attempt <= self._max_retries:
                    wait = 2.0 * attempt
                    log.warning("Notion %s, retrying in %.1fs", exc.code, wait)
                    time.sleep(wait)
                    continue
                raise NotionError(
                    exc.code,
                    parsed.get("code", "unknown"),
                    parsed.get("message", payload[:200]),
                ) from None

    # -- endpoints ------------------------------------------------------
    def get_database(self, db_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/databases/{db_id}")

    def iter_database(
        self,
        db_id: str,
        page_size: int = 100,
        flt: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        cursor: str | None = None
        while True:
            body: dict[str, Any] = {"page_size": page_size}
            if flt:
                body["filter"] = flt
            if cursor:
                body["start_cursor"] = cursor
            result = self._request("POST", f"/v1/databases/{db_id}/query", body)
            yield from result.get("results", [])
            if not result.get("has_more"):
                return
            cursor = result.get("next_cursor")

    def create_page(self, parent_db_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/pages",
            {"parent": {"database_id": parent_db_id}, "properties": properties},
        )

    def update_page(self, page_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", f"/v1/pages/{page_id}", {"properties": properties})

    def get_page(self, page_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/pages/{page_id}")

    def iter_block_children(self, block_id: str) -> Iterator[dict[str, Any]]:
        cursor: str | None = None
        while True:
            path = f"/v1/blocks/{block_id}/children?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
            result = self._request("GET", path)
            yield from result.get("results", [])
            if not result.get("has_more"):
                return
            cursor = result.get("next_cursor")

    def append_blocks(self, page_id: str, blocks: list[dict[str, Any]]) -> dict[str, Any]:
        return self._request("PATCH", f"/v1/blocks/{page_id}/children", {"children": blocks})


# -- property value helpers --------------------------------------------------
def title(text: str) -> dict:
    return {"title": [{"text": {"content": text[:2000]}}]}


def rich_text(text: str) -> dict:
    return {"rich_text": [{"text": {"content": text[:2000]}}]} if text else {"rich_text": []}


def date(iso_day: str | None) -> dict:
    return {"date": {"start": iso_day} if iso_day else None}


def select(name: str | None) -> dict:
    return {"select": {"name": name} if name else None}


def status(name: str) -> dict:
    return {"status": {"name": name}}


def checkbox(value: bool) -> dict:
    return {"checkbox": bool(value)}


def url(value: str | None) -> dict:
    return {"url": value or None}


def relation(page_ids: list[str]) -> dict:
    return {"relation": [{"id": pid} for pid in page_ids]}


def paragraph_block(text: str) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]},
    }


# -- read helpers ----------------------------------------------------------
def plain(prop: dict | None) -> Any:
    """Extract a comparable Python value from a Notion property object."""
    if not prop:
        return None
    kind = prop.get("type")
    val = prop.get(kind)
    if kind in ("title", "rich_text"):
        return "".join(part.get("plain_text", "") for part in val).strip()
    if kind in ("select", "status"):
        return val["name"] if val else None
    if kind == "multi_select":
        return [opt["name"] for opt in val]
    if kind == "date":
        return val["start"] if val else None
    if kind == "number":
        return val
    if kind == "checkbox":
        return bool(val)
    if kind == "url":
        return val
    return val
