"""Multi-model routing -- classifies requests and dispatches to the right provider.

Routing is opt-in (disabled by default).  When enabled, each user message is
classified as ``simple``, ``complex``, or ``creative`` and dispatched to the
provider/model configured for that tier.

Classification is intentionally heuristic-based (keyword + structure checks).
A v1 approach that avoids an extra LLM call per request.
"""

from __future__ import annotations

import re
from typing import Any

from loguru import logger

from nanobot.providers.base import LLMProvider, LLMResponse


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------

class _Classifier:
    """Simple keyword / heuristic classifier.  Returns one of
    ``"simple"``, ``"complex"``, or ``"creative"``."""

    # Code / technical indicators
    _CODE_FENCE = re.compile(r"```")
    _CODE_KEYWORDS = frozenset({
        "def ", "class ", "return ", "async ",
        "=>", "->", "lambda", "yield",
        "try:", "except", "catch", "throw", "raise",
        "SELECT ", "INSERT ", "UPDATE ", "DELETE ", "CREATE TABLE",
        "kubectl ", "pip install",
        "sudo ", "chmod ",
    })
    _TECH_TERMS = frozenset({
        "algorithm", "backend", "concurrency",
        "database", "debug", "docker", "endpoint",
        "graphql", "kubernetes",
        "latency", "microservice", "middleware", "mutex",
        "oauth", "postgres", "redis", "regex",
        "schema", "sql", "tcp",
        "typescript", "webhook", "websocket",
    })
    _COMPLEX_PHRASES = frozenset({
        "step by step", "explain how", "explain why", "walk me through",
        "compare and contrast", "pros and cons", "trade-offs", "tradeoffs",
        "architecture", "design pattern", "refactor", "optimise", "optimize",
        "implement", "write code", "write a function", "write a script",
        "fix this bug", "fix the error", "debug this", "review this code",
        "analyze", "analyse", "evaluate",
    })

    # Creative indicators
    _CREATIVE_PHRASES = frozenset({
        "write a story", "write a poem", "write a song",
        "brainstorm", "come up with", "creative", "imagine",
        "draft an email", "draft a letter", "write a blog",
        "rewrite", "rephrase", "paraphrase",
        "generate ideas", "suggest names",
        "write a speech", "write a essay", "write an essay",
    })

    @classmethod
    def classify(cls, text: str) -> str:
        """Return ``"simple"``, ``"complex"``, or ``"creative"``."""
        if not text or not text.strip():
            return "simple"

        text_lower = text.lower()

        # --- complex: code fences ------------------------------------------------
        if cls._CODE_FENCE.search(text):
            return "complex"

        # --- complex: code keywords (word-boundary match) -------------------------
        for kw in cls._CODE_KEYWORDS:
            # Keywords ending with space (e.g. "def ") need word-boundary check
            # to avoid matching "definitely" etc.
            pattern = r'(?:^|(?<=\s))' + re.escape(kw.rstrip()) + r'(?:\s|$|[({])'
            if re.search(pattern, text_lower):
                return "complex"

        # --- complex: technical terms (whole word) --------------------------------
        words = set(re.findall(r"[a-z]+", text_lower))
        if words & cls._TECH_TERMS:
            return "complex"

        # --- complex: multi-step / analytical phrases -----------------------------
        for phrase in cls._COMPLEX_PHRASES:
            if phrase in text_lower:
                return "complex"

        # --- creative: writing / brainstorming ------------------------------------
        for phrase in cls._CREATIVE_PHRASES:
            if phrase in text_lower:
                return "creative"

        # --- length heuristic: long messages tend to be complex -------------------
        if len(text.split()) > 80:
            return "complex"

        return "simple"


# ---------------------------------------------------------------------------
# ModelRouter
# ---------------------------------------------------------------------------

class ModelRouter(LLMProvider):
    """Wraps multiple providers and routes each request to the best one.

    Parameters
    ----------
    providers : dict[str, LLMProvider]
        Map of tier name (``"simple"``, ``"complex"``, ``"creative"``) to a
        concrete provider instance.  Missing tiers fall back to ``"default"``.
    models : dict[str, str]
        Map of tier name to the model string to pass to ``chat()``.
    default_provider : LLMProvider
        The fallback provider (also used when routing is uncertain).
    default_model : str
        The fallback model string.
    """

    def __init__(
        self,
        providers: dict[str, LLMProvider],
        models: dict[str, str],
        default_provider: LLMProvider,
        default_model: str,
    ):
        super().__init__()
        self._providers = providers
        self._models = models
        self._default_provider = default_provider
        self._default_model = default_model
        # Per-session tier pinning to avoid cross-session leakage
        self._pinned_tiers: dict[str, str] = {}

    # -- LLMProvider interface -------------------------------------------------

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        session_id: str | None = None,
    ) -> LLMResponse:
        session = session_id or "__default__"
        # Use pinned tier if set for this session, otherwise classify and pin.
        if session in self._pinned_tiers:
            tier = self._pinned_tiers[session]
        else:
            tier = self._classify_from_messages(messages)
            self._pinned_tiers[session] = tier

        provider = self._providers.get(tier, self._default_provider)
        routed_model = self._models.get(tier, model or self._default_model)

        logger.debug("Router: tier={} model={}", tier, routed_model)

        return await provider.chat(
            messages=messages,
            tools=tools,
            model=routed_model,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    def get_default_model(self) -> str:
        return self._default_model

    def reset_tier(self, session_id: str | None = None) -> None:
        """Reset the pinned tier so the next chat() call re-classifies."""
        if session_id:
            self._pinned_tiers.pop(session_id, None)
        elif self._current_session:
            self._pinned_tiers.pop(self._current_session, None)
        else:
            self._pinned_tiers.clear()

    # -- internal --------------------------------------------------------------

    @staticmethod
    def _classify_from_messages(messages: list[dict[str, Any]]) -> str:
        """Extract the last user message text and classify it."""
        for msg in reversed(messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                return _Classifier.classify(content)
            if isinstance(content, list):
                # Multi-part content (text + images).  Concatenate text parts.
                text_parts = [
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                return _Classifier.classify(" ".join(text_parts))
        return "simple"
