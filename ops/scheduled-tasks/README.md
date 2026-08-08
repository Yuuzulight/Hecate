# Scheduled task prompts

Copies of the prompts driving the two scheduled checks against the running cluster. They live here so they are versioned and reviewable; the copies that actually execute sit in Claude's local config, which is not backed up by anything.

| file | when | what it does |
|---|---|---|
| `hecate-daily-health.md` | daily, 13:30 | Did the day's run happen? Report gaps. |
| `hecate-growth-check.md` | once, 15 Aug 2026 | Whether growth and momentum mean anything yet, plus a dashboard review. |

The daily one reads `%LOCALAPPDATA%\Hecate\run-log.jsonl` rather than the cluster. Docker is shut down most of the time — `../windowed-run.ps1` starts it, runs the day's jobs and stops it again — so a check that queried Kubernetes directly would report a false gap every day, having found nothing running.

## These are copies, not the source

Editing a file here changes nothing. The live prompts are at:

```
~/.claude/scheduled-tasks/<task-id>/SKILL.md
```

Change the live one, then copy it back here. Keeping them in sync is manual, which is a real weakness — if the two ever disagree, the one in `~/.claude` is what runs.

## Why they read the way they do

Each run starts with no memory of the conversation that created it, so the prompts repeat context that would otherwise be obvious: where the repo is, which namespace, what a snapshot is. That is not padding.

They also explain *why* a gap matters rather than just how to detect one. Snapshots describe a day that has passed and cannot be backfilled, so a missed night is permanent — the daily check exists to catch that the next morning, while the rest of the month is still intact. A check that reported "1 day of history" without saying why that is bad would be worse than no check.

Both are observation-only and say so explicitly. Neither should change code or push anything.

## The daily one is meant to be quiet

Three or four lines when everything is fine. If it starts producing reports about a healthy system, the prompt needs tightening rather than the system needing attention.
