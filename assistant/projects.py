"""Projects-database lookup + fuzzy name resolution, for the task review queue.

Optional ``[[projects.split]]`` rules in config.toml handle a project that the
Projects database splits into parts which source text doesn't distinguish
(e.g. "Tower North"/"Tower South"): if the trigger text appears, pick the part
whose keyword is also present, else the rule's default.
"""

from __future__ import annotations

import logging

from . import notion_api as N
from .config import load_config

log = logging.getLogger(__name__)


def _load_splits() -> list[dict]:
    try:
        return list(load_config().get("projects", {}).get("split", []))
    except FileNotFoundError:
        return []


class ProjectDirectory:
    def __init__(self, notion: N.Notion, projects_db_id: str):
        self._notion = notion
        self._db_id = projects_db_id
        self._by_name: dict[str, str] = {}
        self._name_by_id: dict[str, str] = {}
        self._splits = _load_splits()
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        for page in self._notion.iter_database(self._db_id):
            title_prop = next(
                (v for v in page["properties"].values() if v["type"] == "title"), None
            )
            name = (N.plain(title_prop) or "").strip()
            if name:
                self._by_name[name.lower()] = page["id"]
                self._name_by_id[page["id"]] = name
        self._loaded = True
        log.debug("loaded %d project(s)", len(self._by_name))

    def name_for_id(self, page_id: str | None) -> str | None:
        self._load()
        return self._name_by_id.get(page_id) if page_id else None

    def all(self) -> list[tuple[str, str]]:
        """All known (name, id) pairs, sorted by name."""
        self._load()
        return sorted(
            ((name, pid) for pid, name in self._name_by_id.items()),
            key=lambda t: t[0].lower(),
        )

    def resolve(self, text: str | None) -> tuple[str | None, str | None]:
        """Return ``(canonical_display_name, page_id)``, or ``(None, None)``."""
        self._load()
        low = (text or "").strip().lower()
        if not low:
            return None, None
        if low in self._by_name:
            pid = self._by_name[low]
            return self._name_by_id.get(pid), pid
        for name, pid in self._by_name.items():
            if name and (name in low or low in name):
                return self._name_by_id.get(pid), pid
        for rule in self._splits:
            trigger = str(rule.get("trigger", "")).lower()
            if trigger and trigger in low:
                chosen = rule.get("default")
                for keyword, canonical in (rule.get("parts") or {}).items():
                    if keyword.lower() in low:
                        chosen = canonical
                        break
                pid = self._by_name.get(str(chosen or "").lower())
                if pid:
                    return self._name_by_id.get(pid), pid
        return None, None
