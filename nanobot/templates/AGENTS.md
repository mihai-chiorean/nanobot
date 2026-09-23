# Agent Instructions

## Workspace Guidance

Use this file for project-specific preferences, recurring workflow conventions, and instructions you want the agent to remember for this workspace. Keep durable facts about the user in `USER.md`, personality/style guidance in `SOUL.md`, and long-term memory in `memory/MEMORY.md`.

## Scheduling

Use `cron` for a simple reminder that only needs to deliver reminder text.

**Do NOT just write reminders to MEMORY.md** — that won't trigger actual notifications.

Use `schedule_work` for recurring or background tasks. Follow the workflow
intake policy, ask for missing decisions, and confirm the assembled plan before
creating the schedule. Do not put new user workflows in `HEARTBEAT.md`.
