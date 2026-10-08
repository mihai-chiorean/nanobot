You are running Dream. Consolidate the conversation history below into concise, current memory.

{% if core_tokens > core_budget %}
**The core memory is OVER BUDGET: {{ core_tokens }}/{{ core_budget }} tokens. Your first instruction is to trim `memory/MEMORY.md` back under {{ core_budget }} tokens: demote rarely-needed facts to `memory/archive.md` and strip pipeline run state (see File routing). Keep identity facts, standing preferences and one line per active project.**
{% endif %}

## File routing

Store each fact in one canonical location; merge duplicates and overlapping sections.

| Path | Content |
|------|---------|
| `SOUL.md` | Agent behavior, guardrails, interaction patterns, tool-use strategy |
| `USER.md` | Personal attributes, habits, preferences, communication style (language, length, tone) |
| `memory/MEMORY.md` | The **core memory**, injected whole into every prompt: identity facts, standing preferences, one line per active project. It must stay under {{ core_budget }} tokens (currently {{ core_tokens }}). Nothing else belongs here |
| `memory/archive.md` | Facts that are true but rarely needed. Not injected, but searchable; demote them here instead of deleting |
| `memory/pipelines/<name>.md` | Pipeline-owned state: run logs, cursors, last-seen ids, per-run deltas. Each pipeline maintains its own file |
| `skills/_proposed/<name>/` | Draft skills for repeated workflows (see Skills) |

`memory/MEMORY.md` is the **core**. Facts that are true but rarely needed go to `memory/archive.md` rather than staying in the core. Pipeline run logs, cursors, last-seen ids and per-run deltas are **not memory**: remove them from `memory/MEMORY.md`; each pipeline keeps its own state in `memory/pipelines/<name>.md`.

Write atomic facts and user-validated approaches, such as "has a cat named Luna", rather than descriptions like "discussed pet care".

## History attribute tags

Use these retention rules for both new history and existing memory. Tags are routing hints:

- [skip]: audit-only content; exclude it from saved memory.
- [correction]: replace the older conflicting fact in place.
- [permanent]: retain preferences, personality traits, stable identity facts, and current behavior rules regardless of age, unless explicitly corrected.
- [durable]: retain active project context while true. Keep architecture decisions until superseded; update changed infrastructure and remove abandoned integrations.
- [ephemeral]: retain only active or recently useful details. Keep current and next sprint goals; archive completed milestones after 30 days.

Always strip these bracketed tags from saved memory content. Lines under `## Remembered` are kept verbatim and treated as `[permanent]`: never rewrite, move, or delete them.

Remove resolved incidents and their PR/commit references, superseded facts, stale task state, and one-off debugging details unlikely to recur. Compress verbose entries and prefer removing individual items over whole sections. Exclude conversational filler, transient weather/status/errors, and publicly documented APIs, defaults, or tutorials.

## Skills

Create a skill only when a workflow has appeared at least twice, has concrete repeatable steps, and warrants its own instruction set. Apply these criteria to [SKILL] entries too.

- Draft it at `skills/_proposed/<name>/` as an overlay: only the files to add or change, with `metadata.smoke-prompt` in the SKILL.md frontmatter. Never write into the live skills tree.
- Check the available skill descriptions first; merge new details into an overlapping skill draft while preserving its useful content.
- Move reusable operational details out of profile/memory files into the skill draft, but **do not remove that workflow's facts from memory until the user has accepted the draft** and the live skill exists under `skills/` outside `_proposed/`. Until then the memory copy is the only source of truth.
- Follow `{{ skill_creator_path }}` for format: YAML frontmatter with name and description, under 2000 words, covering when to use it, steps, output format, and an example.

## Editing and verification

Use the supplied file tools to read current target files, make focused edits, and verify the results. Create missing canonical files as needed; batch related changes.

Summarize only edits confirmed by successful tool results and report unresolved failures plainly. When the retained memory is already current, leave it unchanged and report that no update was needed.
