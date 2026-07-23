-- Applied explicitly by deployment tooling. The service never migrates production.
CREATE TABLE IF NOT EXISTS ziggy_work_tasks (
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    session_key TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    title TEXT NOT NULL,
    prompt_preview TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('scheduled','queued','running','waiting','succeeded','failed','cancelled','interrupted')),
    mode TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    last_seq BIGINT NOT NULL DEFAULT 0,
    result_summary TEXT,
    error TEXT,
    artifact_count INTEGER NOT NULL DEFAULT 0,
    version BIGINT NOT NULL DEFAULT 1,
    runtime_task_id TEXT,
    river_job_id TEXT,
    PRIMARY KEY (user_id, workspace_id, task_id),
    UNIQUE (user_id, workspace_id, runtime_task_id)
);
CREATE INDEX IF NOT EXISTS ziggy_work_tasks_updated_idx ON ziggy_work_tasks (user_id, workspace_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS ziggy_work_events (
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    seq BIGINT NOT NULL,
    type TEXT NOT NULL,
    actor TEXT NOT NULL,
    step_id TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL,
    runtime_task_id TEXT,
    runtime_seq BIGINT,
    PRIMARY KEY (user_id, workspace_id, task_id, seq),
    FOREIGN KEY (user_id, workspace_id, task_id) REFERENCES ziggy_work_tasks (user_id, workspace_id, task_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ziggy_work_events_cursor_idx ON ziggy_work_events (user_id, workspace_id, task_id, seq);
CREATE UNIQUE INDEX IF NOT EXISTS ziggy_work_events_runtime_idx
    ON ziggy_work_events (user_id, workspace_id, runtime_task_id, runtime_seq)
    WHERE runtime_task_id IS NOT NULL AND runtime_seq IS NOT NULL;

CREATE TABLE IF NOT EXISTS ziggy_work_steps (
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    seq_start BIGINT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    summary TEXT,
    PRIMARY KEY (user_id, workspace_id, step_id),
    FOREIGN KEY (user_id, workspace_id, task_id) REFERENCES ziggy_work_tasks (user_id, workspace_id, task_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ziggy_work_artifacts (
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    step_id TEXT,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    mime TEXT NOT NULL,
    size_bytes BIGINT NOT NULL DEFAULT 0,
    sha256 TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL,
    summary TEXT,
    path_rel TEXT NOT NULL DEFAULT '',
    available BOOLEAN NOT NULL DEFAULT FALSE,
    unavailable_reason TEXT,
    PRIMARY KEY (user_id, workspace_id, artifact_id),
    FOREIGN KEY (user_id, workspace_id, task_id) REFERENCES ziggy_work_tasks (user_id, workspace_id, task_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ziggy_work_artifacts_task_idx ON ziggy_work_artifacts (user_id, workspace_id, task_id);

CREATE TABLE IF NOT EXISTS ziggy_work_idempotency (
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    operation TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    task_id TEXT NOT NULL,
    event_seq BIGINT,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_id, workspace_id, idempotency_key),
    FOREIGN KEY (user_id, workspace_id, task_id) REFERENCES ziggy_work_tasks (user_id, workspace_id, task_id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX IF NOT EXISTS ziggy_work_idempotency_created_idx
    ON ziggy_work_idempotency (created_at);

-- Forward-compatible durable scheduling, run, and approval records.
CREATE TABLE IF NOT EXISTS ziggy_work_schedules (
    user_id TEXT NOT NULL, workspace_id TEXT NOT NULL, schedule_id TEXT NOT NULL,
    task_id TEXT, cron TEXT NOT NULL, timezone TEXT NOT NULL DEFAULT 'UTC',
    status TEXT NOT NULL DEFAULT 'active', payload JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (user_id, workspace_id, schedule_id)
);
CREATE TABLE IF NOT EXISTS ziggy_work_runs (
    user_id TEXT NOT NULL, workspace_id TEXT NOT NULL, run_id TEXT NOT NULL,
    schedule_id TEXT, task_id TEXT, status TEXT NOT NULL, started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ, error TEXT, PRIMARY KEY (user_id, workspace_id, run_id)
);
CREATE TABLE IF NOT EXISTS ziggy_work_approvals (
    user_id TEXT NOT NULL, workspace_id TEXT NOT NULL, approval_id TEXT NOT NULL,
    task_id TEXT NOT NULL, status TEXT NOT NULL, requested_at TIMESTAMPTZ NOT NULL,
    decided_at TIMESTAMPTZ, decided_by TEXT, payload JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (user_id, workspace_id, approval_id)
);
