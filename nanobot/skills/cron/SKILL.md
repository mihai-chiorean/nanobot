---
name: cron
description: Schedule reminders, recurring tasks, and durable background Work.
---

# Cron

Use `cron` for simple reminders. Use `schedule_work` for reports, digests,
monitoring, research, analysis, or other jobs whose progress and artifacts
should remain visible in the Work view.

## Four Modes

1. **Reminder** - message is sent directly to user
2. **Task** - message is a task description, agent executes and sends result
3. **One-time** - runs once at a specific time, then auto-deletes
4. **Scheduled Work** - creates a visible plan and a new Work run when it fires

## Examples

Fixed reminder:
```
cron(action="add", message="Time to take a break!", every_seconds=1200)
```

Dynamic task (agent executes each time):
```
cron(action="add", message="Check HKUDS/nanobot GitHub stars and report", every_seconds=600)
```

One-time scheduled task (compute ISO datetime from current time):
```
cron(action="add", message="Remind me about the meeting", at="<ISO datetime>")
```

Timezone-aware cron:
```
cron(action="add", message="Morning standup", cron_expr="0 9 * * 1-5", tz="America/Vancouver")
```

Scheduled Work:
```
schedule_work(
  title="Daily backend health digest",
  goal="Summarize service health and actionable failures.",
  instructions="Check the configured services and publish backend-health.md with Health, Risks, and Next Actions sections.",
  schedule_kind="cron",
  cron_expr="0 8 * * *",
  tz="America/Los_Angeles",
  deliverable="markdown_digest",
  tools_needed=["shell"],
  risk_level="low",
  risk_notes="Tenant-local read-only health report.",
  confirmed=true
)
```

List/remove:
```
cron(action="list")
cron(action="remove", job_id="abc123")
```

## Time Expressions

| User says | Parameters |
|-----------|------------|
| every 20 minutes | every_seconds: 1200 |
| every hour | every_seconds: 3600 |
| every day at 8am | cron_expr: "0 8 * * *" |
| weekdays at 5pm | cron_expr: "0 17 * * 1-5" |
| 9am Vancouver time daily | cron_expr: "0 9 * * *", tz: "America/Vancouver" |
| at a specific time | at: ISO datetime string (compute from current time) |

## Confirmation

Ask the user before creating a schedule when timing is ambiguous, the job is
recurring, reads private data, writes externally, can delete or send data, may
spend money, or otherwise has medium/high risk. If `schedule_work` reports that
confirmation is required, use `ask_user`, then call it again with
`confirmed=true` only after explicit approval.

## Timezone

Use `tz` with `cron_expr` to schedule in a specific IANA timezone. Without `tz`, the server's local timezone is used.
