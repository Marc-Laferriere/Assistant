"""Command-line entry point:  python -m assistant <command>

Commands
--------
  set-secret <name>     store a secret in Windows Credential Manager (prompts, no echo)
  migrate-token         move .notion_token into Credential Manager, then delete the file
  init-db               create the local SQLite schema
  check                 verify token + reachability of Notion + IMAP
  capture [--add]       process capture.txt into Notion tasks (--add: prompt first)
  ingest [--full]       read-only IMAP sync of configured folders (Phase 3)
  messages [--limit N]  list stored messages
  classify [--limit N]  rules + Gemini triage of ingested email (Phase 4)
  brief [--open]        render the morning brief to briefs/latest.html (Phase 4)
  review                approve/reject/edit queued task candidates -> Notion (Phase 5)
  cleanup-scan          propose Project/Priority/Horizon for pre-assistant open tasks
  plan                  print today's plan + someday resurfacing (Phase 6, also in the brief)
  note [--review-only]  capture call notes, then file each against a task's page body
  mirror [--dry-run]    run the Submittal Inbox -> Tasks mirror (Phase 1)
  run [--open-brief]    full cycle: ingest + classify + capture + mirror + brief
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys

from . import __version__
from .config import PROJECT_ROOT, SECRET_NOTION_TOKEN, load_config, service_name
from .db import connect, init_db
from .logging_setup import setup_logging

log = logging.getLogger("assistant")


def _notion():
    from . import notion_api
    from .secrets_store import get_secret

    return notion_api.Notion(get_secret(SECRET_NOTION_TOKEN))


def cmd_set_secret(args: argparse.Namespace) -> int:
    from .secrets_store import set_secret

    value = getpass.getpass(f"value for {args.name!r} (input hidden): ").strip()
    if not value:
        log.error("empty value, nothing stored")
        return 1
    set_secret(args.name, value)
    log.info("stored %r in Credential Manager", args.name)
    return 0


def cmd_migrate_token(_: argparse.Namespace) -> int:
    from .secrets_store import get_secret, set_secret

    token_file = PROJECT_ROOT / ".notion_token"
    if not token_file.exists():
        log.error("no .notion_token file at %s", token_file)
        return 1
    raw = token_file.read_bytes().replace(b"\x00", b"")
    token = raw.decode("utf-8", "ignore").lstrip("﻿").strip()
    if not token.startswith(("ntn_", "secret_")):
        log.error("that does not look like a Notion token (prefix %r)", token[:4])
        return 1
    set_secret(SECRET_NOTION_TOKEN, token)
    # read it back before removing the file
    assert get_secret(SECRET_NOTION_TOKEN) == token
    token_file.unlink()
    log.info("token moved to the credential store under %r; .notion_token deleted", service_name())
    return 0


def cmd_init_db(_: argparse.Namespace) -> int:
    with connect() as conn:
        init_db(conn)
    log.info("SQLite schema ready")
    return 0


def cmd_check(_: argparse.Namespace) -> int:
    cfg = load_config()
    notion = _notion()
    me = notion._request("GET", "/v1/users/me")
    log.info("Notion token OK — integration %r, workspace %r",
             me.get("name"), me.get("bot", {}).get("workspace_name"))
    for label, key in (("Tasks", "tasks_db_id"), ("Submittal Inbox", "submittal_db_id"),
                       ("Projects", "projects_db_id")):
        if key not in cfg["notion"]:
            log.info("  %s db: not configured (skipped)", label)
            continue
        db = notion.get_database(cfg["notion"][key])
        name = "".join(t.get("plain_text", "") for t in db.get("title", []))
        log.info("  %s db reachable: %r (%d properties)", label, name, len(db["properties"]))

    from .secrets_store import SecretNotFound

    try:
        from .imap_ingest import ImapIngest

        with connect() as conn:
            with ImapIngest(cfg, conn) as ing:
                folders = ing.resolve_folders()
        log.info("IMAP login OK — %d folder(s) configured: %s",
                 len(folders), ", ".join(folders) or "(none)")
    except SecretNotFound:
        log.warning("IMAP: no password stored yet (set-secret imap_password)")
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    from .capture import add_interactive, process

    if args.add:
        add_interactive()
    else:
        process(dry_run=args.dry_run)
    return 0


def _run_mirror(dry_run: bool = False) -> None:
    from .submittal_mirror import MirrorConfig, SubmittalMirror

    cfg = load_config()
    # The submittal mirror is an optional module - skip cleanly if unconfigured.
    if not cfg.get("submittal_mirror") or not cfg.get("notion", {}).get("submittal_db_id"):
        log.info("submittal mirror not configured - skipping")
        return
    mcfg = MirrorConfig.from_toml(cfg)
    with connect() as conn:
        init_db(conn)
        SubmittalMirror(_notion(), conn, mcfg, dry_run=dry_run).run()


def cmd_mirror(args: argparse.Namespace) -> int:
    _run_mirror(dry_run=args.dry_run)
    if args.dry_run:
        log.info("dry-run only — nothing was written to Notion")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    from .imap_ingest import ingest, list_server_folders

    if args.list_folders:
        list_server_folders()
        return 0
    ingest(full=args.full, only_folder=args.folder)
    return 0


def cmd_messages(args: argparse.Namespace) -> int:
    with connect() as conn:
        init_db(conn)
        rows = conn.execute(
            "SELECT date_utc, from_addr, subject, folder, is_bulk, has_attach, "
            "length(body_text) AS blen FROM messages "
            "ORDER BY COALESCE(date_utc, first_seen_at) DESC LIMIT ?",
            (args.limit,),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    for r in rows:
        flags = ("B" if r["is_bulk"] else "-") + ("A" if r["has_attach"] else "-")
        print(f"{(r['date_utc'] or '')[:16]:16} {flags} "
              f"{(r['from_addr'] or '')[:30]:30} {(r['subject'] or '')[:58]:58} "
              f"[{r['folder']}] {r['blen'] or 0}b")
    print(f"\n{total} message(s) stored")
    return 0


def cmd_classify(args: argparse.Namespace) -> int:
    from .classify import run

    run(limit=args.limit, reclassify=args.reclassify)
    return 0


def cmd_plan(_: argparse.Namespace) -> int:
    from .plan import build

    data = build()
    print(f"Today's plan ({len(data['plan'])}/{data['active_count']} active tasks):\n")
    for i, t in enumerate(data["plan"], 1):
        print(f"{i}. [{t['project'] or '-'}] {t['title']}"
              f"{'  due ' + t['due'] if t['due'] else ''}"
              f"{'  ' + t['priority'] if t['priority'] else ''}")
        if t.get("reason"):
            print(f"   {t['reason']}")
    if data["note"]:
        print(f"\n{data['note']}")
    if data["left_out"]:
        print(f"({data['left_out']} more active task(s) not scheduled today)")
    if data["someday"]:
        print(f"\nFrom the someday pile ({data['someday_count']} total):")
        for t in data["someday"]:
            print(f"  - [{t['project'] or '-'}] {t['title']}")
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    from .notes import capture_interactive, review_interactive

    if not args.review_only:
        capture_interactive()
    # Always review, even if nothing new was captured this time - anything
    # skipped in a previous session (e.g. "I need to make that project first")
    # still needs a way back in without hunting for a flag.
    review_interactive()
    return 0


def cmd_brief(args: argparse.Namespace) -> int:
    from .brief import build

    path = build(since_hours=args.since_hours, open_after=args.open or None)
    print(path)
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    from .task_review import pending_count, run_interactive

    if args.list:
        print(f"{pending_count()} candidate(s) awaiting review")
        return 0
    run_interactive(populate=not args.no_populate)
    return 0


def cmd_cleanup_scan(args: argparse.Namespace) -> int:
    from .task_review import populate_cleanup

    with connect() as conn:
        init_db(conn)
        n = populate_cleanup(conn, limit=args.limit)
    log.info("cleanup-scan: %d proposal(s) queued — run `assistant review` to go through them", n)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .capture import process
    from .task_review import populate_from_email

    for step in ("ingest", "classify"):
        try:
            if step == "ingest":
                from .imap_ingest import ingest

                ingest()
            else:
                from .classify import run as classify_run

                classify_run()
        except Exception as exc:  # noqa: BLE001 - one step failing must not block the rest
            log.exception("%s failed, continuing: %s", step, exc)

    try:
        with connect() as conn:
            init_db(conn)
            n = populate_from_email(conn)
            if n:
                log.info("queued %d new task candidate(s) for review", n)
    except Exception as exc:  # noqa: BLE001
        log.exception("task-candidate queue failed: %s", exc)

    process()
    _run_mirror()

    try:
        from .brief import build

        build(open_after=args.open_brief or None)
    except Exception as exc:  # noqa: BLE001
        log.exception("brief failed: %s", exc)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="assistant", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging to console")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("set-secret", help="store a secret in Credential Manager")
    sp.add_argument("name")
    sp.set_defaults(func=cmd_set_secret)

    sub.add_parser("migrate-token", help="move .notion_token into Credential Manager").set_defaults(func=cmd_migrate_token)
    sub.add_parser("init-db", help="create local SQLite schema").set_defaults(func=cmd_init_db)
    sub.add_parser("check", help="verify Notion token + database access").set_defaults(func=cmd_check)

    cp = sub.add_parser("capture", help="process capture.txt into Notion tasks")
    cp.add_argument("--add", action="store_true",
                    help="interactive prompt to add lines, then process")
    cp.add_argument("--dry-run", action="store_true", help="parse only, write nothing")
    cp.set_defaults(func=cmd_capture)

    ip = sub.add_parser("ingest", help="read-only IMAP sync of configured folders")
    ip.add_argument("--full", action="store_true", help="ignore saved state, re-scan (capped)")
    ip.add_argument("--folder", help="sync just this one folder")
    ip.add_argument("--list-folders", action="store_true",
                    help="print the server folder list and exit")
    ip.set_defaults(func=cmd_ingest)

    xp = sub.add_parser("messages", help="list stored messages")
    xp.add_argument("--limit", type=int, default=30)
    xp.set_defaults(func=cmd_messages)

    clp = sub.add_parser("classify", help="rules + Gemini triage of ingested email")
    clp.add_argument("--limit", type=int, help="only the N most recent unclassified")
    clp.add_argument("--reclassify", action="store_true", help="redo all, ignore cache")
    clp.set_defaults(func=cmd_classify)

    bp = sub.add_parser("brief", help="render the morning brief HTML")
    bp.add_argument("--since-hours", type=int, help="override the brief window")
    bp.add_argument("--open", action="store_true", help="open the file when built")
    bp.set_defaults(func=cmd_brief)

    rvp = sub.add_parser("review", help="approve/reject/edit task candidates -> Notion")
    rvp.add_argument("--list", action="store_true", help="just print the pending count")
    rvp.add_argument("--no-populate", action="store_true",
                     help="skip pulling in new email-derived candidates first")
    rvp.set_defaults(func=cmd_review)

    csp = sub.add_parser("cleanup-scan",
                         help="propose Project/Priority/Horizon for pre-assistant open tasks")
    csp.add_argument("--limit", type=int, help="only propose for the first N")
    csp.set_defaults(func=cmd_cleanup_scan)

    sub.add_parser("plan", help="print today's plan + someday resurfacing").set_defaults(func=cmd_plan)

    ntp = sub.add_parser("note", help="capture call notes, then file each against a task")
    ntp.add_argument("--review-only", action="store_true",
                     help="skip capture, just review notes already pending")
    ntp.set_defaults(func=cmd_note)

    mp = sub.add_parser("mirror", help="run the Submittal Inbox -> Tasks mirror")
    mp.add_argument("--dry-run", action="store_true", help="log intended changes, write nothing")
    mp.set_defaults(func=cmd_mirror)

    runp = sub.add_parser("run", help="full cycle: ingest + classify + capture + mirror + brief")
    runp.add_argument("--open-brief", action="store_true", help="open the brief when it's built")
    runp.set_defaults(func=cmd_run)

    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # keep em-dashes/arrows readable in cmd.exe
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    setup_logging(verbose=args.verbose)
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 - top-level guard, full detail to logfile
        log.exception("command failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
