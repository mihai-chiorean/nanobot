---
name: cron
description: Schedule reminders, recurring tasks, and background Work tasks.
always: true
---

# Cron

Use the `cron` tool to schedule simple reminders.

Prefer the `schedule_work` tool for reports, digests, monitoring, research, analysis, health checks, or any task whose result should persist in the Work/briefcase surface. `schedule_work` creates a visible scheduled Work plan first, then backs it with cron.

Use `ask_user` before scheduling when the user's request is ambiguous, destructive, externally visible, uses private data, spends money, or is likely to create an unintended recurring job.

## Three Modes

1. **Reminder** - message is sent directly to user
2. **Task** - message is a task description, agent executes and sends result
3. **One-time** - runs once at a specific time, then auto-deletes
4. **Scheduled Work** - creates a visible Work plan now, then creates Work run items when it fires

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

Scheduled Work plan:
```
schedule_work(
  title="Daily backend health digest",
  goal="Check Ziggy backend health and summarize relevant upstream Nanobot and llama.cpp changes.",
  instructions="Create a backend health digest for Ziggy. Check nanobot-gateway, anti-sycophancy-proxy, qwen-vllm or llama-minimax, and cloudflared. Summarize anything unhealthy. Check local Ziggy/Nanobot repo status. If web search is available, check upstream Nanobot and llama.cpp recent updates and include only changes that matter for Ziggy. Publish a markdown artifact named backend-health-digest.md with sections: Service Health, Local Repo Changes, Upstream Updates, Risks, Next Actions.",
  schedule_kind="cron",
  cron_expr="0 8 * * *",
  tz="America/Los_Angeles",
  deliverable="markdown_digest",
  tools_needed=["shell", "web_search", "web_fetch"],
  risk_level="low",
  risk_notes="Owner-local read/report job; no external writes.",
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

## Natural Language Scheduling

When the user asks for a recurring background job in natural language:

1. Decide whether it should be a reminder or background Work. Use background Work for health checks, digests, research, reports, monitoring, and analysis.
2. Convert the time expression into `schedule_kind` plus `cron_expr`, `every_seconds`, or `at`.
3. If the user asks for "start once soon and then repeat", create two jobs:
   - one `schedule_work(schedule_kind="at", at=<ISO datetime>, ...)` one-shot plan for the first run
   - one `schedule_work(schedule_kind="cron", cron_expr=..., ...)` recurring plan for future runs
4. Use a clear `title`, `goal`, `deliverable`, and full `instructions`.
5. Include expected artifacts in `instructions`.
6. Ask for clarification if any required timing, timezone, source, or side effect is unclear.

Example: "Start 3 minutes from now and schedule it daily at the same time" means:

- compute the one-shot `at` timestamp from the current runtime time
- compute a daily `cron_expr` matching that same hour/minute
- use the configured timezone if known; otherwise ask which timezone to use

## Confirmation Rules

Ask with `ask_user` before creating jobs when:

- the schedule is ambiguous
- the timezone is missing and the time is user-facing
- the request implies multiple jobs
- the task will run frequently
- the job may write externally, send messages, change files, or publish publicly
- the job reads private user data, such as Gmail, Drive, calendars, or private repos
- the job may spend money or call paid APIs
- the requested data source is unclear

For safe owner-local read/report one-shot jobs, you may schedule directly if the timing and task are clear. Recurring scheduled Work normally requires confirmation; if `schedule_work` returns "Confirmation required", call `ask_user` with the returned summary, then call `schedule_work` again with `confirmed=true`.

## Timezone

Use `tz` with `cron_expr` to schedule in a specific IANA timezone. Without `tz`, the server's local timezone is used.
