"""Ziggy-local (MIT-1010): ``work_task`` cron kind and tolerant job loading.

Regression cover for the 0.3.0 cutover:

* the four owner ``work_task`` jobs survive a store round-trip, migrate to the
  session-bound shape, schedule, and fire;
* one malformed entry in ``jobs.json`` is quarantined instead of taking the
  whole gateway down, while a genuinely unparseable file still hard-fails.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from nanobot.cron.service import CronService
from nanobot.cron.session_turns import is_bound_cron_job
from nanobot.cron.types import CronJob, CronPayload, CronSchedule
from nanobot.cron.work_runner import (
    run_work_task_cron_job,
    work_routing,
    work_session_key,
)

# The four owner jobs as they exist on spark-094a, trimmed to the fields the
# scheduler reads.  Shapes captured 2026-09-15 from
# ~/.nanobot/workspace/cron/jobs.json (read-only).
OWNER_WORK_JOBS: list[dict[str, Any]] = [
    {
        "id": "cbc6a67c",
        "name": "Daily backend health & upstream updates digest",
        "enabled": True,
        "schedule": {"kind": "cron", "expr": "16 19 * * *", "tz": "UTC"},
        "payload": {
            "kind": "work_task",
            "message": "Create a daily backend health digest.",
            "deliver": False,
            "channel": "websocket",
            "to": "bbed5fcf-25b6-44e1-ad52-ba5f489dac34",
            "channelMeta": {
                "remote": ["127.0.0.1", 37962],
                "_scope": "owner",
                "_wants_stream": True,
                "work_scope": "owner",
                "work_title": "Daily backend health & upstream updates digest",
                "work_chat_id": "bbed5fcf-25b6-44e1-ad52-ba5f489dac34",
                "work_plan_task_id": "work_d52a3a497f664e5fb329322cecc796d4",
                "work_deliverable": "markdown_digest",
            },
            "sessionKey": "websocket:bbed5fcf-25b6-44e1-ad52-ba5f489dac34",
        },
        "createdAtMs": 1787598960000,
        "updatedAtMs": 1789499760000,
        "deleteAfterRun": False,
    },
    {
        "id": "2ccb6d82",
        "name": "Weekly Acquire.com Deal Analysis",
        "enabled": True,
        "schedule": {"kind": "cron", "expr": "0 10 * * 1", "tz": "Pacific/Auckland"},
        "payload": {
            "kind": "work_task",
            "message": "You are analyzing Acquire.com business listings.",
            "deliver": False,
            "channel": "websocket",
            "to": "D29F9818-D757-4545-A8EF-C43ECC05B3C4",
            "channelMeta": {
                "remote": ["100.86.74.94", 44202],
                "_wants_stream": True,
                "work_title": "Weekly Acquire.com Deal Analysis",
                "work_chat_id": "D29F9818-D757-4545-A8EF-C43ECC05B3C4",
                "work_plan_task_id": "work_d11a273b839b4afaba5768821a80a8ef",
                "work_deliverable": "markdown_digest",
            },
            "sessionKey": "websocket:D29F9818-D757-4545-A8EF-C43ECC05B3C4",
        },
        "createdAtMs": 1785103200000,
        "updatedAtMs": 1789336800000,
        "deleteAfterRun": False,
    },
    {
        "id": "e971284f",
        "name": "Daily Newsletter Digest with App Update",
        "enabled": True,
        "schedule": {"kind": "cron", "expr": "0 15 * * *", "tz": "America/Los_Angeles"},
        "payload": {
            "kind": "work_task",
            "message": "DAILY NEWSLETTER DIGEST WORKFLOW",
            "deliver": False,
            "channel": "websocket",
            "to": "a519f4a3-d034-4836-a0ea-a7da3a99fde5",
            "channelMeta": {
                "_wants_stream": True,
                "client_message_id": "6cbeadb5-2fa4-4992-8eb6-36a5df57b9c9",
                "reasoning_profile": "auto",
                "remote": ["100.86.74.94", 35402],
                "work_title": "Daily Newsletter Digest with App Update",
                "work_chat_id": "a519f4a3-d034-4836-a0ea-a7da3a99fde5",
                "work_plan_task_id": "work_14df5cab30d542fa9acc7c3e0f1bd752",
                "work_deliverable": "curated_digest_in_work_app",
            },
            "sessionKey": "websocket:a519f4a3-d034-4836-a0ea-a7da3a99fde5",
        },
        "createdAtMs": 1787608800000,
        "updatedAtMs": 1789509600000,
        "deleteAfterRun": False,
    },
    {
        "id": "92d1142e",
        "name": "Daily Acquire.com Opportunity Digest + Briefs",
        "enabled": True,
        "schedule": {"kind": "cron", "expr": "30 18 * * *", "tz": "America/Los_Angeles"},
        "payload": {
            "kind": "work_task",
            "message": "Run the daily Acquire.com ingestion + brief pipeline.",
            "deliver": False,
            "channel": "websocket",
            "to": "chat_619ae8b697b6424187dfc9f7511b16d7",
            "channelMeta": {
                "_wants_stream": True,
                "client_message_id": "3eaa9d43-34d0-45cf-8d1e-34fa0376a138",
                "explicit_final_message": True,
                "reasoning_profile": "auto",
                "remote": ["100.86.74.94", 43838],
                "work_title": "Daily Acquire.com Opportunity Digest + Briefs",
                "work_chat_id": "chat_619ae8b697b6424187dfc9f7511b16d7",
                "work_plan_task_id": "work_bbff5c5e7508494dbc5f06241dc021ca",
                "work_deliverable": "report",
            },
            "sessionKey": "websocket:chat_619ae8b697b6424187dfc9f7511b16d7",
        },
        "createdAtMs": 1788831000000,
        "updatedAtMs": 1789435800000,
        "deleteAfterRun": False,
    },
]


def _write_store(path: Path, jobs: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "jobs": jobs}), encoding="utf-8")


# --------------------------------------------------------------------------
# Payload round-trip
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw", OWNER_WORK_JOBS, ids=lambda job: job["id"])
def test_owner_work_task_payload_round_trip(raw: dict[str, Any]) -> None:
    """Each owner job parses, keeps ``work_task``, and keeps its routing hints."""
    job = CronJob.from_store_dict(raw)

    assert job.payload.kind == "work_task"
    assert job.payload.session_key == raw["payload"]["sessionKey"]
    assert job.payload.channel_meta == raw["payload"]["channelMeta"]
    assert job.schedule.expr == raw["schedule"]["expr"]
    assert job.schedule.tz == raw["schedule"]["tz"]

    routing = work_routing(job)
    meta = raw["payload"]["channelMeta"]
    assert routing["work_chat_id"] == meta["work_chat_id"]
    assert routing["work_title"] == meta["work_title"]
    assert routing["work_plan_task_id"] == meta["work_plan_task_id"]
    assert routing["work_deliverable"] == meta["work_deliverable"]


@pytest.mark.parametrize("raw", OWNER_WORK_JOBS, ids=lambda job: job["id"])
def test_owner_work_task_jobs_migrate_to_bound_shape(tmp_path: Path, raw) -> None:
    """Loading migrates legacy delivery fields and leaves the job session-bound."""
    store_path = tmp_path / "cron" / "jobs.json"
    _write_store(store_path, [raw])

    service = CronService(store_path)
    loaded = service._load_jobs()
    assert loaded is not None
    jobs, _version = loaded
    (job,) = jobs

    assert job.payload.kind == "work_task"
    assert is_bound_cron_job(job), "work_task must satisfy the 0.3.0 binding contract"
    assert job.payload.session_key == raw["payload"]["sessionKey"]
    assert job.payload.origin_channel == "websocket"
    assert job.payload.origin_chat_id == raw["payload"]["to"]
    # Legacy delivery fields are cleared, but the work_* hints survive.
    assert job.payload.channel is None
    assert job.payload.to is None
    assert job.payload.channel_meta == {}
    routing = work_routing(job)
    assert routing["work_chat_id"] == raw["payload"]["channelMeta"]["work_chat_id"]

    # Execution never uses the bound session; it gets its own.
    assert work_session_key(job) == f"cron:{raw['id']}"
    assert work_session_key(job) != job.payload.session_key


def test_all_four_owner_work_jobs_remain_enabled(tmp_path: Path) -> None:
    """Regression for C4: none of the four is silently disabled at load."""
    store_path = tmp_path / "cron" / "jobs.json"
    _write_store(store_path, OWNER_WORK_JOBS)

    service = CronService(store_path)
    loaded = service._load_jobs()
    assert loaded is not None
    jobs, _ = loaded
    assert len(jobs) == 4
    assert all(job.enabled for job in jobs)
    assert all(job.payload.kind == "work_task" for job in jobs)
    assert all(is_bound_cron_job(job) for job in jobs)


# --------------------------------------------------------------------------
# Scheduling and firing
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_work_task_job_schedules_and_fires(tmp_path: Path) -> None:
    """A due ``work_task`` job reaches the executor instead of being skipped."""
    store_path = tmp_path / "cron" / "jobs.json"
    fired: list[CronJob] = []

    async def executor(job: CronJob) -> str:
        fired.append(job)
        return "ran"

    service = CronService(store_path, on_job=executor, max_sleep_ms=20)
    await service.start()
    try:
        job = service.add_job(
            name="scheduled work",
            schedule=CronSchedule(kind="every", every_ms=1000),
            message="do the thing",
            payload_kind="work_task",
            channel="websocket",
            to="chat-1",
            channel_meta={"work_chat_id": "chat-1", "work_title": "scheduled work"},
            session_key="websocket:chat-1",
        )
        assert job.payload.kind == "work_task"
        assert is_bound_cron_job(job)
        assert job.state.next_run_at_ms is not None

        # Make it due now and let the timer pick it up.
        job.state.next_run_at_ms = int(time.time() * 1000) - 1
        service._arm_timer()

        deadline = time.monotonic() + 3.0
        while not fired and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert fired, "work_task job never fired"
        assert fired[0].payload.kind == "work_task"
    finally:
        service.stop()


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeWorkStore:
    def __init__(self) -> None:
        self.tasks: list[dict[str, Any]] = []
        self.events: list[tuple[str, str, dict[str, Any]]] = []
        self.statuses: list[tuple[str, str]] = []

    async def run_io(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def create_task(self, **kwargs: Any) -> dict[str, Any]:
        task = {"task_id": f"work_{len(self.tasks)}", **kwargs}
        self.tasks.append(task)
        return task

    def append_event(self, task_id: str, name: str, payload: dict[str, Any], **_: Any) -> None:
        self.events.append((task_id, name, payload))

    def update_status(self, task_id: str, status: str, **_: Any) -> None:
        self.statuses.append((task_id, status))


class _FakeAgent:
    def __init__(self, store: _FakeWorkStore, workspace: Path) -> None:
        self.work_store = store
        self.workspace = workspace
        self.model = "test-model"
        self.calls: list[dict[str, Any]] = []

    async def process_direct(self, content: str, **kwargs: Any):
        self.calls.append({"content": content, **kwargs})
        return _FakeResponse("digest body")


@pytest.mark.asyncio
async def test_work_task_runs_in_its_own_session(tmp_path: Path) -> None:
    """The run executes in ``cron:<job_id>``, not in the bound session."""
    job = CronJob.from_store_dict(OWNER_WORK_JOBS[0])
    store = _FakeWorkStore()
    agent = _FakeAgent(store, tmp_path)

    result = await run_work_task_cron_job(job, agent=agent)

    assert result == "digest body"
    (call,) = agent.calls
    assert call["session_key"] == "cron:cbc6a67c"
    assert call["session_key"] != job.payload.session_key
    assert call["chat_id"] == "bbed5fcf-25b6-44e1-ad52-ba5f489dac34"
    assert call["metadata"]["cron_job_id"] == "cbc6a67c"
    assert call["metadata"]["work_mode"] == "scheduled"
    assert call["metadata"]["work_task_id"] == store.tasks[0]["task_id"]

    # The plan task gets the scheduled-run breadcrumbs.
    names = [name for _task, name, _payload in store.events]
    assert "scheduled_run.created" in names
    assert "scheduled_run.completed" in names
    assert ("work_0", "succeeded") in store.statuses


@pytest.mark.asyncio
async def test_work_task_records_failure_on_the_task(tmp_path: Path) -> None:
    job = CronJob.from_store_dict(OWNER_WORK_JOBS[1])
    store = _FakeWorkStore()
    agent = _FakeAgent(store, tmp_path)

    async def boom(_content: str, **_kwargs: Any):
        raise RuntimeError("provider down")

    agent.process_direct = boom  # type: ignore[assignment]

    with pytest.raises(RuntimeError):
        await run_work_task_cron_job(job, agent=agent)

    assert ("work_0", "failed") in store.statuses
    assert any(name == "scheduled_run.failed" for _t, name, _p in store.events)


# --------------------------------------------------------------------------
# Tolerant job loading (C6)
# --------------------------------------------------------------------------


def test_one_malformed_entry_is_quarantined_and_the_rest_load(tmp_path: Path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    good = OWNER_WORK_JOBS[0]
    # Missing the required "name" key: CronJob.from_store_dict raises KeyError.
    bad = {"id": "broken", "schedule": {"kind": "every", "everyMs": 1000}}
    _write_store(store_path, [good, bad, OWNER_WORK_JOBS[1]])

    service = CronService(store_path)
    loaded = service._load_jobs()

    assert loaded is not None, "one bad entry must not fail the whole store"
    jobs, _version = loaded
    assert [job.id for job in jobs] == ["cbc6a67c", "2ccb6d82"]
    # The original file is untouched; the bad record is preserved beside it.
    assert store_path.exists()
    quarantine = list(store_path.parent.glob("jobs.json.quarantine-*.jsonl"))
    assert len(quarantine) == 1
    records = [json.loads(line) for line in quarantine[0].read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["entry"]["id"] == "broken"
    assert "reason" in records[0]


def test_unparseable_store_still_hard_fails(tmp_path: Path) -> None:
    """Upstream's whole-file protection is preserved: garbage still returns None."""
    store_path = tmp_path / "cron" / "jobs.json"
    store_path.parent.mkdir(parents=True)
    store_path.write_text("{not json", encoding="utf-8")

    service = CronService(store_path)
    assert service._load_jobs() is None
    assert not store_path.exists()
    assert list(store_path.parent.glob("jobs.json.corrupt-*"))


def test_non_object_root_hard_fails(tmp_path: Path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    store_path.parent.mkdir(parents=True)
    store_path.write_text('["not", "a", "store"]', encoding="utf-8")

    service = CronService(store_path)
    assert service._load_jobs() is None
    assert list(store_path.parent.glob("jobs.json.corrupt-*"))


@pytest.mark.asyncio
async def test_gateway_starts_with_a_malformed_entry(tmp_path: Path) -> None:
    """The quarantine path must let ``CronService.start`` succeed."""
    store_path = tmp_path / "cron" / "jobs.json"
    bad = {"id": "broken", "schedule": {"kind": "every", "everyMs": 1000}}
    _write_store(store_path, [OWNER_WORK_JOBS[2], bad])

    service = CronService(store_path)
    await service.start()
    try:
        ids = [job.id for job in service.list_jobs(include_disabled=True)]
        assert ids == ["e971284f"]
    finally:
        service.stop()


def test_cron_payload_kind_literal_includes_work_task() -> None:
    """Guard the Literal itself: a silent drop is how C4 happened."""
    payload = CronPayload.from_store_dict({"kind": "work_task", "message": "x"})
    assert payload.kind == "work_task"


# --------------------------------------------------------------------------
# Delivery (tech-lead review, P0-3)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_work_task_publishes_its_response(tmp_path: Path) -> None:
    """Without this the four owner digests run, cost tokens, and post nothing.

    All four jobs are ``deliver: false`` at the cron layer and
    ``process_direct`` does not publish, so the runner has to deliver.
    """
    job = CronJob.from_store_dict(OWNER_WORK_JOBS[0])
    store = _FakeWorkStore()
    agent = _FakeAgent(store, tmp_path)
    delivered: list[Any] = []

    async def _deliver(message: Any) -> None:
        delivered.append(message)

    await run_work_task_cron_job(job, agent=agent, deliver=_deliver)

    assert len(delivered) == 1
    assert delivered[0].content == "digest body"


@pytest.mark.asyncio
async def test_work_task_does_not_deliver_for_a_non_websocket_origin(
    tmp_path: Path,
) -> None:
    raw = dict(OWNER_WORK_JOBS[0])
    raw["payload"] = {**raw["payload"], "channel": "discord"}
    job = CronJob.from_store_dict(raw)
    store = _FakeWorkStore()
    agent = _FakeAgent(store, tmp_path)
    delivered: list[Any] = []

    async def _deliver(message: Any) -> None:
        delivered.append(message)

    await run_work_task_cron_job(job, agent=agent, deliver=_deliver)
    assert delivered == []


@pytest.mark.asyncio
async def test_a_failed_run_delivers_nothing(tmp_path: Path) -> None:
    job = CronJob.from_store_dict(OWNER_WORK_JOBS[0])
    store = _FakeWorkStore()
    agent = _FakeAgent(store, tmp_path)
    delivered: list[Any] = []

    async def _deliver(message: Any) -> None:
        delivered.append(message)

    async def boom(_content: str, **_kwargs: Any):
        raise RuntimeError("provider down")

    agent.process_direct = boom  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        await run_work_task_cron_job(job, agent=agent, deliver=_deliver)
    assert delivered == []


# --------------------------------------------------------------------------
# Quarantine hygiene (tech-lead review, P1-9)
# --------------------------------------------------------------------------


def test_a_repeated_load_does_not_re_quarantine_the_same_entry(tmp_path: Path) -> None:
    """``_load_jobs`` runs on nearly every public call; one bad record must not
    produce a file and an ERROR line per reload, forever."""
    store_path = tmp_path / "cron" / "jobs.json"
    bad = {"id": "broken", "schedule": {"kind": "every", "everyMs": 1000}}
    _write_store(store_path, [OWNER_WORK_JOBS[0], bad])

    service = CronService(store_path)
    for _ in range(5):
        loaded = service._load_jobs()
        assert loaded is not None
        assert [job.id for job in loaded[0]] == ["cbc6a67c"]

    quarantine = list(store_path.parent.glob("jobs.json.quarantine-*.jsonl"))
    assert len(quarantine) == 1
    records = [line for line in quarantine[0].read_text().splitlines() if line.strip()]
    assert len(records) == 1


def test_two_different_bad_entries_are_both_quarantined(tmp_path: Path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    _write_store(
        store_path,
        [
            {"id": "broken-a", "schedule": {"kind": "every", "everyMs": 1000}},
            {"id": "broken-b", "schedule": {"kind": "every", "everyMs": 2000}},
        ],
    )
    service = CronService(store_path)
    assert service._load_jobs() == ([], 1)
    records = [
        line
        for path in store_path.parent.glob("jobs.json.quarantine-*.jsonl")
        for line in path.read_text().splitlines()
        if line.strip()
    ]
    assert len(records) == 2


# --------------------------------------------------------------------------
# Disabling must stay reversible (PR review, C5)
# --------------------------------------------------------------------------


def test_disabling_a_malformed_work_task_preserves_its_routing(tmp_path: Path) -> None:
    """Restoring work_task into BINDABLE_PAYLOAD_KINDS routes it through
    _disable_malformed_legacy_job, which clears channel_meta -- where a
    work_task's work_chat_id and work_plan_task_id live. Losing them turns a
    recoverable failure into a permanent one."""
    raw = json.loads(json.dumps(OWNER_WORK_JOBS[0]))
    raw["payload"]["to"] = None  # unroutable: forces the disable path
    store_path = tmp_path / "cron" / "jobs.json"
    _write_store(store_path, [raw])

    service = CronService(store_path)
    loaded = service._load_jobs()
    assert loaded is not None
    (job,) = loaded[0]

    assert job.enabled is False
    assert job.state.last_status == "error"
    # The hints survive, so the job can be rebuilt.
    routing = work_routing(job)
    assert routing["work_chat_id"] == "bbed5fcf-25b6-44e1-ad52-ba5f489dac34"
    assert routing["work_plan_task_id"] == "work_d52a3a497f664e5fb329322cecc796d4"
    assert routing["work_deliverable"] == "markdown_digest"


def test_a_healthy_work_task_is_never_disabled(tmp_path: Path) -> None:
    store_path = tmp_path / "cron" / "jobs.json"
    _write_store(store_path, list(OWNER_WORK_JOBS))
    service = CronService(store_path)
    loaded = service._load_jobs()
    assert loaded is not None
    assert all(job.enabled for job in loaded[0])
    assert all(job.state.last_status != "error" for job in loaded[0])
