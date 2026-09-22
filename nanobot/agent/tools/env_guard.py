"""Refuse package installs and environment construction inside a chat turn.

Ziggy-local (fork, MIT-1014). ``exec`` is an unrestricted shell. When a web
lookup fails, the model escalates instead of concluding: on 2026-09-15 the
owner asked Ziggy to plan a trip, three travel sites refused ``web_fetch``,
and the model then ran ``pip3 install playwright``, ``python3 -m venv
.venv-flights``, ``playwright install chromium`` and a scraper script -- live,
inside the chat turn, for three minutes, with no chance of succeeding.

This module answers one question -- "does this shell command build an
environment?" -- and answers it structurally rather than by substring match,
because the model writes compound commands::

    cd /tmp && python3 -m venv .venv && . .venv/bin/activate && pip install x
    bash -c "pip3 install playwright && playwright install chromium"
    sudo -n apt-get install -y chromium ; curl -fsSL https://x/i.sh | sh
    (PIP_NO_INPUT=1 python3 -m pip install --quiet playwright)

Detection therefore:

1. splits the command into top-level lists on ``&&`` ``||`` ``;`` ``&`` and
   newlines, then each list into pipeline elements on ``|``, honouring quotes,
   parentheses and escapes;
2. recurses into subshells ``( ... )``, command substitutions ``$( ... )``,
   backticks, process substitutions ``<( ... )`` and quoted strings, so an
   installer smuggled inside ``sh -c "..."`` is still seen;
3. strips leading noise from each element -- ``sudo``/``sudo -n``, ``env``,
   ``command``, ``nohup``, ``time``, ``xargs``, ``VAR=value`` prefixes,
   wrapping parens/braces -- before looking at argv[0];
4. matches the *program* and its *subcommand*, never a substring, so
   ``grep 'pip install' notes.txt``, ``echo pip install`` and ``pip list``
   are all left alone while ``python3.11 -m pip install x`` is caught;
5. detects the download-and-run pipeline (``curl ... | sh``) at pipeline
   level, plus ``bash <(curl ...)`` via the nested-fragment pass.

The verdict is advisory: the caller decides what to do with it. Nothing here
raises, and the refusal text below is deliberately NOT prefixed with "Error"
so the runtime returns it as an ordinary tool observation rather than a tool
error with a "try a different approach" retry hint appended.
"""

from __future__ import annotations

import re

__all__ = [
    "detect_environment_build",
    "install_refusal",
    "INSTALL_ATTEMPT_BUDGET",
]

# How many refusals a single interactive turn gets before the wording stops
# being a suggestion and becomes a stop instruction. The command is refused
# either way; the budget only controls how hard we tell the model to give up.
INSTALL_ATTEMPT_BUDGET = 2

_MAX_RECURSION = 4
_MAX_SEGMENTS = 400

_LIST_OPS = ("&&", "||", ";;", ";", "&", "\n", "\r")
_PIPE_OPS = ("|&", "|")

_ASSIGN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
_PYTHON_RE = re.compile(r"python(\d+(\.\d+)?)?")
_PIP_RE = re.compile(r"pip(\d+(\.\d+)?)?")

# Commands that merely wrap another command; skip them and their own flags.
_WRAPPERS = frozenset({
    "sudo", "doas", "env", "command", "builtin", "nohup", "time", "exec",
    "nice", "ionice", "stdbuf", "setsid", "xargs", "then", "do", "else",
    "elif", "fi", "done", "!",
})

# Interpreters that will run whatever they are handed on stdin.
_INTERPRETERS = frozenset({
    "sh", "bash", "zsh", "dash", "ksh", "ash", "fish", "csh", "tcsh",
    "python", "python2", "python3", "perl", "ruby", "node", "php",
    "pwsh", "powershell",
})

_DOWNLOADERS = frozenset({"curl", "wget", "fetch", "aria2c", "httpie", "http"})

# Programs that execute a *quoted argument* as a command. Quoted text is only
# recursed into for these: otherwise ``grep 'pip install' docs/`` and
# ``echo "run npm install"`` would be read as installs. Command substitutions
# and process substitutions always run, so those are recursed unconditionally.
_ARG_EXECUTORS = frozenset({
    "eval", "su", "ssh", "docker", "podman", "kubectl", "nsenter", "chroot",
    "screen", "tmux", "distrobox", "toolbox", "systemd-run", "runuser",
}) | frozenset({
    "sh", "bash", "zsh", "dash", "ksh", "ash", "fish", "csh", "tcsh",
    "python", "python2", "python3", "perl", "ruby", "node", "php",
    "pwsh", "powershell",
})


def detect_environment_build(command: str) -> str | None:
    """Return a short label for the install/env-build this command performs.

    ``None`` means the command does not install software or construct an
    environment, as far as this guard can tell. The label is human-readable
    and is quoted back to the model so the refusal names what it caught.
    """
    if not command or not command.strip():
        return None
    budget = [_MAX_SEGMENTS]
    return _scan(command, depth=0, budget=budget)


def install_refusal(command: str, detected: str, attempt: int) -> str:
    """The tool result returned instead of running an installer.

    Worded to make the model *conclude*, not retry: it names what was
    refused, says the block is unconditional, points at the tools that do
    exist, and tells it to report the limitation to the user. Past the
    per-turn budget the wording becomes a flat stop instruction.
    """
    first_line = command.strip().splitlines()[0][:160]
    if attempt > INSTALL_ATTEMPT_BUDGET:
        return (
            "Installing software is still not available. This is attempt "
            f"{attempt} in this conversation turn, and every one of them has "
            "been refused.\n\n"
            "Stop building an environment. No package manager, virtualenv, "
            "browser download, or install script will run here, so no further "
            "variation of this command is worth trying.\n\n"
            "Answer the user now with what you already have, and say plainly "
            "what you could not do and what you would need to do it."
        )
    return (
        "Installing software is not available during a conversation.\n\n"
        f"Refused ({detected}): {first_line}\n\n"
        "Package installs, virtual environments, browser/runtime downloads "
        "and download-and-run scripts are disabled inside a chat turn. This "
        "is not a transient failure and not a permissions problem -- there is "
        "no flag, no alternative package manager, and no working directory "
        "that changes it.\n\n"
        "Use the tools you already have, or tell the user what you could not "
        "do and why. If the task genuinely needs software that is not "
        "installed, say so and stop; do not spend the conversation building "
        "an environment."
    )


# --------------------------------------------------------------------------
# scanning
# --------------------------------------------------------------------------


def _scan(text: str, *, depth: int, budget: list[int]) -> str | None:
    if depth > _MAX_RECURSION or budget[0] <= 0:
        return None

    # A newline is a list operator, so without this every line of a script
    # being *written out* would be parsed as a command to run. Writing a
    # bootstrap script is an ordinary request; the heredoc body is data.
    text = _strip_heredocs(text)

    for chunk in _split_on(text, _LIST_OPS):
        if budget[0] <= 0:
            return None
        elements = _split_on(chunk, _PIPE_OPS)
        budget[0] -= len(elements)

        verdict = _scan_pipeline(elements)
        if verdict:
            return verdict

        for element in elements:
            inner = _unwrap_group(element)
            if inner is not None:
                verdict = _scan(inner, depth=depth + 1, budget=budget)
                if verdict:
                    return verdict
                continue

            tokens = _strip_wrappers(_tokenize(element))
            verdict = _classify(tokens)
            if verdict:
                return verdict

            prog = _basename(tokens[0]) if tokens else ""
            executes_args = prog in _ARG_EXECUTORS
            for kind, fragment in _nested_fragments(element):
                if kind == "quote" and not executes_args:
                    continue
                verdict = _scan(fragment, depth=depth + 1, budget=budget)
                if verdict:
                    return verdict
    return None


def _scan_pipeline(elements: list[str]) -> str | None:
    """Catch ``curl https://... | sh`` and ``bash <(curl ...)``."""
    if len(elements) >= 2:
        seen_download = False
        for element in elements:
            prog = _program(element)
            if prog in _DOWNLOADERS:
                seen_download = True
                continue
            if seen_download and prog in _INTERPRETERS:
                return "download-and-run pipeline"
    for element in elements:
        if _program(element) not in _INTERPRETERS:
            continue
        for kind, fragment in _nested_fragments(element):
            if kind != "subst":
                continue
            if _program(fragment) in _DOWNLOADERS:
                return "download-and-run pipeline"
    return None


def _program(element: str) -> str:
    tokens = _strip_wrappers(_tokenize(_unwrap_group(element) or element))
    return _basename(tokens[0]) if tokens else ""


# --------------------------------------------------------------------------
# rules
# --------------------------------------------------------------------------


def _classify(tokens: list[str]) -> str | None:
    if not tokens:
        return None
    prog = _basename(tokens[0])
    rest = tokens[1:]
    # Positional (non-flag) arguments, in order. Keying on these rather than
    # on the raw string is what makes `pip list` and `apt list` safe while
    # `pip --quiet install x` is still caught.
    words = [t for t in rest if not t.startswith("-")]
    flags = [t for t in rest if t.startswith("-")]

    def sub(index: int = 0) -> str:
        return words[index] if len(words) > index else ""

    if _PYTHON_RE.fullmatch(prog):
        return _classify_python(rest)

    if _PIP_RE.fullmatch(prog) and sub() in {"install", "download", "wheel"}:
        return "pip install"

    if prog in {"uv", "uvx"}:
        return _classify_uv(prog, words)

    if prog in {"virtualenv", "mkvirtualenv"}:
        return "virtualenv"

    if prog == "pipx" and sub() in {"install", "run", "inject", "upgrade"}:
        return f"pipx {sub()}"

    if prog == "pipenv" and sub() in {"install", "sync", "update"}:
        return f"pipenv {sub()}"

    if prog == "poetry" and sub() in {"install", "add", "update"}:
        return f"poetry {sub()}"

    if prog in {"conda", "mamba", "micromamba"} and sub() in {
        "install", "create", "env", "update"
    }:
        return f"{prog} {sub()}"

    if prog == "playwright" and sub() in {"install", "install-deps"}:
        return "playwright install"

    if prog in {"npm", "pnpm", "bun"} and sub() in {
        "install", "i", "ci", "add", "install-test", "exec", "link"
    }:
        return f"{prog} {sub()}"

    if prog == "yarn" and (not words or sub() in {"install", "add", "dlx"}):
        return f"yarn {sub() or 'install'}".strip()

    if prog in {"npx", "pnpx", "bunx"}:
        return f"{prog} (downloads packages on demand)"

    if prog in {"apt", "apt-get", "aptitude"} and sub() in {
        "install", "reinstall", "build-dep"
    }:
        return f"{prog} {sub()}"

    if prog == "dpkg" and any(f in {"-i", "--install"} for f in flags):
        return "dpkg -i"

    if prog in {"yum", "dnf", "microdnf"} and sub() in {
        "install", "groupinstall", "builddep", "reinstall"
    }:
        return f"{prog} {sub()}"

    if prog == "zypper" and sub() in {"install", "in"}:
        return "zypper install"

    if prog == "apk" and sub() == "add":
        return "apk add"

    if prog == "pacman" and any(re.fullmatch(r"-S[yu]*", f) for f in flags):
        return "pacman -S"

    if prog in {"snap", "flatpak"} and sub() == "install":
        return f"{prog} install"

    if prog == "brew" and sub() in {"install", "reinstall", "tap", "bundle"}:
        return f"brew {sub()}"

    if prog == "port" and sub() == "install":
        return "port install"

    if prog in {"choco", "winget", "scoop"} and sub() == "install":
        return f"{prog} install"

    if prog == "cargo" and sub() in {"install", "add"}:
        return f"cargo {sub()}"

    if prog == "go" and sub() in {"install", "get"}:
        return f"go {sub()}"

    if prog == "gem" and sub() == "install":
        return "gem install"

    if prog in {"bundle", "bundler"} and sub() == "install":
        return "bundle install"

    if prog == "composer" and sub() in {"install", "require", "update"}:
        return f"composer {sub()}"

    if prog == "rustup" and sub() in {
        "install", "toolchain", "target", "component", "update"
    }:
        return f"rustup {sub()}"

    if prog in {
        "asdf", "nvm", "fnm", "volta", "pyenv", "rbenv", "nodenv", "sdk"
    } and sub() in {"install", "add"}:
        return f"{prog} {sub()}"

    return None


def _classify_python(rest: list[str]) -> str | None:
    """``python3 -m pip install x`` / ``python -m venv .venv`` and friends."""
    module = ""
    tail: list[str] = []
    for index, token in enumerate(rest):
        if token == "-m":
            if index + 1 < len(rest):
                module = _basename(rest[index + 1])
                tail = [t for t in rest[index + 2:] if not t.startswith("-")]
            break
        if token.startswith("-m") and len(token) > 2:
            module = _basename(token[2:])
            tail = [t for t in rest[index + 1:] if not t.startswith("-")]
            break

    first = tail[0] if tail else ""
    if module in {"venv", "virtualenv"}:
        return "python -m venv"
    if module == "ensurepip":
        return "python -m ensurepip"
    if _PIP_RE.fullmatch(module) and first in {"install", "download", "wheel"}:
        return "python -m pip install"
    if module == "playwright" and first in {"install", "install-deps"}:
        return "playwright install"
    if module == "uv":
        return _classify_uv("uv", tail)
    if module in {"pipx", "poetry", "pipenv", "conda"} and first in {
        "install", "add", "sync", "run", "create"
    }:
        return f"{module} {first}"

    # `python get-pip.py` / `python setup.py install` are environment
    # construction too, and neither goes through -m.
    words = [t for t in rest if not t.startswith("-")]
    if words:
        script = _basename(words[0])
        if script == "get-pip.py":
            return "get-pip.py"
        if script == "setup.py" and len(words) > 1 and words[1] in {
            "install", "develop"
        }:
            return f"setup.py {words[1]}"
    return None


def _classify_uv(prog: str, words: list[str]) -> str | None:
    if prog == "uvx":
        return "uvx (downloads packages on demand)"
    first = words[0] if words else ""
    second = words[1] if len(words) > 1 else ""
    if first == "pip" and second in {"install", "sync", "compile"}:
        return f"uv pip {second}"
    if first in {"venv", "add", "sync", "init"}:
        return f"uv {first}"
    if first == "tool" and second in {"install", "run"}:
        return f"uv tool {second}"
    return None


# --------------------------------------------------------------------------
# shell-aware splitting / tokenizing
# --------------------------------------------------------------------------


def _strip_heredocs(text: str) -> str:
    """Drop heredoc bodies, keeping the command lines around them.

    ``cat > setup.sh <<'EOF' / pip install requests / EOF`` writes a file; it
    does not install anything. The introducing line is kept (so ``cat`` is
    still classified normally) and everything from the following line up to
    and including the terminator is removed. An unterminated heredoc consumes
    the rest of the text, which is the shell's own behaviour.
    """
    if "<<" not in text:
        return text
    lines = text.split("\n")
    kept: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        index += 1
        for delimiter, strip_tabs in _heredoc_delimiters(line):
            while index < len(lines):
                body = lines[index]
                index += 1
                candidate = body.lstrip("\t") if strip_tabs else body
                if candidate.rstrip() == delimiter:
                    break
    return "\n".join(kept)


def _heredoc_delimiters(line: str) -> list[tuple[str, bool]]:
    """Return ``(delimiter, strips_leading_tabs)`` for each heredoc the line opens."""
    found: list[tuple[str, bool]] = []
    quote: str | None = None
    escaped = False
    index = 0
    length = len(line)

    while index < length:
        ch = line[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if ch == "\\" and quote != "'":
            escaped = True
            index += 1
            continue
        if quote is not None:
            if ch == quote:
                quote = None
            index += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            index += 1
            continue
        # `<<<` is a here-string: the word after it is the data itself, on the
        # same line, so there is no body to strip.
        if line.startswith("<<", index) and not line.startswith("<<<", index):
            cursor = index + 2
            strip_tabs = False
            if cursor < length and line[cursor] == "-":
                strip_tabs = True
                cursor += 1
            while cursor < length and line[cursor] in " \t":
                cursor += 1
            delimiter = ""
            if cursor < length and line[cursor] in ("'", '"'):
                closer = line[cursor]
                cursor += 1
                start = cursor
                while cursor < length and line[cursor] != closer:
                    cursor += 1
                delimiter = line[start:cursor]
                cursor = min(cursor + 1, length)
            else:
                start = cursor
                while cursor < length and (
                    line[cursor].isalnum() or line[cursor] in "_-.\\"
                ):
                    cursor += 1
                delimiter = line[start:cursor].replace("\\", "")
            if delimiter:
                found.append((delimiter, strip_tabs))
            index = cursor
            continue
        index += 1

    return found


def _split_on(text: str, operators: tuple[str, ...]) -> list[str]:
    """Split on top-level ``operators``, honouring quotes, escapes and groups."""
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    escaped = False
    depth = 0
    index = 0
    length = len(text)

    while index < length:
        ch = text[index]

        if escaped:
            buf.append(ch)
            escaped = False
            index += 1
            continue
        if ch == "\\" and quote != "'":
            buf.append(ch)
            escaped = True
            index += 1
            continue
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
            index += 1
            continue
        if ch in ("'", '"', "`"):
            buf.append(ch)
            quote = ch
            index += 1
            continue
        if ch in "({":
            depth += 1
            buf.append(ch)
            index += 1
            continue
        if ch in ")}" and depth > 0:
            depth -= 1
            buf.append(ch)
            index += 1
            continue

        if depth == 0:
            matched = ""
            for op in operators:
                if len(op) > len(matched) and text.startswith(op, index):
                    matched = op
            if matched and not _is_redirection(text, index, matched):
                segment = "".join(buf).strip()
                if segment:
                    parts.append(segment)
                buf = []
                index += len(matched)
                continue

        buf.append(ch)
        index += 1

    segment = "".join(buf).strip()
    if segment:
        parts.append(segment)
    return parts


def _is_redirection(text: str, index: int, matched: str) -> bool:
    """``2>&1``, ``&>log`` and ``>|f`` are redirections, not separators."""
    previous = text[index - 1] if index > 0 else ""
    if matched == "&":
        return previous in "<>&" or text.startswith("&>", index)
    if matched in {"|", "|&"}:
        return previous in "<>"
    return False


def _unwrap_group(element: str) -> str | None:
    """Return the body of ``( ... )`` / ``{ ... }``, else ``None``."""
    text = element.strip()
    if len(text) < 3:
        return None
    if text[0] == "(" and text[-1] == ")":
        return text[1:-1]
    if text[0] == "{" and text[-1] == "}":
        return text[1:-1]
    return None


def _tokenize(segment: str) -> list[str]:
    """Split on whitespace and drop quoting, so ``pi"p" install`` is ``pip install``."""
    tokens: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    escaped = False

    for ch in segment:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if ch == "\\" and quote != "'":
            escaped = True
            continue
        if quote is not None:
            if ch == quote:
                quote = None
            else:
                buf.append(ch)
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch.isspace():
            if buf:
                tokens.append("".join(buf))
                buf = []
            continue
        buf.append(ch)

    if buf:
        tokens.append("".join(buf))
    return tokens


def _strip_wrappers(tokens: list[str]) -> list[str]:
    """Drop ``VAR=x``, ``sudo -n``, ``env``, stray group punctuation, etc."""
    index = 0
    while index < len(tokens):
        token = tokens[index].strip("(){};&")
        if not token:
            index += 1
            continue
        if _ASSIGN_RE.fullmatch(token):
            index += 1
            continue
        base = _basename(token)
        if base in _WRAPPERS:
            index += 1
            while index < len(tokens) and tokens[index].startswith("-"):
                index += 1
            continue
        remainder = list(tokens[index:])
        remainder[0] = token
        return remainder
    return []


def _nested_fragments(text: str) -> list[tuple[str, str]]:
    """Pull out quoted strings and command/process substitutions.

    Each entry is ``("subst", body)`` for ``$( )`` / ``<( )`` / backticks --
    which the shell always executes -- or ``("quote", body)`` for a quoted
    string, which only runs when the surrounding program executes its
    arguments. The caller uses the distinction to tell ``bash -c "pip
    install x"`` apart from ``grep 'pip install' docs/``.
    """
    fragments: list[tuple[str, str]] = []
    index = 0
    length = len(text)

    while index < length:
        ch = text[index]
        if ch == "\\":
            index += 2
            continue
        if text.startswith(("$(", "<(", ">("), index):
            depth = 1
            cursor = index + 2
            while cursor < length and depth:
                if text[cursor] == "(":
                    depth += 1
                elif text[cursor] == ")":
                    depth -= 1
                cursor += 1
            fragments.append(("subst", text[index + 2:cursor - 1]))
            index = cursor
            continue
        if ch == "`":
            close = text.find("`", index + 1)
            if close == -1:
                break
            fragments.append(("subst", text[index + 1:close]))
            index = close + 1
            continue
        if ch in ("'", '"'):
            cursor = index + 1
            while cursor < length:
                if text[cursor] == "\\" and ch == '"':
                    cursor += 2
                    continue
                if text[cursor] == ch:
                    break
                cursor += 1
            if cursor >= length:
                break
            fragments.append(("quote", text[index + 1:cursor]))
            index = cursor + 1
            continue
        index += 1

    return [(kind, body) for kind, body in fragments if body.strip()]


def _basename(token: str) -> str:
    cleaned = token.strip("(){};&").replace("\\", "/")
    if "/" in cleaned:
        cleaned = cleaned.rsplit("/", 1)[-1]
    if cleaned.lower().endswith(".exe"):
        cleaned = cleaned[:-4]
    return cleaned.lower()
