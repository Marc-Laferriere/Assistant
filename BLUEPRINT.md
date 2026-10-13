# Personal work-automation assistant — build blueprint

A reference specification for an AI coding assistant to build a similar
system from scratch for a different user. Distilled from a real, working
implementation; all names, companies, domains, and identifiers below are
generic or invented — none are real.

## 1. Who this is for, and the problem

A solo operator or small-team professional in a project-based trade or
service business (construction subcontractor, consultant, tradesperson,
similar) who:

- gets meaningful work-related information mixed into a high volume of email
  noise (newsletters, automated notifications, marketing) and doesn't
  reliably separate the two,
- maintains a task list in a hosted tool (Notion, Asana, Linear — anything
  with an API) but is inconsistent about adding to it or reviewing it,
- captures most real to-dos on paper or not at all, because opening the task
  tool has too much friction in the moment,
- runs a single always-on Windows PC and wants the whole thing local — no
  cloud hosting, no subscription service, no data leaving the machine except
  to the task API and an LLM API.

The system is single-user, runs unattended on a schedule, and treats **human
review as the default** for anything uncertain — full automation is reserved
for narrow, low-risk, easily-reversed operations.

## 2. Architecture at a glance

```
 [mailbox]  --IMAP, read-only-->  [ingest] --> [local SQLite state]
                                                      |
                                      [rules funnel] -+- [LLM classifier]
                                                      |
                                            [task candidate queue]
                                                      |
                                    [human review, single keystroke]
                                                      |
                                                      v
[external system A]  --API, read-only-->  [one-way mirror]  -->  [task API] <--- [quick-capture tools]
                                                                      ^
                                                                      |
                                                        [daily planning pass, LLM]
                                                                      |
                                                                      v
                                                           [rendered HTML brief]
                                                        (opened on a schedule)
```

Everything is one local process invoked by the OS scheduler; there is no
server, no long-running daemon, no web app. State lives in one SQLite file.
Secrets live in the OS credential store, never in a config file or the
codebase.

## 3. Tech stack and why

| Concern | Choice | Why |
|---|---|---|
| Language/runtime | Python 3.12+, a local venv | ubiquitous, good stdlib coverage for everything needed |
| Local state | SQLite, one file | zero-ops, transactional, trivial to inspect/back up |
| Email | IMAP (`IMAPClient` library), read-only | works with any IMAP provider; no vendor lock to a specific webmail API |
| Task system | REST API of whatever hosted tool the user already uses | avoid inventing a new task tool; meet the user where they are |
| LLM | Hosted API called directly over HTTPS with the stdlib `urllib` (no SDK) | keeps the dependency footprint minimal and the client's behavior fully understood; swapping providers is a small, isolated module |
| Scheduling | OS task scheduler (Windows Task Scheduler; `cron`/`systemd timer` on Linux) | no framework needed for "run this every hour and every morning" |
| Output | A single self-contained HTML file per run, opened in the default browser | no server to run, works offline, trivially emailable/shareable if ever needed |
| Secrets | OS credential manager (Windows Credential Manager via `keyring`; equivalent keychain on macOS/Linux) | never on disk in plaintext, no `.env` file to accidentally commit |

Deliberately avoided: a web framework, a task queue/broker, a vector DB (until
genuinely needed), an LLM orchestration framework, a Docker/container setup.
The whole system is a handful of Python modules and a scheduled CLI entry
point. Add complexity only when a concrete requirement demands it.

## 4. Local data model (SQLite)

Core tables, generalized:

- **`messages`** — one row per ingested email: message-id (dedupe key),
  folder, thread key (normalized subject, for lightweight thread grouping),
  from/to/subject/date, a size-capped body text, bulk-mail flag, attachment
  flag, and an "answered" flag mirrored from the mail protocol.
- **`classifications`** — one row per message: importance tier, category,
  matched project/context, a one-line reason, whether it implies an action,
  a suggested action text, and provenance (which rule or which model/prompt
  version produced it). Kept separate from `messages` so re-classifying is
  cheap and non-destructive.
- **`task_candidates`** — a local review queue: source type (from-email or
  from-a-cleanup-scan), proposed title/due/priority/project, a status
  (`proposed` / `accepted` / `rejected` / `pushed`), and the resulting
  external task ID once accepted. This is the *only* thing standing between
  an AI inference and a write to the task system.
- **`feedback`** — every accept/reject decision with an optional reason.
  Written from day one even before anything reads it back — cheap now,
  valuable once there's enough of it to learn from.
- **Mirror/sync bookkeeping tables** — one row per synced external item,
  mapping it to the created local task, with enough state (a status hash or
  last-known external status) to make re-runs idempotent and safe to re-run
  after state loss (see §6, "self-healing sync").
- **A small event log** — append-only audit trail of every write the system
  makes to the external task system, for tracing "why did this appear."

## 5. Build phases, in a sensible order

Each phase should be independently useful and independently testable before
the next begins.

1. **Project skeleton.** Config loading (a plain file for non-secrets),
   secrets via the OS credential store, SQLite connection/schema helper,
   logging (console + rotating file), a single CLI entry point with
   subcommands. No feature logic yet.
2. **A one-way, low-risk mirror.** Pick the lowest-stakes possible sync — an
   existing automated data source that should also show up in the main task
   list (e.g., a status queue from another internal tool). One-way,
   idempotent, never deletes, closes/completes the mirrored task when the
   source item completes. This exercises the whole plumbing (external API
   auth, local state, scheduled execution) on the safest possible feature.
3. **Quick capture.** A keyboard-launched, near-zero-friction way to create a
   task in seconds — type a line, it's created immediately, not deferred
   until some later "save" step (see pitfall in §6 about deferred sends).
   Optional trailing shorthand for priority/due-date/project.
4. **Read-only ingestion of the primary data source** (email, in this case).
   Incremental sync, deduplicated by a stable ID, capped first-run backfill,
   never mutates the source.
5. **Filtering: deterministic rules first, LLM second.** A rules layer
   (sender/domain allow- and block-lists, bulk-mail detection) resolves the
   obvious cases for free. Only the remainder goes to the LLM, with a
   structured-output schema and a short written "what matters to me" profile
   as context. This ordering matters for both cost and reliability — see §6.
6. **A rendered daily summary.** One HTML file, sections ordered by what the
   user needs first (a proposed plan for the day, then important items, then
   everything else, with the least important collapsed by default).
7. **A human-in-the-loop review queue** for anything AI-inferred. One item
   at a time, single-keystroke accept/edit/reject, never a list the user has
   to remember to open — bundle it into the same launcher pattern as capture.
8. **A daily planning pass.** Read the live state of the task system (not a
   local cache), ask the LLM to propose a realistic ordered subset of active
   work for the day with reasons, with a deterministic fallback (sort by
   due-date/priority) if the LLM call fails so this step never comes up
   empty. Separately, rotate a small sample of "someday/backlog" items into
   view once a day so they resurface without nagging.
9. **Iterate on user feedback.** Expect the filtering rules and the "what
   matters" profile to need several rounds of correction once real data
   flows through. Build the correction loop to be cheap (a config file and a
   markdown profile, both hand-editable, not requiring a code change).

## 6. Cross-cutting lessons (read before building)

**Human review is the trust mechanism, not a formality.** Every AI-inferred
write goes through a queue the user actively approves. The one exception —
full automation — is reserved for mirrors of an already-automated,
already-trusted source, and even then: never delete, only create/update/close.

**Deferred-send capture UX loses data.** An early version of quick-capture
batched everything typed in a session and only sent it to the task API after
an explicit "I'm done" signal (blank line / Ctrl+C). Closing the window right
after typing one item — the natural thing to do — killed the process before
that signal ever fired, silently stranding the item. Fix: send each item the
moment it's entered, not deferred; if the send fails, fall back to a local
file a scheduled job retries, so nothing is ever silently lost either way.

**A "skip" or "later" action must say so, explicitly, every time.** Users
will not infer that skipping in a review flow is safe. Print exactly what
happened and how to get back to it. Also: a "run again" entry point that only
proceeds to review *if something new was captured this session* is a trap —
older skipped/pending items become unreachable through the obvious path.
Always surface pending review items, regardless of whether anything new
happened this run.

**LLM "thinking" tokens can silently eat your output budget.** When using a
model with an internal reasoning/thinking mode for structured-output tasks
(classification, extraction), disable it (or set a low budget) and size
`max_output_tokens` for the answer alone. Otherwise responses truncate
mid-JSON under a token cap that seemed generous for the actual answer,
intermittently, in a way that's easy to misdiagnose as a formatting bug.

**Pin model identifiers loosely, or expect breakage.** Hosted LLM providers
deprecate specific model IDs and redirect new users to newer ones on their
own schedule. Prefer a provider's "current/latest" alias for a
background-scheduled system over a hard-pinned dated model ID, and design the
model-selection config to be a one-line edit.

**Structured writes to a task-API relation/reference field need a resolver,
not string matching.** If the task system's "project" or "category" field is
a relation to another database/table, build a small directory that loads
once, maps free-text (from an LLM's guess, or a user's typed shorthand) to
the canonical ID via exact match, then substring match, then any documented
domain-specific fallback rule — and always return the *canonical* stored
name for display, not the caller's raw text (a subtle bug: returning the
input text back to the user misrepresents what was actually recorded).

**Deduplicate proposed tasks by an extracted reference, not free text.** If
the source data has a natural identifier embedded in it (a ticket number, an
instruction/change-order number, an invoice ID), extract it with a narrow
regex and dedupe on that — a real recurring failure mode is the same
real-world item reaching the system twice (direct notification + a forward,
or a two-part "issued" + "supporting documents released" notification pair).
Do **not** dedupe on the raw subject/thread alone: two genuinely different
items can share an identical generic subject line (a templated notification
subject), and subject-based dedup will silently and incorrectly merge them.
Verify this against real captured data before trusting either approach.

**"Already handled" detection needs more than one signal, and the obvious
one may not fire.** A read-status or "answered" flag from a mail protocol is
not reliably set by every mail client in every workflow. A same-thread check
against the account's own sent-mail folder is more robust — but only if
replies are actually being saved to that folder by whatever client the user
is running (verify this against the account's real configuration; a
secondary/legacy mail account or a client setting that files replies
alongside the original message rather than in a sent folder will silently
defeat this check for that mail source).

**A locally-scheduled background job needs a console-free execution mode,
and that has its own trap.** Running a scheduled job as a normal
console-subsystem process pops a visible window that can steal keyboard
focus at an arbitrary moment — disruptive for a background task. Running the
windowless variant of the interpreter fixes that, but that variant typically
has no attached output streams at all (not merely redirected) — any logging
setup that assumes a writable console stream will raise the first time it
tries to write. Guard for a null output stream explicitly; route everything
through a file logger regardless.

**Log rotation can fail on a locked file, and should degrade quietly.** On
platforms with mandatory file locking (Windows in particular), a scheduled
process and an interactively-run process touching the same log file at the
same size threshold can collide on rotation. Catch and swallow that specific
failure in the rotation path rather than letting the whole run crash on a
logging side-effect; also silence known-chatty third-party library debug
logging (a mail protocol client's raw wire-level logging, an OS credential
library's backend-probing logging) so the file grows slowly enough that this
almost never happens in the first place.

**Prefer additive schema changes with an explicit migration guard.** A
"create table if not exists" pattern doesn't add a column to a table that
already exists with an older shape. Ship a small migration step that checks
for each expected column and adds any that are missing, run automatically at
startup, so the local schema can evolve without a manual reset.

**TLS chain-of-trust issues on a client can be real but not a security
problem.** A certificate authority mid-rotation to a new root can produce a
"the issuer isn't trusted" warning on one client while a standards-compliant
verifier (a systems language's default TLS stack, for instance) accepts the
exact same chain without complaint, because the client's bundled trust store
hasn't been updated yet. Verify independently (check the certificate's
subject matches the expected host, check the issuing chain resolves to a
recognized root through an independent tool) before concluding it's either
safe to bypass or genuinely a problem.

## 7. Safety model (non-negotiable defaults)

- The mailbox connection is opened read-only at the protocol level; the code
  never issues a mutating command against it, ever.
- Nothing is ever deleted from the task system by this code — only created,
  updated, or marked complete.
- No outbound message of any kind (email, chat, notification to a third
  party) is ever sent automatically.
- Every write the system makes is logged with enough context to explain,
  after the fact, exactly why it happened.
- Secrets are never written to a config file, a log, or committed to version
  control; they live only in the OS credential store, entered once at setup.

## 8. Suggested first step for a from-scratch build

Confirm, with the actual user, before writing anything:
1. Which task system and how it's accessed (an API with an installable
   per-workspace token is far simpler than OAuth for a single-user tool).
2. Which mailbox, IMAP host/port, and whether the account's own mail client
   actually files sent mail where a "did the user already reply" check would
   look (ask, don't assume — see §6).
3. Which LLM provider, and confirm the account is on a paid/enterprise tier
   if the content is sensitive (free tiers of some providers use submitted
   content for training).
4. A rough one-paragraph profile of what the user cares about, to seed the
   classifier's context — expect to revise it repeatedly once real data
   starts flowing.

Then follow the phase order in §5, keeping each phase demonstrably working
end-to-end (with a manual dry-run and a small amount of real data) before
starting the next.
