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
#
# Match strategy: basename.  `/bin/sh`, `/usr/bin/bash`, `./bash`, and plain
# `bash` are all treated as the same shell because we cannot distinguish
# a real shell from a user-supplied executable with the same basename at
# prescreen time.  Codex review (round 7) noted this can over-block a
# repo-local helper named `./bash` or `./sh` — documented and accepted:
# the prescreen's stance throughout `_BLOCKED_SHELL_COMMANDS` is already
# basename-keyed (e.g. `\bcat\s+...` matches `cat`, `./cat`, `/usr/bin/cat`
# alike), and a security prescreen is allowed to over-block edge cases
# in exchange for closing the bypass.  Callers with legitimate workspace-
# local shells-named-`bash` binaries should invoke them with an explicit
# non-wrapper form (positional script) which the stripper correctly
# classifies as script-file mode and leaves alone.
_SHELL_WRAPPER_BASENAMES: frozenset[str] = frozenset(
    {"sh", "bash", "zsh", "dash", "ash", "ksh"}
)

# Recursion depth cap for nested wrappers (`sh -c "bash -c '...'"`).  Three
# is well beyond anything seen in practice.  Reaching the cap while there
# is STILL another wrapper to peel causes `check_shell_command` to FAIL
# CLOSED — see the function for rationale.  A non-wrapper inner script at
# any depth is allowed regardless of the cap (codex-review-round-5 P2:
# the cap applies only to wrapper-to-wrapper transitions, not to already-
# unwrapped inner scripts).
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
    # Accept both `-` and `+` option prefixes.  POSIX / bash / zsh all
    # treat `+<letters>` as "turn off option(s)" in the same cluster
    # grammar as `-<letters>` ("turn on").  `+c` isn't a documented
    # shell option but `+n`/`+i`/`+x` are (bash/zsh), and any of them
    # can legitimately appear before `-c` in a wrapper invocation —
    # codex-round-13 P1.  Recognising `+X...` as option-shaped here
    # ensures the scanner skips those tokens instead of treating them
    # as positional script-file-mode triggers.
    if not (token.startswith("-") or token.startswith("+")):
        return False
    if token in ("-", "+", "--", "++"):
        return False
    if token.startswith("--") or token.startswith("++"):
        return False
    letters = token[1:]
    # Short-option clusters are alphabetic in POSIX + bash + zsh.  If the
    # token is e.g. `-123` or `-O2`, it's a numeric/value-style option
    # and does not carry a script.
    if not letters.isalpha():
        return False
    return "c" in letters


# Short-option tokens (exact match, no cluster) that take the NEXT argv
# slot as their VALUE (not a positional).  Occurs before ``-c`` in
# invocations like ``bash -O extglob -c '...'`` or ``zsh -o no_aliases
# -c '...'``.  We need to skip past the value when scanning for the
# script-carrying option so that `extglob` / `no_aliases` don't falsely
# terminate the scan.  Kept deliberately small — adding a shape here
# means "skip the following token" during the option scan, which can
# in principle hide a script-carrying cluster.  Only short-option
# tokens that are BOTH (a) value-taking in at least one of our recognised
# shells AND (b) do not themselves contain ``c`` belong here.
_SHORT_OPTS_TAKING_VALUE: frozenset[str] = frozenset({
    "-O",   # bash --shopt setting
    "+O",   # bash --shopt-off setting (mirror of -O)
    "-o",   # sh/bash/zsh setopt name (`set -o <name>` family)
    "+o",   # zsh / bash setopt-off
})

# env(1) flags that take a VALUE as the next argv slot.  Occurs in
# prefixes like ``env -u HOME bash -c '...'`` or ``/usr/bin/env -C /tmp
# sh -c '...'``.  The equivalent ``--option=value`` forms are self-
# contained single tokens and handled by the generic ``-*``/``--*``
# skip rule — only the space-separated forms need special handling.
# Covers GNU env's full flag surface (BSD env is a strict subset):
#   * ``-u NAME``  / ``--unset=NAME``           — remove NAME from env
#   * ``-C DIR``   / ``--chdir=DIR``            — chdir before exec
#   * ``-S CMD``   / ``--split-string=CMD``     — GNU env's multi-arg split
#   * ``-a ARGV0`` / ``--argv0=ARGV0``          — override argv[0]
# GNU env signal flags (``--block-signal``, ``--default-signal``,
# ``--ignore-signal``) only accept equals form; the space-separated
# form is a no-op (env exec's the following token as the command and
# errors on non-signal-name values).  Equals forms are self-contained
# and handled by the generic skip-1 rule.
# `-i` / `-0` / `-v` / `--help` / `--version` / `--null` do NOT take a
# value and are handled by the generic skip-1 rule.
_ENV_FLAGS_TAKING_VALUE: frozenset[str] = frozenset({
    "-u", "--unset",
    "-C", "--chdir",
    "-S", "--split-string",
    "-a", "--argv0",
})
# Signal flags (``--block-signal``, ``--default-signal``,
# ``--ignore-signal``) are NOT listed here.  Counter-intuitively, GNU env
# only accepts them in equals form (``--default-signal=PIPE``): the
# space-separated form ``--default-signal PIPE bash ...`` is not an env
# option — env treats `PIPE` as the command to exec and errors out.
# Equals forms are single tokens and are correctly skipped by the
# generic ``-*``-fallback branch.

# Long options (exact match) that consume the NEXT argv slot as their
# value.  Same skip-next-token semantic as ``_SHORT_OPTS_TAKING_VALUE``.
# The ``--option=value`` form is handled separately — it's a single
# token so no skip is needed.  Only enumerate the ones commonly seen
# before ``-c`` in wrapper-adjacent invocations; anything we miss here
# merely causes early-termination of the scan on an option-value pair,
# which is a false-NEGATIVE on some wrapper shapes.  Mitigation: if
# this list turns out to be too narrow, add the missing option.
_LONG_OPTS_TAKING_VALUE: frozenset[str] = frozenset({
    "--rcfile",      # bash: alternate startup file
    "--init-file",   # bash: same as --rcfile
    "--emulate",     # zsh: emulation mode (sh/ksh/csh) — closes
                     # codex-round-9 P1 bypass `zsh --emulate sh -c '...'`
})


# Regex for a POSIX shell-style assignment token: `NAME=value`, where
# `NAME` follows POSIX identifier rules (letter or underscore, then
# letters / digits / underscores).  `value` can be empty or anything
# (shlex has already handled quoting).  Used to skip over assignment
# prefixes before the shell binary: `FOO=1 BAR=2 bash -c '...'`.
_ASSIGNMENT_TOKEN_RE: re.Pattern = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Broader regex for env(1)-style assignments: env accepts ANY `NAME=value`
# where NAME has no `=` and no whitespace — including names that are NOT
# valid POSIX shell identifiers (e.g. ``X-Y=1`` or ``A.B=2``).  Used
# inside the env-prefix state (pre-`--`) to close the codex-round-9 P1
# bypass (``/usr/bin/env 'X-Y=1' bash -c '...'``).  Excludes leading
# ``-`` so option-looking tokens aren't mistaken for assignments.
_ENV_ASSIGNMENT_TOKEN_RE: re.Pattern = re.compile(r"^[^-\s=][^\s=]*=")

# Even looser: POST-`env --` assignments.  GNU env treats tokens after
# `--` as strictly positional, and any `NAME=value` is accepted
# regardless of whether NAME starts with ``-`` — so
# ``/usr/bin/env -- '-X=1' bash -c '...'`` is a valid env invocation.
# This regex drops the leading-``-`` exclusion.  Used ONLY when the
# `past_env_dashdash` flag is True; otherwise we stick with the
# pre-`--` regex so we don't over-consume option-looking tokens.
_ENV_POST_DASHDASH_ASSIGNMENT_TOKEN_RE: re.Pattern = re.compile(r"^[^\s=]+=")


def _join_env_split_payload(payload: str, trailing: list[str]) -> str:
    """Concatenate an `env -S PAYLOAD` split-string with its trailing argv.

    GNU env's `-S PAYLOAD` (aka `--split-string=PAYLOAD`) re-splits
    PAYLOAD as if it were a separate command line, then APPENDS the
    remaining argv tokens as additional arguments.  So:

        env -S 'bash -c' 'printenv'
          → bash -c printenv          (payload: `bash -c`, trailing: `printenv`)

        env -S bash -c printenv
          → bash -c printenv          (payload: `bash`, trailing: `-c`, `printenv`)

        env --split-string='bash -c printenv'
          → bash -c printenv          (payload carries the whole command, no trailing)

    The prescreen needs to see the full reconstructed command in order
    to re-screen it via `check_shell_command` recursion.  We shlex-quote
    each trailing token so the re-split by `check_shell_command` yields
    identical tokenisation.
    """
    if not trailing:
        return payload
    parts = [payload] + [shlex.quote(t) for t in trailing]
    return " ".join(parts)


def _is_assignment_token(
    token: str,
    allow_env_style: bool = False,
    allow_post_dashdash_env: bool = False,
) -> bool:
    """Return True iff *token* looks like a `NAME=value` assignment.

    Three modes of increasing permissiveness:

    * POSIX (default): NAME matches shell identifier rules —
      `[A-Za-z_][A-Za-z0-9_]*`.  Used to strip pre-shell assignments
      like `FOO=1 BAR=2 bash -c '...'`.

    * env-style (``allow_env_style=True``): NAME is any non-whitespace,
      non-``=``, non-``-`` sequence.  env(1) accepts arbitrary names
      including shell-invalid ones (``X-Y=1``, ``A.B=2``); enabled
      inside the env-prefix state pre-`--`.

    * post-dashdash env-style (``allow_post_dashdash_env=True``): NAME
      is any non-whitespace, non-``=`` sequence INCLUDING ones that
      start with ``-``.  After ``env --``, env stops parsing options
      and accepts everything as positional — so ``-X=1`` is a valid
      assignment in that position.  Closes codex-round-12 P1:
      ``/usr/bin/env -- '-X=1' bash -c '...'``.

    Each mode supersedes the previous; the most permissive mode that
    is enabled determines acceptance.
    """
    if _ASSIGNMENT_TOKEN_RE.match(token):
        return True
    if allow_env_style and _ENV_ASSIGNMENT_TOKEN_RE.match(token):
        return True
    if allow_post_dashdash_env and _ENV_POST_DASHDASH_ASSIGNMENT_TOKEN_RE.match(token):
        return True
    return False


def _extract_shell_wrapper_inner(command: str) -> str | None:
    """If *command* runs a shell with an inline script argument, return the script.

    Handles arbitrary orderings of shell options before the script-
    carrying flag.  All of the following are recognised:

    * ``sh -c 'printenv'``                          — canonical
    * ``bash -lc "printenv"``                       — flag bundle
    * ``bash -cl 'printenv'``                       — ``c`` first in bundle
    * ``bash --noprofile -c 'printenv'``            — long option BEFORE -c
    * ``bash -O extglob -c 'printenv'``             — short option with value
    * ``bash +O extglob -c 'printenv'``             — bash shopt-off variant
    * ``zsh -o no_aliases -c 'env'``                — zsh setopt pair
    * ``bash --rcfile /dev/null -lc 'printenv'``    — several pre-options + bundle
    * ``/usr/bin/bash -lc "cat /etc/shadow"``       — path-prefixed shell
    * ``FOO=1 bash -c 'printenv'``                  — POSIX assignment prefix
    * ``env bash -c 'printenv'``                    — env-runner prefix
    * ``/usr/bin/env FOO=1 bash -c 'printenv'``     — env-runner + assignments

    Explicitly NOT recognised (correctly treated as non-wrapper shape):

    * ``bash script.sh -c printenv``                — script-file mode;
      ``script.sh`` is a positional, ``-c printenv`` is forwarded as
      positional args to ``script.sh``.  Returns ``None``.
    * ``bash -- script.sh -c printenv``             — ``--`` ends option
      parsing; everything after is positional.  Returns ``None``.
    * ``bash -O extglob script.sh``                 — no c-cluster at all.
      Returns ``None``.

    Algorithm:

    1. Shlex-split the command; if split fails (unclosed quote), bail
       out — the outer regex check is authoritative for malformed input.
    2. Confirm ``tokens[0]`` is a recognised shell.
    3. Walk ``tokens[1:]`` maintaining a cursor.  At each step:

       a. If the token is ``--`` → end of options; RETURN None (we hit
          positional territory without finding ``-c``).
       b. If the token is a c-bearing short-option cluster (``-c``,
          ``-lc``, ``-cl``, etc.) → RETURN the next token as the script.
       c. If the token is in ``_SHORT_OPTS_TAKING_VALUE`` or
          ``_LONG_OPTS_TAKING_VALUE`` → skip THIS token AND the next one
          (the value).
       d. If the token is any other option-shaped token
          (``-<letters>`` with no c, or ``--<anything>``) → skip
          THIS token only.
       e. If the token is a POSITIONAL (no leading ``-``) → end of
          options (shell is in script-file mode, not wrapper mode);
          RETURN None.

    Step (e) is the codex-round-3 fix: ``bash script.sh -c printenv``
    is NOT a wrapper and must not be unwrapped — the ``-c`` in that
    invocation is forwarded to ``script.sh`` as ``$2``.

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

    # Minimal: need at least 2 tokens (e.g. `env -S <payload>`) for any
    # unwrap path to succeed.  The stricter `<shell> -c <script>` shape
    # requires 3 and is enforced AFTER prefix stripping (see the
    # `len(tokens) - shell_idx < 3` check below).
    if len(tokens) < 2:
        return None

    # MIT-164 review 4-5 / codex P1: command-prefix stripping.  Tokens
    # before the shell can legitimately include:
    #
    #   * POSIX-style VAR=value assignments: `FOO=1 BAR=2 bash -c '...'`
    #   * an `env` (or `/usr/bin/env`) runner, optionally followed by
    #     env's OWN flags (`-i` ignore-env, `-u NAME` unset,
    #     `--chdir=/dir`, etc.) and then more VAR=value assignments:
    #     `/usr/bin/env -i bash -c '...'`, `env -u HOME sh -c '...'`.
    #   * both combined.
    #
    # All of these are standard Unix idioms for invoking a shell with a
    # modified environment, and all of them bypass the prescreen if we
    # require the shell to be `tokens[0]`.  Advance a cursor past any
    # such prefix and treat the next non-prefix token as the shell.
    #
    # Assignments are identified by an identifier-name followed by `=`.
    # `env` is identified by basename so `/usr/bin/env`, `/bin/env`,
    # `env` are all recognised.  env's own flags are enumerated in two
    # sets (flags-only and value-taking) — both GNU and BSD env implement
    # the same core set.
    shell_idx = 0
    # `seen_env` is a STICKY flag that stays True for the entire prefix
    # strip after we encounter an `env` token.  It governs the
    # assignment-name loosening (env accepts `X-Y=1`, shell doesn't) —
    # see `_is_assignment_token(..., allow_env_style=...)`.  Must NOT
    # be cleared on `env --` because env-style assignments can appear
    # after the `--` terminator (`/usr/bin/env -- 'X-Y=1' bash -c
    # '...'` — codex-round-11 P1).
    seen_env = False
    # `in_env_flags` is a TRANSIENT flag that is True while we're in
    # env's OWN option-parsing state (after an `env` token, until
    # env's `--` or until we hit a non-flag token).  Only it governs
    # the env-flag-specific branches below (-i, -u, -S, etc.).
    in_env_flags = False
    # `past_env_dashdash` is a STICKY flag set after `env --` that
    # allows the MOST permissive assignment regex — including ``NAME=v``
    # where NAME starts with ``-`` — because env stops parsing options
    # post-`--` and accepts arbitrary-name assignments.  Closes
    # codex-round-12 P1: `/usr/bin/env -- '-X=1' bash -c '...'`.
    past_env_dashdash = False
    # Loop guard: keep going as long as there's at least ONE token to
    # inspect.  The "at least `<shell> -c <script>` remaining" check
    # happens AFTER the loop — this loop is only the prefix-stripping
    # walk, which can terminate early by returning an inner (e.g. from
    # `env -S <payload>`) regardless of how many tokens remain.
    while shell_idx < len(tokens):
        tok = tokens[shell_idx]
        # (1) Assignment token — always allowed in the prefix.  Before
        # we've seen `env`, only POSIX-valid identifiers count (shell
        # syntax).  AFTER `env` (including post-`--`), we loosen the
        # identifier rule because env accepts arbitrary NAME=value
        # pairs (e.g. ``X-Y=1``) — closes the codex-round-9 and
        # round-11 P1 bypasses.  Post-`env --` we loosen further to
        # also accept dash-prefixed names (`-X=1`) — codex-round-12 P1.
        if _is_assignment_token(
            tok,
            allow_env_style=seen_env,
            allow_post_dashdash_env=past_env_dashdash,
        ):
            shell_idx += 1
            continue
        # (2) `env` runner — basename match catches path-prefixed forms.
        # Tradeoff (codex round 7 P3): a repo-local helper named `./env`
        # that is NOT the system env runner will be misclassified here
        # and its following arguments will be inspected as if env had
        # run them.  Same stance as the `_SHELL_WRAPPER_BASENAMES`
        # comment above — basename-keyed matching is the security
        # prescreen's established model, and over-blocking on a
        # collision is preferable to leaking env-wrapped secrets.
        if os.path.basename(tok) == "env":
            shell_idx += 1
            seen_env = True
            in_env_flags = True
            continue
        # (3) After we've seen `env`, skip env's own flags until we hit
        # an assignment or the shell.  This closes the codex-round-5 P1:
        # `/usr/bin/env -i bash -c '...'`, `env -u HOME sh -c '...'`,
        # etc.  Flag recognition covers:
        #   * `--` separator (end of env's options — but NOT end of the
        #     prefix strip: assignments may follow, e.g. `env -- FOO=1
        #     bash -c '...'`).  Clear the env-flag state and continue.
        #   * GNU env `-S`/`--split-string` (codex-round-6 P1): the
        #     value IS a full command to be re-split and executed, so
        #     we return it directly as the "inner" — outer recursion in
        #     `check_shell_command` will re-run the whole prescreen on
        #     the re-split payload (regex + unwrap + recurse).  This
        #     transparently covers `/usr/bin/env -S 'bash -c printenv'`.
        #   * Other value-taking flags: `-u NAME`, `--unset=NAME`,
        #     `-C DIR`, `--chdir=DIR`.  Skip this token AND its value
        #     (for non-`=` forms) or skip just this token (for `=`
        #     forms — self-contained).
        #   * Flags without value: `-i`, `--ignore-environment`,
        #     `-0`, `--null`, `-v`, `--debug`, `--help`, `--version`.
        # Unknown `-*`/`--*` tokens in the env-flag position are skipped
        # as "some env flag we don't recognise" — the security-safe
        # direction (same rationale as elsewhere: over-stripping a
        # non-env-flag token is at worst a false-negative on an already-
        # unusual command shape, not a bypass).
        if in_env_flags and tok.startswith("-"):
            # `--` ends env's OWN options but NOT the prefix strip.
            # After `env -- FOO=1 bash -c '...'`, the `FOO=1` must
            # still be consumed as an assignment before we hit the
            # shell.  Clear ONLY `in_env_flags` — keep `seen_env`
            # True so the loose assignment regex stays active for
            # env-style non-POSIX names like ``X-Y=1`` (codex-
            # round-11 P1).
            if tok == "--":
                shell_idx += 1
                in_env_flags = False
                past_env_dashdash = True
                continue
            # `-S <payload>` (GNU env split-string): the payload is
            # re-split by env and CONCATENATED with any trailing
            # argv tokens.  `/usr/bin/env -S 'bash -c' 'printenv'`
            # actually executes `bash -c printenv`, so we must glue
            # the payload and trailing argv together before recursing.
            # Shlex-quote the trailing args so `check_shell_command`
            # re-parses the joined string identically.
            if tok in ("-S", "--split-string") and shell_idx + 1 < len(tokens):
                return _join_env_split_payload(
                    tokens[shell_idx + 1],
                    tokens[shell_idx + 2 :],
                )
            # `-S<PAYLOAD>` attached-argument form (codex-round-10 P1).
            # GNU env accepts `-Sbash -c printenv` as a single token
            # that is equivalent to `-S bash -c printenv`.  Strip the
            # `-S` prefix, treat the rest as the payload, and glue
            # trailing argv.
            if tok.startswith("-S") and len(tok) > 2 and not tok.startswith("-S-"):
                return _join_env_split_payload(
                    tok[2:],
                    tokens[shell_idx + 1 :],
                )
            # `--split-string=PAYLOAD` — self-contained, then trailing argv.
            if tok.startswith("--split-string="):
                return _join_env_split_payload(
                    tok[len("--split-string="):],
                    tokens[shell_idx + 1 :],
                )
            if tok in _ENV_FLAGS_TAKING_VALUE and shell_idx + 1 < len(tokens):
                shell_idx += 2
                continue
            # All other `-*` tokens (flag-only or `--option=value`).
            # The GNU env signal flags (``--block-signal``,
            # ``--default-signal``, ``--ignore-signal``) are handled
            # here via the equals form (``--default-signal=PIPE`` is a
            # self-contained single token and is correctly skipped).
            # The bare / space-separated forms are NOT bypass paths —
            # verified empirically: `/usr/bin/env --default-signal
            # PIPE bash -c '...'` errors with "PIPE: No such file"
            # because env treats `PIPE` as the command to exec.  So
            # the codex-round-7 concern about space-separated signal
            # flags is a false positive on actual GNU env semantics;
            # no special handling is needed.
            shell_idx += 1
            continue
        # (4) Not an assignment, not `env`, not an env-flag.  This is
        # either the shell binary itself or a positional — let the
        # caller decide.
        break

    # After stripping prefixes, require at least `<shell> -c <script>`
    # remaining (3 tokens).
    if len(tokens) - shell_idx < 3:
        return None

    shell_basename = os.path.basename(tokens[shell_idx])
    if shell_basename not in _SHELL_WRAPPER_BASENAMES:
        return None

    # Walk option-prefix, looking for the script-carrying cluster.  The
    # cursor advances by 1 for flags-without-value and by 2 for flags-
    # with-value.  End-of-options (either ``--`` or a bare positional)
    # exits with ``None`` — shell is in script-file mode, not wrapper
    # mode.  See codex-review-round-3 rationale in the docstring.
    idx = shell_idx + 1
    while idx < len(tokens) - 1:
        tok = tokens[idx]

        # (a) `--` separator ends option parsing — not a wrapper.
        if tok == "--":
            return None

        # (b) c-bearing cluster → NEXT token is the script.
        if _is_script_carrying_option(tok):
            return tokens[idx + 1]

        # (c) Value-taking option → skip this token AND the next.
        if tok in _SHORT_OPTS_TAKING_VALUE or tok in _LONG_OPTS_TAKING_VALUE:
            idx += 2
            continue

        # (d) Any other option-shaped token (short flag without ``c``,
        # long flag without a value, or ``--opt=val`` form) — skip it.
        # Both ``-``- and ``+``-prefixed tokens count as option-shaped:
        # bash and zsh accept ``+n``, ``+i``, ``+x``, etc. as "turn
        # option off" pre-`-c` (codex-round-13 P1).  Filter out bare
        # ``-`` and ``+`` which are positional conventions in some
        # tools.
        if (tok.startswith("-") or tok.startswith("+")) and tok not in ("-", "+"):
            # Short option: if the cluster contains ``c`` we already
            # returned at (b); so this is a flag-only cluster.
            # Long option: either no value or ``--opt=val`` (self-
            # contained).  Either way, advance one step.
            idx += 1
            continue

        # (e) Positional (not starting with ``-``/``+``, or bare
        # ``-``/``+``) — shell is in script-file mode.  Not a
        # wrapper.  See codex-round-3: ``bash script.sh -c printenv``
        # must not unwrap to ``printenv``.
        return None

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

    # 2. MIT-164: shell-wrapper unwrap + recurse.  Extract the inner
    # script FIRST; only consult the depth cap when we would actually
    # recurse into another wrapper layer.
    #
    # Rationale (codex-review-round-5 P2): the previous ordering
    # checked `_depth` before extraction, which rejected benign
    # commands whose innermost script happened to be a non-wrapper
    # (e.g. `sh -c "bash -c 'zsh -c \"echo ok\"'"` — 3 legitimate
    # wrapper layers, innermost script is plain `echo ok`).  With
    # extraction first, we only pay the cap when there IS another
    # layer to process; a non-wrapper inner exits at step 3 below
    # regardless of depth.
    inner = _extract_shell_wrapper_inner(command)
    if inner is None:
        # 3. Clean bottom — regex already matched "clean" above and
        # there's no further wrapper to peel.  Allow.
        return None

    # There IS another wrapper layer.  Enforce the cap.  `_depth` is a
    # runtime bound on pathological nesting (`sh -c "bash -c 'sh -c
    # ...'"`).  Reaching the cap while there is still another wrapper
    # to unwrap is itself strong evidence of an evasion attempt —
    # letting it through would reopen the bypass (we'd stop looking
    # before reaching the denylisted innermost command).  Block.
    if _depth >= _MAX_SHELL_WRAPPER_DEPTH:
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

    # Under the cap — recurse into the unwrapped inner script.
    return check_shell_command(inner, _depth + 1)
