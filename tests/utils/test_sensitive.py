"""Tests for nanobot.utils.sensitive — regex coverage for MIT-139 gaps."""

from __future__ import annotations

import pytest

from nanobot.utils.sensitive import (
    check_shell_command,
    is_sensitive_path,
    redact_if_sensitive,
    scan_content,
)


# ---------------------------------------------------------------------------
# Shell command pre-screening — MIT-139 Gap A: absolute SSH paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        # Absolute paths (the bug MIT-139 fixes)
        "cat /home/mihai/.ssh/id_rsa",
        "cat /root/.ssh/id_rsa",
        "cat /Users/alice/.ssh/id_ed25519",
        # Home-relative
        "cat ~/.ssh/id_rsa",
        # Bare
        "cat .ssh/id_rsa",
        # Dot-relative
        "cat ./.ssh/id_rsa",
        # Case-insensitive command
        "CAT /home/mihai/.ssh/id_rsa",
        # Other reader tools — same gap, same fix
        "less /home/mihai/.ssh/id_rsa",
        "more /home/mihai/.ssh/config",
        "head /home/mihai/.ssh/id_rsa",
        "tail /root/.ssh/authorized_keys",
        "bat /home/mihai/.ssh/id_ed25519",
        "vim /home/mihai/.ssh/id_rsa",
        "vi /home/mihai/.ssh/id_rsa",
        "view /home/mihai/.ssh/id_rsa",
        "nano /home/mihai/.ssh/id_rsa",
        # Exfiltration variants
        "base64 /home/mihai/.ssh/id_rsa",
        "xxd /home/mihai/.ssh/id_rsa",
        "od /home/mihai/.ssh/id_rsa",
        "hexdump /home/mihai/.ssh/id_rsa",
    ],
)
def test_check_shell_command_blocks_ssh_paths(command: str) -> None:
    """All variants of reading .ssh/ — absolute, home, relative, bare — must be blocked."""
    result = check_shell_command(command)
    assert result is not None, f"Expected block for: {command!r}"
    assert "blocked by security policy" in result


@pytest.mark.parametrize(
    "command",
    [
        # Pre-MIT-139 cases must still block (regression guard)
        "cat ~/.ssh/id_rsa",
        "cat /etc/shadow",
        "cat .env",
        "cat /path/to/app.pem",
        "cat secrets.key",
        "cat /home/user/credentials.json",
        "less ~/.ssh/config",
        "less /etc/shadow",
        "base64 ~/.ssh/id_rsa",
        "base64 foo.pem",
        "base64 app.key",
        "xxd ~/.ssh/id_ed25519",
        "printenv",
        "env",
        "export -p",
        "declare -x",
        "ssh-add -l",
        "ssh-add -L",
        "gpg --export-secret-keys",
    ],
)
def test_check_shell_command_preserves_existing_blocks(command: str) -> None:
    """Existing MIT-123-era denials must keep working after MIT-139 widening."""
    assert check_shell_command(command) is not None, f"Regression: expected block for: {command!r}"


@pytest.mark.parametrize(
    "command",
    [
        # Ordinary commands must not be caught by the widened pattern
        "cat README.md",
        "cat /etc/hostname",
        "ls /home/mihai/.ssh",  # listing is out of scope — only content reads are blocked
        "echo hello",
        "grep TODO src/",
        "env VAR=value some_cmd",  # 'env' as prefix command, not dumper
        "cat /home/mihai/notes.txt",
        "less /var/log/syslog",
        "head -n 10 data.csv",
        # `.ssh` as a substring, not as a path segment, must not trigger
        "cat foo.sshkey",  # no '/' boundary
    ],
)
def test_check_shell_command_allows_benign_commands(command: str) -> None:
    """Widened patterns must not regress into false positives."""
    assert check_shell_command(command) is None, f"False positive for: {command!r}"


# ---------------------------------------------------------------------------
# Sensitive filename — MIT-139 Gap B: decision documented narrow
# ---------------------------------------------------------------------------


def test_dotenv_variants_blocked() -> None:
    """Files whose basename begins with .env — the canonical cases — must block."""
    assert is_sensitive_path(".env") is True
    assert is_sensitive_path("/home/mihai/project/.env") is True
    assert is_sensitive_path("/home/mihai/project/.env.local") is True
    assert is_sensitive_path("/home/mihai/project/.env.production") is True


def test_env_suffix_filenames_not_broadly_blocked() -> None:
    """MIT-139 decision: keep the `.env` regex narrow to filename-starts-with-.env.

    Widening to `*.env` would catch legitimate fixtures (`example.env`,
    `template.env`, documentation samples).  Operators who need to block
    custom-named dotenv files (e.g. `secrets/app.env`) should add the
    containing directory to `_SENSITIVE_PATH_PATTERNS` rather than broaden
    the filename regex.
    """
    # Not blocked by filename alone — documented as intentional
    assert is_sensitive_path("/tmp/example.env") is False
    assert is_sensitive_path("/tmp/template.env") is False


def test_sensitive_paths_still_caught() -> None:
    """Regression: unrelated sensitive paths must still be caught."""
    assert is_sensitive_path("/home/mihai/.ssh/id_rsa") is True
    assert is_sensitive_path("/etc/shadow") is True
    assert is_sensitive_path("/home/mihai/.aws/credentials") is True
    assert is_sensitive_path("/home/mihai/project/server.pem") is True
    assert is_sensitive_path("/home/mihai/project/server.key") is True


def test_ordinary_paths_not_flagged() -> None:
    """No false positives on regular source files."""
    assert is_sensitive_path("/home/mihai/project/main.py") is False
    assert is_sensitive_path("/home/mihai/project/README.md") is False


# ---------------------------------------------------------------------------
# MIT-140: narrow id_*.pub exclusion from sensitive filename patterns
#
# Public SSH keys (id_*.pub) are, by definition, safe to disclose — agents
# legitimately need to read them to deploy authorized_keys, configure CI
# runners, etc.  The filename-only regex must block bare private-key names
# while allowing `.pub` siblings.  Private keys *inside* `~/.ssh/` are still
# blocked by the separate `/.ssh/` path-prefix rule, which catches public
# AND private alike — callers who need to read `id_*.pub` should keep the
# file outside `~/.ssh/`, or the block will (correctly) fire anyway.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename",
    [
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "/tmp/keys/id_rsa",
        "/tmp/keys/id_ed25519",
    ],
)
def test_ssh_private_key_filenames_still_blocked(filename: str) -> None:
    """MIT-140: bare SSH private-key filenames remain blocked regardless of directory."""
    assert is_sensitive_path(filename) is True, f"Expected block for: {filename!r}"


@pytest.mark.parametrize(
    "filename",
    [
        "id_rsa.pub",
        "id_dsa.pub",
        "id_ecdsa.pub",
        "id_ed25519.pub",
        "/tmp/keys/id_rsa.pub",
        "/tmp/keys/id_ed25519.pub",
    ],
)
def test_ssh_public_key_filenames_allowed_outside_ssh_dir(filename: str) -> None:
    """MIT-140: `.pub` siblings of SSH keys are public info — filename regex must allow them.

    (Files under `~/.ssh/` are still caught by the `/.ssh/` path-prefix rule,
    which is intentional — the filename regex itself should not block `.pub`.)
    """
    assert is_sensitive_path(filename) is False, f"Unexpected block for: {filename!r}"


def test_ssh_public_key_inside_ssh_dir_still_blocked_by_path_rule() -> None:
    """Regression: `.pub` files inside `~/.ssh/` stay blocked via the path-prefix rule.

    The filename rule now permits `id_*.pub`, but the `/.ssh/` path prefix in
    `_SENSITIVE_PATH_PATTERNS` catches anything under the SSH directory.  This
    is the defence-in-depth split MIT-140 documents.
    """
    assert is_sensitive_path("/home/mihai/.ssh/id_rsa.pub") is True
    assert is_sensitive_path("/root/.ssh/id_ed25519.pub") is True


# ---------------------------------------------------------------------------
# MIT-148: HTTP Authorization Bearer header detection
#
# The pre-MIT-148 labeled-credential regex matched `bearer=token` or
# `bearer: token` shapes, but NOT the real-world HTTP header
# `Authorization: Bearer <token>` — `Bearer` is the *prefix* of the token,
# not a label followed by `=` / `:`.  The new parallel pattern fills that
# gap without broadening the labeled regex (which, if widened, would catch
# too much ordinary prose / logs).
#
# Note on test fixtures: all Bearer-token literals below are deliberately
# synthetic and do NOT match any provider-specific secret shape (no
# `sk_live_`, `pk_`, `ghp_`, etc.). This avoids tripping GitHub's push
# protection secret scanner on what are clearly test-only strings.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # Canonical HTTP header form with a JWT-shaped token
        "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abc.def",
        # Header-less Bearer form (curl examples, middleware logs)
        "Bearer kid_abcdefghij1234567890",
        # Mid-line occurrence in a log line
        "2026-04-21 http POST /api Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9xxxxxxx 200",
        # With surrounding structured-output quotes — generic synthetic token
        'headers = {"Authorization": "Bearer tkn_fakefakefakefakefakefake"}',
        # Case variants — RFC 7235 says auth-scheme is case-insensitive
        "authorization: bearer abcdefghijklmnopqrstuvwxyz",  # lowercase
        "Authorization: BEARER ABCDEFGHIJKLMNOPQRSTUVWXYZ",  # uppercase
        "Authorization: BeArEr MixedCaseTokenAbcdefghijklmn",  # mixed
        # Base64 token with `/` and `+` characters (opaque tokens)
        "Authorization: Bearer abc/def+ghi/jkl+mno/pqrstuvw",
    ],
)
def test_bearer_token_is_detected(text: str) -> None:
    """MIT-148: real-world Bearer-prefix tokens (20+ chars) must be flagged."""
    assert scan_content(text) == "bearer token", f"Expected bearer-token match for: {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        # Documentation-style placeholders — too short to be a real token
        "Bearer short",
        "Bearer token",
        "Bearer xxx",
        "Bearer abc123",
        "Authorization: Bearer YOUR_TOKEN_HERE",  # 16 chars, under the 20-char floor
        # Prose usage of the word "bearer" unrelated to auth
        "The bearer of this letter is authorized to...",
        # Bearer followed by too-short token at end of string
        "Try: Bearer foo",
    ],
)
def test_bearer_placeholder_and_prose_not_flagged(text: str) -> None:
    """MIT-148: short placeholders (< 20 chars) and non-auth prose must not match."""
    # Either no match at all, or a match from a different pattern — just not "bearer token".
    assert scan_content(text) != "bearer token", f"False positive bearer-token for: {text!r}"


def test_bearer_redaction_output_shape() -> None:
    """The full redact_if_sensitive pipeline must replace the token with the REDACTED notice."""
    payload = "Authorization: Bearer eyJhbGciOiJIUzI1NiIs_abcdefghijklmnopqrst\n"
    result = redact_if_sensitive(payload)
    assert "eyJhbGciOiJIUzI1NiIs_abcdefghijklmnopqrst" not in result
    assert "REDACTED" in result
    assert "bearer token" in result


def test_bearer_case_insensitive_redaction() -> None:
    """Defence-in-depth regression: the redactor must scrub lowercase/UPPERCASE Bearer too."""
    # Lowercase header — common when middleware normalizes headers
    lower = "authorization: bearer opaque_token_abcdefghij1234567890\n"
    out_lower = redact_if_sensitive(lower)
    assert "opaque_token_abcdefghij1234567890" not in out_lower
    assert "REDACTED" in out_lower

    # All-caps
    upper = "AUTHORIZATION: BEARER opaque_token_ABCDEFGHIJ1234567890\n"
    out_upper = redact_if_sensitive(upper)
    assert "opaque_token_ABCDEFGHIJ1234567890" not in out_upper
    assert "REDACTED" in out_upper


def test_bearer_base64_token_redaction() -> None:
    """Regression: base64 tokens with `/` and `+` in the payload must be scrubbed.

    Opaque/session bearer tokens are often raw base64 — the pattern must include
    those characters (matching the labeled-credential regex charset) or real
    Authorization headers will leak through.
    """
    payload = "Authorization: Bearer abc/def+ghi/jkl+mno/pqrstuvwxyz01\n"
    result = redact_if_sensitive(payload)
    assert "abc/def+ghi/jkl+mno/pqrstuvwxyz01" not in result
    assert "REDACTED" in result


def test_existing_credential_patterns_still_match() -> None:
    """Regression: MIT-148 added a parallel pattern; existing detections must still fire."""
    # The labeled form — `bearer=<token>` — should still match (via the
    # pre-existing labeled-credential regex, now labeled "credential/token").
    # We only assert a match occurred; the label may be "credential/token"
    # OR "bearer token" depending on ordering, both are correct.
    assert scan_content("bearer=abcdefghij1234567890xxxx") is not None

    # AWS access key
    assert scan_content("AKIAABCDEFGHIJKLMNOP") == "AWS access key"
    # GitHub token
    assert scan_content("ghp_0123456789abcdef0123456789abcdef0123") == "GitHub token"
    # Private key blob
    assert scan_content("-----BEGIN RSA PRIVATE KEY-----\n...") == "private key"


def test_clean_text_not_flagged() -> None:
    """No false positives on ordinary prose (no secrets at all)."""
    assert scan_content("hello world") is None
    assert scan_content("The quick brown fox.") is None
    assert scan_content("Response headers: Content-Type: application/json") is None


# ---------------------------------------------------------------------------
# MIT-164: `sh -c "..."` / `bash -c '...'` / `zsh -c ...` quote-bypass
#
# Before MIT-164 the shell pre-screen used regexes anchored at
# `(?:^|\|)` (start-of-command or after-pipe), so any denylisted command
# could be smuggled past the check by wrapping it in `sh -c "..."` — the
# denylisted token lived inside a quoted argument the regex never saw.
# The fix detects the `<shell> -c <script>` shape (for sh/bash/zsh/dash/
# ash/ksh, with or without a `/bin/` or `/usr/bin/` path prefix), extracts
# the inner script via shlex.split, and recursively screens it.  Depth is
# capped to guard against pathological nesting.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        # Canonical bypass — single-quoted, inner command is a secret-dumper
        "sh -c 'printenv'",
        # Double-quoted wrapper
        'sh -c "printenv"',
        # Unquoted inner (bash accepts this for a single-token script)
        "sh -c printenv",
        # SSH-key read inside a wrapper
        'sh -c "cat ~/.ssh/id_rsa"',
        "sh -c 'cat /home/mihai/.ssh/id_rsa'",
        "sh -c 'cat /etc/shadow'",
        # bash / zsh wrappers
        "bash -c 'base64 ~/.ssh/id_rsa'",
        'zsh -c "ssh-add -l"',
        "dash -c 'printenv'",
        # Path-prefixed shell binaries
        "/bin/sh -c 'export -p'",
        '/usr/bin/bash -c "cat /etc/shadow"',
        "/usr/local/bin/zsh -c 'printenv'",
        # env dumper inside wrapper
        'bash -c "env"',
        # Nested wrappers — recursion handles this via _depth
        "sh -c \"bash -c 'printenv'\"",
        # gpg export inside wrapper
        "sh -c 'gpg --export-secret-keys'",
        # xxd / hexdump on SSH keys inside wrapper
        "sh -c 'xxd ~/.ssh/id_ed25519'",
    ],
)
def test_mit164_wrapper_bypass_is_blocked(command: str) -> None:
    """MIT-164: `sh -c "<denylisted>"` and friends must recurse into the inner script."""
    result = check_shell_command(command)
    assert result is not None, f"Expected block for wrapped command: {command!r}"
    assert "blocked by security policy" in result


@pytest.mark.parametrize(
    "command",
    [
        # MIT-143 replacement shape — the exact string used by
        # tests/tools/test_exec_env.py::test_exec_allowed_env_keys_passthrough.
        # If the MIT-164 wrapper detector falsely blocks this, the MIT-143
        # battery regresses.
        'sh -c \'echo "$MY_CUSTOM_VAR"\'',
        # MIT-143 "missing var" shape — also pulled verbatim from
        # tests/tools/test_exec_env.py::test_exec_allowed_env_keys_missing_var_ignored.
        'sh -c \'[ -z "${NONEXISTENT_VAR_12345+set}" ] || exit 1\'',
        # Ordinary `sh -c` usage — no denylisted command inside
        "sh -c 'cd foo && make'",
        "sh -c 'git status'",
        'bash -c "npm install"',
        "bash -c 'pytest tests/'",
        "sh -c 'echo hello'",
        "sh -c 'ls /tmp'",
        # Shell invoked WITHOUT -c (not a wrapper) — must not be treated as
        # a wrapper regardless of remaining args
        "sh script.sh",
        "bash --version",
        # Shell invoked with -c but the script is a benign pipeline
        "sh -c 'ls | wc -l'",
        # Short invocation (len < 3 tokens) — must pass through silently
        "sh",
        "sh -c",
        # `zsh -c "echo $VAR"` — env expansion, not env dump
        'zsh -c "echo $HOME"',
    ],
)
def test_mit164_legitimate_wrapper_usage_still_allowed(command: str) -> None:
    """MIT-164: benign `sh -c` / `bash -c` usage must not be false-positived.

    In particular, the two MIT-143 replacement shapes used by
    ``tests/tools/test_exec_env.py`` must stay green — that test suite is the
    canonical regression signal for "did we over-block the wrapper path?".
    """
    assert check_shell_command(command) is None, f"False positive for: {command!r}"


def test_mit164_malformed_quoting_does_not_crash() -> None:
    """Unclosed-quote command must not raise — shlex.split ValueError is caught.

    Contract: malformed shell input falls through to the "clean" result
    (same behaviour as before MIT-164).  The outer regex layer has already
    run and is the authoritative result for input shlex can't parse.
    Crucially, we must NOT raise — the shell tool treats exceptions from
    the prescreen as unexpected errors.
    """
    # Unclosed single quote — shlex will raise ValueError internally.
    result = check_shell_command("sh -c 'printenv")
    # Either None (no regex match on the malformed outer string) or a
    # block string; the important part is that no exception escapes.
    assert result is None or "blocked by security policy" in result


def test_mit164_deep_nesting_fails_closed() -> None:
    """Wrappers nested past `_MAX_SHELL_WRAPPER_DEPTH` must BLOCK, not fall through.

    Codex review of MIT-164 (round 1) correctly flagged that returning
    `None` at the depth cap trivially reopens the bypass — stack one
    more `sh -c` than the cap allows and the innermost denylisted
    command sails through.  The fix: at the cap, return the standard
    block string.  This is fail-closed, which matches the rest of the
    security layer's default (better to over-block than leak a key).

    Four nested `sh -c` wrappers is well above anything seen in
    legitimate traffic — depth 0 → 1 → 2 → 3 recursive calls, at which
    point the cap fires before unwrapping the fourth layer.
    """
    deep = "sh -c \"sh -c 'sh -c \\\"sh -c printenv\\\"'\""
    result = check_shell_command(deep)
    assert result is not None, "Depth-cap must fail closed"
    assert "blocked by security policy" in result


def test_mit164_nested_wrapper_blocks_inner_denylist() -> None:
    """Two-level nesting within the depth cap must still block via inner regex.

    `sh -c "bash -c 'printenv'"` → unwrap to `bash -c 'printenv'` (depth 1)
    → unwrap to `printenv` (depth 2) → regex hits.  No cap involvement.
    """
    result = check_shell_command("sh -c \"bash -c 'printenv'\"")
    assert result is not None
    assert "blocked by security policy" in result


# ---------------------------------------------------------------------------
# MIT-164 round 2 — codex review findings
#
# Round 1 of MIT-164 only recognised `<shell> -c <script>` as a wrapper
# shape.  Codex review flagged two residual bypasses:
#
#   P1. `bash -lc`, `bash -ec`, `zsh -ic`, etc. — short-option bundles
#       that combine `-c` with other flags.  All of those shells still
#       execute the following arg as a script, so they reopen the same
#       hole.  Fix: scan for any short-option cluster containing `c`.
#
#   P2. Depth-cap fall-through — returning None at _MAX_SHELL_WRAPPER_DEPTH
#       meant `sh -c "sh -c 'sh -c \"sh -c printenv\"'"` (one level
#       beyond the cap) was allowed.  Fix: fail closed at the cap.
#
# These tests cover the round-2 fixes specifically.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        # Login shell (`-lc`) — common in CI
        "bash -lc 'printenv'",
        'bash -lc "cat /etc/shadow"',
        "bash -lc 'base64 ~/.ssh/id_rsa'",
        # Errexit + -c
        "bash -ec 'printenv'",
        'bash -ec "cat /etc/shadow"',
        # xtrace + -c
        "bash -xc 'printenv'",
        # Interactive + -c
        "zsh -ic 'ssh-add -l'",
        "bash -ic 'printenv'",
        # `c` not last in the bundle — bash still treats next arg as script
        "bash -cl 'printenv'",
        # Multiple stacked flags
        "sh -eic 'printenv'",
        "bash -lxc 'cat ~/.ssh/id_rsa'",
        # Path-prefixed + bundle
        "/bin/bash -lc 'printenv'",
        "/usr/bin/bash -lc \"cat /etc/shadow\"",
    ],
)
def test_mit164r2_flag_bundle_wrapper_is_blocked(command: str) -> None:
    """MIT-164 round 2: `-lc`/`-ec`/`-ic`/`-xc` and friends must recurse into the script."""
    result = check_shell_command(command)
    assert result is not None, f"Expected block for flag-bundle wrapper: {command!r}"
    assert "blocked by security policy" in result


@pytest.mark.parametrize(
    "command",
    [
        # Bundle wrapper with benign inner — must still pass
        "bash -lc 'echo hello'",
        "bash -lc 'git status'",
        "bash -ec 'npm install'",
        "bash -xc 'ls /tmp'",
        # Shell invoked with short options that do NOT include `c` — not a wrapper
        "bash -l",           # login shell, no script
        "bash -x script.sh", # xtrace on a script, no -c
        "sh -s",             # read-from-stdin, no -c
        # Long options — not short-option bundles
        "bash --login",
        "bash --version",
        "bash --help",
        # `--` separator — stops option parsing
        "sh -- foo",
    ],
)
def test_mit164r2_benign_flag_bundle_usage_still_allowed(command: str) -> None:
    """MIT-164 round 2: flag-bundle detection must not false-positive on benign shells."""
    assert check_shell_command(command) is None, f"False positive for: {command!r}"



# ---------------------------------------------------------------------------
# MIT-164 round 3 — codex review iteration 2
#
# Round 2 closed the short-option-bundle bypass (`bash -lc '...'`) but
# still terminated the option scan on long options or value-taking short
# options.  Codex flagged three new shapes that bypass:
#
#   * ``bash --noprofile -c 'printenv'``      (long option before -c)
#   * ``bash -O extglob -c 'cat ~/.ssh/id_rsa'`` (short opt with value)
#   * ``zsh -o no_aliases -c 'env'``          (zsh setopt pair)
#
# The round-3 fix makes the scanner permissive: it walks ALL tokens
# after the shell binary and returns the first token following a
# ``c``-bearing short-option cluster.  Over-unwrapping a non-wrapper
# only risks over-blocking (safe direction); under-unwrapping is a
# security hole.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        # Long option BEFORE -c
        "bash --noprofile -c 'printenv'",
        'bash --noprofile -c "cat /etc/shadow"',
        "bash --norc -c 'printenv'",
        "bash --noprofile --norc -c 'printenv'",
        # Short option WITH VALUE (takes next token) before -c
        "bash -O extglob -c 'printenv'",
        "bash -O extglob -c 'cat ~/.ssh/id_rsa'",
        "zsh -o no_aliases -c 'printenv'",
        "zsh -o no_aliases -c 'env'",
        # --rcfile path pair before -c
        "bash --rcfile /dev/null -c 'printenv'",
        # Combinations of long + bundle
        "bash --rcfile /dev/null -lc 'printenv'",
        "bash --noprofile -lc 'cat /etc/shadow'",
        # Option with value followed by bundle (codex P1 round 2 + round 3 combined)
        "bash -O extglob -lc 'printenv'",
        # Path-prefixed shell + long option
        "/usr/bin/bash --noprofile -c 'printenv'",
        "/bin/bash --norc -c 'ssh-add -l'",
    ],
)
def test_mit164r3_options_before_script_wrapper_is_blocked(command: str) -> None:
    """MIT-164 round 3: long options and value-taking short options BEFORE -c must be transparent."""
    result = check_shell_command(command)
    assert result is not None, f"Expected block for: {command!r}"
    assert "blocked by security policy" in result


@pytest.mark.parametrize(
    "command",
    [
        # Benign inner with options-before — must still pass
        "bash --noprofile -c 'echo hi'",
        "bash -O extglob -c 'git status'",
        "zsh -o no_aliases -c 'pytest'",
        "bash --rcfile /dev/null -c 'make'",
        "bash --norc -c 'ls /tmp'",
        # Options-only, no -c — not a wrapper shape
        "bash -O extglob script.sh",     # value-option + positional, no c-bundle
        "bash --noprofile script.sh",    # long option + positional, no c
        "bash --rcfile /dev/null -i",    # long-option-pair + -i (no c)
        # Shell binaries NOT in our wrapper list — out of scope, allow
        "fish -c 'printenv'",            # fish is not in _SHELL_WRAPPER_BASENAMES
        # Arg-separator usage
        "bash -- script.sh arg1",
    ],
)
def test_mit164r3_benign_options_before_script_still_allowed(command: str) -> None:
    """MIT-164 round 3: benign options-before shapes must not false-positive."""
    assert check_shell_command(command) is None, f"False positive for: {command!r}"


def test_mit164r3_script_is_token_after_first_c_cluster() -> None:
    """The unwrapped script is always the token immediately after the first c-bundle.

    Scanning past arbitrary options is intentional (the permissive direction),
    but when a c-bundle IS found we must not skip past it — the very next
    token is the script, even if more options trail (``bash -lc 'printenv'
    --somearg`` runs `printenv`, not the trailing arg).
    """
    # printenv is the script; --somearg is a positional passed to the
    # resulting shell (as argv[1]).  The prescreen should unwrap `printenv`
    # and block.
    result = check_shell_command("bash -lc 'printenv' --somearg")
    assert result is not None
    assert "blocked by security policy" in result



# ---------------------------------------------------------------------------
# MIT-164 round 4 — codex review iteration 3
#
# Round 3 was too permissive: it unconditionally scanned all tokens for
# a c-cluster, which false-positived on ``bash script.sh -c printenv``
# (script-file mode, where ``-c`` and ``printenv`` are positional args
# to ``script.sh``, not wrapper flags to bash).
#
# Round 4 introduces a structured option-prefix walker with three token
# classes:
#   * script-carrying short cluster (c-bearing) → unwrap
#   * value-taking option (``-O``, ``-o``, ``+o``, ``--rcfile``,
#     ``--init-file``) → skip 2 tokens
#   * any other option-shaped token → skip 1 token
#   * ``--`` separator or bare positional → END scan; not a wrapper
#
# That makes the scanner terminate on positional entry (script-file
# mode) while still transparently skipping legitimate option-value
# pairs before ``-c``.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        # Codex-raised regression cases — MUST NOT unwrap/block.
        # ``bash script.sh -c printenv`` is script-file mode: bash runs
        # script.sh with "-c" and "printenv" as $1 and $2, respectively.
        "bash script.sh -c printenv",
        "bash -- script.sh -c printenv",              # -- ends option parsing
        # Login or xtrace + script-file — still script-file mode
        "bash -l script.sh -c printenv",
        "bash -x script.sh -c printenv",
        # Value-taking option + script-file mode
        "bash -O extglob script.sh -c printenv",
        "bash --rcfile /dev/null script.sh -c printenv",
        # Plain script execution
        "sh script.sh",
        "bash myscript",
        # zsh script-file
        "zsh script.sh -c printenv",
    ],
)
def test_mit164r4_script_file_mode_is_not_a_wrapper(command: str) -> None:
    """Codex-review round 3: ``bash script.sh -c <x>`` must NOT be treated as a wrapper.

    In script-file mode, ``-c`` and anything after it are positional
    arguments passed to the script (``$1``, ``$2``, ...).  The shell
    is not executing them as shell code, so the prescreen must allow
    the command through to its normal regex check (which, for these
    literal strings, does NOT match — ``cat /etc/shadow`` as a
    positional to a user script is the user's problem, not the
    prescreen's concern).
    """
    assert check_shell_command(command) is None, f"False positive (script-file mode): {command!r}"


@pytest.mark.parametrize(
    "command",
    [
        # Value-taking option pair followed by c-cluster — pair is skipped, cluster wins.
        "bash --init-file /dev/null -c 'printenv'",
        "bash --rcfile /dev/null -c 'printenv'",
        "bash --rcfile /dev/null -lc 'printenv'",
        "bash -O extglob -c 'printenv'",
        "bash -O extglob -lc 'printenv'",
        "bash -O extglob -o errexit -c 'printenv'",   # multiple value pairs
        "zsh -o no_aliases -c 'printenv'",
        "zsh +o aliases -c 'printenv'",                # +o is also a value-taker
    ],
)
def test_mit164r4_value_taking_options_skipped_when_c_follows(command: str) -> None:
    """Value-taking option pairs (e.g. ``-O extglob``, ``--rcfile /dev/null``) must be transparently skipped.

    The scanner must jump 2 tokens past a value-taking option so the
    value doesn't falsely terminate the option-prefix scan.  A
    subsequent c-cluster must still unwrap.
    """
    result = check_shell_command(command)
    assert result is not None, f"Expected block (value-taker skipped): {command!r}"
    assert "blocked by security policy" in result


@pytest.mark.parametrize(
    "command",
    [
        # Value-taking option + NO c-cluster — must pass (no wrapper shape).
        "bash -O extglob script.sh",
        "bash -O extglob -O errexit script.sh",
        "zsh -o no_aliases script.sh",
        "bash --rcfile /dev/null -l",                 # long-pair + login flag, no c
        # Benign wrappers + value-taker
        "bash --rcfile /dev/null -c 'echo ok'",
        "bash -O extglob -c 'npm test'",
    ],
)
def test_mit164r4_value_taker_benign_usage_allowed(command: str) -> None:
    """Value-taking options followed by positional scripts or benign wrappers must pass."""
    assert check_shell_command(command) is None, f"False positive: {command!r}"


def test_mit164r4_dashdash_ends_options_before_c() -> None:
    """`bash -- -c printenv` treats `-c` and `printenv` as positionals, not a wrapper.

    After ``--``, option parsing is done; everything is positional.
    The prescreen must NOT descend into a ``-c`` that appears post-``--``.

    Note: we test with ``printenv`` as the literal, not ``cat /etc/shadow``
    — the outer regex denylist matches ``cat /etc/shadow`` anywhere in
    the raw command text (it does not require the wrapper to be unwrapped),
    which is a DIFFERENT layer from the wrapper-detection logic tested
    here.  Using ``printenv`` ensures the only layer that can block is
    the wrapper-detection path; if that path correctly treats ``-- -c``
    as positional-only then ``printenv`` will not be examined as a
    standalone command and no block fires.
    """
    assert check_shell_command("bash -- -c printenv") is None
    # Same invariant with dash: -- stops option parsing.
    assert check_shell_command("dash -- -c printenv") is None



# ---------------------------------------------------------------------------
# MIT-164 round 5 — codex review iteration 4
#
# Codex round 4 flagged two residual bypasses in the wrapper detector:
#
#   P1. Command-prefix bypass:
#       * ``FOO=1 bash -c 'printenv'``     (POSIX env-var assignment)
#       * ``/usr/bin/env bash -c 'printenv'`` (env runner)
#       * combinations thereof
#     The round-4 extractor hard-coded ``tokens[0]`` as the shell, so
#     any legitimate prefix broke detection.  Fix: strip leading
#     ``NAME=value`` assignments and leading ``env`` / ``/usr/bin/env``
#     tokens before looking up the shell binary.
#
#   P2. Missing bash ``+O`` form.  ``-O`` (set shopt) was in the
#       value-taking-option allowlist but ``+O`` (unset shopt) was not.
#       Fix: add ``+O`` alongside ``+o``.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        # P1: POSIX env-var assignment prefixes
        "FOO=1 bash -c 'printenv'",
        "FOO=1 BAR=2 bash -c 'printenv'",
        "PATH=/usr/bin bash -c 'cat ~/.ssh/id_rsa'",
        "HOME=/tmp bash -c 'ssh-add -l'",
        # P1: env-runner prefixes
        "/usr/bin/env bash -c 'printenv'",
        "/bin/env sh -c 'printenv'",
        "/usr/bin/env zsh -c 'base64 ~/.ssh/id_rsa'",
        # P1: env-runner + assignments (either order)
        "/usr/bin/env FOO=1 bash -c 'printenv'",
        "env FOO=1 BAR=2 bash -c 'printenv'",
        "FOO=1 env bash -c 'printenv'",
        "FOO=1 /usr/bin/env bash -c 'printenv'",
        # P2: bash +O form
        "bash +O extglob -c 'printenv'",
        "bash +O no_history -c 'printenv'",
        # P2: +O + existing -O / -o combinations
        "bash -O extglob +O no_history -c 'printenv'",
    ],
)
def test_mit164r5_env_prefix_and_plus_O_wrapper_is_blocked(command: str) -> None:
    """MIT-164 round 5: env-var and /usr/bin/env prefixes + bash ``+O`` must all unwrap."""
    result = check_shell_command(command)
    assert result is not None, f"Expected block for: {command!r}"
    assert "blocked by security policy" in result


@pytest.mark.parametrize(
    "command",
    [
        # Benign env-prefix usage
        "FOO=1 bash -c 'echo hi'",
        "/usr/bin/env bash -c 'npm install'",
        "FOO=1 BAR=baz /usr/bin/env bash -c 'git status'",
        "PATH=/usr/bin bash -c 'git status'",
        # env-prefix + script-file mode (not a wrapper)
        "FOO=1 bash script.sh",
        "/usr/bin/env bash script.sh arg1",
        # env-runner + non-shell target (python, fish, etc.) — not in
        # our wrapper basename set, so we don't unwrap.
        "/usr/bin/env python3 -c 'print(hi)'",
        "/usr/bin/env fish -c 'echo hi'",
        # +O with benign inner
        "bash +O extglob -c 'echo hi'",
        # Just the env-runner calling a shell with no -c (interactive)
        "/usr/bin/env bash -l",
        # Just env-prefix with no shell at all (different command)
        "FOO=1 echo hi",
    ],
)
def test_mit164r5_benign_env_prefix_allowed(command: str) -> None:
    """MIT-164 round 5: benign env-prefix usage must not false-positive."""
    assert check_shell_command(command) is None, f"False positive: {command!r}"


def test_mit164r5_env_token_alone_still_blocks() -> None:
    """Pre-existing: bare `env` at start of command is an env-dumper — still blocked.

    Regression guard: the MIT-164 round-5 wrapper-prefix logic treats
    ``env`` as a runner-prefix only when it's followed by a shell or
    more assignments.  Bare ``env`` alone (``env`` on its own as the
    whole command) is still caught by the outer regex denylist as an
    env-dumper.  This has been true since MIT-123 and must not regress.
    """
    result = check_shell_command("env")
    assert result is not None
    assert "blocked by security policy" in result



# ---------------------------------------------------------------------------
# MIT-164 round 6 — codex review iteration 5
#
# Codex round 5 flagged two issues:
#
#   P1. env's own flags: `/usr/bin/env -i bash -c 'printenv'` and
#       `/usr/bin/env -u HOME sh -c '...'` bypassed because the
#       prefix-stripper didn't understand env's flags (`-i`,
#       `-u NAME`, `-C DIR`, `-S CMD`, `--ignore-environment`, `--`).
#       Fix: after skipping an `env` token, enter env-flag state and
#       skip env's own flags (flag-only and value-taking) until we
#       reach the shell or an assignment.
#
#   P2. Off-by-one on the depth cap.  Three legitimate wrapper layers
#       (e.g. `sh -c "bash -c 'zsh -c \"echo ok\"'"`) were blocked
#       because `_depth >= _MAX_SHELL_WRAPPER_DEPTH` fired on the
#       already-unwrapped innermost non-wrapper (`echo ok`).  Fix:
#       reorder — try extraction first, only consult the cap when
#       there IS another wrapper to peel.  Non-wrapper inners exit
#       cleanly at any depth.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        # env-runner + env's own flags, various shapes
        "/usr/bin/env -i bash -c 'printenv'",
        "/usr/bin/env -u HOME sh -c 'ssh-add -l'",
        "env -i bash -c 'printenv'",
        "env -u FOO bash -c 'printenv'",
        "env -u FOO -u BAR bash -c 'printenv'",
        "env -i -u HOME bash -c 'printenv'",
        "env -C /tmp bash -c 'printenv'",
        "env -0 bash -c 'printenv'",
        "env --ignore-environment bash -c 'printenv'",
        "env --unset=HOME bash -c 'printenv'",
        # env -- terminates env's options, then assignments + shell
        "env -- FOO=1 bash -c 'printenv'",
        # Combined: env with flags + env-vars + shell
        "/usr/bin/env -i FOO=1 bash -c 'printenv'",
        "/usr/bin/env -u HOME FOO=1 bash -c 'printenv'",
        # Path-prefixed env with flags
        "/bin/env -i sh -c 'printenv'",
    ],
)
def test_mit164r6_env_flags_prefix_is_stripped(command: str) -> None:
    """MIT-164 round 6: env's own flags (`-i`, `-u`, `-C`, `--`, etc.) must be skipped."""
    result = check_shell_command(command)
    assert result is not None, f"Expected block for: {command!r}"
    assert "blocked by security policy" in result


@pytest.mark.parametrize(
    "command",
    [
        # Benign env-flag usage — use path-prefixed `/usr/bin/env` so the
        # pre-existing outer env-dumper regex (which anchors at `^env`
        # word-boundary) doesn't fire.  This is a pre-MIT-164 behaviour
        # (bare `env` at line-start is always blocked as an env-dumper)
        # and is out of scope here.
        "/usr/bin/env -i bash -c 'echo hi'",
        "/usr/bin/env -u HOME bash -c 'npm install'",
        "/usr/bin/env -i -u PATH bash -c 'git status'",
        "/usr/bin/env --ignore-environment bash -c 'ls /tmp'",
        # env flags but not followed by a recognised shell
        "/usr/bin/env -i python3 -c 'print(hi)'",
        "/usr/bin/env -i /bin/true",
        # env + -- but no shell after (just a command with assignments)
        "/usr/bin/env -- FOO=1 echo hi",
    ],
)
def test_mit164r6_benign_env_flags_allowed(command: str) -> None:
    """MIT-164 round 6: env-flag prefixes with benign inner must not false-positive.

    Uses path-prefixed `/usr/bin/env` deliberately — bare `env` at line
    start is pre-existing behaviour blocked by the outer regex (since
    MIT-123), and is orthogonal to the wrapper-extractor logic under
    test here.  See ``test_mit164r5_env_token_alone_still_blocks`` for
    the bare-`env` regression guard.
    """
    assert check_shell_command(command) is None, f"False positive: {command!r}"


@pytest.mark.parametrize(
    "command",
    [
        # 3 legitimate wrapper layers with a BENIGN innermost script —
        # must pass.  Previously blocked by off-by-one depth cap.
        'sh -c "bash -c \'zsh -c \\"echo ok\\"\'"',
        "sh -c \"bash -c 'echo hello'\"",
        "bash -c \"sh -c 'git status'\"",
        # 2-level benign nesting — well within the cap.
        "bash -c \"sh -c 'echo hi'\"",
    ],
)
def test_mit164r6_three_level_nesting_with_benign_inner_allowed(command: str) -> None:
    """MIT-164 round 6 (codex P2): 3 wrapper levels with a non-wrapper inner must pass.

    The depth cap is a security fallback for "still another wrapper
    to peel" territory — if the innermost layer is already a plain
    (non-wrapper) script, the cap must not fire regardless of how
    many wrapper layers we peeled to get here.
    """
    assert check_shell_command(command) is None, f"False positive at 3-layer benign: {command!r}"


@pytest.mark.parametrize(
    "command",
    [
        # 3 wrapper layers + denylisted inner — must block via inner regex.
        "sh -c \"bash -c 'zsh -c \\\"printenv\\\"'\"",
        "sh -c \"bash -c 'printenv'\"",
    ],
)
def test_mit164r6_three_level_nesting_with_denylist_inner_blocks(command: str) -> None:
    """Three-level nesting that bottoms out at a denylisted command must still block."""
    result = check_shell_command(command)
    assert result is not None, f"Expected block for: {command!r}"
    assert "blocked by security policy" in result


def test_mit164r6_four_level_nesting_fails_closed_at_cap() -> None:
    """4+ wrapper layers MUST hit the cap and block, regardless of innermost content.

    This is the original fail-closed semantic from round 2: if the
    caller stacks more wrappers than the cap allows AND there is
    still another wrapper to peel at the cap, block.  Even if the
    deepest layer were benign, we cannot see past the cap, so we
    refuse.
    """
    # 4 layers — cap is 3, so this fails at the extraction attempt
    # that would produce the 4th recursive call.
    deep_denylist = "sh -c \"sh -c 'sh -c \\\"sh -c printenv\\\"'\""
    result = check_shell_command(deep_denylist)
    assert result is not None
    assert "blocked by security policy" in result

    # 4 layers with benign innermost — still blocks (we can't see
    # past the cap to know it's benign).
    deep_benign = "sh -c \"sh -c 'sh -c \\\"sh -c echo ok\\\"'\""
    result2 = check_shell_command(deep_benign)
    assert result2 is not None
    assert "blocked by security policy" in result2



# ---------------------------------------------------------------------------
# MIT-164 round 7 — codex review iteration 6
#
# Codex round 6 flagged two more bypasses:
#
#   P1. GNU env -S / --split-string.  `/usr/bin/env -S 'bash -c
#       printenv'` re-splits the argument as if it were the whole
#       command line, so the inner `bash -c printenv` was still
#       executed but my previous env-flag stripper treated -S + value
#       as just another "skip 2 tokens" pair.  Fix: when -S (or the
#       `--split-string=VALUE` equals-form) is seen after env,
#       RETURN the value directly as the inner script.  The outer
#       check_shell_command recursion re-runs the whole prescreen
#       on the re-split payload, so the inner wrapper gets unwrapped
#       and the denylist fires.
#
#   P2. `env --` followed by assignments.  `env --` ends env's OWN
#       options, but the prefix-strip was `break`ing entirely — so
#       `/usr/bin/env -- FOO=1 bash -c '...'` lost the `FOO=1`
#       consumption and tripped the shell-basename check.  Fix:
#       instead of `break`, clear `seen_env` and `continue` so the
#       outer assignment-recogniser branch picks up the `FOO=1`.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        # -S with a denylisted wrapped inner
        "/usr/bin/env -S 'bash -c printenv'",
        "/usr/bin/env -S \"bash -c printenv\"",
        "/usr/bin/env -S 'sh -c printenv'",
        "/usr/bin/env -S 'bash -c \"cat ~/.ssh/id_rsa\"'",
        # -S with a direct denylisted command (no inner wrapper)
        "/usr/bin/env -S printenv",
        # --split-string= (equals form)
        "/usr/bin/env --split-string='bash -c printenv'",
        "/usr/bin/env --split-string='sh -c printenv'",
        # --split-string <value> (space form)
        "/usr/bin/env --split-string 'bash -c printenv'",
        # -S combined with other env flags
        "/usr/bin/env -i -S 'bash -c printenv'",
        "/usr/bin/env -u HOME -S 'bash -c printenv'",
    ],
)
def test_mit164r7_env_split_string_is_unwrapped(command: str) -> None:
    """MIT-164 round 7 (codex P1): env -S / --split-string payload must re-enter the prescreen."""
    result = check_shell_command(command)
    assert result is not None, f"Expected block for env-S: {command!r}"
    assert "blocked by security policy" in result


@pytest.mark.parametrize(
    "command",
    [
        # env -- followed by assignments and a wrapped denylisted command
        "/usr/bin/env -- FOO=1 bash -c 'printenv'",
        "/usr/bin/env -i -- FOO=1 bash -c 'printenv'",
        "/usr/bin/env -u HOME -- FOO=1 BAR=2 bash -c 'cat /etc/shadow'",
        "/usr/bin/env -- bash -c 'printenv'",
        "/usr/bin/env --ignore-environment -- FOO=1 bash -c 'printenv'",
    ],
)
def test_mit164r7_env_dashdash_then_assignments_still_unwraps(command: str) -> None:
    """MIT-164 round 7 (codex P2): `env --` must resume assignment-stripping, not break."""
    result = check_shell_command(command)
    assert result is not None, f"Expected block for env --: {command!r}"
    assert "blocked by security policy" in result


@pytest.mark.parametrize(
    "command",
    [
        # -S with benign payload
        "/usr/bin/env -S 'echo hi'",
        "/usr/bin/env -S 'bash -c echo'",
        "/usr/bin/env --split-string='git status'",
        "/usr/bin/env --split-string='bash -c \"npm install\"'",
        # env -- benign
        "/usr/bin/env -- FOO=1 bash -c 'echo ok'",
        "/usr/bin/env -- FOO=1 echo hi",
        "/usr/bin/env -i -- FOO=1 bash -c 'git status'",
    ],
)
def test_mit164r7_env_flags_benign_payloads_allowed(command: str) -> None:
    """MIT-164 round 7: -S and -- with benign inner payloads must not false-positive."""
    assert check_shell_command(command) is None, f"False positive: {command!r}"



# ---------------------------------------------------------------------------
# MIT-164 round 8 — codex review iteration 7 (GNU env value-takers)
#
# Codex round 7 flagged (P1) that GNU env's `--default-signal SIG`,
# `--block-signal SIG`, `--ignore-signal SIG`, and `-a ARGV0` options
# take values and could stop the prefix stripper.  On empirical
# verification:
#
#   * `-a ARGV0` / `--argv0=ARGV0` — YES, these genuinely take values
#     and a space-separated form is accepted.  Added to
#     `_ENV_FLAGS_TAKING_VALUE`.
#
#   * `--block-signal`, `--default-signal`, `--ignore-signal` — the
#     codex concern was a FALSE POSITIVE.  On actual GNU env, these
#     flags accept their signal argument ONLY in equals form
#     (`--default-signal=PIPE`).  The space-separated form
#     (`--default-signal PIPE bash -c '...'`) is not a valid env
#     invocation — env treats `PIPE` as the command to exec and
#     errors with "PIPE: No such file".  No special handling needed;
#     the equals form is a self-contained token and the generic
#     skip-1 fallback handles it.
#
# The tests below cover both axes.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        # Codex round 7 P1: env -a ARGV0 (space-separated, value-taking)
        "/usr/bin/env -a myarg0 bash -c 'printenv'",
        "/usr/bin/env -a agent-shell bash -c 'cat ~/.ssh/id_rsa'",
        # --argv0=value equals form
        "/usr/bin/env --argv0=myarg0 bash -c 'printenv'",
        # Signal flags in equals form — ARE valid bypass shapes
        "/usr/bin/env --default-signal=PIPE bash -c 'printenv'",
        "/usr/bin/env --block-signal=INT bash -c 'printenv'",
        "/usr/bin/env --ignore-signal=HUP bash -c 'printenv'",
        # -a + equals-form signal combined
        "/usr/bin/env -a myarg0 --default-signal=PIPE bash -c 'printenv'",
    ],
)
def test_mit164r8_gnu_env_value_takers_unwrap(command: str) -> None:
    """MIT-164 round 8: GNU env's -a (ARGV0) and equals-form signal flags must not block the strip."""
    result = check_shell_command(command)
    assert result is not None, f"Expected block: {command!r}"
    assert "blocked by security policy" in result


def test_mit164r8_space_separated_signal_flags_documented() -> None:
    """Documentation test: `/usr/bin/env --default-signal PIPE bash -c '...'` is NOT a bypass.

    Contrary to the codex-round-7 concern, GNU env's signal flags only
    accept the equals form.  `env --default-signal PIPE bash -c X` is
    not a valid invocation — env treats `PIPE` as the command to exec
    and errors with "PIPE: No such file".  Therefore the prescreen's
    current behaviour (treat `--default-signal` as a plain long-option
    skip-1, hit `PIPE` as a positional, halt the strip, return None)
    is SAFE: env errors at runtime before the inner `bash -c '...'`
    runs, so there's nothing to block.

    This test is a canary: if GNU env ever changes to accept the
    space-separated form (or if BSD env does), the prescreen will need
    to be revisited.  For now, the expected behaviour is that the
    space-separated form falls through as `None`.
    """
    assert check_shell_command(
        "/usr/bin/env --default-signal PIPE bash -c 'printenv'"
    ) is None


@pytest.mark.parametrize(
    "command",
    [
        "/usr/bin/env -a myarg0 bash -c 'echo hi'",
        "/usr/bin/env --argv0=myarg0 bash -c 'git status'",
        "/usr/bin/env --default-signal=PIPE bash -c 'npm install'",
    ],
)
def test_mit164r8_gnu_env_value_takers_benign_allowed(command: str) -> None:
    """MIT-164 round 8: -a / --argv0 / equals-signal with benign payloads must pass."""
    assert check_shell_command(command) is None, f"False positive: {command!r}"



# ---------------------------------------------------------------------------
# MIT-164 round 9 — codex review iteration 8 (env -S argv concatenation)
#
# Codex round 8 flagged (P1) that GNU env's `-S PAYLOAD` re-splits
# PAYLOAD as if it were a command line AND APPENDS the trailing argv
# tokens to that split, forming the final command.  So:
#
#     /usr/bin/env -S 'bash -c' 'printenv'   → actually runs `bash -c printenv`
#     /usr/bin/env -S bash -c printenv        → actually runs `bash -c printenv`
#
# The round-7 fix returned just the PAYLOAD token, dropping the
# trailing argv.  That missed the denylisted inner because `bash -c`
# alone does not match any regex and its unwrap attempt saw no script.
#
# Round 9 fix: concatenate payload + shlex-quoted trailing argv before
# recursing.  A new `_join_env_split_payload` helper does the join so
# the downstream `shlex.split` in `_extract_shell_wrapper_inner`
# tokenises identically to what env would have executed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        # Codex-specific bypass shapes
        "/usr/bin/env -S 'bash -c' 'printenv'",
        "/usr/bin/env -S bash -c printenv",
        "/usr/bin/env -S 'bash' -c 'printenv'",
        "/usr/bin/env -S 'bash -c' 'cat /etc/shadow'",
        # --split-string= equals form with trailing argv
        "/usr/bin/env --split-string='bash -c' 'printenv'",
        "/usr/bin/env --split-string='bash' -c 'printenv'",
        "/usr/bin/env --split-string=bash -c 'printenv'",
        # -S with other env flags + trailing argv
        "/usr/bin/env -i -S 'bash -c' 'printenv'",
        "/usr/bin/env -u HOME -S bash -c printenv",
        # -S with even more trailing args (env appends all of them)
        "/usr/bin/env -S 'bash' -c printenv",
    ],
)
def test_mit164r9_env_S_concatenates_trailing_argv(command: str) -> None:
    """MIT-164 round 9: env -S PAYLOAD must be recombined with trailing argv.

    GNU env appends post-`-S` argv tokens to the payload before
    executing.  The prescreen must reconstruct the full command
    or it misses denylisted innermost calls.
    """
    result = check_shell_command(command)
    assert result is not None, f"Expected block: {command!r}"
    assert "blocked by security policy" in result


@pytest.mark.parametrize(
    "command",
    [
        "/usr/bin/env -S 'bash -c' 'echo hi'",
        "/usr/bin/env -S bash -c 'echo hello'",
        "/usr/bin/env --split-string='bash -c' 'echo hi'",
        "/usr/bin/env -S 'bash' -c 'ls /tmp'",
    ],
)
def test_mit164r9_env_S_benign_trailing_argv_allowed(command: str) -> None:
    """MIT-164 round 9: env -S + trailing argv with benign inner must pass."""
    assert check_shell_command(command) is None, f"False positive: {command!r}"
