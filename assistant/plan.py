"""Phase 6: daily planning pass.

Reads live Notion state (Horizon=Active, not done) and asks Gemini to propose a
realistic ordered plan for today. Separately rotates a small daily sample of
Horizon=Someday items into view so they resurface without nagging. Read-only
against Notion — writes nothing back, ever.
"""

from __future__ import annotations

import logging
from datetime import date as date_cls

from . import notion_api as N
from .config import PROFILE_PATH, SECRET_NOTION_TOKEN, load_config, owner_name
from .db import connect, init_db, now_iso
from .llm import LLMError, complete_json
from .projects import ProjectDirectory
from .secrets_store import get_secret

log = logging.getLogger(__name__)

_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "plan": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "reason"],
            },
        },
        "note": {"type": "string", "nullable": True},
    },
    "required": ["plan"],
}

_PLAN_SYSTEM = """You help {owner} plan the work day. Use this profile as ground truth:

{profile}

Below is every currently-active task (Horizon=Active, not done). Propose a
realistic plan for today: pick up to {max_items} tasks that most deserve doing
today, in the order to tackle them, with a short concrete reason each. Use
judgment, not just due-date sorting — overdue items and real deadlines first,
but also group by project where that saves switching, and don't be afraid to
surface a quick win. Reference each task by its exact id (e.g. "T4"). If fewer
than {max_items} genuinely deserve a slot today, return fewer — don't pad the
list. Use `note` for one short line about what's left out, only if worth saying.
Today is {today}."""

_PRIORITY_ORDER = {"High": 0, "Medium": 1, "Low": 2, None: 3}


def _notion() -> N.Notion:
    return N.Notion(get_secret(SECRET_NOTION_TOKEN))


def _fetch_open_tasks(notion, tasks_db_id: str, horizon: str, directory: ProjectDirectory) -> list[dict]:
    flt = {"and": [
        {"property": "Horizon", "select": {"equals": horizon}},
        {"property": "Status", "status": {"does_not_equal": "Done"}},
        {"property": "Status", "status": {"does_not_equal": "Archived"}},
    ]}
    out = []
    for page in notion.iter_database(tasks_db_id, flt=flt):
        p = page["properties"]
        rel = p.get("Project", {}).get("relation") or []
        project = directory.name_for_id(rel[0]["id"]) if rel else None
        out.append({
            "id": page["id"],
            "title": N.plain(p.get("Task name")) or "(untitled)",
            "due": N.plain(p.get("Due")),
            "priority": N.plain(p.get("Priority")),
            "status": N.plain(p.get("Status")),
            "project": project,
            "notes": N.plain(p.get("Notes")) or "",
            "url": page.get("url"),
        })
    return out


def _fallback_plan(tasks: list[dict], max_items: int) -> list[dict]:
    """Deterministic sort used only if the AI call fails — keeps `plan` useful."""
    ranked = sorted(tasks, key=lambda t: (t["due"] or "9999-99-99",
                                          _PRIORITY_ORDER.get(t["priority"], 3)))
    for t in ranked:
        t["reason"] = "(fallback sort — AI planning was unavailable)"
    return ranked[:max_items]


def _ai_plan(active: list[dict], max_items: int, model: str) -> tuple[list[dict], str | None]:
    by_tid = {f"T{i}": t for i, t in enumerate(active, 1)}
    lines = [
        f"{tid}: [{t['project'] or '-'}] {t['title']} — due {t['due'] or 'none'} — "
        f"priority {t['priority'] or 'not set'}"
        for tid, t in by_tid.items()
    ]
    profile = PROFILE_PATH.read_text(encoding="utf-8")
    system = _PLAN_SYSTEM.format(owner=owner_name(), profile=profile,
                                 max_items=max_items, today=date_cls.today().isoformat())
    data = complete_json(model, system, "\n".join(lines), schema=_PLAN_SCHEMA,
                         max_tokens=2500, thinking_budget=0)
    plan = []
    for item in data.get("plan", [])[:max_items]:
        t = by_tid.get(item.get("id"))
        if t:
            plan.append({**t, "reason": item.get("reason", "")})
    return plan, (data.get("note") or None)


def _rotate_someday(conn, someday: list[dict], sample_size: int) -> list[dict]:
    if not someday:
        return []
    today_prefix = date_cls.today().isoformat()
    ids = [t["id"] for t in someday]
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT notion_page_id, last_shown_at FROM someday_rotation "
        f"WHERE notion_page_id IN ({placeholders})", ids,
    ).fetchall()
    shown = {r["notion_page_id"]: r["last_shown_at"] for r in rows}

    already_today = [t for t in someday if (shown.get(t["id"]) or "").startswith(today_prefix)]
    if already_today:
        return already_today[:sample_size]

    someday_sorted = sorted(someday, key=lambda t: shown.get(t["id"]) or "")
    picked = someday_sorted[:sample_size]
    now = now_iso()
    for t in picked:
        conn.execute(
            "INSERT INTO someday_rotation (notion_page_id, last_shown_at) VALUES (?, ?) "
            "ON CONFLICT(notion_page_id) DO UPDATE SET last_shown_at = excluded.last_shown_at",
            (t["id"], now),
        )
    conn.commit()
    return picked


def build(*, max_items: int | None = None, someday_sample: int | None = None) -> dict:
    cfg = load_config()
    pcfg = cfg.get("plan", {})
    max_items = max_items if max_items is not None else int(pcfg.get("max_items", 8))
    someday_sample = someday_sample if someday_sample is not None else int(pcfg.get("someday_sample", 3))

    notion = _notion()
    tasks_db_id = cfg["notion"]["tasks_db_id"]
    directory = ProjectDirectory(notion, cfg["notion"]["projects_db_id"])

    active = _fetch_open_tasks(notion, tasks_db_id, "Active", directory)
    someday = _fetch_open_tasks(notion, tasks_db_id, "Someday", directory)

    plan, note = [], None
    if active:
        try:
            plan, note = _ai_plan(active, max_items, cfg["ai"]["reasoning_model"])
        except LLMError as exc:
            log.error("planning failed, using fallback sort: %s", exc)
            plan = _fallback_plan(active, max_items)

    with connect() as conn:
        init_db(conn)
        someday_picked = _rotate_someday(conn, someday, someday_sample)

    left_out = max(0, len(active) - len(plan))
    log.info("plan: %d/%d active task(s) scheduled, %d someday resurfaced",
             len(plan), len(active), len(someday_picked))
    return {
        "plan": plan,
        "note": note,
        "active_count": len(active),
        "left_out": left_out,
        "someday": someday_picked,
        "someday_count": len(someday),
    }
