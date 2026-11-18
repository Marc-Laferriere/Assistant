"""A Notion client that cannot write.

Notion permissions belong to the integration, not to a page: once a colleague's page
is shared with the assistant's integration, the same token could edit it. So "read
only" has to be enforced here. This class has no write methods, and every request
is checked against an allow-list of read endpoints before it leaves the process
(Notion's database query and search are POSTs, but they only read).
"""

from __future__ import annotations

import re
from typing import Any, Iterator

from . import notion_api as N

_ID = r"[0-9a-fA-F-]{32,36}"
_ALLOWED: tuple[tuple[str, re.Pattern], ...] = (
    ("GET", re.compile(rf"^/v1/pages/{_ID}$")),
    ("GET", re.compile(rf"^/v1/databases/{_ID}$")),
    ("GET", re.compile(rf"^/v1/blocks/{_ID}/children(\?[\w=&-]*)?$")),
    ("POST", re.compile(rf"^/v1/databases/{_ID}/query$")),
    ("POST", re.compile(r"^/v1/search$")),
)


class ReadOnlyViolation(RuntimeError):
    pass


class ReadOnlyNotion:
    def __init__(self, token: str):
        self._inner = N.Notion(token)

    def _read(self, method: str, path: str, body: dict | None = None) -> dict[str, Any]:
        if not any(method == m and rx.match(path) for m, rx in _ALLOWED):
            raise ReadOnlyViolation(f"refusing {method} {path}: not a read-only endpoint")
        return self._inner._request(method, path, body)

    def get_page(self, page_id: str) -> dict[str, Any]:
        return self._read("GET", f"/v1/pages/{page_id}")

    def get_database(self, db_id: str) -> dict[str, Any]:
        return self._read("GET", f"/v1/databases/{db_id}")

    def iter_block_children(self, block_id: str) -> Iterator[dict[str, Any]]:
        cursor: str | None = None
        while True:
            path = f"/v1/blocks/{block_id}/children?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
            result = self._read("GET", path)
            yield from result.get("results", [])
            if not result.get("has_more"):
                return
            cursor = result.get("next_cursor")

    def iter_database(self, db_id: str, page_size: int = 100) -> Iterator[dict[str, Any]]:
        cursor: str | None = None
        while True:
            body: dict[str, Any] = {"page_size": page_size}
            if cursor:
                body["start_cursor"] = cursor
            result = self._read("POST", f"/v1/databases/{db_id}/query", body)
            yield from result.get("results", [])
            if not result.get("has_more"):
                return
            cursor = result.get("next_cursor")
