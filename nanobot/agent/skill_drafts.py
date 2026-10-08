"""Skill drafts: check, diff, accept and reject the overlays in ``skills/_proposed/``.

Ziggy-local (fork, MIT-1856, design §3).  After SM-04 an agent or Dream write
to ``skills/<name>/`` is refused and the changed files are written instead to
``skills/_proposed/<name>/`` as an **overlay**: it holds only the files to add
or change.  The **merged view** of a draft is the live ``skills/<name>/``
(possibly absent for a new skill) with the draft's files laid over it.

Nothing goes live until the owner sends ``/skill accept <name>`` (a user
message, so the model cannot accept its own draft).  Accept copies each draft
file over the live skill -- creating directories, atomic writes, and it
**never deletes live files** -- then deletes the draft dir and records the
change through the SM-02 workspace history.

An ``on_commit`` callback (:func:`make_offer_callback`, attached by
:func:`wire`) watches commits that touch ``skills/_proposed/<name>/`` and
publishes one offer message per draft: to the turn's own session for a turn
commit, and for a Dream commit to the owner's most recent non-room session --
never a shared room, whose guests can be testers and drafts are built from
the owner's private workflows.

Integration note (SM-02, MIT-1845): ``wire`` expects SM-02's
``WorkspaceHistory`` -- an object with an ``on_commit`` list of
``(sha, changed_paths)`` callables and
``commit_if_changed(subject, session_key=None, channel=None) -> sha | None``.
Turn commits carry ``session: <key>`` in the message body; Dream commits carry
no session line.  Until SM-02 lands, the seams stay unset and every entry
point degrades safely (checks/commands work, no commits, no offers).
"""

from __future__ import annotations

import difflib
import importlib.util
import os
import re
import shutil
import tempfile
from contextlib import suppress
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from loguru import logger

from nanobot.agent.skills import parse_skill_metadata, valid_skill_metadata
from nanobot.bus.events import INBOUND_META_ROOM_SCOPE, OutboundMessage
from nanobot.session.keys import UNIFIED_SESSION_KEY, last_channel_from_metadata

# Canonical definition lives in nanobot/agent/tools/filesystem.py (SM-04);
# mirrored here so this module does not import the tool stack.  Skill names
# may not contain "_" (skills.py:_SKILL_NAME), so no real skill can collide.
PROPOSED_DIR = "_proposed"

# Same cap the workspace-history working-tree diff uses (gitstore.py).
DIFF_MAX_CHARS = 6000

_SKILL_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")

_VALIDATORS: ModuleType | None = None
_VALIDATORS_RESOLVED = False

# The SM-02 WorkspaceHistory instance registered via wire(); accept/reject
# commit through it.  None until SM-02 lands and wiring calls wire().
_HISTORY: Any = None


class SkillDraftError(ValueError):
    """Raised for an unusable draft name (bad characters, path traversal)."""


def _validate_name(name: str) -> str:
    if not _SKILL_NAME_RE.fullmatch(name):
        raise SkillDraftError(
            f"'{name}' is not a valid skill name (lowercase letters, digits and single hyphens)"
        )
    return name


def _quick_validate() -> ModuleType | None:
    """Import skill-creator's quick_validate.py (a script, not a package module)."""
    global _VALIDATORS, _VALIDATORS_RESOLVED
    if _VALIDATORS_RESOLVED:
        return _VALIDATORS
    _VALIDATORS_RESOLVED = True
    from nanobot.agent.skills import BUILTIN_SKILLS_DIR

    script = BUILTIN_SKILLS_DIR / "skill-creator" / "scripts" / "quick_validate.py"
    try:
        spec = importlib.util.spec_from_file_location("nanobot_skill_quick_validate", script)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {script}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _VALIDATORS = module
    except Exception:
        logger.exception("skill_drafts: could not load quick_validate.py from {}", script)
    return _VALIDATORS


# ---------------------------------------------------------------------------
# paths and the merged view
# ---------------------------------------------------------------------------


def skills_root(ws: Path | str) -> Path:
    return Path(ws) / "skills"


def live_skill_dir(ws: Path | str, name: str) -> Path:
    return skills_root(ws) / _validate_name(name)


def draft_dir(ws: Path | str, name: str) -> Path:
    return skills_root(ws) / PROPOSED_DIR / _validate_name(name)


def _iter_files(base: Path) -> dict[str, bytes]:
    """All files under *base*, keyed by forward-slash relpath."""
    files: dict[str, bytes] = {}
    if not base.is_dir():
        return files
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(base).as_posix()
        try:
            files[rel] = path.read_bytes()
        except OSError:
            logger.warning("skill_drafts: unreadable file {}", path)
    return files


def list_drafts(ws: Path | str) -> list[str]:
    """Draft names under ``skills/_proposed/``, sorted."""
    root = skills_root(ws) / PROPOSED_DIR
    if not root.is_dir():
        return []
    return sorted(entry.name for entry in root.iterdir() if entry.is_dir())


def merged_view(ws: Path | str, name: str) -> dict[str, bytes]:
    """Live ``skills/<name>/`` with the draft's files laid over it."""
    merged = _iter_files(live_skill_dir(ws, name))
    merged.update(_iter_files(draft_dir(ws, name)))
    return merged


def _draft_files(ws: Path | str, name: str) -> dict[str, bytes]:
    return _iter_files(draft_dir(ws, name))


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def check(ws: Path | str, name: str) -> list[str]:
    """Problems with draft *name*'s merged view; empty list means acceptable."""
    try:
        _validate_name(name)
    except SkillDraftError as exc:
        return [str(exc)]
    if not draft_dir(ws, name).is_dir():
        return [f"No draft named '{name}'."]
    merged = merged_view(ws, name)
    if not merged:
        return [f"Draft '{name}' is empty."]

    problems: list[str] = []
    skill_md = merged.get("SKILL.md")
    if skill_md is None:
        problems.append("Merged skill has no SKILL.md")
    else:
        try:
            content = skill_md.decode("utf-8")
        except UnicodeDecodeError:
            content = None
            problems.append("SKILL.md is not valid UTF-8 text")
        if content is not None:
            meta = parse_skill_metadata(content)
            if meta is None:
                problems.append("SKILL.md is missing valid '---' YAML frontmatter")
            elif not valid_skill_metadata(meta, name):
                declared = meta.get("name")
                description = meta.get("description")
                if declared != name:
                    problems.append(
                        f"Frontmatter name {declared!r} must equal the skill name '{name}'"
                    )
                if not isinstance(description, str) or not description.strip():
                    problems.append("Frontmatter description is missing or empty")
                elif len(description.strip()) > 1024:
                    problems.append(
                        f"Frontmatter description is too long ({len(description.strip())} "
                        "characters, maximum 1024)"
                    )
                else:  # name/description pass the identity contract; name rules are stricter there
                    problems.append(
                        f"Skill name '{name}' is not hyphen-case (or is longer than 64 characters)"
                    )

    validator = _quick_validate()
    if validator is not None:
        # quick_validate works on a directory; materialise the merged view in a
        # sibling temp dir so its root-entry rules see live+draft, not either alone.
        try:
            with tempfile.TemporaryDirectory(prefix="skill-draft-check-") as tmp:
                stage = Path(tmp) / name
                for rel, data in merged.items():
                    target = stage / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                ok, message = validator.validate_skill(stage)
                if not ok:
                    problems.append(str(message))
        except OSError as exc:
            problems.append(f"Could not stage the merged view for validation: {exc}")
    else:
        problems.append("skill-creator quick_validate.py could not be loaded")
    return problems


def skill_description(merged: dict[str, bytes]) -> str:
    skill_md = merged.get("SKILL.md")
    if skill_md is None:
        return "(no SKILL.md)"
    try:
        meta = parse_skill_metadata(skill_md.decode("utf-8", errors="replace"))
    except Exception:
        meta = None
    description = (meta or {}).get("description")
    return description.strip() if isinstance(description, str) and description.strip() else "(no description)"


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


def _text_lines(data: bytes) -> list[str] | None:
    try:
        return data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return None


def _file_diff(live: dict[str, bytes], merged: dict[str, bytes], rel: str) -> list[str]:
    old = _text_lines(live[rel]) if rel in live else []
    new = _text_lines(merged[rel]) if rel in merged else []
    if old is None or new is None:
        verb = "added" if rel not in live else ("removed" if rel not in merged else "changed")
        return [f"Binary file {rel} {verb}"]
    return list(
        difflib.unified_diff(
            old, new, fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm=""
        )
    )


def _diff_parts(live: dict[str, bytes], merged: dict[str, bytes]) -> tuple[str, int, int]:
    """(unified diff text, added lines, removed lines) of merged vs live."""
    chunks: list[str] = []
    added = removed = 0
    for rel in sorted(set(live) | set(merged)):
        lines = _file_diff(live, merged, rel)
        for line in lines:
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
        chunks.extend(lines)
    return "\n".join(chunks), added, removed


def diff(ws: Path | str, name: str, *, max_chars: int = DIFF_MAX_CHARS) -> str:
    """Unified diff of the merged view against the live skill, capped at *max_chars*."""
    _validate_name(name)
    if not draft_dir(ws, name).is_dir():
        raise SkillDraftError(f"No draft named '{name}'.")
    text, _, _ = _diff_parts(_iter_files(live_skill_dir(ws, name)), merged_view(ws, name))
    if not text:
        return "(no textual changes)"
    if len(text) > max_chars:
        marker = "\n… (diff truncated)"
        return text[: max(0, max_chars - len(marker))].rstrip() + marker
    return text


# ---------------------------------------------------------------------------
# accept / reject
# ---------------------------------------------------------------------------


def _atomic_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=dst.parent, prefix=".skill-draft-")
    try:
        with os.fdopen(fd, "wb") as handle, open(src, "rb") as source:
            shutil.copyfileobj(source, handle)
        os.replace(tmp_name, dst)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_name)
        raise


def _commit(subject: str) -> None:
    """Record *subject* through SM-02's WorkspaceHistory; no-op before it lands."""
    if _HISTORY is None:
        logger.debug(
            "skill_drafts: no workspace history registered; skipping commit '{}'", subject
        )
        return
    try:
        _HISTORY.commit_if_changed(subject)
    except Exception:
        # SM-02 promises commit_if_changed never raises; belt and braces.
        logger.exception("skill_drafts: history commit failed for '{}'", subject)


def accept(ws: Path | str, name: str) -> list[str]:
    """Apply draft *name* over the live skill. Returns problems; [] means accepted.

    Never deletes live files: only the draft's own files are copied over.
    """
    problems = check(ws, name)
    if problems:
        return problems
    live = live_skill_dir(ws, name)
    draft = draft_dir(ws, name)
    for rel in _draft_files(ws, name):
        rel_path = Path(rel)
        if rel_path.is_absolute() or ".." in rel_path.parts:
            return [f"Draft file '{rel}' escapes the skill directory."]
        _atomic_copy(draft / rel_path, live / rel_path)
    shutil.rmtree(draft)
    parent = draft.parent
    if parent.is_dir() and not any(parent.iterdir()):
        with suppress(OSError):
            parent.rmdir()
    _commit(f"skill: accept {name}")
    return []


def reject(ws: Path | str, name: str) -> bool:
    """Delete draft *name* (live files untouched). True if a draft was removed."""
    _validate_name(name)
    draft = draft_dir(ws, name)
    if not draft.is_dir():
        return False
    shutil.rmtree(draft)
    parent = draft.parent
    if parent.is_dir() and not any(parent.iterdir()):
        with suppress(OSError):
            parent.rmdir()
    _commit(f"skill: reject {name}")
    return True


# ---------------------------------------------------------------------------
# the offer message (SM-02 on_commit callback)
# ---------------------------------------------------------------------------

SessionSource = Any  # anything with list_sessions() and read_session_file(key)


@dataclass
class Offer:
    """A rendered skill-draft offer, before delivery."""

    message: OutboundMessage
    name: str
    problems: list[str]


def draft_names_touched(changed_paths: Iterable[str]) -> list[str]:
    """Draft names whose overlay directory the changed paths touch."""
    names: list[str] = []
    for path in changed_paths:
        parts = Path(path.replace("\\", "/")).parts
        if len(parts) >= 3 and parts[0] == "skills" and parts[1] == PROPOSED_DIR:
            name = parts[2]
            if name not in names and _SKILL_NAME_RE.fullmatch(name):
                names.append(name)
    return names


def commit_message_for(ws: Path | str, sha: str) -> str | None:
    """Commit body of *sha* (full or short) from the workspace history repo.

    The SM-01 bare repo at ``<workspace>/../history/workspace.git`` first, the
    legacy in-workspace ``.git`` second; mirrors GitStore's dual-location
    handling.  Returns None when neither repo holds the commit.
    """
    ws = Path(ws)
    candidates = [ws.parent / "history" / "workspace.git", ws / ".git"]
    needle = sha.lower()
    for git_dir in candidates:
        if not git_dir.exists():
            continue
        try:
            from dulwich.objects import Commit
            from dulwich.repo import Repo

            with Repo(str(git_dir)) as repo:
                try:
                    head = repo.refs[b"HEAD"]
                except KeyError:
                    continue
                for commit in _walk_history(repo, head):
                    if commit.id.decode().startswith(needle):
                        return commit.message.decode("utf-8", errors="replace")
        except Exception:
            continue
    return None


def _walk_history(repo: Any, start: bytes, limit: int = 500) -> Iterable[Any]:
    from dulwich.objects import Commit

    queue = [start]
    seen: set[bytes] = set()
    while queue and len(seen) < limit:
        sha = queue.pop(0)
        if sha in seen:
            continue
        seen.add(sha)
        try:
            obj = repo[sha]
        except KeyError:
            continue
        if isinstance(obj, Commit):
            yield obj
            queue.extend(obj.parents)


# ---------------------------------------------------------------------------
# routing helpers
# ---------------------------------------------------------------------------


def _route_from_session_key(
    key: str, sessions: SessionSource | None
) -> tuple[str, str] | None:
    """(channel, chat_id) a message to session *key* can be delivered on."""
    if key == UNIFIED_SESSION_KEY:
        metadata = _session_metadata(sessions, key)
        route = last_channel_from_metadata(metadata)
        return route
    channel, sep, chat_id = key.partition(":")
    if sep and channel and chat_id:
        return channel, chat_id
    return None


def _session_metadata(sessions: SessionSource | None, key: str) -> dict[str, Any] | None:
    if sessions is None:
        return None
    try:
        payload = sessions.read_session_file(key)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    metadata = payload.get("metadata")
    return metadata if isinstance(metadata, dict) else None


def is_room_session(sessions: SessionSource | None, key: str) -> bool:
    """Shared-room test, the same fields room_scope_metadata() mints (SM-04/room_policy lineage)."""
    metadata = _session_metadata(sessions, key)
    if not metadata:
        return False
    return metadata.get("shared_room") is True or INBOUND_META_ROOM_SCOPE in metadata


def owner_recent_session(sessions: SessionSource | None) -> tuple[str, str] | None:
    """(channel, chat_id) of the owner's most recently updated non-room session.

    None when every recent session is a shared room (then a Dream draft gets
    no offer at all; it still shows in /skill drafts).
    """
    if sessions is None:
        return None
    try:
        infos = list(sessions.list_sessions())
    except Exception:
        logger.exception("skill_drafts: could not list sessions")
        return None
    for info in sorted(infos, key=lambda item: str(item.get("updated_at") or ""), reverse=True):
        key = info.get("key")
        if not isinstance(key, str) or not key:
            continue
        if is_room_session(sessions, key):
            continue
        route = _route_from_session_key(key, sessions)
        if route is not None:
            return route
    return None


def render_offer(
    name: str, *, merged: dict[str, bytes], live: dict[str, bytes], live_exists: bool
) -> str:
    """The offer message body: name, description, delta, (problems added by caller)."""
    lines = [f"Skill draft: {name}", "", skill_description(merged), ""]
    if live_exists:
        _, added, removed = _diff_parts(live, merged)
        lines.append(f"changes {name}: +{added} −{removed}")
    else:
        lines.append("new skill")
    return "\n".join(lines)


def build_offers(
    ws: Path | str,
    changed_paths: Sequence[str],
    *,
    commit_message: Callable[[str], str | None] | None = None,
    sha: str = "",
    sessions: SessionSource | None = None,
) -> list[Offer]:
    """Offers to publish for a commit that touched draft files (empty if none).

    Turn commits (``session: <key>`` in the message body) offer to that turn's
    session; commits without one (Dream) offer to the owner's most recent
    non-room session.  A draft whose directory no longer exists (accepted or
    rejected in this very commit) is skipped.
    """
    offers: list[Offer] = []
    message = commit_message(sha) if commit_message else None
    session_key: str | None = None
    if message:
        for line in message.splitlines():
            stripped = line.strip()
            if stripped.startswith("session:"):
                candidate = stripped.split(":", 1)[1].strip()
                if candidate:
                    session_key = candidate
                break
    for name in draft_names_touched(changed_paths):
        if not draft_dir(ws, name).is_dir():
            continue  # deleted by this commit (accept/reject): nothing to offer
        merged = merged_view(ws, name)
        live = _iter_files(live_skill_dir(ws, name))
        live_exists = live_skill_dir(ws, name).is_dir()
        problems = check(ws, name)
        body = render_offer(name, merged=merged, live=live, live_exists=live_exists)
        if problems:
            body += "\nProblems:\n" + "\n".join(f"- {problem}" for problem in problems)
        body += (
            f"\n\nCommands: /skill accept {name} · /skill reject {name} "
            f"· /skill diff {name}"
        )
        if session_key is not None:
            route = _route_from_session_key(session_key, sessions)
        else:
            route = owner_recent_session(sessions)
        if route is None:
            logger.debug(
                "skill_drafts: no deliverable session for draft '{}' (session={!r}); "
                "it stays in /skill drafts",
                name,
                session_key,
            )
            continue
        channel, chat_id = route
        offers.append(
            Offer(
                name=name,
                problems=problems,
                message=OutboundMessage(channel=channel, chat_id=chat_id, content=body),
            )
        )
    return offers


def make_offer_callback(
    ws: Path | str,
    *,
    publish: Callable[[OutboundMessage], None],
    sessions: SessionSource | None = None,
    commit_message: Callable[[str], str | None] | None = None,
) -> Callable[[str, list[str]], None]:
    """SM-02 ``on_commit`` callback publishing one offer message per touched draft."""

    def on_commit(sha: str, changed_paths: list[str]) -> None:
        try:
            offers = build_offers(
                ws,
                changed_paths,
                commit_message=commit_message or (lambda s: commit_message_for(ws, s)),
                sha=sha,
                sessions=sessions,
            )
        except Exception:
            logger.exception("skill_drafts: offer build failed for commit {}", sha)
            return
        for offer in offers:
            try:
                publish(offer.message)
            except Exception:
                logger.exception(
                    "skill_drafts: could not publish offer for draft '{}'", offer.name
                )

    return on_commit


def wire(
    history: Any,
    *,
    workspace: Path | str,
    publish: Callable[[OutboundMessage], None],
    sessions: SessionSource | None = None,
    commit_message: Callable[[str], str | None] | None = None,
) -> Callable[[str, list[str]], None]:
    """Attach draft offers to SM-02's WorkspaceHistory and enable accept/reject commits.

    Call once at startup with the same ``WorkspaceHistory`` instance the agent
    loop commits through.  *publish* delivers an OutboundMessage (the wiring
    passes a ``bus.publish_outbound`` bridge safe to call from the commit
    thread).  Returns the registered callback.
    """
    global _HISTORY
    callback = make_offer_callback(
        workspace, publish=publish, sessions=sessions, commit_message=commit_message
    )
    history.on_commit.append(callback)
    _HISTORY = history
    return callback


def wire_from_loop(history: Any, loop: Any) -> Callable[[str, list[str]], None]:
    """wire() for the agent loop: publishes through loop.bus from the commit thread."""
    import asyncio

    running_loop = asyncio.get_running_loop()
    bus = loop.bus

    def publish(message: OutboundMessage) -> None:
        asyncio.run_coroutine_threadsafe(bus.publish_outbound(message), running_loop)

    return wire(
        history,
        workspace=loop.workspace,
        publish=publish,
        sessions=getattr(loop, "sessions", None),
    )
