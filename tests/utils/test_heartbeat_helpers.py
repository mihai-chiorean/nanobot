import random

import pytest

from nanobot.utils.helpers import (
    THINKING_EMOJIS,
    TOOL_EMOJIS,
    extract_latest_sentence,
    pick_thinking_emoji,
    pick_tool_emoji,
    summarize_tool_call,
)


class TestPickEmoji:
    def test_thinking_returns_known_emoji(self):
        assert pick_thinking_emoji() in THINKING_EMOJIS

    def test_tool_returns_known_emoji(self):
        assert pick_tool_emoji() in TOOL_EMOJIS

    def test_seeded_rng_is_deterministic(self):
        rng = random.Random(42)
        results = [pick_thinking_emoji(_rng=rng) for _ in range(5)]
        rng2 = random.Random(42)
        expected = [pick_thinking_emoji(_rng=rng2) for _ in range(5)]
        assert results == expected

    def test_tool_seeded_rng_is_deterministic(self):
        rng = random.Random(99)
        a = pick_tool_emoji(_rng=rng)
        rng2 = random.Random(99)
        b = pick_tool_emoji(_rng=rng2)
        assert a == b


class TestSummarizeToolCall:
    def test_path_arg(self):
        s = summarize_tool_call("edit_file", {"path": "nanobot/loop.py"})
        assert s == "edit_file: nanobot/loop.py"

    def test_command_arg_short(self):
        s = summarize_tool_call("exec", {"command": "pytest tests/"})
        assert s == "exec: pytest tests/"

    def test_command_arg_truncated(self):
        long_cmd = "pytest tests/unit/test_long_suite.py --tb=short --no-header"
        s = summarize_tool_call("exec", {"command": long_cmd})
        assert s.startswith("exec: ")
        # Command truncated to 40 chars + "..."
        assert "..." in s
        assert len(s) <= 180

    def test_no_obvious_arg_uses_first_value(self):
        s = summarize_tool_call("grep", {"pattern": "import asyncio"})
        assert s.startswith("grep: ")

    def test_no_args_falls_back_to_searching(self):
        s = summarize_tool_call("grep", {})
        assert s == "grep: searching"

    def test_capped_at_180(self):
        huge = "x" * 300
        s = summarize_tool_call("read_file", {"path": huge})
        assert len(s) <= 180


class TestExtractLatestSentence:
    def test_single_sentence(self):
        assert extract_latest_sentence("The file exists.") == "The file exists"

    def test_returns_last_complete_sentence(self):
        buf = "First sentence. Second sentence. Incomplete"
        assert extract_latest_sentence(buf) == "Second sentence"

    def test_no_sentence_boundary_returns_none(self):
        assert extract_latest_sentence("no boundary here") is None

    def test_empty_returns_none(self):
        assert extract_latest_sentence("") is None

    def test_strips_think_tags(self):
        # The reasoning ends at a sentence boundary followed by whitespace;
        # the think tags are stripped before scanning.
        buf = "<think>Reasoning step one. Reasoning step two. </think>Incomplete"
        result = extract_latest_sentence(buf)
        assert result == "Reasoning step two"

    def test_caps_at_120_chars(self):
        long_sentence = "A" * 150 + ". Short."
        result = extract_latest_sentence(long_sentence)
        assert result is not None
        assert len(result) <= 123  # 120 + "..."

    def test_exclamation_boundary(self):
        assert extract_latest_sentence("Hello! World") == "Hello"

    def test_question_boundary(self):
        assert extract_latest_sentence("What is this? Unknown") == "What is this"

    def test_whitespace_only_returns_none(self):
        assert extract_latest_sentence("   ") is None
