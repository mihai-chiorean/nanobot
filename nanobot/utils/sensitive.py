"""Sensitive data detection, path blocking, and content redaction.

This module provides the shared security layer used by both the filesystem
tools (read_file, edit_file) and the shell tool (exec) to prevent leakage
of private keys, credentials, tokens, and other secrets.
"""

import os
import re
import shlex
from pathlib import Path

from loguru import logger

# ---------------------------------------------------------------------------
# 1. Sensitive path detection
# ---------------------------------------------------------------------------

# Directories / files that must never be read or targeted by commands.
# Paths are checked after expanding ~ and resolving symlinks.
_SENSITIVE_PATH_PATTERNS: list[str] = [
    # SSH key material
    "/.ssh/",
    "/etc/ssh/ssh_host_",
    # System credential stores
    "/etc/shadow",
    "/etc/gshadow",
    "/etc/security/opasswd",
    # GNOME / KDE keyrings
    "/.local/share/keyrings/",
    "/.kde/share/apps/kwallet/",
    # GPG private keyring
    "/.gnupg/private-keys-v1.d/",
    "/.gnupg/secring.gpg",
    # Cloud credential caches
    "/.aws/credentials",
    "/.aws/config",
    "/.azure/",
    "/.config/gcloud/credentials.db",
    "/.config/gcloud/application_default_credentials.json",
    # Docker config (may contain registry passwords)
    "/.docker/config.json",
    # Kubernetes
    "/.kube/config",
    # Password / secret files
    "/.netrc",
    "/.pgpass",
    "/.my.cnf",
]

# File name patterns that are sensitive regardless of directory
#
# Note on `.env` scope: the pattern below matches files whose basename begins
# with `.env` (e.g. `.env`, `.env.local`, `.env.production`).  We intentionally
# do NOT widen this to `*.env` — that would collide with legitimate filenames
# like `example.env`, `template.env`, or documentation fixtures where
# disclosure is safe.  Operators who place dotenv files under non-standard
# names (e.g. `secrets/app.env`) should rely on path-based blocks (by adding
# `/secrets/` to `_SENSITIVE_PATH_PATTERNS`) rather than a broad filename
# catch-all.
#
# Note on SSH key filename scope (MIT-140): the `id_(rsa|dsa|ecdsa|ed25519)`
# pattern intentionally excludes the `.pub` suffix. Public keys are, by
# definition, safe to disclose — agent workflows legitimately need to read
# `id_*.pub` to configure deployment targets, authorized_keys, CI runners,
# etc. Private-key material is still blocked by this filename rule (for the
# bare names) and, more importantly, by the `/.ssh/` path prefix in
# `_SENSITIVE_PATH_PATTERNS` (which catches any file — public or private —
# living under `~/.ssh/`).
_SENSITIVE_FILENAME_PATTERNS: list[re.Pattern] = [
    re.compile(r"(^|/)\.env(\..+)?$", re.IGNORECASE),            # .env, .env.local, .env.production
    re.compile(r"(^|/)credentials\.json$", re.IGNORECASE),
    re.compile(r"(^|/)service[-_]?account[-_]?key\.json$", re.IGNORECASE),
    re.compile(r"(^|/)id_(rsa|dsa|ecdsa|ed25519)$"),               # SSH private key files by name (MIT-140: exclude .pub)
    re.compile(r"(^|/).*\.pem$", re.IGNORECASE),
    re.compile(r"(^|/).*\.key$", re.IGNORECASE),                   # TLS private keys
]


def is_sensitive_path(path: str | Path) -> bool:
    """Return True if *path* points to a known sensitive location or file.

    The check is intentionally broad — it is better to block a false-positive
    than to leak a private key.
    """
    try:
        resolved = str(Path(path).expanduser().resolve())
    except Exception:
        resolved = str(path)

    # Absolute directory / prefix checks
    for pattern in _SENSITIVE_PATH_PATTERNS:
        if pattern in resolved:
            return True

    # Basename / filename checks
    for regex in _SENSITIVE_FILENAME_PATTERNS:
        if regex.search(resolved):
            return True

    return False


# ---------------------------------------------------------------------------
# 2. Content-level secret detection & redaction
# ---------------------------------------------------------------------------

# Regex patterns that match secret material inside file / command output.
_SECRET_CONTENT_PATTERNS: list[tuple[re.Pattern, str]] = [
    # PEM-encoded private keys (RSA, EC, DSA, OPENSSH, PKCS8, etc.)
    (re.compile(r"-----BEGIN\s+[\w\s]*PRIVATE\s+KEY-----", re.IGNORECASE), "private key"),
    # Certificates (optional — certificates are less secret, but the request asked for them)
    (re.compile(r"-----BEGIN\s+CERTIFICATE-----", re.IGNORECASE), "certificate"),
    # AWS-style keys
    (re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}", re.IGNORECASE), "AWS access key"),
    # Generic long hex/base64 tokens preceded by common labels
    (re.compile(
        r"""(?:api[_-]?key|api[_-]?secret|secret[_-]?key|access[_-]?token|auth[_-]?token|"""
        r"""password|passwd|bearer)\s*[:=]\s*['"]?[A-Za-z0-9_\-/.+]{20,}""",
        re.IGNORECASE,
    ), "credential/token"),
    # HTTP Authorization Bearer header (MIT-148).
    # The labeled-credential regex above matches `bearer=xyz` / `bearer: xyz`
    # forms, but NOT the actual HTTP header shape where `Bearer` is the
    # *prefix* of the token (not a label followed by `=` or `:`). Added as a
    # parallel pattern rather than broadening the labeled regex above —
    # widening that one would catch far too much ordinary prose.
    #
    # Case-insensitive: RFC 7235 says auth-scheme names are case-insensitive,
    # and middleware / log formatters freely normalize between `Bearer`,
    # `bearer`, and `BEARER`. Charset matches the labeled-credential regex
    # above (`[A-Za-z0-9_\-/.+]`) so opaque base64 tokens with `/` and `+`
    # aren't missed. Minimum length of 20 filters out short placeholders
    # (`Bearer token`, `Bearer xxx`, `Bearer TODO`); longer token-shaped
    # placeholders like `Bearer YOUR_DEVELOPMENT_ACCESS_TOKEN` may still
    # match. Accepted false-positive cost — redacting a stray placeholder
    # is cheaper than leaking a real token.
    (re.compile(r"\bBearer\s+[A-Za-z0-9_\-/.+]{20,}", re.IGNORECASE), "bearer token"),
    # GitHub / GitLab / npm tokens
    (re.compile(r"(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}"), "GitHub token"),
    (re.compile(r"glpat-[A-Za-z0-9\-_]{20,}"), "GitLab token"),
    (re.compile(r"npm_[A-Za-z0-9]{36,}"), "npm token"),
    # Slack tokens
    (re.compile(r"xox[bpras]-[A-Za-z0-9\-]{10,}"), "Slack token"),
    # Generic "PRIVATE KEY" blob (catches partial dumps)
    (re.compile(r"-----BEGIN\s+RSA", re.IGNORECASE), "RSA key material"),
    (re.compile(r"-----BEGIN\s+EC", re.IGNORECASE), "EC key material"),
    (re.compile(r"-----BEGIN\s+DSA", re.IGNORECASE), "DSA key material"),
    (re.compile(r"-----BEGIN\s+OPENSSH", re.IGNORECASE), "OpenSSH key material"),
]

_REDACTION_NOTICE = (
    "[REDACTED — sensitive content detected ({detail}). "
    "Displaying secrets is blocked by security policy.]"
)


def scan_content(text: str) -> str | None:
    """Scan *text* for secret material.

    Returns a human-readable description of the first match, or ``None``
    if the content appears clean.
    """
    for pattern, label in _SECRET_CONTENT_PATTERNS:
        if pattern.search(text):
            return label
    return None


def redact_if_sensitive(text: str) -> str:
    """Return *text* unchanged if clean, or a redacted message otherwise."""
    match = scan_content(text)
    if match:
        logger.warning("Redacted output containing: {}", match)
        return _REDACTION_NOTICE.format(detail=match)
    return text


# ---------------------------------------------------------------------------
# 3. Shell command pre-screening
# ---------------------------------------------------------------------------

# Reusable fragment: optional path prefix ending in `/` that precedes `.ssh/`.
# Matches `~/`, `./`, `/home/user/`, `/root/`, or empty (bare `.ssh/`).
# Shape: `(?:~/|\S*/)?\.ssh/`
#   - `~/`          — explicit home-relative
#   - `\S*/`        — any non-space run ending in `/` (absolute, relative, etc.)
#   - `?`           — or no prefix at all (just `.ssh/`)
_SSH_PATH_PREFIX = r"(?:~/|\S*/)?\.ssh/"

# Commands whose primary purpose is to dump environment / secrets.
_BLOCKED_SHELL_COMMANDS: list[re.Pattern] = [
    # env-dumping commands (standalone or at start of pipe)
    re.compile(r"(?:^|\|)\s*(?:printenv|/usr/bin/printenv)\b"),
    re.compile(r"(?:^|\|)\s*\benv\b(?!\s+\S+\s*=)"),   # bare 'env' but not 'env VAR=val cmd'
    re.compile(r"(?:^|\|)\s*\bexport\s+-p\b"),
    re.compile(r"(?:^|\|)\s*\bset\s*$"),                 # bare 'set' dumps shell vars
    re.compile(r"(?:^|\|)\s*\bdeclare\s+-x\b"),
    # Direct reads of sensitive paths (absolute, ~, or relative)
    re.compile(r"\bcat\s+" + _SSH_PATH_PREFIX, re.IGNORECASE),
    re.compile(r"\bcat\s+/etc/shadow\b", re.IGNORECASE),
    re.compile(r"\bcat\s+.*\.env\b", re.IGNORECASE),
    re.compile(r"\bcat\s+.*\.pem\b", re.IGNORECASE),
    re.compile(r"\bcat\s+.*\.key\b", re.IGNORECASE),
    re.compile(r"\bcat\s+.*/credentials\.json\b", re.IGNORECASE),
    # Reading sensitive dirs with other tools
    re.compile(r"\b(?:less|more|head|tail|bat|nano|vim?|view)\s+" + _SSH_PATH_PREFIX, re.IGNORECASE),
    re.compile(r"\b(?:less|more|head|tail|bat|nano|vim?|view)\s+/etc/shadow\b", re.IGNORECASE),
    # Key scanning / dumping
    re.compile(r"\bssh-add\s+-[lL]\b"),
    re.compile(r"\bgpg\s+--export-secret", re.IGNORECASE),
    # Base64 encoding of key files (exfiltration attempt)
    re.compile(r"\bbase64\s+" + _SSH_PATH_PREFIX, re.IGNORECASE),
    re.compile(r"\bbase64\s+.*\.pem\b", re.IGNORECASE),
    re.compile(r"\bbase64\s+.*\.key\b", re.IGNORECASE),
    # xxd / od / hexdump on key files
    re.compile(r"\b(?:xxd|od|hexdump)\s+" + _SSH_PATH_PREFIX, re.IGNORECASE),
]


# Shell-wrapper names that take `-c <script>` and execute the script argument
# as a new shell command. Basename match against the first token of the
# command (after stripping any directory prefix like `/bin/` or `/usr/bin/`).
#
# MIT-164: `sh -c "printenv"` escaped the regex-based prescreen because the
# denylisted command was wrapped inside a quoted argument the regexes never
# saw. Detecting the wrapper shape and recursing into the extracted script
# closes that bypass without reworking the regex layer.
_SHELL_WRAPPER_BASENAMES: frozenset[str] = frozenset(
    {"sh", "bash", "zsh", "dash", "ash", "ksh"}
)

# Recursion depth cap for nested wrappers (`sh -c "bash -c '...'"`).  Three
# is well beyond anything seen in practice.  Reaching the cap causes
# `check_shell_command` to FAIL CLOSED (block) — see the comment in that
# function for the security rationale.  Widening the cap is fine for
# ergonomics, but the fail-closed semantic at the cap is load-bearing:
# without it, an attacker can trivially bypass the whole recursion by
# stacking one more wrapper than the cap.
_MAX_SHELL_WRAPPER_DEPTH = 3


def _is_script_carrying_option(token: str) -> bool:
    """Return True iff *token* is a short-option cluster containing ``c``.

    The bash/sh/zsh/dash/ash/ksh family all parse short options as a
    cluster after a single ``-`` (e.g. ``-lc`` == ``-l`` + ``-c``).  For
    all of them, once ``c`` appears in the cluster the NEXT argv slot is
    consumed as the script body.  Recognised shapes:

    * ``-c`` (canonical)
    * ``-lc``, ``-cl``, ``-ec``, ``-xc``, ``-ic``, ``-eic``, ``-lxc``, …
      (cluster with ``c`` anywhere among the letters)

    Deliberately excluded (NOT script-carrying, must return False):

    * Long options: ``--login``, ``--noprofile``, ``--rcfile``, ``--``
      — use ``tok.startswith("--")``.  Long options never introduce a
      shell script; bash has no ``--command=`` form (verified: bash
      rejects ``--command=echo hi`` with "invalid option").
    * Other short options that take a value: ``-O extglob`` (bash shopt),
      ``-o no_aliases`` (zsh setopt), ``-D`` (dump strings).  None of
      these contain the letter ``c``, so "cluster contains c" correctly
      excludes them.
    * Non-letter shapes (``-123``, ``-`` alone).

    This predicate is load-bearing for the MIT-164 fix — anything it
    excludes must genuinely NOT carry a script, or we reopen the bypass.
    """
    if not token.startswith("-"):
        return False
    if token == "-" or token == "--" or token.startswith("--"):
        return False
    letters = token[1:]
    # Short-option clusters are alphabetic in POSIX + bash + zsh.  If the
    # token is e.g. `-123` or `-O2`, it's a numeric/value-style option
    # and does not carry a script.
    if not letters.isalpha():
        return False
    return "c" in letters


def _extract_shell_wrapper_inner(command: str) -> str | None:
    """If *command* runs a shell with an inline script argument, return the script.

    Handles arbitrary orderings of shell options before the script-
    carrying flag.  All of the following are recognised:

    * ``sh -c 'printenv'``                          — canonical
    * ``bash -lc "printenv"``                       — flag bundle
    * ``bash -cl 'printenv'``                       — ``c`` first in bundle
    * ``bash --noprofile -c 'printenv'``            — long option BEFORE -c
    * ``bash -O extglob -c 'printenv'``             — short option with value
    * ``zsh -o no_aliases -c 'env'``                — zsh setopt pair
    * ``bash --rcfile /dev/null -lc 'printenv'``    — several pre-options + bundle
    * ``/usr/bin/bash -lc "cat /etc/shadow"``       — path-prefixed shell

    The script is always the token immediately AFTER the first
    ``c``-bearing short-option cluster.  Everything before that cluster
    is treated as an opaque option prefix and skipped over — we do not
    try to model each shell's option grammar, we just look for the ``c``
    token.  This is intentionally permissive: over-unwrapping a non-
    wrapper shape only risks over-blocking (the inner regex check runs
    again on a best-guess inner string), which is the safe direction
    for a security prescreen.

    Uses :func:`shlex.split` so quoting forms (``'..'``, ``".."``,
    unquoted single-token) are all normalised uniformly.

    Returns ``None`` when the command does not look like a recognised
    wrapper, or when :mod:`shlex` cannot parse it (malformed quoting).
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        # Unclosed quotes, etc. — don't attempt inner extraction; the outer
        # regex check has already run and is the authoritative result for
        # malformed input.  Returning None here means the caller falls
        # through to "clean" exactly as before this change — no regression.
        return None

    if len(tokens) < 3:
        return None

    # First token: the shell binary, possibly path-prefixed.
    shell_basename = os.path.basename(tokens[0])
    if shell_basename not in _SHELL_WRAPPER_BASENAMES:
        return None

    # Scan ALL tokens after the shell binary (no early-termination on
    # positionals or long options — those can legitimately appear before
    # ``-c``, e.g. ``bash --noprofile -c '...'`` or ``bash -O extglob -c
    # '...'``).  Take the token AFTER the first ``c``-bearing short-option
    # cluster as the script.  If we never find one, the command is not a
    # wrapper shape.
    #
    # NOTE: scanning past arbitrary options is the security-safe direction.
    # The alternative (bail on the first unrecognised token) is what
    # codex-review round 2 flagged as bypassable via ``bash --noprofile
    # -c '...'``.  Over-unwrapping a non-wrapper command just means we
    # run the regex pass a second time on a best-guess inner string,
    # which is harmless; under-unwrapping is a security hole.
    for idx in range(1, len(tokens) - 1):
        if _is_script_carrying_option(tokens[idx]):
            return tokens[idx + 1]
    return None


def check_shell_command(command: str, _depth: int = 0) -> str | None:
    r"""Screen a shell command for attempts to access sensitive data.

    Returns an error string if the command is blocked, or ``None`` if it
    passes the check.

    MIT-164: if *command* is a shell-wrapper invocation of the form
    ``sh -c "<inner>"`` (or ``bash``/``zsh``/``dash``/``ash``/``ksh``,
    with or without a path prefix), the inner script is extracted via
    :func:`shlex.split` and recursively screened.  This closes a whole-layer
    bypass where any denylisted command could be run simply by wrapping it:
    the outer regex layer never saw the denylisted command because it was
    inside a quoted argument.

    Known remaining gap (tracked separately — MIT-165 scope): the env-dumper
    regexes (``printenv``/``env``/``export -p``/``set``/``declare -x``)
    anchor at ``(?:^|\|)`` — start-of-string or after a pipe — so a command
    like ``sh -c 'cd /tmp && printenv'`` unwraps to ``cd /tmp && printenv``
    and passes the regex layer because the dumper is after ``&&``, not
    after ``^`` or ``|``.  Widening the anchor set (``;``, ``&&``, ``||``,
    subshell openers) is the MIT-165 fix; MIT-164 is scoped to closing the
    quote-wrapper layer.
    """
    # 1. Regex denylist on the literal command text.
    for pattern in _BLOCKED_SHELL_COMMANDS:
        if pattern.search(command):
            logger.warning("Blocked sensitive shell command: {}", command)
            return (
                "Error: Command blocked by security policy — "
                "accessing sensitive data (keys, credentials, secrets) is not permitted."
            )

    # 2. MIT-164: shell-wrapper unwrap + recurse.  `_depth` is a runtime
    # bound on pathological nesting like `sh -c "bash -c 'sh -c ...'"`.
    # In practice depth > 1 is extremely rare; legitimate depth > 3 has
    # never been observed.  At the cap we FAIL CLOSED: if the caller is
    # trying to stack more than _MAX_SHELL_WRAPPER_DEPTH wrappers, that
    # is itself strong evidence of an evasion attempt, and letting it
    # through would reopen the bypass (the innermost script might be
    # denylisted — we just stopped looking).  Return the same block
    # string the regex layer would use.
    if _depth >= _MAX_SHELL_WRAPPER_DEPTH:
        # Only reachable via the `check_shell_command(inner, _depth + 1)`
        # tail call below — so `command` here is an already-unwrapped
        # inner script.  A depth-cap block doubles as a canary: if this
        # ever fires on legitimate traffic the cap should be revisited,
        # not the fail-closed semantic.
        logger.warning(
            "Blocked shell command at max wrapper depth "
            "({} levels of sh/bash/zsh/dash/ash/ksh -c nesting): {}",
            _MAX_SHELL_WRAPPER_DEPTH,
            command,
        )
        return (
            "Error: Command blocked by security policy — "
            "accessing sensitive data (keys, credentials, secrets) is not permitted."
        )

    inner = _extract_shell_wrapper_inner(command)
    if inner is not None:
        return check_shell_command(inner, _depth + 1)

    return None
