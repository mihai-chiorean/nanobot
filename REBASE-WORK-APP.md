# Work app on 0.3.0: adopt, port, or bridge

Branch: `ziggy/work-app-0.3.0` off `ziggy/rebase-shared-rooms-work` (`99667fc0`).
Companion: `REBASE-SHARED-ROOMS.md` (§7 leaves this as the last open item).
Linear: MIT-1010.

Source of truth for current behaviour: the deployed snapshot
`spark-094a:/home/mihai/workspace/ziggy/releases/editorial-20260910/runtime`
(fork `0.1.5.post3`), read-only.

---

## 1. What each side actually is

The framing in the cutover checklist -- "the Work UI stays dark until the stream
lands" -- understates it. The `work.*` websocket contract is not only how the
Work app renders; it is how `services/ziggy-work` **executes** a task. Its River
job dials the gateway socket, sends `work.create`, learns the runtime task id
from `work.created`, and mirrors every `work.event` into its own Postgres
(`services/ziggy-work/internal/executor/executor.go:128-250`). Without the
stream, ziggy-work cannot run a task at all; it is not a rendering gap.

### The snapshot's Work event stream

| Surface | Snapshot location | Shape |
| --- | --- | --- |
| `work.create` → `work.created` | `channels/websocket.py:2900` | `{task_id, task}` where `task` is the full `work_tasks` row |
| `work.subscribe` → `work.subscribed` + replay | `:2986`, `:3008` | `{task_id}`, then every event after `after_seq`, paged at `MAX_EVENT_PAGE` |
| `work.event` (live fan-out) | `:3118`, `:3132` | `{task_id, seq, type, payload, actor, step_id, created_at}` |
| `work.cancel`, `work.message` | `:3024`, `:3045` | idempotency-keyed, `ACTIVE_STATUSES` gated |
| `GET/POST /api/work`, `/api/work/<id>`, `/<id>/events`, `/<id>/cancel`, `/<id>/message`, `/api/work/artifacts/<id>` | `:1542-1838` | paged list with `order=task_id` + `after_task_id` for the reconciler |
| `_WorkHook` | `agent/loop.py:279` | turns one agent run into `status.changed` / `tool.started` / `tool.finished` / step / artifact rows |
| transport of events | `agent/loop.py:781` → `channels/websocket.py:3185` | `OutboundMessage(metadata={"_work_event": …})`, intercepted in `send()` |

### 0.3.0's Automations

`/api/webui/automations`, `automation.{run,update,delete,enable,disable}`,
`AutomationTurnCoordinator`, `CronTurnCoordinator`, `LocalTriggerTurnCoordinator`.

Read in full (`webui/session_automations.py`, `webui/ws_http.py:1148-1420`,
`agent/automation_turns.py`), this is a **cron/trigger management** surface:
list jobs, enable/disable/delete/update them, run one now, and fetch the stored
response of one past run keyed `(job_id, run_at_ms)`. `AutomationTurnCoordinator`
is an in-process future that resolves with the turn's single `OutboundMessage`.

### Side-by-side

| Capability the Work app needs | Snapshot `work.*` | 0.3.0 Automations |
| --- | --- | --- |
| Unit of work | a **task** (`work_…`), created ad hoc | a **job** (cron/trigger), must pre-exist |
| Background task with no schedule | yes (`mode="background"`) | **no** -- every automation is a schedule or a trigger |
| Live push while running | yes (`work.event` fan-out) | **no** -- HTTP poll only |
| Per-step progress | `step.started` / `step.finished` | no |
| Per-tool-call events | `tool.started` / `tool.finished` | no |
| Artifacts | `artifact.published` + `/api/work/artifacts/<id>` | no |
| Ordered resume cursor | `seq` + `after_seq` replay | no (`run_at_ms` only, one blob per run) |
| Cancel a running unit | `work.cancel` | no |
| Message into a running unit | `work.message` | no |
| Exactly-once create | `idempotency_key` → `request_id` | no |
| Durable event log | `work_events` (SQLite, already ported) | `run_history[-5:]` |

They are not two implementations of one thing. Automations is scheduling;
`work.*` is task telemetry. The one genuine overlap -- a *scheduled* Work task
is also a cron job -- is already handled: `cron/work_runner.py` on the rebase
branch runs `work_task` jobs and writes the Work rows.

---

## 2. The three options, costed

### (a) Adopt -- move the Work app onto Automations events

Cost, concretely:

- **Rewrite `services/ziggy-work`'s executor**, which is the durable Work
  service, not a view. `executor.go:152` (`work.create`), `:138`/`:159`
  (`work.subscribe`), `:128`/`:244` (`work.cancel`), `:131` (`work.message`),
  and the `frame` decoder at `:66-78`. Automations has no create-a-task verb,
  so every background task would first have to become a cron job.
- **Rewrite the reconciler** (`internal/executor/reconcile.go:77,113`), which
  pages `GET /api/work?order=task_id&after_task_id=`. Automations has no
  equivalent pagination.
- **Rewrite the web client** (`web/src/lib/nanobot-client.ts:242-353`,
  `web/src/lib/types.ts:441-496`) and **iOS**
  (`ios/Ziggy/Core/Models/WebSocketModels.swift:546-636`,
  `ios/Ziggy/App/AppModel.swift:4271-4276`).
- And then **re-implement, inside Automations, everything in the table above
  that Automations does not have**: steps, tool events, artifacts, the `seq`
  cursor, cancel, message-into-a-run. That is the whole feature, rebuilt on a
  surface whose unit of work is wrong.

Zero Ziggy code references `automation.*` or `/api/webui/automations` today, so
this is not a migration -- it is a greenfield rewrite of three clients plus a
server-side feature build. **Disproportionate.**

### (c) Bridge -- Ziggy's wire contract as a projection over Automations

Impossible for the majority case. A background Work task created by
`work.create` has **no cron job to project from**, and the events the app
renders (`tool.started`, `step.finished`, `artifact.published`) are produced by
the agent run, not by the cron layer. A bridge would have to invent a cron job
per ad-hoc task and then still read every event from `WorkStore` -- i.e. it is
option (b) with a fictional cron row attached. It buys nothing and adds a
failure mode (orphan jobs) we do not have today.

### (b) Port -- reinstate `work.*` on 0.3.0's `WorkStore`

**Chosen.**

The usual objection to porting -- "we carry a parallel system through every
upstream merge" -- mostly does not apply here, because the expensive half is
already carried:

- `nanobot/work/store.py` (804 L) is **already on the branch** and is
  byte-identical to the snapshot apart from lazy schema init. It is pure stdlib
  SQLite; upstream has nothing near it to conflict with.
- `nanobot/agent/tools/{work,schedule_work}.py` and `nanobot/cron/work_runner.py`
  are already on the branch (`204b7549`).
- What is missing is the **projection**: a hook that turns a run into rows, and
  a transport that fans those rows out. Both are new files.

Merge cost is therefore bounded to the touch points, kept deliberately small
and mirroring how shared rooms were ported:

| File | Change | Upstream-conflict risk |
| --- | --- | --- |
| `channels/websocket/work_stream.py` | **new** (subscriptions, envelope handlers, fan-out) | none |
| `webui/work_http.py` | **new** (`/api/work*`) | none |
| `channels/websocket/runtime.py` | construct the hub, detach on cleanup, one `if` in `send()`, one line in `stop()` | low |
| `webui/inbound_commands.py` | one `if` block delegating `work.*` types | low |
| `webui/ws_http.py` | one attribute + one dispatch delegation next to `shared_rooms` | low |
| `agent/loop.py` | `_WorkHook` + a hook factory + work-context binding | medium (loop.py is upstream's churniest file) |

`loop.py` is the only real ongoing cost, and it is ~70 lines expressed through
0.3.0's own `AgentTurnHookFactory` extension point rather than by editing the
hook-assembly code, which keeps it a mechanical re-apply.

**Decision: (b) Port.** Clients are unchanged; ziggy-work keeps executing.

This is an engineering call, not a product call: the contract has three live
consumers and one of them is a durable service, so "the Work app stays dark" is
not the decision boundary -- "ziggy-work cannot run a task" is.

---

## 3. What was implemented

1. **`nanobot/agent/loop.py`**
   - `_WorkHook` (`AgentHook`): `before_iteration` → `status.changed` to
     `running`; `before_execute_tools` → `tool.started`; `after_iteration` →
     replays rows the Work tools appended, then `tool.finished`, then
     `status.changed` to `waiting` on `stop_reason == "ask_user"`.
   - Registered through `self._hook_factories`, returning `None` when the turn
     carries no `work_task_id`, so a normal turn pays one dict lookup.
   - `_publish_work_event` publishes `OutboundMessage(metadata={"_work_event": …})`,
     unchanged from the snapshot.
   - Work contextvars (`set_work_context`) are bound for any turn whose metadata
     carries `work_task_id`, so `report_progress` / `publish_artifact` work for
     `work.create` tasks and not only for cron ones.
   - `_record_work_status` marks a Work turn `succeeded` / `failed` and
     `_work_artifact_refs` attaches artifact refs to the reply.

2. **`nanobot/channels/websocket/work_stream.py`** (new)
   `WorkStreamHub`: `_work_subs` / `_conn_work` bookkeeping, the four envelope
   handlers, `publish_work_inbound`, cancel/message bookkeeping, replay and
   fan-out. Id regexes and `decode_id` live here.

3. **`nanobot/webui/work_http.py`** (new)
   `WorkRouter.dispatch` serving exactly the snapshot's six routes with the
   snapshot's response envelopes, including `order=task_id` + `after_task_id`
   paging for `reconcile.go` and `TransportFileResponse` for artifacts.

4. **Authorization.** `work.*` envelopes are refused on a room-credentialled
   connection, and the `/api/work*` routes take the owner API token only --
   never an `nbrt_` room bearer. A room guest driving the agent cannot reach
   another session's Work rows.

5. **`reasoning_profile`** is validated against the snapshot's enum values
   (`auto`, `fast`, `think`, `think-code`) and stored, so the wire contract is
   preserved, but it has no behavioural effect until `agent/reasoning_policy.py`
   is carried forward. That module is a separate fork feature, not part of this
   train.

---

## 4. Not ported, and whether it belongs in this train

| Item | Belongs here? | Why |
| --- | --- | --- |
| `agent/tools/briefing.py` (C8) | **no** | Independent editorial feature; its only `shared_room` tie is a refusal check. Its own carry-forward. |
| `agent/tools/publish_file.py`, `published_file_grants`, `/api/sessions/<key>/files/<id>` | **no, but it is the next one** | Room file downloads 404 until it lands. Unrelated to Work events; blocks `shared_rooms.go:615` only. |
| `agent/reasoning_policy.py` | **no** | Wire field preserved; behaviour is a separate fork feature. Nothing in the Work app reads it back. |
| `/api/work/experience/*`, `/api/work/apps*`, `/api/work/approvals*` | **no** | Served by `services/ziggy-work`, not by nanobot. ziggy-control proxies them there. |
| `/api/work/<id>/messages` (plural) | **no** | iOS's plural spelling terminates at ziggy-work, not the gateway. The gateway keeps the snapshot's `/message`. |

---

## 5. Ziggy-side changes required

**None for the Work app.** The wire contract is byte-identical to the snapshot,
so `services/ziggy-work`, `web/`, and the iOS Work views need no change.

The two P0s already recorded in `REBASE-SHARED-ROOMS.md` §6 are unchanged and
still required in this train:

- `services/ziggy-worker/.../client.go:291` → `/webui-thread`
- `ios/Ziggy/Networking/ZiggyRESTClient.swift:238` → `/webui-thread`

---

## 6. Updated cutover checklist

- [ ] C1 runtime identity (`WorkingDirectory=`) -- prerequisite, not this branch
- [x] C2 shared rooms rebased onto the 0.3.0 channel package
- [x] C3 authorization replacement
- [x] C4 `work_task` cron + `work`/`schedule_work` tools
- [x] C6 per-entry `jobs.json` quarantine
- [x] **Work event stream** -- `work.created` / `work.subscribed` / `work.event`
      plus `/api/work*`, ported onto 0.3.0's `WorkStore`; clients unchanged
- [ ] **P0** migrate `client.go:291` and `ZiggyRESTClient.swift:238` off
      `/messages` to `/webui-thread` -- same release train as the cutover
- [ ] Decide whether the aiohttp transport is acceptable for the owner runtime
      or whether shared rooms should move to a dedicated tenant
- [ ] C8 briefing/editorial carry-forward (`agent/tools/briefing.py`)
- [ ] Published files carry-forward (`agent/tools/publish_file.py`)
- [ ] `agent/reasoning_policy.py` carry-forward (wire field is already accepted)
- [ ] Canary one tenant with `sharedRoomCollaborationEnabled` before repointing
      `current-nanobot`
