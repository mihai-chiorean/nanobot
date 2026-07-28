---
name: cron
description: Schedule reminder delivery and durable background Work.
---

# Cron

Use `cron` for simple reminders. Use `schedule_work` for reports, digests,
monitoring, research, analysis, or other jobs whose progress and artifacts
should remain visible in the Work view.

## Two Modes

1. **Reminder** - `cron` sends reminder text to the user.
2. **Scheduled Work** - `schedule_work` creates a visible plan and a new Work
   run when it fires.

## Examples

Fixed reminder:
```
cron(action="add", message="Time to take a break!", every_seconds=1200)
```

One-time reminder (compute ISO datetime from current time):
```
cron(action="add", message="Remind me about the meeting", at="<ISO datetime>")
```

Timezone-aware recurring reminder:
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
  success_criteria="Every configured service is represented and failures have actionable evidence.",
  delivery="Publish the digest in the Work tab.",
  tools_needed=["shell"],
  assumptions=[],
  open_questions=[],
  context_confidence=95,
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

For workflow intent, follow the workflow scheduling policy. Interview the user
until material decisions are resolved and context confidence is at least 80.
Ask the user before creating a schedule when the job is recurring, has material
assumptions, reads private data, writes externally, can delete or send data, may
spend money, or otherwise has medium/high risk. If `schedule_work` reports that
confirmation is required, use `ask_user`, then call it again with
`confirmed=true` only after explicit approval.

## Timezone

Use `tz` with `cron_expr` to schedule in a specific IANA timezone. Without `tz`, the server's local timezone is used.
