"""MIT-1414: prompt surfaces must steer recurring work to the ``schedule_work`` tool.

Production (feat/shared-rooms) shows scheduled digests and reports in the Work
app because the prompts name ``schedule_work`` for background/recurring work.
The 0.3.0 line reverted these texts to upstream phrasing (plain ``cron`` agent
turns, "ask in your reply"), so digests never reached the Work app. These
assertions pin the prod-parity wording and keep it pointing only at registered
tools.
"""

from __future__ import annotations

import re
from pathlib import Path

from nanobot.agent.context import ContextBuilder
from nanobot.agent.skills import BUILTIN_SKILLS_DIR
from nanobot.agent.tools.loader import ToolLoader
from nanobot.utils.helpers import load_bundled_template
from nanobot.utils.prompt_templates import render_template

# The tools the scheduling prompts are allowed to name (all registered on 0.3.0;
# ``ask_user`` arrived with PR #68). ``cron(as_work=true)`` stays forbidden: the
# ``as_work`` parameter was deliberately dropped on this branch.
_STEERED_TOOLS = {"cron", "schedule_work", "ask_user", "briefing"}


def _registered_tool_names() -> set[str]:
    """Tool names the production loader can register (mirrors ToolLoader.load)."""
    names: set[str] = set()
    for tool_cls in ToolLoader().discover():
        descriptor: object = None
        for klass in tool_cls.__mro__:
            if "name" in vars(klass):
                descriptor = vars(klass)["name"]
                break
        if isinstance(descriptor, property) and descriptor.fget is not None:
            try:
                value = descriptor.fget(object.__new__(tool_cls))
            except Exception:
                continue
        elif isinstance(descriptor, str):
            value = descriptor
        else:
            continue
        if isinstance(value, str):
            names.add(value)
    return names


def _cron_skill_text() -> str:
    """The built-in cron skill exactly as the agent sees it."""
    path = BUILTIN_SKILLS_DIR / "cron" / "SKILL.md"
    assert path.is_file(), f"missing built-in cron skill at {path}"
    return path.read_text(encoding="utf-8")


def _normalized(text: str) -> str:
    return " ".join(text.split())


def test_owner_system_prompt_names_schedule_work(tmp_path: Path) -> None:
    """An owner turn's built system prompt must steer recurring work to schedule_work.

    Today the gateway always has a cron service, so ``_register_default_tools``
    leaves ``workflow_scheduling`` on and every owner turn carries the intake
    policy; the scheduling guidance must therefore reach the model on a plain
    turn too, not only when the tool flags happen to be set.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    builder = ContextBuilder(workspace)

    prompt = builder.build_system_prompt()

    assert "schedule_work" in prompt
    # Negative control: the shared-room contract is a replacement prompt and
    # must not leak owner-only scheduling guidance into a guest-visible room.
    room_prompt = builder.build_system_prompt(shared_room=True, channel="websocket")
    assert "schedule_work" not in room_prompt
    assert "Workflow Scheduling Policy" not in room_prompt


def test_cron_skill_text_has_scheduled_work_mode() -> None:
    """The cron skill must document the Scheduled Work mode and the tool name."""
    skill = _cron_skill_text()
    normalized = _normalized(skill)

    assert "Scheduled Work" in skill
    assert "schedule_work" in skill
    # The interview/confirmation path names the real tool (prod parity).
    assert "ask_user" in normalized
    # A bare "recurring tasks" pitch is the upstream regression: the skill must
    # route reports/digests to schedule_work, not just to cron agent turns.
    assert "schedule_work" in normalized.split("# Cron")[1].split("## Examples")[0]


def test_workflow_intake_uses_ask_user_and_forbids_cron_bypass() -> None:
    """The rendered intake policy must interview via ask_user, not reply text."""
    rendered = render_template("agent/workflow_intake.md", cron_scheduling=True)
    normalized = _normalized(rendered)

    assert "ask_user" in normalized
    assert "ask in your reply" not in normalized
    # Prod parity: the bypass ban survived the 0.3.0 port, and it still forbids
    # the exact removed escape hatch (``as_work`` was dropped from cron).
    assert "Do not use `HEARTBEAT.md` or `cron(as_work=true)` to bypass workflow intake" in normalized
    assert "briefing" in normalized  # briefing routing kept (MIT-1028)

    # Gating stays wired: the cron/schedule_work routing paragraph is only
    # rendered for turns that actually have those tools registered.
    plain = _normalized(render_template("agent/workflow_intake.md", cron_scheduling=False))
    assert "Use `cron` only for simple reminder delivery" not in plain
    assert "ask_user" in plain  # the interview rule itself is unconditional


def test_scheduling_prompts_only_name_registered_tools() -> None:
    """Every tool the scheduling texts name must exist (no hallucinated tools).

    After 5b4bb53c changed the wording, the texts must not reference a tool the
    0.3.0 registry does not actually provide.
    """
    registered = _registered_tool_names()
    assert {"cron", "schedule_work", "ask_user"} <= registered, registered

    texts = {
        "cron/SKILL.md": _cron_skill_text(),
        "templates/AGENTS.md": load_bundled_template("AGENTS.md") or "",
        "workflow_intake.md": render_template("agent/workflow_intake.md", cron_scheduling=True),
    }
    # The examples call tools inline; every call-like token must be a real
    # registered tool (the ``as_work`` ban in the intake names a dropped
    # parameter, and parameter names never appear before a parenthesis).
    call_re = re.compile(r"([a-z][a-z0-9_]{2,})\(")
    for name, text in texts.items():
        for token in call_re.findall(text):
            assert token in _STEERED_TOOLS, (
                f"{name} tells the model to call unknown tool {token}()"
            )
