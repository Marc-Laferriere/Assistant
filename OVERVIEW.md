# Automation Assistant — how it works

A personal assistant that watches incoming email and a Notion task list,
filters out the noise, and makes it fast to capture and plan work. It runs
quietly on its owner's own PC — nothing is hosted anywhere else.

## The daily rhythm

- **Every hour**, in the background: checks for new email, updates the task
  list from a couple of automated sources, and refreshes a summary page.
- **Every morning**, that summary — the "morning brief" — opens automatically
  in the browser, ready before the day starts.

## What the brief shows

One page, four sections:

1. **Today's plan** — a short, ordered list of what's worth doing today,
   picked from the active task list (not everything — just what actually
   deserves a slot).
2. **Important email** — anything that needs a look, with a one-line reason.
3. **Possible tasks** — things email implied should get done, waiting for a
   quick yes/no.
4. **A few "someday" items** — older, non-urgent tasks resurfaced a few at a
   time so they don't get forgotten, without nagging.

Everything else — newsletters, notifications, routine automated mail — is
filtered out automatically so it never has to be looked at.

## How the filtering works

A set of simple rules catches the obvious noise for free. Anything left over
goes to an AI model, which judges importance against a short written profile
of what actually matters — the owner's role, active jobs, key people, and
what counts as noise. That profile is editable at any time to correct
mistakes.

## Capturing things quickly

Two tools, launched instantly from the keyboard (Windows key, type the name):

- **Add Task** — jot a task in a few words; it's created in Notion
  immediately.
- **Add Note** — for phone calls: jot notes freely, then a moment later pick
  which task each note belongs to. The note itself ends up on that task in
  Notion, so it's there when it's time to do the work.

## Nothing happens without a look first

Anything the assistant *infers* — a task implied by an email, for example —
goes into a queue rather than straight into Notion. A third keyboard-launched
tool, **Review Tasks**, is how that queue gets cleared: it shows one proposed
task at a time — what it's for, why it was suggested — and accepts, edits, or
rejects it with a single keypress. Nothing reaches Notion from that queue
until it's been looked at this way.

Only one thing is fully automatic end-to-end: mirroring entries from an
existing, separate submittal-tracking system into the task list, since that's
low-risk and easily undone.

## What it will never do

- Never sends, replies to, or deletes email — read-only, always.
- Never deletes anything in Notion.
- Every automatic decision is logged, so anything it gets wrong can be traced
  and corrected.
