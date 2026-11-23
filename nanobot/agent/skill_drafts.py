"""Skill drafts: check, diff, accept and reject the overlays in ``skills/_proposed/``.

Ziggy-local (fork, MIT-1856, design §3).  After SM-04 an agent or Dream write
to ``skills/<name>/`` is refused and the changed files are written instead to
``skills/_proposed/<name>/`` as an **overlay**: it holds only the files to add
or change.  The **merged view** of a draft is the live ``skills/<name>/``
(possibly absent for a new skill) with the draft's files laid over it.

Nothing goes live until the owner sends ``/skill accept <name>`` (a user
turn, so the model cannot accept its own draft; ``cmd_skill`` refuses accept
and reject from any other turn).  Accept first commits the current state to
the workspace history (the bare GitStore at ``<workspace>/../history``), so
every live file it overwrites and the draft it removes stay restorable, then
copies the draft's files over the live skill (staged temp files, then
renames; it **never deletes live files**), deletes the draft dir and commits
``skill: accept <name>``.  Drafts or live skills containing symlinks are
refused: nothing is read or written through a link.

An ``on_commit`` callback (:func:`make_offer_callback`, attached by
:func:`wire`) watches commits that touch ``skills/_proposed/<name>/`` and
publishes one offer message per draft: to the turn's own session for a turn
commit, and for a Dream commit to the owner's most recent non-room session --
never a shared room, whose guests can be testers and drafts are built from
the owner's private workflows.

Integration note (SM-02, MIT-1845): ``wire`` attaches the offer callback to
an ``on_commit`` list of ``(sha, changed_paths)`` callables, which SM-02's
per-turn commits will provide.  Turn commits carry ``session: <key>`` in the
message body; Dream commits carry no session line.  Until SM-02 is wired, no
offers are sent; drafts still show in ``/skill drafts``.  Accept/reject do not
depend on SM-02: they commit through the GitStore handed to them.
"""

from __future__ import annotations

import difflib
import importlib.util
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from loguru import logger

from nanobot.agent.skills import (
    PROPOSED_SKILLS_DIR,
    parse_skill_metadata,
    valid_skill_metadata,
)
from nanobot.bus.events import INBOUND_META_ROOM_SCOPE, OutboundMessage
from nanobot.session.keys import UNIFIED_SESSION_KEY, last_channel_from_metadata
from nanobot.utils.gitstore import _MAX_TRACKED_FILE_BYTES, GitStore, history_dir_for

PROPOSED_DIR = PROPOSED_SKILLS_DIR

# Same cap the workspace-history working-tree diff uses (gitstore.py).
DIFF_MAX_CHARS = 6000

_SKILL_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")

_VALIDATORS: ModuleType | None = None
_VALIDATORS_RESOLVED = False


class SkillDraftError(ValueError):
    """Raised for an unusable draft name (bad characters, path traversal)."""


def is_valid_name(name: str) -> bool:
    return bool(_SKILL_NAME_RE.fullmatch(name))


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


def _symlinks(base: Path) -> list[str]:
    """Relpaths of symlinks (to files or dirs) anywhere under *base*."""
    links: list[str] = []
    if base.is_symlink() or not base.is_dir():
        return links
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        for entry in [*dirnames, *filenames]:
            path = Path(dirpath) / entry
            if path.is_symlink():
                links.append(path.relative_to(base).as_posix())
    return sorted(links)


def _iter_files(base: Path) -> dict[str, bytes]:
    """Regular files under *base*, keyed by forward-slash relpath.

    Never follows a symlink: a link is how a draft could pull in a file from
    outside the workspace (or from the history dir).  ``check`` reports links
    as problems, so accept refuses such a draft.
    """
    files: dict[str, bytes] = {}
    if base.is_symlink() or not base.is_dir():
        return files
    for dirpath, _dirnames, filenames in os.walk(base, followlinks=False):
        for entry in filenames:
            path = Path(dirpath) / entry
            if path.is_symlink() or not path.is_file():
                continue
            rel = path.relative_to(base).as_posix()
            try:
                files[rel] = path.read_bytes()
            except OSError:
                logger.warning("skill_drafts: unreadable file {}", path)
    return dict(sorted(files.items()))


def _link_problems(ws: Path | str, name: str) -> list[str]:
    """Symlinks on the draft's or live skill's path, or inside either tree."""
    root = skills_root(ws)
    problems: list[str] = []
    for path in (root, root / PROPOSED_DIR, draft_dir(ws, name), live_skill_dir(ws, name)):
        if path.is_symlink():
            problems.append(f"{path.relative_to(Path(ws)).as_posix()} is a symlink")
    if problems:
        return problems
    for label, base in (("Draft", draft_dir(ws, name)), ("Live skill", live_skill_dir(ws, name))):
        for rel in _symlinks(base):
            problems.append(f"{label} '{name}' contains a symlink: {rel}")
    return problems


def list_drafts(ws: Path | str) -> list[str]:
    """Draft names under ``skills/_proposed/``, sorted."""
    root = skills_root(ws) / PROPOSED_DIR
    if not root.is_dir():
        return []
    return sorted(
        entry.name for entry in root.iterdir() if entry.is_dir() and not entry.is_symlink()
    )


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
    links = _link_problems(ws, name)
    if links:
        return links
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


def _prune_empty_proposed(draft: Path) -> None:
    parent = draft.parent
    if parent.is_dir() and not any(parent.iterdir()):
        with suppress(OSError):
            parent.rmdir()


def _ensure_history(history: GitStore) -> None:
    if not history.is_initialized():
        history.init()


def accept(ws: Path | str, name: str, history: GitStore) -> list[str]:
    """Apply draft *name* over the live skill. Returns problems; [] means accepted.

    Owner-initiated only (``/skill accept`` on a user turn).  Records the
    pre-accept state in *history* first, so every live file it overwrites and
    the draft it removes can be restored; refuses (writing nothing) when that
    commit fails or a file is too large for history to hold.  Copies only the
    draft's files, never deletes live files, then commits
    ``skill: accept <name>``.
    """
    problems = check(ws, name)
    if problems:
        return problems
    if Path(history._workspace).resolve() != Path(ws).resolve():
        return ["Workspace history belongs to a different workspace; nothing changed"]
    live = live_skill_dir(ws, name)
    draft = draft_dir(ws, name)
    draft_files = _draft_files(ws, name)
    live_files = _iter_files(live)
    for rel, data in draft_files.items():
        target = live / rel
        if target.is_dir():
            problems.append(f"Live skill has a directory where the draft has a file: {rel}")
            continue
        for parent in Path(rel).parents:
            if str(parent) != "." and (live / parent).is_file():
                problems.append(f"Live skill has a file where the draft needs a directory: {parent.as_posix()}")
        old = live_files.get(rel)
        if old is not None and old != data and len(old) > _MAX_TRACKED_FILE_BYTES:
            problems.append(
                f"Live file {rel} is too large for workspace history, so overwriting it "
                "could not be undone"
            )
    if problems:
        return problems

    try:
        _ensure_history(history)
        history.auto_commit(f"skill: before accept {name}")
    except Exception as exc:
        logger.exception("skill_drafts: pre-accept history commit failed for '{}'", name)
        return [f"Could not record the current state in workspace history ({exc}); nothing changed"]

    # All or nothing, by GitStore's own write path (the one /undo uses):
    # staged temps, hard-link backups of replaced files, rollback by rename.
    # A failed rollback keeps the backup and names it in the GitStoreError.
    rel_live = live.relative_to(Path(ws)).as_posix()
    history._apply_writes([(f"{rel_live}/{rel}", data) for rel, data in draft_files.items()])
    shutil.rmtree(draft)
    _prune_empty_proposed(draft)
    try:
        history.auto_commit(f"skill: accept {name}")
    except Exception:
        # The pre-accept commit already holds everything this changed.
        logger.exception("skill_drafts: history commit failed for 'skill: accept {}'", name)
    return []


def _reject_target(ws: Path | str, name: str) -> Path | None:
    """The ``_proposed/`` entry *name* names, valid skill name or not.

    Matches only an existing direct child of ``skills/_proposed/`` by exact
    name, so a malformed draft (e.g. ``Demo``) can still be removed while
    ``..`` or a path with separators never resolves outside it.
    """
    root = skills_root(ws) / PROPOSED_DIR
    if root.is_symlink() or not root.is_dir() or name not in os.listdir(root):
        return None
    return root / name


def _missing_from_head(history: GitStore, ws: Path, base: Path) -> list[str]:
    """Entries under *base* that the history HEAD does not hold byte for byte."""
    from dulwich.repo import Repo

    missing: list[str] = []
    with Repo(str(history._git_dir)) as repo:
        tree = GitStore._head_tree(repo)
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            for entry in [*dirnames, *filenames]:
                path = Path(dirpath) / entry
                rel = path.relative_to(ws).as_posix()
                if path.is_symlink():
                    missing.append(f"{rel} (symlink)")
                    continue
                if path.is_dir():
                    continue
                try:
                    data = path.read_bytes()
                except OSError:
                    missing.append(f"{rel} (unreadable)")
                    continue
                stored = GitStore._read_blob_from_tree(repo, tree, rel) if tree else None
                if stored != data:
                    note = " (too large)" if len(data) > _MAX_TRACKED_FILE_BYTES else ""
                    missing.append(f"{rel}{note}")
    return missing


def reject(ws: Path | str, name: str, history: GitStore) -> bool:
    """Delete draft *name* (live files untouched). True if a draft was removed.

    Owner-initiated only.  Also removes a draft whose directory name is not a
    valid skill name.  The draft is committed to *history* first and every
    file in it is checked against that commit; if any file is not held there
    (too large, a symlink, unreadable) nothing is deleted and
    :class:`SkillDraftError` explains why, so a reject can always be undone.
    """
    draft = _reject_target(ws, name)
    if draft is None:
        return False
    if draft.is_symlink() or not draft.is_dir():
        raise SkillDraftError(
            f"skills/{PROPOSED_DIR}/{name} is not a draft directory; not removing it."
        )
    _ensure_history(history)
    history.auto_commit(f"skill: before reject {name}")
    missing = _missing_from_head(history, Path(ws), draft)
    if missing:
        raise SkillDraftError(
            f"Not rejecting draft '{name}': workspace history cannot hold "
            + ", ".join(missing)
            + ", so deleting it could not be undone. Nothing was changed."
        )
    shutil.rmtree(draft)
    _prune_empty_proposed(draft)
    try:
        history.auto_commit(f"skill: reject {name}")
    except Exception:
        logger.exception("skill_drafts: history commit failed for 'skill: reject {}'", name)
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

    The SM-01 bare repo at ``<workspace>/../history/workspace.git`` (the only
    history location since MIT-1842).  Returns None when it lacks the commit.
    """
    candidates = [history_dir_for(Path(ws)) / "workspace.git"]
    needle = sha.lower()
    for git_dir in candidates:
        if not git_dir.exists():
            continue
        try:
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
    """Attach draft offers to SM-02's per-turn commit hook (``history.on_commit``).

    Call once at startup with the object the agent loop commits turns through.  *publish* delivers an OutboundMessage (the wiring
    passes a ``bus.publish_outbound`` bridge safe to call from the commit
    thread).  Returns the registered callback.
    """
    callback = make_offer_callback(
        workspace, publish=publish, sessions=sessions, commit_message=commit_message
    )
    history.on_commit.append(callback)
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
