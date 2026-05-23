# OSS Tracker Agent

A Microsoft Agent Framework (Python) multi-agent system that runs **daily at 1:00 AM PST**
to triage your open OSS pull requests, perform safe auto-actions, and email you a digest
only when something needs your attention.

## What it does

1. Refreshes `..\OSS-STATUS.md` via your existing `refresh-oss-status.ps1`.
2. Snapshots every open PR you author via `gh`.
3. **Analyzer agent** (LLM) classifies each PR: rerun flaky checks, update branch, or escalate to human.
4. **Executor** (deterministic Python) runs auto-actions via `gh run rerun --failed` / `gh pr update-branch`.
5. **Digest drafter agent** (LLM) writes a Markdown email body.
6. If any PR needs human action **or** any auto-action failed, sends the email via Outlook M365 SMTP.

When everything is healthy, **no email is sent**.

## Architecture

```
+-------------------+      +----------------------+      +-------------------+
| refresh_tracker   | -->  | fetch_open_prs (gh)  | -->  | analyzer (LLM)    |
| (PowerShell)      |      |                      |      |                   |
+-------------------+      +----------------------+      +---------+---------+
                                                                   |
                                                                   v
+-------------------+      +----------------------+      +-------------------+
| send_email_smtp   | <--  | digest_drafter (LLM) | <--  | executor (gh)     |
| (Outlook SMTP)    |      |                      |      |                   |
+-------------------+      +----------------------+      +-------------------+
```

Two LLM agents (`oss_pr_analyzer`, `oss_digest_drafter`), three deterministic Python tools
(refresh, gh, smtp). The orchestrator in [oss_tracker_agent/main.py](oss_tracker_agent/main.py)
wires them up linearly.

## Setup

```pwsh
cd C:\Users\luojohn\github\jluocsa\oss-tracker-agent

# Already done by scaffold, but if you ever wipe .venv:
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Configure
Copy-Item .env.template .env
# Edit .env — fill in OPENAI_API_KEY (or Azure block), SMTP_PASSWORD, NOTIFY_EMAIL_TO/FROM
```

### Required env vars

| Var                            | Purpose                                      |
| ------------------------------ | -------------------------------------------- |
| `OPENAI_API_KEY` *or*          | OpenAI direct path                           |
| `AZURE_OPENAI_API_KEY` + `AZURE_OPENAI_ENDPOINT` + `AZURE_OPENAI_CHAT_MODEL` | Azure OpenAI path |
| `OPENAI_CHAT_MODEL`            | Defaults to `gpt-4o-mini`                    |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | Outlook M365 SMTP login (app password)    |
| `NOTIFY_EMAIL_FROM` / `NOTIFY_EMAIL_TO` | Sender / recipient                  |
| `REFRESH_SCRIPT`               | Path to `refresh-oss-status.ps1` (defaulted) |
| `GH_AUTHOR`                    | GitHub username (defaults to `jluocsa`)      |
| `AUTO_RERUN_FAILED_CHECKS`     | `true`/`false` — gate auto-reruns            |
| `AUTO_UPDATE_BRANCH`           | `true`/`false` — gate branch updates         |
| `EMAIL_DRY_RUN`                | `true` to log email instead of sending       |

For Outlook SMTP you need an **app password**, not your Microsoft account password.
Generate one at <https://account.live.com/proofs/AppPassword>.

## Manual test

```pwsh
# One-shot CLI run (this is what Task Scheduler invokes)
.\.venv\Scripts\python.exe -m oss_tracker_agent.main --cli --verbose

# Dry-run email (set EMAIL_DRY_RUN=true in .env first)
```

Or via VS Code: open the **oss-tracker-agent** folder, then F5 → **"OSS Tracker — CLI (one-shot)"**.

## Install the daily 1:00 AM PST schedule

```pwsh
# From an elevated PowerShell (or one with task-create rights):
.\install-task.ps1
```

The trigger uses **Windows local time**. If your system clock is set to Pacific, this is
1:00 AM PST/PDT automatically. To pin to a UTC hour instead:

```pwsh
.\install-task.ps1 -StartTime '09:00' -StartTimeUtc   # 09:00 UTC = 01:00 PST (standard) / 02:00 PDT
```

Verify / manage:

```pwsh
Get-ScheduledTask    -TaskName OSS-Tracker-Agent
Start-ScheduledTask  -TaskName OSS-Tracker-Agent   # run it now
Unregister-ScheduledTask -TaskName OSS-Tracker-Agent -Confirm:$false
```

Logs land in `.\logs\YYYY-MM-DD_HHmm.log`.

## Deploy as a GitHub Action (cloud cron, no PC required)

The local Task Scheduler is great but won't run while your PC is hibernated or
shut down. The included workflow at
[.github/workflows/oss-tracker.yml](.github/workflows/oss-tracker.yml)
runs the same agent on a GitHub-hosted Ubuntu runner, every day at **09:00 UTC**
(~1 AM PST in winter, 2 AM PDT in summer; GitHub Actions cron is UTC-only).

### One-time setup

1. **Push this folder as a GitHub repo.**

   ```pwsh
   cd C:\Users\luojohn\github\jluocsa\oss-tracker-agent
   git init -b main
   git add .
   git commit -m "init: oss-tracker-agent"
   gh repo create oss-tracker-agent --private --source=. --push
   ```

2. **Add repo secrets** (Settings → Secrets and variables → Actions → New secret):

   | Secret               | Value                                                                  |
   | -------------------- | ---------------------------------------------------------------------- |
   | `OPENAI_API_KEY`     | your OpenAI key                                                        |
   | `GH_PAT_OSS_TRACKER` | classic PAT with `repo` + `workflow` scopes (needs to act on external repos like `microsoft/graphrag`, `github/github-mcp-server`, etc.) |
   | `SMTP_USERNAME`      | your `@outlook.com` address                                            |
   | `SMTP_PASSWORD`      | Outlook **app password** (NOT your account password)                   |
   | `NOTIFY_EMAIL_FROM`  | same as `SMTP_USERNAME`                                                |
   | `NOTIFY_EMAIL_TO`    | where to send the digest                                               |

3. **(Optional)** Add repo variables for overrides (Variables tab):
   `GH_AUTHOR`, `IGNORE_REPOS`, `OPENAI_CHAT_MODEL`.

4. **Test manually** from the Actions tab → **OSS Tracker Agent** → **Run workflow**.
   Set `dry_run=true` for the first run so no email is sent.

### Why a fine-grained PAT?

The runner's built-in `GITHUB_TOKEN` is scoped to the workflow's own repo only.
This agent needs to query and act on PRs across **other repositories**
(`gh pr view`, `gh run rerun --failed`, `gh pr update-branch`), so it needs your
personal token. Create at <https://github.com/settings/tokens> → classic →
scopes `repo` + `workflow` → expiry 1 year.

### DST handling

`0 9 * * *` is fine for most folks. If you want strict 1 AM Pacific year-round,
the cleanest option is two crons — one for PST, one for PDT:

```yaml
schedule:
  - cron: '0 9 * * *'   # 1 AM PST (Nov-Mar)
  - cron: '0 8 * * *'   # 1 AM PDT (Mar-Nov)
```

Yes, this means it runs twice during the months that straddle DST — accept the
duplicate emails for those ~2 days a year, or just live with the 1-hour drift.

### GitHub Actions cron quirks to know

- **Delays.** Scheduled workflows can be delayed 5-30 min during peak load. Not a real-time scheduler.
- **Auto-disable on 60-day inactivity.** If you don't push anything to the repo for 60 days, GitHub disables scheduled workflows. Re-enable from Actions tab, or push any commit.
- **Free tier.** Public repos: unlimited. Private repos: 2,000 min/month. A daily run takes ~30-60 sec, so ~30 min/month — well within free tier.

### Migrating from local Task Scheduler

Once the action runs reliably, remove the local schedule to avoid double emails:

```pwsh
Unregister-ScheduledTask -TaskName OSS-Tracker-Agent -Confirm:$false
```

## Foundry Toolkit Agent Inspector (optional)

Run the server-mode entrypoint so you can step through the agents in the Foundry Toolkit
Agent Inspector (formerly AI Toolkit):

```pwsh
.\.venv\Scripts\python.exe -m debugpy --listen 127.0.0.1:5679 -m agentdev run oss_tracker_agent/main.py --verbose --port 8088 -- --server
```

Or **Tasks: Run Task → oss-tracker: serve (Agent Inspector)**, then **Attach (port 5679)**.

## Troubleshooting

- **`No module named oss_tracker_agent`** — run from the project root, or use `python -m oss_tracker_agent.main`.
- **`No model credentials configured`** — fill in `OPENAI_API_KEY` (or the Azure trio) in `.env`.
- **SMTP `(535, b'5.7.3 Authentication unsuccessful')`** — you used your account password instead of an app password.
- **`gh: command not found`** — install GitHub CLI (`winget install --id GitHub.cli`).
- **No PRs returned** — confirm `gh auth status` and that `GH_AUTHOR` matches your login.
- **The scheduler ran but did nothing** — open the latest file in `.\logs\` for the full trace.

## Files

- [oss_tracker_agent/main.py](oss_tracker_agent/main.py) — orchestrator (CLI + server modes)
- [oss_tracker_agent/agents.py](oss_tracker_agent/agents.py) — LLM agent factories
- [oss_tracker_agent/tools.py](oss_tracker_agent/tools.py) — gh CLI / refresh / SMTP
- [oss_tracker_agent/models.py](oss_tracker_agent/models.py) — Pydantic models
- [run-once.ps1](run-once.ps1) — Task Scheduler wrapper (logs to `.\logs\`)
- [install-task.ps1](install-task.ps1) — registers the daily trigger
- [.env.template](.env.template) — copy to `.env` and fill in
