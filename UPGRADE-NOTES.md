# Upstream merge — `ziggy-main-upgrade-2026-09`

Merge of `HKUDS/nanobot` `main` (`499bf903`, 2026-09-14) into the fork's
`ziggy-main` (`71d5923d`, 2026-05-01).

| | |
| --- | --- |
| Base | `71d5923d` (fork `ziggy-main`) |
| Merged | `upstream/main` @ `499bf903` |
| Merge base | `861fbb0d` |
| Upstream commits merged | **2,146** |
| Fork-local commits preserved | 88 |
| Upstream releases crossed | v0.2.0, v0.2.1, v0.2.2, v0.3.0 |
| Files changed vs base | 1,465 (+395,924 / −35,582) |
| Conflicted files | 22 (67 conflict hunks) |
| Tests | **7,630 passed, 27 skipped, 2 failed** — both failures reproduce on pristine upstream (see "Known failures") |

The two bugs we hand-patched on the Spark were fixed upstream on **2026-05-03**,
two days after this fork's merge cutoff, in PRs **#3613** (merge `614b2136`) and
**#3614** (merge `2a7433b7`). Both verified present in this merge. Our patches
are dropped in favour of upstream — see "Local patches dropped".

---

## 1. Conflicts and how each was resolved

### Source files

| File | Hunks | Resolution |
| --- | --- | --- |
| `nanobot/agent/loop.py` | 13 | Upstream turn pipeline taken wholesale (typed events, `TurnDelivery`, `TurnContext`, hook factories). Four Ziggy features re-applied on top — see §3. |
| `nanobot/agent/tools/shell.py` | 9 | Upstream (`ExecSessionManager`, `_prepare_command`, device-path allowlist, `current_scope_allows_loopback`). Kept MIT-123 prescreen and the MIT-203 `allow_loopback` override; dropped MIT-162 (superseded). |
| `nanobot/agent/runner.py` | 7 | **Reset to upstream verbatim**, then re-applied the Langfuse iteration span and `latency_ms` as two explicit edits. The fork's version of this file no longer exists in any recognisable form upstream. |
| `nanobot/agent/subagent.py` | 6 | Upstream (`workspace_scope`, `LLMRuntime`, `_run_admitted_subagent`, request-context binding). Re-threaded `trace_context` and re-wrapped `runner.run` in `observe_subagent`. |
| `nanobot/agent/tools/registry.py` | 5 | Upstream (`prepare_call`, `_coerce_params`, `ToolResult`). Ziggy audit + Prometheus + redaction layer rebuilt on top of `ToolResult`. `_definitions_cache` renamed to upstream's `_cached_definitions`. |
| `nanobot/channels/base.py` | 4 | Upstream (pairing store, `authorization_id`). Kept the `sanitize_input` prompt-injection layer. Upstream's `is_allowed` is still fail-closed, so the fork's hardening is subsumed. |
| `nanobot/agent/tools/search.py` | 4 | Both sides kept: upstream's document-line sources plus MIT-136's sensitive-path skipping and its `(skipped N sensitive-path files)` note. |
| `nanobot/agent/tools/filesystem.py` | 3 | Upstream (`_resolve_read` / `_resolve_write`). MIT-121 guards re-applied against the new resolvers and converted to `ToolResult.error(...)`. Kept the module-level `_current_sender_id` (MIT-138) that `registry.set_context` and its tests depend on. |
| `nanobot/security/network.py` | 3 | Upstream (`resolve_url_target`, `_normalize_addr`, `_is_allowed_loopback_target`). `configure_loopback_exception` kept; `allow_loopback` widened to `bool \| None` on `resolve_url_target` / `validate_url_target` / `contains_internal_url` so `None` still falls back to the Ziggy module default. `_is_private` regained its optional `allow_loopback` kwarg for `validate_resolved_url`. |
| `nanobot/providers/openai_compat_provider.py` | 2 | Upstream entirely — **file is now byte-identical to upstream**. |
| `nanobot/templates/AGENTS.md` | 2 | Upstream (built-in `cron` tool, protected heartbeat job). |
| `nanobot/channels/manager.py` | 1 | Upstream event dispatch, with the Ziggy `_status_delta` heartbeat branch handled *before* it (it is not an upstream event type). |
| `nanobot/agent/hook.py` | 1 | Upstream `usage: LLMUsage \| None`, plus the fork's `latency_ms` field. |
| `nanobot/config/schema.py` | 1 | Upstream — `ExecToolConfig` / `WebToolsConfig` moved into their tool modules. MIT-203's `timeout=180` and `allow_loopback` re-applied to `shell.ExecToolConfig`. |
| `nanobot/providers/base.py` | 1 | Upstream. The fork's `ttft_ms` field was dropped: **upstream already has `LLMResponse.ttft_ms`.** |
| `nanobot/utils/__init__.py` | 1 | Upstream lazy module aliases, with `get_data_path` / `get_workspace_path` re-exported for fork call sites. |
| `nanobot/utils/helpers.py` | 1 | Both: upstream's `load_bundled_template` plus the Discord heartbeat helpers. |
| `pyproject.toml` | 1 | Both `bedrock` (upstream) and `ziggy` (fork) extras. Note upstream ships its own `langfuse` extra pinned to 3.x; Ziggy needs 4.x, so the two stay separate and the `ziggy` extra is the one to install. |

### Test files

| File | Resolution |
| --- | --- |
| `tests/tools/test_tool_registry.py` | Upstream's `prepare_call` tests restored (upstream reinstated the method the fork had removed). MIT-122/147 redaction tests kept but retargeted at `ToolResult`. |
| `tests/tools/test_exec_security.py` | Both: upstream's #3599 device-path suite and the fork's MIT-123 prescreen suite. |
| `tests/tools/test_filesystem_tools.py` | Both. |
| `tests/providers/test_providers_init.py` | Upstream, with `CustomProvider` added back to the expected `__all__`. |
| `tests/security/test_allow_loopback.py` | Kept, adjusted to upstream semantics (see §5). |
| `tests/tools/test_registry_classification.py` | Kept, adjusted to the `ToolResult` contract. |

---

## 2. Local patches dropped because upstream fixed it

| Fork patch | Upstream replacement | Why |
| --- | --- | --- |
| **ExecTool `/dev/null` guard false positive** (hand-patched on the Spark, uncommitted; also fork commit `e9daa2a0` on `feat/shared-rooms`) | PR **#3613** + **#3614** | Upstream allow-lists `/dev/null`, `/dev/zero`, `/dev/full`, `/dev/random`, `/dev/urandom`, `/dev/std{in,out,err}`, `/dev/tty` and `/dev/fd/N` before the workspace-path check, and stopped treating `\|~` as a home-dir prefix. Strictly broader than our patch. **Delete the Spark's `nanobot/agent/tools/shell.py.bak-20260913T213019`.** |
| **stderr / streamed-reply drop** (`deploy/runtime/reliability-20260906.patch`, never committed, never deployed) | PR **#3613**, third fix | `loop.py` now reads `if streamed_content and stop_reason not in {"error", "tool_error"}`. The `tool_error` stop no longer marks the reply as already-streamed, so the channel manager stops dropping it. **This bug was live in production until this merge.** |
| **MIT-162 process-group teardown** (`os.killpg` in a `finally`) | `ExecTool._kill_process_tree` | Upstream spawns with `start_new_session=True`, kills the whole group, reaps with `_reap_pid`, and adds Windows Job Object support. Superset of ours. |
| **MIT-144 / MIT-185 TTFT capture in `OpenAICompatProvider`** | `LLMResponse.ttft_ms` + provider-base streaming instrumentation | Upstream measures TTFT for *every* provider, not just the OpenAI-compatible one. `openai_compat_provider.py` is now identical to upstream. `tests/providers/test_openai_compat_ttft.py` deleted. |
| **MIT-203 "classify by explicit marker, not `startswith('Error')`"** (the *detection* half) | `ToolResult.is_error` | Upstream made failure detection structural. `_looks_like_error` now just defers to it, and the fork's string-marker scan is gone. The *classification* half (prescreen / timeout / nonzero_exit / exception / misclassified, for the audit log) is kept. **Behaviour change: a tool returning a bare string that merely starts with `"Error"` is no longer treated as a failure.** That was the original MIT-203 goal. |
| **`.ipynb` edit guard in `EditFileTool`** | Upstream supports editing `.ipynb` as JSON | Dropped; upstream has tests asserting the new behaviour. |
| **Verbose `ExecTool.description`** | Upstream's concise one | Upstream has `test_exec_tool_descriptions_are_concise` enforcing brevity; the guidance moved into the prompt templates. |
| **Fork's removal of `ToolRegistry.prepare_call`** (commit `1d18d24`, worked around in `execute()`) | Upstream reinstated and extended `prepare_call` | Fork's re-targeted tests reverted to upstream's. |
| **`get_workspace_path` in `nanobot/utils/helpers.py`** | `nanobot/config/paths.py` | Upstream's is canonical; ours stays only as a re-export shim. |

---

## 3. Local features re-applied on upstream's new architecture

| Feature | Where it lives now | Note |
| --- | --- | --- |
| **Langfuse turn span (MIT-202)** | `loop.py` — `_process_message` is now a thin `observe_turn` wrapper around `_process_message_impl` | Unchanged shape; `**kwargs` passthrough so upstream can keep adding parameters. |
| **Langfuse iteration span (MIT-202/210)** | `runner.py` — `with observe_llm_iteration(...)` wraps the entire agent-iteration body | Model name now comes from `spec.runtime.model` (upstream moved it onto `LLMRuntime`). |
| **Langfuse tool span + input redaction (MIT-202/211)** | `nanobot/agent/tools/execution.py` | Upstream moved tool dispatch out of `runner.py` into this module, so the span moved with it. |
| **Langfuse subagent span (MIT-186)** | `subagent.py` — `trace_context` threaded through `_run_subagent` → `_run_admitted_subagent`, span wraps `runner.run` | |
| **LLM latency (`AgentHookContext.latency_ms`)** | `runner.py`, using `time.monotonic()` | Deliberately **not** `perf_counter`: upstream's provider-timing tests patch `perf_counter` with an exact `side_effect` budget, and an extra call broke them. |
| **Discord progress heartbeat + LLM telemetry** | `loop.py` — new `_ZiggyTurnHook(AgentHook)`, registered through `self._hook_factories` | The old `_LoopHook` was wired into the removed positional-callback pipeline. Rebuilt against upstream's `AgentTurnHookContext` (which carries channel / chat_id / metadata) and publishes `_status_delta` straight onto the bus, which decouples it from the callback plumbing entirely. |
| **`_status_delta` channel dispatch** | `channels/manager.py::_send_once`, checked before upstream's event dispatch | |
| **ChromaDB RAG (`recall` / `ingest`)** | `nanobot/agent/tools/recall.py` — now exposes `enabled(ctx)` / `create(ctx)` | Upstream replaced manual registration with an auto-discovering `ToolLoader`. `enabled()` gates on `importlib.util.find_spec("chromadb")`, so an install without the `ziggy` extra stays clean. Verified: both tools are discovered and registered when chromadb is present, and skipped when it is not. |
| **Audit log + Prometheus (MIT-203)** | `tools/registry.py` — `_audit` / `_prom_observe` | Rebuilt on `ToolResult`. Invalid-parameter rejections (now returned by upstream's `prepare_call`) are audited as `prescreen`. |
| **Secret redaction (MIT-122/147)** | `tools/registry.py::execute` | Both success and error branches still run `redact_if_sensitive`. The old "re-prefix the scrubbed body with `Error:`" hack is **gone** — the error flag now rides on the rewrapped `ToolResult`, which is a strictly better version of the same guarantee. |
| **Sensitive-path blocking (MIT-121/136/139/140)** | `tools/filesystem.py`, `tools/search.py`, `utils/sensitive.py` | Re-applied against upstream's new resolvers; `utils/sensitive.py` merged without conflict. |
| **Shell prescreen (MIT-123)** | `tools/shell.py::_guard_command`, now the **last** check | Moved to the end so an out-of-workspace path keeps upstream's more specific `path outside working dir` message. |
| **Exec hardening (MIT-203)** | `shell.ExecToolConfig.timeout = 180`, `allow_loopback` | `allow_loopback` now ORs with upstream's `current_scope_allows_loopback`: an explicit per-instance `True` wins, `None`/`False` defers to upstream's WebUI-scoped check. |
| **Prompt-injection sanitising** | `channels/base.py` | Unchanged. |
| **Owner-only system-modification guard** | `loop.py::_dispatch` | Unchanged; the fork's duplicate pending-queue setup next to it was dropped (upstream does that inside the session lock). |
| **`CustomProvider` (MiniMax M2.5 sampling)** | `providers/custom_provider.py` | Merged cleanly; re-added to the expected `__all__` in `tests/providers/test_providers_init.py`. |
| **Dashboard / Prometheus server** | `nanobot/dashboard/` | Merged cleanly, no conflicts. |

---

## 4. Not in this merge

- **Shared rooms.** The Mac lock (`.ziggy/nanobot.lock.json`) names fork commit `e9daa2a0`, which lives only on the fork's `feat/shared-rooms` branch and was never merged to `ziggy-main`. It is therefore **not** in this merge. It must be rebased onto `ziggy-main-upgrade-2026-09` separately — and note that upstream restructured `nanobot/channels/websocket.py` into a `nanobot/channels/websocket/` package (`runtime.py`, `manifest.py`, `validation.py`, `webui/`), so that rebase is non-trivial.
- **The Spark's other uncommitted work.** `cron.py`, `cron/service.py`, `cron/types.py`, `cli/commands.py`, `nanobot/work/`, `agent/tools/work.py`, `agent/tools/schedule_work.py`, `utils/vision.py`, and the `webui/` changes are all uncommitted on the Spark and exist in no repository. They are **not** in this merge and **will be lost** by the rollout below unless they are committed first. See step 0 of the rollout.
- The fork's other ~27 topic branches were not reviewed.

---

## 5. Behaviour changes worth knowing before deploying

1. **Deliberate fork divergence: the exec safety guard stays unconditional.**
   Upstream's `_prepare_command` calls `_guard_command` only under
   `if access.restrict_to_workspace:` — "full access is an explicit trust
   decision". But `restrict_to_workspace` **defaults to `False` in the schema**,
   so a config that simply never set it is treated identically to a deliberate
   grant, and loses the deny-pattern filter, the SSRF / internal-URL check and
   the MIT-123 secret-dump prescreen along with the workspace boundary. None of
   those three are workspace-confinement policy, and all three ran
   unconditionally in the fork before this merge.

   The Spark's Discord gateway (`~/.nanobot/config.json`) runs in exactly that
   shape — `restrict_to_workspace` unset, `exec.enable` unset (defaults `True`),
   `allow_loopback: true` — so taking upstream verbatim would have silently
   removed the fork's entire shell-safety layer on deploy. Verified read-only on
   `spark-094a`.

   The fork therefore narrows the skip to an **explicitly bound** unrestricted
   workspace scope (the WebUI Full Access grant, where `access.scope is not
   None`). Upstream's `test_exec_full_workspace_scope_skips_command_guard`
   still passes unchanged. Upstream's
   `test_exec_full_access_skips_command_guard` — which asserts the
   *config-default* case also skips — is forked into
   `test_exec_unrestricted_config_still_applies_the_command_guard`, plus two
   tests pinning that this does **not** become a blanket block and does **not**
   start confining paths. See `nanobot/agent/tools/shell.py` in
   `_prepare_command`. **Expect this to conflict on the next upstream merge;
   keep the divergence.**

   The three `nanobot-tenant@ws_*` runtimes on the Spark all have
   `exec.enable: false`, so they were never exposed either way.

   Two fork tests in `tests/security/test_allow_loopback.py` were still updated
   to construct `ExecTool(restrict_to_workspace=True)`, because they assert on
   the *workspace-gated* portion of the guard.
2. **Loopback allowance narrowed.** Upstream permits loopback only when the
   *host itself* is a literal loopback name/IP and every resolved address is
   loopback — a public DNS name that resolves to `127.0.0.1` stays blocked
   (DNS-rebinding defence). Ziggy's `configure_loopback_exception` module
   default still works, but through this narrower gate.
3. **Tool failure detection is structural.** Any Ziggy or MCP tool that signals
   failure by returning a bare `"Error: ..."` string is now treated as a
   *success*. Tools must return `ToolResult.error(...)`. Upstream wraps
   entry-point plugins in `_LegacyErrorPrefixTool` for compatibility, but
   in-tree tools get no such wrapper.
4. **`nanobot-ai` version.** This branch is upstream ≥ v0.3.0, which supersedes
   the dependency audit's "bump `nanobot-ai` to ≥ 0.2.1 for the web-tool SSRF
   (GHSA-434r-7c99-hwf3)" item. **Do not `pip install nanobot-ai`** on the
   Spark — it is an editable install pointing at this checkout, and a PyPI
   install would silently replace the fork with an upstream wheel.

---

## 5b. Security review findings and fixes

A security review of the merge found five controls that the merge dropped or
that upstream routed around. All are fixed on this branch; each has a
regression test in `tests/security/test_ziggy_merge_regressions.py` or
`tests/tools/test_exec_security.py`.

| # | Finding | Fix |
| --- | --- | --- |
| 1 | **The first attempt at the §5.1 fix was a no-op.** It gated on `access.scope is not None`, but `AgentLoop` binds a scope on *every* turn and `WorkspaceScopeResolver.for_turn` returns `default()` for any non-websocket channel — a real object with `access_mode="full"`, `restrict_to_workspace=False`, `source_channel=None`. So the guard was still skipped on every Discord/Telegram/CLI turn. The tests passed only because they called `execute()` with **no scope bound**, a state that never occurs in the running gateway. | Gate on `source_channel == "websocket" and access_mode == "full"`, mirroring `current_scope_allows_loopback`. Added `test_exec_guard_holds_under_a_default_bound_scope` (which binds a scope exactly the way `AgentLoop` does) and `test_prepare_command_applies_the_mit123_prescreen` (covering the production wiring, not just `_guard_command` in isolation). |
| 2 | **`apply_patch` bypassed the MIT-121 sensitive-path guard.** The tool is upstream-new, is described in its own schema as the "default tool for code edits", and resolved writes with no blocklist check — so anything `edit_file` refused could be routed through it. `write_file` never had the check either. | Hoisted the guard into `_FsTool._resolve_write`, which now raises `SensitivePathError` (a `PermissionError`, already funnelled to a clean `ToolResult.error` by every write tool). Structural, so future write tools inherit it instead of each re-implementing it. `edit_file`'s redundant post-resolve check removed. |
| 3 | **`ingest` had no containment and no blocklist.** `create()` hardcoded `allowed_dir=None`, and `execute()` accepted any absolute path with no `is_sensitive_path` check — so `ingest("~/.config/gcloud/application_default_credentials.json")` loaded credentials into the RAG store, where `recall` hands them back to the model. Upstream's auto-discovering loader widened the exposure from "registered by `loop.py`" to "registered whenever chromadb is importable". | `create()` now honours `restrict_to_workspace`; `execute()` checks the raw and resolved path; `RAGStore.ingest_directory` skips sensitive files per-file (the suffix filter alone is not enough — `credentials.json` is `.json`) and reports the skip count. |
| 4 | **`validate_resolved_url` used the wide loopback gate.** It passed the flag straight into `_is_private`, with no literal-host and no all-addresses requirement — so with `allowLoopback` on, a public URL that 302s to `127.0.0.1` was accepted. That is exactly the DNS-rebinding case upstream hardened the forward path against. | Use `_is_allowed_loopback_target`, the same narrow gate as `resolve_url_target`. `_is_private` is now byte-identical to upstream again (its `allow_loopback` kwarg had no other caller). |
| 5 | **Fork security errors were no longer classified as failures.** MIT-136's direct-file grep block and every error return in `recall.py` returned bare strings, which under the new `ToolResult.is_error` contract (§5.3) are *successes* — so they were audited as `ok`, never became non-retryable security hints, and the model was free to retry variants in a loop. No content leaked; this was an observability and retry-loop defect. | All wrapped in `ToolResult.error(...)`. |

Also removed: an unreachable block in `channels/base.py::is_allowed` (it sat
after `return False`). It used to match each component of a composite
`"<id>|<username>"` sender id, which the Telegram and Signal runtimes build.
Upstream requires an exact match on the whole token, and upstream is kept —
usernames are mutable on Telegram, so matching the username component lets
anyone who claims that handle inherit the allowlist entry. **Consequence:
`allowFrom` entries for Telegram and Signal that list a bare numeric id or a
bare username now fail closed and get a pairing code instead. Audit and migrate
them to the full `"<id>|<username>"` token before restarting those channels.**

Two findings are recorded but not fixed here, because both are pre-existing and
neither is a merge regression: the `nanobot.utils.security.sanitize_input`
prompt-injection layer does not cover the upstream-new Signal channel (which
overrides `_handle_message` and publishes to the bus directly), and
`prepare_call` rejection strings skip the redactor (they do not echo parameter
values, so there is no known leak).

---

## 5c. One thing that will make the *next* merge noisy

`nanobot/agent/runner.py` diffs **649 lines** against upstream, but with
`git diff -w` it is **15 lines**. The rest is pure reindentation: the MIT-202
Langfuse span wraps the whole agent-iteration body, so ~350 lines of upstream
code shifted right by four spaces.

```
$ git diff --stat    upstream/main HEAD -- nanobot/agent/runner.py   # 649 changed
$ git diff --stat -w upstream/main HEAD -- nanobot/agent/runner.py   #  15 added
```

That is a real cost — `runner.py` is one of upstream's most-churned files, and a
whitespace-shifted block conflicts on almost every hunk. It was kept anyway
because the alternatives are worse:

- Entering the span on an `ExitStack` and closing it at the top of the next
  iteration would keep the original indentation, but `ExitStack.close()` does
  not pass `exc_info` into the context manager, which breaks MIT-210 (exceptions
  must propagate through the span so failed turns are not recorded as
  successful).
- Extracting the loop body into a helper is a larger refactor of upstream code,
  which makes future merges harder, not easier.

**For the next merge:** take `runner.py` from upstream wholesale, then re-apply
the 15 lines. `git diff -w upstream/main HEAD -- nanobot/agent/runner.py` prints
exactly what to re-apply. Do not try to merge it hunk by hunk.

---

## 6. Known failures (pre-existing, not caused by this merge)

Both reproduce on a pristine `upstream/main` checkout:

| Test | Cause |
| --- | --- |
| `tests/channels/test_channel_setup.py::test_every_runtime_channel_field_has_a_webui_contract` | Optional channel deps missing (`nh3`, `matrix-nio`). Install the matrix plugin extras or accept the skip. |
| `tests/session/test_recovery.py::test_bus_remains_quiet_after_recovered_state` | Upstream test leaks a `SessionUpdatedEvent` onto the bus. Upstream bug. |

`ruff check nanobot/` is clean for `F` and `E9`. The remaining ~80 style
findings (`W293`, `I001`, `N802`) are all in fork-only files and pre-date this
merge; upstream's own tree is ruff-clean.

---

## 7. Spark rollout — DO NOT RUN FROM THIS BRANCH WITHOUT READING STEP 0

Target: `/home/mihai/workspace/ziggy/vendor/nanobot` on `spark-094a`, currently
at `71d5923d` on `ziggy-main` with a **dirty tree**. The runtime is an editable
install into `/home/mihai/workspace/ziggy/.venv`. The units are **user** units
(`systemctl --user`), not system units.

### Step 0 — preserve the Spark's uncommitted work (MANDATORY)

The Spark carries roughly 12 modified and 10 untracked files that exist in no
repository. A checkout will destroy them.

```bash
ssh mihai@spark-094a.local
cd /home/mihai/workspace/ziggy/vendor/nanobot

# Full snapshot of the working tree, tracked and untracked, outside the repo.
tar czf ~/nanobot-spark-worktree-$(date +%Y%m%dT%H%M%S).tar.gz .

# And a reviewable patch of just the tracked modifications.
git diff > ~/nanobot-spark-tracked-$(date +%Y%m%dT%H%M%S).patch
git status --porcelain > ~/nanobot-spark-status-$(date +%Y%m%dT%H%M%S).txt

# Commit them on a rescue branch so they are recoverable by ref, not just by tarball.
git checkout -b spark-rescue-$(date +%Y%m%d)
git add -A
git commit -m "chore(spark): snapshot uncommitted Spark working tree before upstream merge"
git push origin HEAD
```

Files that are *deliberately* discarded and must **not** be carried forward:

- `nanobot/agent/tools/shell.py.bak-20260913T213019` — the hand-written
  `/dev/null` patch, superseded by upstream #3613/#3614.
- `nanobot/channels/websocket.py.pre-clerk-*` (5 files) — stale backups of a
  file upstream has since turned into a package.

### Step 1 — record the rollback point

```bash
cd /home/mihai/workspace/ziggy/vendor/nanobot
git rev-parse HEAD > ~/nanobot-rollback-commit.txt      # expect 71d5923d...
/home/mihai/workspace/ziggy/.venv/bin/pip list --format=freeze \
  > ~/nanobot-venv-rollback-$(date +%Y%m%dT%H%M%S).txt
```

### Step 2 — fetch and check out the upgrade branch

```bash
cd /home/mihai/workspace/ziggy/vendor/nanobot
git fetch origin ziggy-main-upgrade-2026-09
git checkout ziggy-main-upgrade-2026-09
git status --short     # must be empty
git log --oneline -1
```

### Step 3 — refresh the editable install

The dependency set moved a long way across four upstream releases, so `-e .`
alone is not enough; the extras must be named or `recall`/`ingest` and the
Langfuse hooks silently disappear.

```bash
cd /home/mihai/workspace/ziggy
./.venv/bin/pip install -e "vendor/nanobot[ziggy]"
./.venv/bin/pip check
./.venv/bin/python -c "import nanobot; print(nanobot.__version__, nanobot.__file__)"
./.venv/bin/python -c "
from nanobot.agent.tools.loader import ToolLoader
names = sorted(c.__name__ for c in ToolLoader().discover())
assert 'RecallTool' in names and 'IngestTool' in names, names
print('RAG tools discovered OK')
"
```

`pip check` should now come back **clean**. The two long-standing complaints on
this venv —

```
nanobot-ai has requirement dulwich<1.0.0,>=0.22.0, but you have dulwich 1.2.12
nanobot-ai has requirement pypdf<6.0.0,>=5.0.0,  but you have pypdf 6.14.2
```

— are fixed on this branch by raising both caps in `pyproject.toml`
(`dulwich<2.0.0`, `pypdf<7.0.0`). That matters beyond tidiness: with upstream's
caps, `pip install -e` would have **downgraded pypdf back out of the range that
fixes six resource-exhaustion advisories** on attacker-supplied PDFs. The full
suite is verified green on dulwich 1.2.12 + pypdf 6.16.1.

Note `chromadb>=0.6.0,<1.0.0` in the `ziggy` extra: the audit's Tier 3 item 27
(chromadb → 1.x, for the code-injection and tenant-blind RBAC advisories) is now
a change to this `pyproject.toml`, not a venv-side bump.

### Step 4 — confirm the exec-guard posture

No config change is required: §5.1 keeps the command guard unconditional for
config-default-unrestricted runtimes, which is what the gateway is. Confirm
the shape has not drifted since this was written:

```bash
python3 -c '
import json
d = json.load(open("/home/mihai/.nanobot/config.json"))
t = d.get("tools", {})
print("restrict_to_workspace:", t.get("restrict_to_workspace"))
print("exec:", t.get("exec"))
'
```

Expected at the time of writing: `restrict_to_workspace` unset (schema default
`False`), `exec.allow_loopback: true`. If someone has since set
`restrict_to_workspace: true`, that is fine and strictly stricter.

### Step 4b — migrate Telegram / Signal allowlists (if those channels are used)

Per §5b, composite sender ids must now match exactly. Before restarting
either channel, convert every `allowFrom` entry to the full
`"<id>|<username>"` token, or those senders will be denied and handed a
pairing code. The Spark currently runs Discord and websocket tenants, so this
is a no-op today — check anyway.

```bash
python3 -c '
import json
d = json.load(open("/home/mihai/.nanobot/config.json"))
for name, ch in (d.get("channels") or {}).items():
    if name in {"telegram", "signal"} and isinstance(ch, dict):
        print(name, ch.get("allowFrom"))
'
```

### Step 5 — restart, one tenant first

```bash
systemctl --user daemon-reload
systemctl --user restart nanobot-tenant@ws_584aff5a-a0a5-4b78-8a4f-89baf3847ce7.service
systemctl --user status  nanobot-tenant@ws_584aff5a-a0a5-4b78-8a4f-89baf3847ce7.service
journalctl --user -u nanobot-tenant@ws_584aff5a-a0a5-4b78-8a4f-89baf3847ce7.service -n 200 --no-pager
```

Smoke-test that one tenant end to end — send a message, run a tool, confirm a
reply arrives — then do the rest:

```bash
systemctl --user restart nanobot-tenant@ws_aa52c124-7112-49b1-9157-c3afb2300c37.service
systemctl --user restart nanobot-tenant@ws_dee4fcfe-c759-4916-8a2c-55513b96dc7e.service
systemctl --user restart nanobot-gateway.service
systemctl --user restart nanobot-dashboard.service
```

`nanobot-analytics.service` and `nanobot-observability.service` are `exited`
one-shots and do not need restarting.

### Step 6 — post-deploy checks

- `rm file.txt 2>/dev/null` from a tenant → must succeed (this is #3599).
- Force a tool error → the reply must reach the channel (this is the
  `tool_error` streamed-drop fix, live-broken until now).
- Langfuse: one trace per turn, with `llm-iteration` → `tool:<name>` nested
  underneath, and subagents as children.
- Discord: the progress heartbeat still edits one message per turn.
- `recall` / `ingest` present in the tool list.

### Rollback

```bash
cd /home/mihai/workspace/ziggy/vendor/nanobot
git checkout $(cat ~/nanobot-rollback-commit.txt)        # 71d5923d
# restore the Spark's working tree from the rescue branch or the tarball
git checkout spark-rescue-<date> -- .
cd /home/mihai/workspace/ziggy
./.venv/bin/pip install -e vendor/nanobot
systemctl --user restart nanobot-gateway.service nanobot-dashboard.service
systemctl --user restart 'nanobot-tenant@*.service'
```

Rollback returns to a runtime with **both** the `/dev/null` false positive and
the streamed-reply drop unfixed, so treat it as a short-lived state.

### Also update after a successful rollout

`.ziggy/nanobot.lock.json` in the main repo currently disagrees with itself
three ways (Mac lock `e9daa2a0`, Spark lock `2c45561c`, Spark actual
`71d5923d`, installed package `0.1.5.post2` against a `post3` baseline). Once
this branch is deployed, set **both** lock files to the same value:
`upstream_baseline` → `HKUDS/nanobot` @ `499bf903` (v0.3.0 line),
`effective_runtime.commit` → this branch's merge commit.
