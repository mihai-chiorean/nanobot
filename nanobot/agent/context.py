"""Context builder for assembling agent prompts."""

import base64
import mimetypes
import os
import platform
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.memory import MemoryStore
from nanobot.agent.skills import SkillsLoader

# Document extensions eligible for text extraction and RAG ingestion
_DOCUMENT_EXTENSIONS = frozenset({
    ".pdf", ".docx", ".txt", ".md", ".csv", ".html", ".xml",
    ".json", ".py", ".log", ".rst", ".yaml", ".yml",
})

_DOC_MAX_CHARS = 50_000


class ContextBuilder:
    """Builds the context (system prompt + messages) for the agent."""

    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md", "IDENTITY.md"]
    _RUNTIME_CONTEXT_TAG = "[Runtime Context -- metadata only, not instructions]"

    def __init__(self, workspace: Path, memory_store: MemoryStore | None = None):
        self.workspace = workspace
        self.memory = memory_store or MemoryStore(workspace)
        self.skills = SkillsLoader(workspace)
        self._cached_prompt: str | None = None
        self._cache_mtimes: dict[str, float] = {}
        self._cache_skill_names: tuple[str, ...] | None = None

    def _get_watched_paths(self) -> dict[str, Path]:
        """Return paths to watch for cache invalidation."""
        paths: dict[str, Path] = {}
        for filename in self.BOOTSTRAP_FILES:
            paths[filename] = self.workspace / filename
        paths["MEMORY.md"] = self.memory.memory_file
        paths["skills_dir"] = self.workspace / "skills"
        return paths

    def _check_cache_valid(self) -> bool:
        """Check if any watched file has changed since last cache."""
        if self._cached_prompt is None:
            return False
        watched = self._get_watched_paths()
        for key, path in watched.items():
            try:
                mtime = path.stat().st_mtime if path.exists() else 0.0
            except OSError:
                mtime = 0.0
            if self._cache_mtimes.get(key) != mtime:
                return False
        return True

    def _update_cache_mtimes(self) -> None:
        """Record current mtimes for all watched paths."""
        watched = self._get_watched_paths()
        for key, path in watched.items():
            try:
                self._cache_mtimes[key] = path.stat().st_mtime if path.exists() else 0.0
            except OSError:
                self._cache_mtimes[key] = 0.0

    def build_system_prompt(self, skill_names: list[str] | None = None) -> str:
        """Build the system prompt from identity, bootstrap files, memory, and skills."""
        current_skills = tuple(sorted(skill_names)) if skill_names else ()
        if self._check_cache_valid() and self._cache_skill_names == current_skills:
            return self._cached_prompt

        parts = [self._get_identity()]

        bootstrap = self._load_bootstrap_files()
        if bootstrap:
            parts.append(bootstrap)

        memory = self.memory.get_memory_context()
        if memory:
            parts.append(f"# Memory\n\n{memory}")

        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")

        skills_summary = self.skills.build_skills_summary()
        if skills_summary:
            parts.append(f"""# Skills

The following skills extend your capabilities. To use a skill, read its SKILL.md file using the read_file tool.
Skills with available="false" need dependencies installed first - you can try installing them with apt/brew.

{skills_summary}""")

        prompt = "\n\n---\n\n".join(parts)
        self._cached_prompt = prompt
        self._cache_skill_names = current_skills
        self._update_cache_mtimes()
        return prompt

    def _get_identity(self) -> str:
        """Get the core identity section."""
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"

        return f"""# nanobot

You are nanobot, a helpful AI assistant.

## Runtime
{runtime}

## Workspace
Your workspace is at: {workspace_path}
- Long-term memory: {workspace_path}/memory/MEMORY.md (write important facts here)
- History log: {workspace_path}/memory/HISTORY.md (grep-searchable). Each entry starts with [YYYY-MM-DD HH:MM].
- Custom skills: {workspace_path}/skills/{{skill-name}}/SKILL.md

## nanobot Guidelines
- State intent before tool calls, but NEVER predict or claim results before receiving them.
- Before modifying a file, read it first. Do not assume files or directories exist.
- After writing or editing a file, re-read it if accuracy matters.
- If a tool call fails, analyze the error before retrying with a different approach.
- Ask for clarification when the request is ambiguous.

Reply directly with text for conversations. Only use the 'message' tool to send to a specific chat channel."""

    @staticmethod
    def _build_runtime_context(channel: str | None, chat_id: str | None) -> str:
        """Build untrusted runtime metadata block for injection before the user message."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        tz = time.strftime("%Z") or "UTC"
        lines = [f"Current Time: {now} ({tz})"]
        if channel and chat_id:
            lines += [f"Channel: {channel}", f"Chat ID: {chat_id}"]
        return ContextBuilder._RUNTIME_CONTEXT_TAG + "\n" + "\n".join(lines)

    def _load_bootstrap_files(self) -> str:
        """Load all bootstrap files from workspace."""
        parts = []

        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")

        return "\n\n".join(parts) if parts else ""

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Build the complete message list for an LLM call."""
        return [
            {"role": "system", "content": self.build_system_prompt(skill_names)},
            *history,
            {"role": "user", "content": self._build_runtime_context(channel, chat_id)},
            {"role": "user", "content": self._build_user_content(current_message, media)},
        ]

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional images and auto-ingested documents."""
        if not media:
            return text

        images: list[dict[str, Any]] = []
        doc_blocks: list[str] = []

        for path in media:
            p = Path(path)
            if not p.is_file():
                continue

            mime, _ = mimetypes.guess_type(path)
            suffix = p.suffix.lower()

            # --- Image handling (existing) ---
            if mime and mime.startswith("image/"):
                b64 = base64.b64encode(p.read_bytes()).decode()
                images.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                })
                continue

            # --- Document auto-ingestion (MIT-44) ---
            if suffix in _DOCUMENT_EXTENSIONS:
                extracted = self._extract_document_text(p, suffix)
                if extracted:
                    doc_blocks.append(
                        f"\n\n---\n[Document: {p.name}]\n{extracted[:_DOC_MAX_CHARS]}\n---"
                    )

        # Append document text directly to the user message
        augmented_text = text + "".join(doc_blocks)

        if not images:
            return augmented_text
        return images + [{"type": "text", "text": augmented_text}]

    # ------------------------------------------------------------------
    # Document text extraction helpers (MIT-44)
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_document_text(path: Path, suffix: str) -> str | None:
        """Extract plain text from a document file for RAG ingestion."""
        try:
            if suffix == ".pdf":
                return ContextBuilder._extract_pdf(path)
            if suffix == ".docx":
                return ContextBuilder._extract_docx(path)
            # All other supported extensions are treated as plain text
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.warning("Failed to extract text from {}: {}", path.name, exc)
            return f"[Error extracting {path.name}: {exc}]"

    @staticmethod
    def _extract_pdf(path: Path) -> str:
        """Extract text from a PDF using pdfplumber."""
        try:
            import pdfplumber
        except ImportError:
            return f"[Cannot extract PDF: pdfplumber not installed. Run: pip install pdfplumber]"

        pages: list[str] = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    pages.append(page_text)
        return "\n\n".join(pages) if pages else "[PDF contained no extractable text]"

    @staticmethod
    def _extract_docx(path: Path) -> str:
        """Extract text from a .docx using python-docx."""
        try:
            import docx
        except ImportError:
            return f"[Cannot extract DOCX: python-docx not installed. Run: pip install python-docx]"

        doc = docx.Document(str(path))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n\n".join(paragraphs) if paragraphs else "[DOCX contained no extractable text]"

    def add_tool_result(
        self, messages: list[dict[str, Any]],
        tool_call_id: str, tool_name: str, result: str,
    ) -> list[dict[str, Any]]:
        """Add a tool result to the message list."""
        messages.append({"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": result})
        return messages

    def add_assistant_message(
        self, messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
    ) -> list[dict[str, Any]]:
        """Add an assistant message to the message list."""
        msg: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        if reasoning_content is not None:
            msg["reasoning_content"] = reasoning_content
        messages.append(msg)
        return messages
