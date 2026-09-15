# Rebase inventory: shared rooms + `work_task` cron onto 0.3.0

Branch: `ziggy/rebase-shared-rooms-work` off `ziggy-main-upgrade-2026-09` (`797a7bfc`).
Source of truth for current behaviour: the deployed snapshot
`spark-094a:/home/mihai/workspace/ziggy/releases/editorial-20260910/runtime`
(fork `0.1.5.post3`), read-only. Companion memos:
`docs/research/nanobot-030-cutover-readiness.md` (C2/C3/C4/C5/C6) and
`docs/research/nanobot-upstream-whats-new-2026-09.md`.

Linear: MIT-1010.

---

## 1. Snapshot files that carry shared rooms

`grep -rl shared_room` in the snapshot finds 8 `.py` files under `nanobot/`
(plus 7 test files). Three further modules are pulled in by them but do not
themselves mention `shared_room`; they are listed under "Support modules".

| # | Snapshot file | What it implements | 0.3.0 home | Notes |
| - | --- | --- | --- | --- |
| 1 | `nanobot/channels/collaborative_rooms.py` (326 L) | `CollaborativeRooms` mixin: room-v1 intents (`discussion` / `proposal` / `ask_ziggy`), owner-only review + publication HTTP surface (`preview`/`upgrade`/`state`/`prepare`/`approve`/`publish`/`decline`), presence broadcast, room liveness (`_room_is_active`) | **split.** Mixin state + intent handling → `nanobot/channels/websocket/rooms.py`; the seven HTTP actions → `nanobot/webui/shared_rooms_http.py`, dispatched from `WebUIHTTPRouter` | Imports `_http_error` etc. from `channels.websocket` — those helpers now live in `nanobot/webui/http_utils.py` |
| 2 | `nanobot/channels/websocket.py` (3267 L; ~1234 L of room diff) | `_RoomCredential`, `_ROOM_ID_RE`/`_PARTICIPANT_ID_RE`, `_room_ws_tokens`/`_room_api_tokens`/`_conn_room` pools, `_take_room_token_if_valid` handshake path, `_room_api_credential` REST path, `/auth/shared-room*` routes, room-scoped `/api/sessions/<key>/messages` + `/files/<id>`, inbound `shared_room` metadata stamping | **split three ways.** Credential dataclass + token pools + connection scope + inbound metadata → `nanobot/channels/websocket/rooms.py` and `runtime.py`; `/auth/shared-room*` + room-scoped session reads → `nanobot/webui/shared_rooms_http.py`; regexes/config fields → `runtime.py` `WebSocketConfig` | Upstream `462a0dfb`/`8423cf3e`/`281b4b7f` split the monolith: HTTP is `webui/ws_http.py`, inbound frames are `webui/inbound_commands.py`, transport is `channels/websocket/runtime.py` |
| 3 | `nanobot/session/manager.py` | `clone_session(shared_room_owner=…)`, `_shareable_messages()` redaction, `set_session_title(room_id=, title_revision=)` revision protocol, `read_session_file` | `nanobot/session/manager.py` (same file) | Pure-ish; the redaction allowlist is the content boundary and ports as-is |
| 4 | `nanobot/agent/loop.py` | `shared_room = bool(msg.metadata.get("shared_room"))` → skip slash-command dispatch, skip `pending_ask_user_id`, skip memory `raw_archive`, stamp `extra["shared_room"]` on saved turns | `nanobot/agent/loop.py` | 0.3.0 reindented `runner.py`/`loop.py`; hunks re-homed by hand |
| 5 | `nanobot/agent/context.py` | Participant attribution: prefix history lines with `participant_display_name` when `shared_room` | `nanobot/agent/context.py` | |
| 6 | `nanobot/agent/autocompact.py` | Skip auto-compaction for shared-room sessions (compaction would rewrite guest-visible history) | `nanobot/agent/autocompact.py` | |
| 7 | `nanobot/cli/commands.py` | Wires `connected_room_executor` when `sharedRoomCollaborationEnabled`, reading `sharedRoomConnectorServer`; also the `work_task` cron branch | **split.** Room executor wiring → `nanobot/cli/gateway_runtime.py` (0.3.0 moved gateway wiring out of `commands.py`); `work_task` branch → `nanobot/cron/work_runner.py` + `gateway_runtime.on_cron_job` | |
| 8 | `nanobot/agent/tools/briefing.py` | Briefing tool refuses to operate inside a shared room | **not ported here.** Tracked as C8 (briefing/editorial carry-forward) — separate work item | Listed for completeness; `grep shared_room` hits it |

### Support modules (no `shared_room` string, required by the above)

| Snapshot file | What it implements | 0.3.0 home |
| --- | --- | --- |
| `nanobot/channels/room_work.py` (233 L) | `RoomWorkStore` — flock'd JSON proposal store, `canonical_action`/`action_hash`, `propose`/`consume`/`finish`/`publish`, `connected_read_executor(agent, server_name)` bounded to `gmail_search` + `gmail_get_message` | `nanobot/channels/websocket/room_work.py` (verbatim; pure stdlib) |
| `nanobot/channels/chat_inbox.py` (453 L) | `ChatInboxStore` — SQLite durable, ordered, exactly-once chat inbox backing room `ask_ziggy` and normal delivery | `nanobot/channels/websocket/chat_inbox.py` (verbatim) |
| `nanobot/channels/websocket_server.py` (183 L) | aiohttp transport adapter for the websocket channel | **not ported.** 0.3.0's `runtime.py` serves via `websockets.asyncio.server` with its own HTTP dispatch; the aiohttp shim is fork-local dead weight after the repackage |

---

## 2. Cron / Work inventory

### Payload kinds

| Kind | Snapshot (`cron/types.py:24`) | 0.3.0 (`cron/types.py`) | Meaning |
| --- | --- | --- | --- |
| `system_event` | yes | yes | internal job (`dream`, `heartbeat`); runs outside the agent loop |
| `agent_turn` | yes | yes | a normal session turn; 0.3.0 requires session binding (`b24b5f19`) |
| `work_task` | **yes** | **no — dropped** | creates a durable Work task, then runs the prompt against a dedicated `cron:<job_id>` session |

### What `work_task` did (snapshot `cli/commands.py:708-800`)

1. Read `channel_meta` for `work_chat_id`, `work_title`, `work_plan_task_id`, `work_deliverable`.
2. `WorkStore.create_task(session_key=f"cron:{job.id}", chat_id=…, mode="scheduled", title=…)`.
3. If `work_plan_task_id` looks like `work_…`, append a `scheduled_run.created` event to the *plan* task.
4. `agent.process_direct(message, session_key=f"cron:{job.id}", metadata={"work_task_id":…, "work_mode":"scheduled", "cron_job_id":…})`.
5. On `CancelledError` → `update_status(task, "interrupted")` + `scheduled_run.interrupted`; on exception → `"failed"` + `scheduled_run.failed`.

The deliberate design point is step 2/4: **scheduled Work gets its own durable
session**, so a 25-minute job cannot serialize against, or inject history into,
the live chat the owner created it from. `payload.session_key` is recorded but
deliberately *not* used as the execution session.

### The four owner `work_task` jobs (read-only from `~/.nanobot/workspace/cron/jobs.json`)

All four are `enabled: true`, `deliver: false`, `channel: "websocket"`,
`delete_after_run: false`, and all four already carry
`sessionKey == "websocket:" + to`, so upstream's session-binding requirement is
satisfied by the data already on disk.

| id | name | schedule | tz | `to` / `work_chat_id` | `work_deliverable` | extra `channelMeta` |
| --- | --- | --- | --- | --- | --- | --- |
| `cbc6a67c` | Daily backend health & upstream updates digest | `16 19 * * *` | UTC | `bbed5fcf-25b6-…-ba5f489dac34` | `markdown_digest` | `_scope: owner`, `work_scope: owner`, `_wants_stream` |
| `2ccb6d82` | Weekly Acquire.com Deal Analysis | `0 10 * * 1` | Pacific/Auckland | `D29F9818-…-C43ECC05B3C4` | `markdown_digest` | `_wants_stream` |
| `e971284f` | Daily Newsletter Digest with App Update | `0 15 * * *` | America/Los_Angeles | `a519f4a3-…-a7da3a99fde5` | `curated_digest_in_work_app` | `_wants_stream`, `client_message_id`, `reasoning_profile: auto` |
| `92d1142e` | Daily Acquire.com Opportunity Digest + Briefs | `30 18 * * *` | America/Los_Angeles | `chat_619ae8b697b6424187dfc9f7511b16d7` | `report` | `_wants_stream`, `client_message_id`, `reasoning_profile: auto`, `explicit_final_message: true` |

The other five jobs are `agent_turn` × 4 (`ccdc733f`, `cf268c26`, `ec348672`,
`c5ca2d08`) and `system_event` × 1 (`dream`). `ccdc733f` is the one with
`schedule.tz = null` flagged as C12 — unchanged by this work.

### Work tools on `rescue/spark-2026-09-14` (`cd8c0c48`)

| File | Size | Purpose |
| --- | --- | --- |
| `nanobot/agent/tools/work.py` | 118 L | `work` tool — report progress / deliverables into the durable Work task for the current turn |
| `nanobot/agent/tools/schedule_work.py` | 536 L | `schedule_work` tool — interview-then-schedule; creates the cron job with `payload_kind="work_task"` (`schedule_work.py:312`) |
| `nanobot/work/{__init__,context,store}.py` | 5 / 32 / 779 L | `WorkStore` (SQLite), `WorkContext` contextvar |
| `nanobot/cron/service.py` | +3 L | `add_job(payload_kind=…)` |
| `nanobot/cron/types.py` | +1 L | `CronPayload.kind` literal gains `"work_task"` |

---

## 3. What 0.3.0 changed underneath, and the decision taken

### 3a. Channels became packages

`nanobot/channels/websocket.py` → `nanobot/channels/websocket/{__init__,runtime,manifest,validation}.py`
(`462a0dfb`, `8423cf3e`, `281b4b7f`), with HTTP in `nanobot/webui/ws_http.py`
(1906 L) and inbound frames in `nanobot/webui/inbound_commands.py` (991 L).
The `nanobot.channels` entry-point group is gone, so an out-of-tree channel is
never loaded. **Decision: shared rooms live in-tree**, as new modules inside
`nanobot/channels/websocket/` plus one `nanobot/webui/shared_rooms_http.py`
router that `WebUIHTTPRouter._dispatch_resolved` delegates to.

### 3b. `Tool.available()` and request-scoped session grants were deleted (`6e9ae5bd`)

**Finding: C3's premise is wrong in one direction and understated in the other.**

The deployed snapshot has no `Tool.available()` anywhere — `grep -rn 'def available'`
in the snapshot returns nothing. Shared-room authorization never keyed off it.
The snapshot's real model is a *connection-scoped credential* plus an
*inbound-metadata flag* (see §4).

What `6e9ae5bd` actually broke for Ziggy is different and worse: 0.3.0 **adds**
`search_sessions`, `read_session`, `list_sessions` and `send_session_message`,
and that commit removed the only scope restriction they ever had
(`INBOUND_META_SESSION_READ_SCOPE`, `SessionAccessScope`). On 0.3.0 those tools
read *any* persisted session by key. In a shared room a guest's message drives
the agent's turn, so at cutover a guest could prompt the room agent into
`read_session websocket:<owner's private chat>`. That is the hole to close.

### 3c. `CronPayload.kind` dropped `work_task`; jobs must be session-bound; corrupt `jobs.json` hard-fails

- `work_task` restored on `CronPayload.kind`, with a `work_task` branch in
  `gateway_runtime.on_cron_job` and the session-binding contract met (§5).
- **Malformed-entry tolerance: per-entry quarantine, whole-file hard-fail kept.**
  Upstream's whole-file `.corrupt-<ts>` + `RuntimeError` is deliberate and
  correct — silently treating an unparseable file as empty would let the next
  save wipe every job. But it is too blunt for *one* bad entry. `_load_jobs`
  now parses each `jobs[]` element individually: a element that raises is
  written to `jobs.json.quarantine-<ts>.jsonl` with the exception text, logged
  at `ERROR`, and skipped; the remaining jobs load and the gateway starts. A
  file that is not valid JSON, or whose top level is not an object with a
  `jobs` list, still takes the whole-file path unchanged.

### 3d. Contract changes respected

- `/api/sessions/<key>/messages` deleted in favour of `/webui-thread`. The
  **room-scoped** variant is reinstated (§6) because the guest read path and
  `shared_rooms.go:607` depend on it; the owner/WebUI path is unaffected and
  stays on `/webui-thread`.
- Mutations require an authenticated `webui_request` frame — the room intent
  frames are not WebUI mutations and are dispatched before that gate, on a
  room-credentialled connection only.
- `enabledTools` narrowing now also disables MCP resources/prompts — noted for
  the connector-server allowlist that `sharedRoomConnectorServer` names; no
  change needed because the connected-read executor calls a tool, not a
  resource.

---

## 4. Authorization model

### Snapshot model (what exists today)

1. **Control-plane routes** `/auth/shared-rooms*` are authorized by the
   tenant-wide `tokenIssueSecret` header. Only ziggy-control calls them.
2. **`_RoomCredential`** `{expires_at, room_id, chat_id, participant_id,
   display_name, role}` is minted as an `nbrt_…` token into two pools:
   `_room_ws_tokens` (single-use, consumed at handshake) and `_room_api_tokens`
   (multi-use REST, valid until TTL).
3. **Connection scope**: after handshake `_conn_room[connection] = credential`;
   a guest frame can only address `credential.chat_id`.
4. **REST scope**: `_room_api_credential(request)` authorizes exactly
   `websocket:{credential.chat_id}` — nothing else, ever.
5. **Liveness** is re-checked on every path (`_room_is_active`): not
   `shared_room_revoked`, and `shared_room_expires_at` still in the future.
   Revoke drops both pools and force-closes with 1008.
6. **Content boundary**: `_shareable_messages()` copies only
   role/content/timestamp/`client_message_id(s)`/participant fields of
   `user`+`assistant` messages. Summaries, tool context, instruction metadata
   and published-file grants are dropped; only server-verified grants are
   re-attached.
7. **Turn isolation**: `metadata["shared_room"] = True` makes `loop.py` skip
   slash-command dispatch, `pending_ask_user` resumption and memory archival,
   makes `context.py` attribute each line to its participant, and makes
   `autocompact.py` refuse to rewrite guest-visible history.

### 0.3.0 model (what is implemented on this branch)

Points 1–7 port unchanged — they never depended on `Tool.available()`.
The replacement for the deleted request-scoped grant is:

- **`ROOM_SCOPE` on the request context.** `runtime.py` stamps
  `metadata[INBOUND_META_ROOM_SCOPE] = {"room_id","chat_id","participant_id","role"}`
  on every inbound frame from a room-credentialled connection *and* on owner
  turns whose session metadata says `shared_room`. It rides the existing
  `RequestContext.metadata` that `6e9ae5bd` left in place, so no new plumbing.
- **The gate moves from `Tool.available()` to `ToolRegistry.prepare_call()`.**
  `prepare_call` is the single funnel every tool call passes through and it
  still exists. A new `Tool.room_policy()` on the base class defaults to
  `RoomPolicy.ALLOWED`; tools that cross a session boundary
  (`search_sessions`, `read_session`, `list_sessions`, `send_session_message`),
  mutate runtime state (`my`, `cron`, `schedule_work`) or touch memory
  (`recall`, `ingest`) declare `RoomPolicy.DENIED`. When a room scope is bound,
  `prepare_call` returns `ToolResult.error(...)` for a `DENIED` tool. Unknown
  and third-party tools default to `ALLOWED` — the same posture as today,
  since today a room turn can already call every registered tool.
- **Defence in depth in `WebuiSessionAccess`.** `search`/`read`/
  `normalize_mentions` gain an `allowed_session_key: str | None` narrow.
  A room turn passes its own key, so even a tool reached another way resolves
  nothing but the room's own session. This restores a *narrower* version of the
  deleted `SessionAccessScope`, scoped to one key rather than to a namespace
  prefix — it does not re-widen anything.

Fail-closed by classification, never by widening: a tool has to be explicitly
`ALLOWED` at the class level to be reachable from a room turn, and the session
reader has to be handed the exact key.

---

## 5. `work_task` session binding — decision

`is_bound_cron_job()` requires `kind == "agent_turn"` **and** `session_key` and
`origin_channel`/`origin_chat_id` set **and** the legacy `deliver`/`channel`/
`to`/`channel_meta` fields cleared. `work_task` cannot satisfy that as written,
because the whole point of the kind is that its execution session is *not* the
originating session.

**Decision: bind the origin, execute in a dedicated session.**

- `_normalize_work_task_job()` runs the same migration `_normalize_agent_turn_job`
  does — `channel`/`to` → `origin_channel`/`origin_chat_id`,
  `channel_meta` → `origin_metadata`, legacy fields cleared, `session_key`
  preserved — so a `work_task` job becomes fully session-bound in the upstream
  sense and the `work_*` keys survive in `origin_metadata`.
- `is_bound_cron_job()` accepts `work_task` alongside `agent_turn`.
- **Execution still uses `cron:<job_id>`**, not `payload.session_key`. The
  bound `session_key` is the *origin* — where the task was created and where
  its result is delivered — and is recorded on the Work row. Changing execution
  to run inside the owner's live chat would reintroduce exactly the
  serialization and history-injection the snapshot design avoids, and would
  change observable behaviour for all four owner jobs.

Net effect on the four owner jobs: they keep firing, their existing on-disk
`sessionKey` is what binds them, and their `channelMeta` `work_*` keys are read
from `origin_metadata` after migration.

---

## 6. Ziggy-side contract

| Consumer | Route / symbol | Status |
| --- | --- | --- |
| `services/ziggy-control/.../shared_rooms.go:211` | `POST /auth/shared-rooms` | unchanged |
| `…:329` | `POST /auth/shared-room-token` | unchanged |
| `…:522` | `POST /auth/shared-rooms/title` | unchanged |
| `…:560` | `POST /auth/shared-room-revoke` | unchanged |
| `…:58` | `/api/shared-rooms/*` proxy prefix | unchanged (ziggy-control's own surface) |
| `…:607` | `GET /api/sessions/websocket:<chat>/messages` | **reinstated, room-scoped only** — see below |
| `…:615` | `GET /api/sessions/websocket:<chat>/files/<id>` | unchanged |
| `deploy/releases/editorial/configure.py:75` | `websocket.sharedRoomConnectorServer` | unchanged |
| `services/ziggy-worker/.../client.go:291` | `GET /api/sessions/websocket:<chat>/messages` | **owner-token caller — must migrate** (C5) |
| `ios/Ziggy/Networking/ZiggyRESTClient.swift:239` | same route | **owner-token caller — must migrate** (C5) |

`/api/sessions/<key>/messages` is restored as a room-credential-only route:
an `nbrt_` bearer authorizes exactly its own `websocket:<chat_id>` and the
response is the `_shareable_messages` projection. It is **not** restored for
owner/API tokens — those callers move to `/api/sessions/<key>/webui-thread`,
which is C5 and out of scope here.

---

## 7. Remaining cutover checklist (shared rooms + Work only)

- [ ] C1 runtime identity (`WorkingDirectory=`) — prerequisite, not this branch
- [x] C2 shared rooms rebased onto the 0.3.0 channel package
- [x] C3 authorization replacement (§4)
- [x] C4 `work_task` cron + `work`/`schedule_work` tools
- [x] C6 per-entry `jobs.json` quarantine
- [ ] C5 migrate `client.go:291` and `ZiggyRESTClient.swift:239` off `/messages`
- [ ] C8 briefing/editorial carry-forward (`agent/tools/briefing.py`)
- [ ] Canary one tenant with `sharedRoomCollaborationEnabled` before repointing
      `current-nanobot`
