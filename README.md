# Work Automation Assistant

A single-user assistant that watches an email inbox and a Notion task list,
filters out the noise, and makes it fast to capture, review, and plan work.
It runs locally on a Windows PC - no server, no hosting - and talks only to
your mailbox (read-only), the Notion API, and one LLM API.

> Personal project, shared as-is. It is built around one person's workflow, so
> expect to adapt it. See [OVERVIEW.md](OVERVIEW.md) for a plain-language
> description and [BLUEPRINT.md](BLUEPRINT.md) for the design rationale and the
> lessons learned building it.

## What it does

- **Email triage.** Pulls new mail over IMAP (read-only), applies cheap
  deterministic rules first, then asks an LLM to judge importance against a
  written profile of what matters to you. Newsletters and notifications never
  reach you.
- **Morning brief.** One HTML page: a proposed plan for the day, important
  mail with a one-line reason, possible tasks, and a few older "someday" items
  resurfaced on a rotation.
- **Task review queue.** Tasks the AI infers from email wait for a one-keystroke
  accept / edit / reject before anything is created in Notion.
- **New-drawing lists.** Automated "new sheets / new drawings" notices
  (Autodesk Forma, Procore) are not turned into one task each. They are parsed
  into one reviewable task per project, with the sheets as a checklist grouped by
  drawing set; later notices are appended while that task is open.
- **RFI tracking.** Internal RFI emails are registered as "your RFIs"; when the
  contractor's Procore notice for one arrives it is matched (by subject matter,
  not number) and the assistant raises a desktop notification plus one Notion task
  when it is answered, reopened or closed. Other people's RFIs get a short
  impact summary in the brief, and the brief lists your RFIs still waiting.
- **Submittal alerts.** When a Procore submittal is distributed, the AI decides
  whether it is one of your own kinds of equipment (`[submittals] our_types`) or
  matches your plain-text watch list (`submittal_watch.txt`). Those raise a
  desktop notification and one Notion task whatever the response; everything
  else gets a one-line summary in the brief. The watch list is also checked
  against ordinary email mentioning a "submittal".
- **Quick capture.** `Add Task` and `Add Note` launchers (Start Menu) to jot a
  task or a phone-call note in seconds. Notes are filed in the body of the
  relevant Notion task.
- **Optional mirror.** One-way sync of a secondary Notion database (a status
  queue maintained by some other tool) into the task list, with a
  "hand-off" state that tracks an item without nagging you about it.

## Requirements

- Windows 10/11 (the scheduling and launchers are Windows-specific; the Python
  core is portable)
- Python 3.12+
- A Notion workspace and an **internal integration** token, with your Tasks and
  Projects databases shared with it
- An IMAP mailbox
- A Google Gemini API key. Use a paid tier if your mail is sensitive: free tiers
  of some providers may use submitted content for training.

### Expected Notion schema

**Tasks** database:

| Property | Type | Notes |
|---|---|---|
| Task name | title | |
| Status | status | needs `Not started`, `In progress`, `Done`, `Archived` |
| Due | date | |
| Priority | select | `Low` / `Medium` / `High` |
| Horizon | select | `Active` / `Someday` / `Parked` |
| Project | relation | to your Projects database |
| Notes | rich text | holds only a short "see body" flag |
| Origin | rich text | where an assistant-created task came from |
| Source | URL | link back to a mirrored source row |
| Added by assistant | checkbox | |
| Date Completed | date | |

**Projects** database: a title property is all that is required.

## Setup

```
git clone https://github.com/Marc-Laferriere/Assistant.git
cd Assistant
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

copy config.example.toml config.toml        :: then edit it
copy profile.example.md profile.md          :: then rewrite it for yourself

.venv\Scripts\python -m assistant set-secret notion_token
.venv\Scripts\python -m assistant set-secret imap_password
.venv\Scripts\python -m assistant set-secret gemini_api_key
.venv\Scripts\python -m assistant init-db
.venv\Scripts\python -m assistant check
```

Secrets are stored in the OS credential store (Windows Credential Manager) under
the service name set in `config.toml` - never in a file.

## Commands

```
python -m assistant check                # verify Notion + IMAP access
python -m assistant ingest               # read-only IMAP sync of configured folders
python -m assistant classify             # rules + LLM triage of ingested mail
python -m assistant brief --open         # render and open the morning brief
python -m assistant review               # accept / edit / reject proposed tasks
python -m assistant plan                 # print today's plan
python -m assistant capture --add        # quick-add tasks (sent immediately)
python -m assistant note                 # capture call notes, then file them
python -m assistant mirror --dry-run     # preview the optional mirror
python -m assistant run                  # the full cycle (what the schedule runs)
python -m assistant run --open-brief     # full cycle, then open the brief
```

Task shorthand for `capture`: a trailing `!` is High priority, `@tomorrow` /
`@friday` / `@2026-09-15` sets a due date, and `#project name` (last) files it
under a project.

## Scheduling (Windows Task Scheduler)

Run two jobs, both **hidden** so no console window steals focus. Call
`pythonw.exe` directly - routing through a `.cmd` wrapper still flashes a window.

```powershell
$py = "<repo>\.venv\Scripts\pythonw.exe"; $wd = "<repo>"
$settings = New-ScheduledTaskSettingsSet -Hidden -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

# Hourly sync
Register-ScheduledTask -TaskName "Assistant - Hourly Sync" -Settings $settings -User $env:USERNAME -RunLevel Limited `
  -Action  (New-ScheduledTaskAction -Execute $py -Argument '-m assistant run' -WorkingDirectory $wd) `
  -Trigger (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Days 3650))

# Morning brief, opened in the browser
Register-ScheduledTask -TaskName "Assistant - Morning Brief" -Settings $settings -User $env:USERNAME -RunLevel Limited `
  -Action  (New-ScheduledTaskAction -Execute $py -Argument '-m assistant run --open-brief' -WorkingDirectory $wd) `
  -Trigger (New-ScheduledTaskTrigger -Daily -At '06:45')
```

## Start Menu launchers

The `scripts\*.cmd` files wrap the interactive commands. To make one reachable
by pressing the Windows key and typing its name, add a shortcut to it in the
Start Menu programs folder:

```powershell
$lnk = Join-Path ([Environment]::GetFolderPath('Programs')) 'Add Task.lnk'
$sc = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$sc.TargetPath = "<repo>\scripts\capture.cmd"; $sc.WorkingDirectory = "<repo>"; $sc.Save()
```

Do the same for `note.cmd` ("Add Note") and `review.cmd` ("Review Tasks").

## Safety model

- Email is opened **read-only** at the protocol level; the code never moves,
  flags, sends, or deletes anything.
- Nothing is ever deleted from Notion - only created, updated, or completed.
- Anything the AI infers goes through the review queue before it is written.
- No outbound message of any kind is sent automatically.
- Secrets never touch a config file, a log, or version control.

## What is deliberately not in this repository

`config.toml`, `profile.md`, and everything the assistant generates at runtime
(`data/`, `logs/`, `briefs/`, `capture*.txt`) are git-ignored: they contain
identifying details, mail content, and Notion IDs. Start from the `*.example.*`
templates.

## Known limitations

- Gemini is the only wired-up LLM provider (the client is a small module, so
  adding another is a local change).
- Runs that write shared state (`run`, `ingest`, `classify`, `mirror`) take a
  lock in `data/run.lock`. A plain scheduled `run` that finds another in
  progress skips itself; `run --open-brief` and manual commands wait up to 10
  minutes for their turn. The OS releases the lock if a run crashes.

## License

No license has been chosen yet.
