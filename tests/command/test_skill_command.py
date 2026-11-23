"""Tests for the /skill built-in command."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.skills import SkillsLoader
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.command.builtin import cmd_skill, register_builtin_commands
from nanobot.command.router import CommandContext, CommandRouter
from nanobot.config.schema import ModelPresetConfig


def _provider(default_model: str = "test-model") -> MagicMock:
    provider = MagicMock()
    provider.get_default_model.return_value = default_model
    provider.generation = MagicMock()
    provider.generation.max_tokens = 4096
    provider.generation.temperature = 0.1
    provider.generation.reasoning_effort = None
    return provider


def _make_loop(tmp_path: Path) -> AgentLoop:
    return AgentLoop(
        bus=MessageBus(),
        provider=_provider(),
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=8000,
        model_presets={
            "default": ModelPresetConfig(
                model="test-model",
                max_tokens=4096,
                context_window_tokens=8000,
            ),
        },
    )


def _ctx(loop: AgentLoop, raw: str = "/skill") -> CommandContext:
    msg = InboundMessage(channel="cli", sender_id="user", chat_id="direct", content=raw)
    return CommandContext(msg=msg, session=None, key=msg.session_key, raw=raw, args="", loop=loop)


def _write_skill(base: Path, name: str, *, description: str = "", body: str = "# Skill\n") -> None:
    skill_dir = base / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    frontmatter = f"---\nname: {name}\n"
    if description:
        frontmatter += f"description: {description}\n"
    frontmatter += "---\n\n"
    (skill_dir / "SKILL.md").write_text(frontmatter + body, encoding="utf-8")


def _loop_with_skills(tmp_path: Path) -> AgentLoop:
    """Create a loop with an empty builtin dir so only workspace skills appear."""
    loop = _make_loop(tmp_path)
    empty_builtin = tmp_path / "empty_builtin"
    empty_builtin.mkdir()
    loop.context.skills = SkillsLoader(tmp_path, builtin_skills_dir=empty_builtin)
    return loop


@pytest.mark.asyncio
async def test_skill_command_no_skills(tmp_path: Path) -> None:
    loop = _loop_with_skills(tmp_path)
    out = await cmd_skill(_ctx(loop))
    assert out.content == "No skills available."


@pytest.mark.asyncio
async def test_skill_command_lists_names_and_descriptions(tmp_path: Path) -> None:
    ws_skills = tmp_path / "skills"
    ws_skills.mkdir()
    _write_skill(ws_skills, "weather", description="Get current weather and forecasts")
    _write_skill(ws_skills, "cron", description="Schedule recurring tasks")

    loop = _loop_with_skills(tmp_path)
    out = await cmd_skill(_ctx(loop))

    assert "Available skills (2):" in out.content
    assert "**weather** — Get current weather and forecasts" in out.content
    assert "**cron** — Schedule recurring tasks" in out.content
    # Must NOT contain file paths
    assert ".md" not in out.content
    assert "/skills/" not in out.content


@pytest.mark.asyncio
async def test_skill_command_excludes_disabled(tmp_path: Path) -> None:
    ws_skills = tmp_path / "skills"
    ws_skills.mkdir()
    _write_skill(ws_skills, "alpha", description="Alpha skill")
    _write_skill(ws_skills, "beta", description="Beta skill")

    loop = _make_loop(tmp_path)
    loop.context.skills.disabled_skills = {"alpha"}

    out = await cmd_skill(_ctx(loop))

    assert "alpha" not in out.content
    assert "**beta** — Beta skill" in out.content


@pytest.mark.asyncio
async def test_skill_command_fallback_description(tmp_path: Path) -> None:
    ws_skills = tmp_path / "skills"
    ws_skills.mkdir()
    _write_skill(ws_skills, "plain", description="", body="# Plain Skill\n")

    loop = _loop_with_skills(tmp_path)
    out = await cmd_skill(_ctx(loop))

    assert "**plain** — plain" in out.content


@pytest.mark.asyncio
async def test_skill_command_no_render_as_text(tmp_path: Path) -> None:
    """Output is markdown; CLI should render it (not forced as plain text)."""
    loop = _make_loop(tmp_path)
    out = await cmd_skill(_ctx(loop))
    assert out.metadata.get("render_as") != "text"


@pytest.mark.asyncio
async def test_skill_command_does_not_list_goal_runtime_protocol(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    out = await cmd_skill(_ctx(loop))

    assert "long-goal" not in out.content


@pytest.mark.asyncio
async def test_skill_command_registered_on_router(tmp_path: Path) -> None:
    router = CommandRouter()
    register_builtin_commands(router)
    loop = _loop_with_skills(tmp_path)

    out = await router.dispatch(_ctx(loop, "/skill"))

    assert out is not None
    assert "No skills available." in out.content


# ---------------------------------------------------------------------------
# draft subcommands (MIT-1856)
# ---------------------------------------------------------------------------


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    # A workspace subdir, so its history (<ws>/../history) is per-test.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return workspace


def _ctx_args(
    loop: AgentLoop, raw: str, args: str, *, is_user_turn: bool = True
) -> CommandContext:
    msg = InboundMessage(channel="cli", sender_id="user", chat_id="direct", content=raw)
    return CommandContext(
        msg=msg, session=None, key=msg.session_key, raw=raw, args=args, loop=loop,
        is_user_turn=is_user_turn,
    )


def _write_draft(ws: Path, name: str, *, valid: bool = True) -> None:
    draft = ws / "skills" / "_proposed" / name
    draft.mkdir(parents=True)
    if valid:
        (draft / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Drafted for you\n---\n\n# {name}\n",
            encoding="utf-8",
        )
    else:
        (draft / "SKILL.md").write_text("# no frontmatter\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_skill_drafts_lists_pending(ws: Path) -> None:
    _write_draft(ws, "demo")
    loop = _make_loop(ws)

    out = await cmd_skill(_ctx_args(loop, "/skill drafts", "drafts"))

    assert "**demo** — Drafted for you" in out.content


@pytest.mark.asyncio
async def test_skill_drafts_empty(ws: Path) -> None:
    loop = _make_loop(ws)

    out = await cmd_skill(_ctx_args(loop, "/skill drafts", "drafts"))

    assert "No skill drafts" in out.content


@pytest.mark.asyncio
async def test_skill_diff_shows_overlay(ws: Path) -> None:
    _write_draft(ws, "demo")
    loop = _make_loop(ws)

    out = await cmd_skill(_ctx_args(loop, "/skill diff demo", "diff demo"))

    assert "+name: demo" in out.content
    assert out.metadata.get("render_as") == "text"


@pytest.mark.asyncio
async def test_skill_diff_unknown_draft(ws: Path) -> None:
    loop = _make_loop(ws)

    out = await cmd_skill(_ctx_args(loop, "/skill diff ghost", "diff ghost"))

    assert "No draft named 'ghost'" in out.content


@pytest.mark.asyncio
async def test_skill_accept_refuses_invalid_frontmatter(ws: Path) -> None:
    _write_draft(ws, "demo", valid=False)
    loop = _make_loop(ws)

    out = await cmd_skill(_ctx_args(loop, "/skill accept demo", "accept demo"))

    assert "Not accepting draft 'demo'" in out.content
    assert "frontmatter" in out.content.lower()
    assert (ws / "skills/_proposed/demo/SKILL.md").exists()
    assert not (ws / "skills/demo").exists()


@pytest.mark.asyncio
async def test_skill_accept_then_reject_flow(ws: Path) -> None:
    _write_draft(ws, "demo")
    loop = _make_loop(ws)

    out = await cmd_skill(_ctx_args(loop, "/skill accept demo", "accept demo"))
    assert "Accepted skill draft 'demo'" in out.content
    assert (ws / "skills/demo/SKILL.md").exists()
    assert not (ws / "skills/_proposed/demo").exists()

    _write_draft(ws, "demo")
    out = await cmd_skill(_ctx_args(loop, "/skill reject demo", "reject demo"))
    assert "Rejected skill draft 'demo'" in out.content
    # The live skill survived the reject.
    assert (ws / "skills/demo/SKILL.md").exists()


@pytest.mark.asyncio
async def test_skill_reject_missing_draft(ws: Path) -> None:
    loop = _make_loop(ws)

    out = await cmd_skill(_ctx_args(loop, "/skill reject ghost", "reject ghost"))

    assert "No draft named 'ghost'" in out.content


@pytest.mark.asyncio
async def test_skill_invalid_name_is_refused(ws: Path) -> None:
    loop = _make_loop(ws)

    out = await cmd_skill(_ctx_args(loop, "/skill accept ../../etc", "accept ../../etc"))

    assert "not a valid skill name" in out.content


@pytest.mark.asyncio
async def test_skill_unknown_subcommand_shows_usage(ws: Path) -> None:
    loop = _make_loop(ws)

    out = await cmd_skill(_ctx_args(loop, "/skill bogus", "bogus"))

    assert "Usage: /skill" in out.content


@pytest.mark.asyncio
async def test_skill_subcommands_dispatch_via_router(ws: Path) -> None:
    _write_draft(ws, "demo")
    router = CommandRouter()
    register_builtin_commands(router)
    loop = _make_loop(ws)

    out = await router.dispatch(_ctx(loop, "/skill drafts"))

    assert out is not None
    assert "**demo** — Drafted for you" in out.content


@pytest.mark.asyncio
async def test_skill_accept_and_reject_refused_outside_a_user_turn(ws: Path) -> None:
    """Cron/trigger/subagent turns and mid-turn injected commands are not user
    turns: "/skill accept" routed there must not let the agent accept its own draft."""
    _write_draft(ws, "demo")
    loop = _make_loop(ws)

    for sub in ("accept", "reject"):
        out = await cmd_skill(
            _ctx_args(loop, f"/skill {sub} demo", f"{sub} demo", is_user_turn=False)
        )
        assert "send it again when it finishes" in out.content

    assert (ws / "skills/_proposed/demo/SKILL.md").exists()
    assert not (ws / "skills/demo").exists()


@pytest.mark.asyncio
async def test_skill_accept_is_recorded_in_workspace_history(ws: Path) -> None:
    _write_draft(ws, "demo")
    loop = _make_loop(ws)

    out = await cmd_skill(_ctx_args(loop, "/skill accept demo", "accept demo"))

    assert "Accepted skill draft 'demo'" in out.content
    git = loop.consolidator.store.git
    assert git._git_dir == ws.parent / "history" / "workspace.git"
    assert git.log(max_entries=1)[0].subject() == "skill: accept demo"


@pytest.mark.asyncio
async def test_skill_drafts_lists_invalid_name_without_failing(ws: Path) -> None:
    _write_draft(ws, "demo")
    bad = ws / "skills" / "_proposed" / "Demo"
    bad.mkdir()
    (bad / "SKILL.md").write_text("---\nname: Demo\ndescription: x\n---\n", encoding="utf-8")
    loop = _make_loop(ws)

    out = await cmd_skill(_ctx_args(loop, "/skill drafts", "drafts"))

    assert "**demo** — Drafted for you" in out.content
    assert "`Demo` — invalid name" in out.content

    out = await cmd_skill(_ctx_args(loop, "/skill reject Demo", "reject Demo"))
    assert "Rejected skill draft 'Demo'" in out.content
    assert not bad.exists()


@pytest.mark.asyncio
async def test_scheduled_work_cannot_accept_a_skill_draft(ws: Path) -> None:
    """A scheduled Work run whose instructions are "/skill accept demo" goes
    through the real work runner and loop dispatch, and is refused."""
    from nanobot.cron.types import CronJob, CronPayload, CronSchedule
    from nanobot.cron.work_runner import run_work_task_cron_job

    _write_draft(ws, "demo")
    loop = _make_loop(ws)
    job = CronJob(
        id="job-1",
        name="sneaky",
        enabled=True,
        schedule=CronSchedule(kind="in", at_ms=1),
        payload=CronPayload(
            kind="work_task",
            message="/skill accept demo",
            channel="websocket",
            to="owner-chat",
            session_key="websocket:owner-chat",
            origin_channel="websocket",
            origin_chat_id="owner-chat",
        ),
        created_at_ms=1,
    )

    result = await run_work_task_cron_job(job, agent=loop)

    assert result is not None and "not by scheduled or automated work" in result
    assert (ws / "skills/_proposed/demo/SKILL.md").exists()
    assert not (ws / "skills/demo").exists()


@pytest.mark.asyncio
async def test_custom_process_direct_caller_cannot_accept_or_reject(ws: Path) -> None:
    """An in-process caller (e.g. sender_id="cron", no automation metadata)
    goes through process_direct, which the history-write gate refuses."""
    _write_draft(ws, "demo")
    loop = _make_loop(ws)

    for sub in ("accept", "reject"):
        out = await loop.process_direct(f"/skill {sub} demo", sender_id="cron")
        assert out is not None and "not by scheduled or automated work" in out.content

    assert (ws / "skills/_proposed/demo/SKILL.md").exists()
    assert not (ws / "skills/demo").exists()
