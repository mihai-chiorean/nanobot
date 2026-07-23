package repository

import (
	"context"
	cryptorand "crypto/rand"
	"errors"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/job"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/model"
)

type Repository interface {
	Health(context.Context) error
	CreateTask(context.Context, model.Tenant, model.CreateInput, model.RequestIdentity, job.Enqueuer) (model.Task, error)
	ListTasks(context.Context, model.Tenant, model.ListFilter) ([]model.Task, error)
	GetTask(context.Context, model.Tenant, string) (model.Task, error)
	ListEvents(context.Context, model.Tenant, string, int64, int) ([]model.Event, error)
	AppendEvent(context.Context, model.Tenant, string, string, map[string]any, string) (model.Event, error)
	AppendRuntimeEvent(context.Context, model.Tenant, string, string, model.Event) (model.Event, bool, error)
	UpdateStatus(context.Context, model.Tenant, string, model.Status, *string, *string) (model.Task, model.Event, error)
	SetRuntimeTask(context.Context, model.Tenant, string, string) error
	EnqueueFollowUp(context.Context, model.Tenant, string, string, model.RequestIdentity, job.Enqueuer) (model.Task, model.Event, error)
	CancelTask(context.Context, model.Tenant, string, model.RequestIdentity) (model.Task, error)
	GetArtifact(context.Context, model.Tenant, string) (model.Artifact, error)
	RuntimeCursor(context.Context, model.Tenant, string) (int64, error)
	UpsertRuntimeTask(context.Context, model.Tenant, model.Task, []model.Event, []model.Artifact) error
	Subscribe(model.Tenant, string) (<-chan struct{}, func())
	Import(context.Context, model.Tenant, LegacyImport) (ImportResult, error)
}

type LegacyImport struct {
	Tasks     []model.Task
	Events    []model.Event
	Steps     []model.Step
	Artifacts []model.Artifact
}
type ImportResult struct{ Tasks, Events, Steps, Artifacts, Skipped int }

type PostgresRepository struct {
	DB  *pgxpool.Pool
	now func() time.Time
}

func NewPostgres(db *pgxpool.Pool) *PostgresRepository {
	return &PostgresRepository{DB: db, now: time.Now}
}
func (r *PostgresRepository) Health(ctx context.Context) error { return r.DB.Ping(ctx) }

func (r *PostgresRepository) CreateTask(ctx context.Context, tenant model.Tenant, input model.CreateInput, request model.RequestIdentity, enqueuer job.Enqueuer) (model.Task, error) {
	if !tenant.Valid() || input.Validate() != nil || !request.Valid() || enqueuer == nil {
		return model.Task{}, model.ErrInvalidInput
	}
	id := newWorkID()
	now := r.now().UTC()
	mode := input.Mode
	if mode == "" {
		mode = "background"
	}
	title := input.Title
	if title == "" {
		title = model.Preview(input.Content, 120)
	}
	tx, err := r.DB.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return model.Task{}, err
	}
	defer tx.Rollback(ctx)
	claimed, existing, err := claimIdempotency(ctx, tx, tenant, request, "create", id, nil, now)
	if err != nil {
		return model.Task{}, err
	}
	if !claimed {
		if err := tx.Rollback(ctx); err != nil {
			return model.Task{}, err
		}
		return r.GetTask(ctx, tenant, existing.taskID)
	}
	task := model.Task{TaskID: id, SessionKey: "work:" + id, ChatID: input.ChatID, Title: title, PromptPreview: model.Preview(input.Content, 240), Status: model.Queued, Mode: mode, Model: input.Model, CreatedAt: now, UpdatedAt: now, LastSeq: 1, Tenant: tenant, Version: 1}
	_, err = tx.Exec(ctx, `INSERT INTO ziggy_work_tasks (user_id,workspace_id,task_id,session_key,chat_id,title,prompt_preview,status,mode,model,created_at,updated_at,last_seq,version) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$11,$12,$13)`, tenant.UserID, tenant.WorkspaceID, id, task.SessionKey, task.ChatID, task.Title, task.PromptPreview, task.Status, task.Mode, task.Model, now, task.LastSeq, task.Version)
	if err != nil {
		return model.Task{}, mapError(err)
	}
	if _, err = tx.Exec(ctx, `INSERT INTO ziggy_work_events (user_id,workspace_id,task_id,seq,type,actor,created_at,payload) VALUES ($1,$2,$3,1,'task.created','system',$4,$5)`, tenant.UserID, tenant.WorkspaceID, id, now, map[string]any{"task_id": id, "title": title, "status": task.Status}); err != nil {
		return model.Task{}, err
	}
	jobID, err := enqueuer.EnqueueTx(ctx, tx, job.Job{TaskID: id, UserID: tenant.UserID, WorkspaceID: tenant.WorkspaceID, Content: input.Content, ChatID: input.ChatID, CommandID: id})
	if err != nil {
		return model.Task{}, err
	}
	if _, err = tx.Exec(ctx, `UPDATE ziggy_work_tasks SET river_job_id=$1, updated_at=$2, version=version+1 WHERE user_id=$3 AND workspace_id=$4 AND task_id=$5`, jobID, now, tenant.UserID, tenant.WorkspaceID, id); err != nil {
		return model.Task{}, err
	}
	if err = tx.Commit(ctx); err != nil {
		return model.Task{}, err
	}
	task.RiverJobID, task.Version = jobID, 2
	return task, nil
}

func (r *PostgresRepository) ListTasks(ctx context.Context, tenant model.Tenant, filter model.ListFilter) ([]model.Task, error) {
	limit := filter.Limit
	if limit <= 0 || limit > 200 {
		limit = 50
	}
	args := []any{tenant.UserID, tenant.WorkspaceID}
	where := ""
	if filter.Status != "" {
		where = " AND status=$3"
		args = append(args, filter.Status)
	}
	args = append(args, limit)
	rows, err := r.DB.Query(ctx, `SELECT task_id,session_key,chat_id,title,prompt_preview,status,mode,model,created_at,updated_at,started_at,completed_at,last_seq,result_summary,error,artifact_count,version,runtime_task_id,river_job_id FROM ziggy_work_tasks WHERE user_id=$1 AND workspace_id=$2`+where+` ORDER BY updated_at DESC LIMIT $`+strconv.Itoa(len(args)), args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []model.Task{}
	for rows.Next() {
		t, err := scanTask(rows, tenant)
		if err != nil {
			return nil, err
		}
		out = append(out, t)
	}
	return out, rows.Err()
}

func (r *PostgresRepository) GetTask(ctx context.Context, tenant model.Tenant, id string) (model.Task, error) {
	if !model.WorkIDPattern.MatchString(id) {
		return model.Task{}, model.ErrNotFound
	}
	row := r.DB.QueryRow(ctx, `SELECT task_id,session_key,chat_id,title,prompt_preview,status,mode,model,created_at,updated_at,started_at,completed_at,last_seq,result_summary,error,artifact_count,version,runtime_task_id,river_job_id FROM ziggy_work_tasks WHERE user_id=$1 AND workspace_id=$2 AND task_id=$3`, tenant.UserID, tenant.WorkspaceID, id)
	t, err := scanTask(row, tenant)
	if err != nil {
		return model.Task{}, err
	}
	return r.loadDetails(ctx, t)
}

func (r *PostgresRepository) ListEvents(ctx context.Context, tenant model.Tenant, id string, after int64, limit int) ([]model.Event, error) {
	if limit <= 0 || limit > 500 {
		limit = 100
	}
	rows, err := r.DB.Query(ctx, `SELECT task_id,seq,type,actor,step_id,created_at,payload FROM ziggy_work_events WHERE user_id=$1 AND workspace_id=$2 AND task_id=$3 AND seq>$4 ORDER BY seq LIMIT $5`, tenant.UserID, tenant.WorkspaceID, id, after, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []model.Event{}
	for rows.Next() {
		var e model.Event
		if err := rows.Scan(&e.TaskID, &e.Seq, &e.Type, &e.Actor, &e.StepID, &e.CreatedAt, &e.Payload); err != nil {
			return nil, err
		}
		e.Tenant = tenant
		out = append(out, e)
	}
	return out, rows.Err()
}

func (r *PostgresRepository) AppendEvent(ctx context.Context, tenant model.Tenant, id, typ string, payload map[string]any, actor string) (model.Event, error) {
	tx, err := r.DB.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return model.Event{}, err
	}
	defer tx.Rollback(ctx)
	e, err := appendEventTx(ctx, tx, tenant, id, typ, payload, actor, r.now().UTC())
	if err != nil {
		return model.Event{}, err
	}
	if err = tx.Commit(ctx); err != nil {
		return model.Event{}, err
	}
	return e, nil
}

func (r *PostgresRepository) AppendRuntimeEvent(ctx context.Context, tenant model.Tenant, id, runtimeTaskID string, event model.Event) (model.Event, bool, error) {
	if runtimeTaskID == "" || event.Seq <= 0 {
		return model.Event{}, false, model.ErrInvalidInput
	}
	tx, err := r.DB.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return model.Event{}, false, err
	}
	defer tx.Rollback(ctx)
	inserted, err := appendRuntimeEventTx(ctx, tx, tenant, id, runtimeTaskID, event, r.now().UTC())
	if err != nil {
		return model.Event{}, false, err
	}
	if err := tx.Commit(ctx); err != nil {
		return model.Event{}, false, err
	}
	return inserted.event, inserted.inserted, nil
}

func (r *PostgresRepository) UpdateStatus(ctx context.Context, tenant model.Tenant, id string, status model.Status, message, summary *string) (model.Task, model.Event, error) {
	if !status.Valid() {
		return model.Task{}, model.Event{}, model.ErrInvalidInput
	}
	tx, err := r.DB.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return model.Task{}, model.Event{}, err
	}
	defer tx.Rollback(ctx)
	var current model.Status
	var version int64
	if err = tx.QueryRow(ctx, `SELECT status,version FROM ziggy_work_tasks WHERE user_id=$1 AND workspace_id=$2 AND task_id=$3 FOR UPDATE`, tenant.UserID, tenant.WorkspaceID, id).Scan(&current, &version); err != nil {
		return model.Task{}, model.Event{}, mapError(err)
	}
	if current.Terminal() {
		return model.Task{}, model.Event{}, model.ErrTerminal
	}
	now := r.now().UTC()
	_, err = tx.Exec(ctx, `UPDATE ziggy_work_tasks SET status=$1,error=COALESCE($2,error),result_summary=COALESCE($3,result_summary),started_at=CASE WHEN $1='running' THEN COALESCE(started_at,$4) ELSE started_at END,completed_at=CASE WHEN $1 IN ('succeeded','failed','cancelled','interrupted') THEN $4 ELSE completed_at END,updated_at=$4,version=version+1 WHERE user_id=$5 AND workspace_id=$6 AND task_id=$7`, status, message, summary, now, tenant.UserID, tenant.WorkspaceID, id)
	if err != nil {
		return model.Task{}, model.Event{}, err
	}
	e, err := appendEventTx(ctx, tx, tenant, id, "status.changed", map[string]any{"status": status, "error": message, "result_summary": summary}, "system", now)
	if err != nil {
		return model.Task{}, model.Event{}, err
	}
	if err = tx.Commit(ctx); err != nil {
		return model.Task{}, model.Event{}, err
	}
	t, err := r.GetTask(ctx, tenant, id)
	return t, e, err
}

func (r *PostgresRepository) SetRuntimeTask(ctx context.Context, tenant model.Tenant, id, runtime string) error {
	_, err := r.DB.Exec(ctx, `UPDATE ziggy_work_tasks SET runtime_task_id=$1,updated_at=$2,version=version+1 WHERE user_id=$3 AND workspace_id=$4 AND task_id=$5`, runtime, r.now().UTC(), tenant.UserID, tenant.WorkspaceID, id)
	return err
}
func (r *PostgresRepository) EnqueueFollowUp(ctx context.Context, tenant model.Tenant, id, content string, request model.RequestIdentity, enqueuer job.Enqueuer) (model.Task, model.Event, error) {
	if !request.Valid() || strings.TrimSpace(content) == "" || enqueuer == nil {
		return model.Task{}, model.Event{}, model.ErrInvalidInput
	}
	tx, err := r.DB.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return model.Task{}, model.Event{}, err
	}
	defer tx.Rollback(ctx)
	claimed, existing, err := claimIdempotency(ctx, tx, tenant, request, "message", id, nil, r.now().UTC())
	if err != nil {
		return model.Task{}, model.Event{}, err
	}
	if !claimed {
		if existing.eventSeq == nil {
			return model.Task{}, model.Event{}, model.ErrConflict
		}
		if err := tx.Rollback(ctx); err != nil {
			return model.Task{}, model.Event{}, err
		}
		task, taskErr := r.GetTask(ctx, tenant, existing.taskID)
		if taskErr != nil {
			return model.Task{}, model.Event{}, taskErr
		}
		event, eventErr := r.getEvent(ctx, tenant, existing.taskID, *existing.eventSeq)
		return task, event, eventErr
	}
	var status model.Status
	var chatID string
	if err := tx.QueryRow(ctx, `SELECT status,chat_id FROM ziggy_work_tasks WHERE user_id=$1 AND workspace_id=$2 AND task_id=$3 FOR UPDATE`, tenant.UserID, tenant.WorkspaceID, id).Scan(&status, &chatID); err != nil {
		return model.Task{}, model.Event{}, mapError(err)
	}
	if status.Terminal() {
		return model.Task{}, model.Event{}, model.ErrTerminal
	}
	jobID, err := enqueuer.EnqueueTx(ctx, tx, job.Job{TaskID: id, UserID: tenant.UserID, WorkspaceID: tenant.WorkspaceID, Content: content, ChatID: chatID, FollowUp: true, CommandID: newCommandID()})
	if err != nil {
		return model.Task{}, model.Event{}, err
	}
	now := r.now().UTC()
	if status == model.Waiting {
		if _, err = tx.Exec(ctx, `UPDATE ziggy_work_tasks SET status='queued',river_job_id=$1,updated_at=$2,version=version+1 WHERE user_id=$3 AND workspace_id=$4 AND task_id=$5`, jobID, now, tenant.UserID, tenant.WorkspaceID, id); err != nil {
			return model.Task{}, model.Event{}, err
		}
		if _, err = appendEventTx(ctx, tx, tenant, id, "status.changed", map[string]any{"status": model.Queued}, "system", now); err != nil {
			return model.Task{}, model.Event{}, err
		}
	} else if _, err = tx.Exec(ctx, `UPDATE ziggy_work_tasks SET river_job_id=$1,updated_at=$2,version=version+1 WHERE user_id=$3 AND workspace_id=$4 AND task_id=$5`, jobID, now, tenant.UserID, tenant.WorkspaceID, id); err != nil {
		return model.Task{}, model.Event{}, err
	}
	e, err := appendEventTx(ctx, tx, tenant, id, "message.received", map[string]any{"content": model.Preview(content, 240)}, "user", now)
	if err != nil {
		return model.Task{}, model.Event{}, err
	}
	if _, err = tx.Exec(ctx, `UPDATE ziggy_work_idempotency SET event_seq=$1 WHERE user_id=$2 AND workspace_id=$3 AND idempotency_key=$4`, e.Seq, tenant.UserID, tenant.WorkspaceID, request.Key); err != nil {
		return model.Task{}, model.Event{}, err
	}
	if err = tx.Commit(ctx); err != nil {
		return model.Task{}, model.Event{}, err
	}
	t, err := r.GetTask(ctx, tenant, id)
	return t, e, err
}
func (r *PostgresRepository) CancelTask(ctx context.Context, tenant model.Tenant, id string, request model.RequestIdentity) (model.Task, error) {
	if !request.Valid() {
		return model.Task{}, model.ErrInvalidInput
	}
	tx, err := r.DB.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return model.Task{}, err
	}
	defer tx.Rollback(ctx)
	claimed, existing, err := claimIdempotency(ctx, tx, tenant, request, "cancel", id, nil, r.now().UTC())
	if err != nil {
		return model.Task{}, err
	}
	if !claimed {
		if err := tx.Rollback(ctx); err != nil {
			return model.Task{}, err
		}
		return r.GetTask(ctx, tenant, existing.taskID)
	}
	var status model.Status
	if err := tx.QueryRow(ctx, `SELECT status FROM ziggy_work_tasks WHERE user_id=$1 AND workspace_id=$2 AND task_id=$3 FOR UPDATE`, tenant.UserID, tenant.WorkspaceID, id).Scan(&status); err != nil {
		return model.Task{}, mapError(err)
	}
	if status.Terminal() {
		return model.Task{}, model.ErrTerminal
	}
	now := r.now().UTC()
	if _, err = appendEventTx(ctx, tx, tenant, id, "cancel.requested", map[string]any{"status": "cancelled"}, "user", now); err != nil {
		return model.Task{}, err
	}
	if _, err = tx.Exec(ctx, `UPDATE ziggy_work_tasks SET status='cancelled',completed_at=$1,updated_at=$1,version=version+1 WHERE user_id=$2 AND workspace_id=$3 AND task_id=$4`, now, tenant.UserID, tenant.WorkspaceID, id); err != nil {
		return model.Task{}, err
	}
	if _, err = appendEventTx(ctx, tx, tenant, id, "status.changed", map[string]any{"status": model.Cancelled}, "system", now); err != nil {
		return model.Task{}, err
	}
	if err = tx.Commit(ctx); err != nil {
		return model.Task{}, err
	}
	return r.GetTask(ctx, tenant, id)
}
func (r *PostgresRepository) GetArtifact(ctx context.Context, tenant model.Tenant, id string) (model.Artifact, error) {
	var a model.Artifact
	err := r.DB.QueryRow(ctx, `SELECT artifact_id,task_id,step_id,kind,name,mime,size_bytes,sha256,created_at,summary,path_rel,available,unavailable_reason FROM ziggy_work_artifacts WHERE user_id=$1 AND workspace_id=$2 AND artifact_id=$3`, tenant.UserID, tenant.WorkspaceID, id).Scan(&a.ArtifactID, &a.TaskID, &a.StepID, &a.Kind, &a.Name, &a.MIME, &a.SizeBytes, &a.SHA256, &a.CreatedAt, &a.Summary, &a.PathRel, &a.Available, &a.UnavailableReason)
	if errors.Is(err, pgx.ErrNoRows) {
		return model.Artifact{}, model.ErrNotFound
	}
	return a, err
}
func (r *PostgresRepository) RuntimeCursor(ctx context.Context, tenant model.Tenant, runtimeTaskID string) (int64, error) {
	var cursor int64
	err := r.DB.QueryRow(ctx, `SELECT COALESCE(MAX(runtime_seq),0) FROM ziggy_work_events WHERE user_id=$1 AND workspace_id=$2 AND runtime_task_id=$3`, tenant.UserID, tenant.WorkspaceID, runtimeTaskID).Scan(&cursor)
	return cursor, err
}
func (r *PostgresRepository) Subscribe(model.Tenant, string) (<-chan struct{}, func()) {
	return nil, func() {}
}

func (r *PostgresRepository) UpsertRuntimeTask(ctx context.Context, tenant model.Tenant, task model.Task, events []model.Event, artifacts []model.Artifact) error {
	tx, err := r.DB.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	var existing string
	err = tx.QueryRow(ctx, `SELECT task_id FROM ziggy_work_tasks WHERE user_id=$1 AND workspace_id=$2 AND runtime_task_id=$3`, tenant.UserID, tenant.WorkspaceID, task.RuntimeTaskID).Scan(&existing)
	if errors.Is(err, pgx.ErrNoRows) {
		_, err = tx.Exec(ctx, `INSERT INTO ziggy_work_tasks (user_id,workspace_id,task_id,session_key,chat_id,title,prompt_preview,status,mode,model,created_at,updated_at,last_seq,version,runtime_task_id) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,0,$13,$14) ON CONFLICT (user_id,workspace_id,task_id) DO UPDATE SET runtime_task_id=EXCLUDED.runtime_task_id,version=ziggy_work_tasks.version+1`, tenant.UserID, tenant.WorkspaceID, task.TaskID, task.SessionKey, task.ChatID, task.Title, task.PromptPreview, task.Status, task.Mode, task.Model, task.CreatedAt, task.UpdatedAt, 1, task.RuntimeTaskID)
	} else if err != nil {
		return err
	} else {
		task.TaskID = existing
	}
	if err != nil {
		return err
	}
	for _, e := range events {
		if _, err = appendRuntimeEventTx(ctx, tx, tenant, task.TaskID, task.RuntimeTaskID, e, r.now().UTC()); err != nil {
			return err
		}
	}
	for _, step := range task.Steps {
		step.TaskID = task.TaskID
		if _, err = tx.Exec(ctx, `INSERT INTO ziggy_work_steps (user_id,workspace_id,step_id,task_id,seq_start,title,status,started_at,completed_at,summary) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT (user_id,workspace_id,step_id) DO UPDATE SET task_id=EXCLUDED.task_id,seq_start=EXCLUDED.seq_start,title=EXCLUDED.title,status=EXCLUDED.status,started_at=EXCLUDED.started_at,completed_at=EXCLUDED.completed_at,summary=EXCLUDED.summary`, tenant.UserID, tenant.WorkspaceID, step.StepID, step.TaskID, step.SeqStart, step.Title, step.Status, step.StartedAt, step.CompletedAt, step.Summary); err != nil {
			return err
		}
	}
	for _, a := range artifacts {
		a.TaskID = task.TaskID
		if _, err = tx.Exec(ctx, `INSERT INTO ziggy_work_artifacts (user_id,workspace_id,artifact_id,task_id,step_id,kind,name,mime,size_bytes,sha256,created_at,summary,path_rel,available,unavailable_reason) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15) ON CONFLICT (user_id,workspace_id,artifact_id) DO UPDATE SET task_id=EXCLUDED.task_id,step_id=EXCLUDED.step_id,kind=EXCLUDED.kind,name=EXCLUDED.name,mime=EXCLUDED.mime,size_bytes=EXCLUDED.size_bytes,sha256=EXCLUDED.sha256,summary=EXCLUDED.summary,path_rel=EXCLUDED.path_rel,available=EXCLUDED.available,unavailable_reason=EXCLUDED.unavailable_reason`, tenant.UserID, tenant.WorkspaceID, a.ArtifactID, a.TaskID, a.StepID, a.Kind, a.Name, a.MIME, a.SizeBytes, a.SHA256, a.CreatedAt, a.Summary, a.PathRel, a.Available, a.UnavailableReason); err != nil {
			return err
		}
	}
	_, err = tx.Exec(ctx, `UPDATE ziggy_work_tasks SET status=CASE WHEN status='cancelled' THEN status WHEN status IN ('succeeded','failed','interrupted') AND $1 IN ('scheduled','queued','running','waiting') THEN status ELSE $1 END,session_key=$2,chat_id=$3,title=$4,prompt_preview=$5,mode=$6,model=$7,started_at=COALESCE($8,started_at),completed_at=COALESCE($9,completed_at),result_summary=COALESCE($10,result_summary),error=COALESCE($11,error),artifact_count=(SELECT COUNT(*) FROM ziggy_work_artifacts WHERE user_id=$12 AND workspace_id=$13 AND task_id=$14),updated_at=GREATEST(updated_at,$15),version=version+1 WHERE user_id=$12 AND workspace_id=$13 AND task_id=$14`, task.Status, task.SessionKey, task.ChatID, task.Title, task.PromptPreview, task.Mode, task.Model, task.StartedAt, task.CompletedAt, task.ResultSummary, task.Error, tenant.UserID, tenant.WorkspaceID, task.TaskID, task.UpdatedAt)
	if err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (r *PostgresRepository) Import(ctx context.Context, tenant model.Tenant, in LegacyImport) (ImportResult, error) {
	if !tenant.Valid() {
		return ImportResult{}, model.ErrInvalidInput
	}
	tx, err := r.DB.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return ImportResult{}, err
	}
	defer tx.Rollback(ctx)
	res := ImportResult{}
	for _, t := range in.Tasks {
		if !model.WorkIDPattern.MatchString(t.TaskID) || !t.Status.Valid() {
			return ImportResult{}, model.ErrInvalidInput
		}
		if t.SessionKey == "" {
			t.SessionKey = "work:" + t.TaskID
		}
		if t.CreatedAt.IsZero() {
			t.CreatedAt = r.now().UTC()
		}
		if t.UpdatedAt.IsZero() {
			t.UpdatedAt = t.CreatedAt
		}
		if t.ArtifactCount == 0 {
			t.ArtifactCount = len(t.Artifacts)
		}
		if t.Status == model.Scheduled || t.Status.Terminal() {
			t.RiverJobID = ""
		}
		tag, execErr := tx.Exec(ctx, `INSERT INTO ziggy_work_tasks (user_id,workspace_id,task_id,session_key,chat_id,title,prompt_preview,status,mode,model,created_at,updated_at,started_at,completed_at,last_seq,result_summary,error,artifact_count,version,runtime_task_id,river_job_id) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21) ON CONFLICT (user_id,workspace_id,task_id) DO NOTHING`, tenant.UserID, tenant.WorkspaceID, t.TaskID, t.SessionKey, t.ChatID, t.Title, t.PromptPreview, t.Status, t.Mode, t.Model, t.CreatedAt, t.UpdatedAt, t.StartedAt, t.CompletedAt, t.LastSeq, t.ResultSummary, t.Error, t.ArtifactCount, max64(t.Version, 1), t.RuntimeTaskID, t.RiverJobID)
		err = execErr
		if err != nil {
			return ImportResult{}, err
		}
		if tag.RowsAffected() == 1 {
			res.Tasks++
		} else {
			res.Skipped++
		}
	}
	for _, s := range in.Steps {
		tag, execErr := tx.Exec(ctx, `INSERT INTO ziggy_work_steps (user_id,workspace_id,step_id,task_id,seq_start,title,status,started_at,completed_at,summary) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT DO NOTHING`, tenant.UserID, tenant.WorkspaceID, s.StepID, s.TaskID, s.SeqStart, s.Title, s.Status, s.StartedAt, s.CompletedAt, s.Summary)
		err = execErr
		if err != nil {
			return ImportResult{}, err
		}
		if tag.RowsAffected() == 1 {
			res.Steps++
		} else {
			res.Skipped++
		}
	}
	for _, a := range in.Artifacts {
		tag, execErr := tx.Exec(ctx, `INSERT INTO ziggy_work_artifacts (user_id,workspace_id,artifact_id,task_id,step_id,kind,name,mime,size_bytes,sha256,created_at,summary,path_rel,available,unavailable_reason) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15) ON CONFLICT DO NOTHING`, tenant.UserID, tenant.WorkspaceID, a.ArtifactID, a.TaskID, a.StepID, a.Kind, a.Name, a.MIME, a.SizeBytes, a.SHA256, a.CreatedAt, a.Summary, a.PathRel, a.Available, a.UnavailableReason)
		err = execErr
		if err != nil {
			return ImportResult{}, err
		}
		if tag.RowsAffected() == 1 {
			res.Artifacts++
		} else {
			res.Skipped++
		}
	}
	for _, e := range in.Events {
		tag, execErr := tx.Exec(ctx, `INSERT INTO ziggy_work_events (user_id,workspace_id,task_id,seq,type,actor,step_id,created_at,payload) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT DO NOTHING`, tenant.UserID, tenant.WorkspaceID, e.TaskID, e.Seq, e.Type, e.Actor, e.StepID, e.CreatedAt, e.Payload)
		err = execErr
		if err != nil {
			return ImportResult{}, err
		}
		if tag.RowsAffected() == 1 {
			res.Events++
		} else {
			res.Skipped++
		}
	}
	for _, e := range in.Events {
		if _, err = tx.Exec(ctx, `UPDATE ziggy_work_tasks SET last_seq=GREATEST(last_seq,$1) WHERE user_id=$2 AND workspace_id=$3 AND task_id=$4`, e.Seq, tenant.UserID, tenant.WorkspaceID, e.TaskID); err != nil {
			return ImportResult{}, err
		}
	}
	if err = tx.Commit(ctx); err != nil {
		return ImportResult{}, err
	}
	return res, nil
}

func max64(a, b int64) int64 {
	if a > b {
		return a
	}
	return b
}

func appendEventTx(ctx context.Context, tx pgx.Tx, tenant model.Tenant, id, typ string, payload map[string]any, actor string, now time.Time) (model.Event, error) {
	var seq int64
	if err := tx.QueryRow(ctx, `SELECT last_seq FROM ziggy_work_tasks WHERE user_id=$1 AND workspace_id=$2 AND task_id=$3 FOR UPDATE`, tenant.UserID, tenant.WorkspaceID, id).Scan(&seq); err != nil {
		return model.Event{}, mapError(err)
	}
	seq++
	if _, err := tx.Exec(ctx, `INSERT INTO ziggy_work_events (user_id,workspace_id,task_id,seq,type,actor,created_at,payload) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)`, tenant.UserID, tenant.WorkspaceID, id, seq, typ, actor, now, payload); err != nil {
		return model.Event{}, err
	}
	if _, err := tx.Exec(ctx, `UPDATE ziggy_work_tasks SET last_seq=$1,updated_at=$2,version=version+1 WHERE user_id=$3 AND workspace_id=$4 AND task_id=$5`, seq, now, tenant.UserID, tenant.WorkspaceID, id); err != nil {
		return model.Event{}, err
	}
	return model.Event{TaskID: id, Seq: seq, Type: typ, Actor: actor, CreatedAt: now, Payload: payload, Tenant: tenant}, nil
}

type runtimeInsert struct {
	event    model.Event
	inserted bool
}

func appendRuntimeEventTx(ctx context.Context, tx pgx.Tx, tenant model.Tenant, id, runtimeTaskID string, event model.Event, fallbackTime time.Time) (runtimeInsert, error) {
	var existingSeq int64
	err := tx.QueryRow(ctx, `SELECT seq FROM ziggy_work_events WHERE user_id=$1 AND workspace_id=$2 AND runtime_task_id=$3 AND runtime_seq=$4`, tenant.UserID, tenant.WorkspaceID, runtimeTaskID, event.Seq).Scan(&existingSeq)
	if err == nil {
		event.TaskID = id
		event.RuntimeTaskID = runtimeTaskID
		event.RuntimeSeq = event.Seq
		event.Seq = existingSeq
		return runtimeInsert{event: event}, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return runtimeInsert{}, err
	}
	var lastSeq int64
	if err := tx.QueryRow(ctx, `SELECT last_seq FROM ziggy_work_tasks WHERE user_id=$1 AND workspace_id=$2 AND task_id=$3 FOR UPDATE`, tenant.UserID, tenant.WorkspaceID, id).Scan(&lastSeq); err != nil {
		return runtimeInsert{}, mapError(err)
	}
	if err := tx.QueryRow(ctx, `SELECT seq FROM ziggy_work_events WHERE user_id=$1 AND workspace_id=$2 AND runtime_task_id=$3 AND runtime_seq=$4`, tenant.UserID, tenant.WorkspaceID, runtimeTaskID, event.Seq).Scan(&existingSeq); err == nil {
		event.TaskID = id
		event.RuntimeTaskID = runtimeTaskID
		event.RuntimeSeq = event.Seq
		event.Seq = existingSeq
		return runtimeInsert{event: event}, nil
	} else if !errors.Is(err, pgx.ErrNoRows) {
		return runtimeInsert{}, err
	}
	createdAt := event.CreatedAt
	if createdAt.IsZero() {
		createdAt = fallbackTime
	}
	localSeq := lastSeq + 1
	if event.Payload == nil {
		event.Payload = map[string]any{}
	}
	if _, err := tx.Exec(ctx, `INSERT INTO ziggy_work_events (user_id,workspace_id,task_id,seq,type,actor,step_id,created_at,payload,runtime_task_id,runtime_seq) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)`, tenant.UserID, tenant.WorkspaceID, id, localSeq, event.Type, event.Actor, event.StepID, createdAt, event.Payload, runtimeTaskID, event.Seq); err != nil {
		return runtimeInsert{}, err
	}
	newStatus := ""
	if event.Type == "status.changed" {
		if value, ok := event.Payload["status"].(string); ok && model.Status(value).Valid() {
			newStatus = value
		}
	}
	errorText := payloadString(event.Payload, "error")
	resultSummary := payloadString(event.Payload, "result_summary")
	if _, err := tx.Exec(ctx, `UPDATE ziggy_work_tasks SET last_seq=$1,updated_at=GREATEST(updated_at,$2),status=CASE WHEN status='cancelled' OR $6::text='' THEN status WHEN status IN ('succeeded','failed','interrupted') AND $6::text IN ('scheduled','queued','running','waiting') THEN status ELSE $6::text END,started_at=CASE WHEN $6::text='running' THEN COALESCE(started_at,$2) ELSE started_at END,completed_at=CASE WHEN $6::text IN ('succeeded','failed','cancelled','interrupted') THEN COALESCE(completed_at,$2) ELSE completed_at END,error=COALESCE($7,error),result_summary=COALESCE($8,result_summary),version=version+1 WHERE user_id=$3 AND workspace_id=$4 AND task_id=$5`, localSeq, createdAt, tenant.UserID, tenant.WorkspaceID, id, newStatus, errorText, resultSummary); err != nil {
		return runtimeInsert{}, err
	}
	event.TaskID = id
	event.RuntimeTaskID = runtimeTaskID
	event.RuntimeSeq = event.Seq
	event.Seq = localSeq
	return runtimeInsert{event: event, inserted: true}, nil
}

func payloadString(payload map[string]any, key string) *string {
	value, ok := payload[key].(string)
	if !ok || value == "" {
		return nil
	}
	return &value
}

type rowScanner interface{ Scan(...any) error }

func scanTask(row rowScanner, tenant model.Tenant) (model.Task, error) {
	var t model.Task
	var status string
	var runtimeTaskID, riverJobID *string
	err := row.Scan(&t.TaskID, &t.SessionKey, &t.ChatID, &t.Title, &t.PromptPreview, &status, &t.Mode, &t.Model, &t.CreatedAt, &t.UpdatedAt, &t.StartedAt, &t.CompletedAt, &t.LastSeq, &t.ResultSummary, &t.Error, &t.ArtifactCount, &t.Version, &runtimeTaskID, &riverJobID)
	if errors.Is(err, pgx.ErrNoRows) {
		return model.Task{}, model.ErrNotFound
	}
	if err != nil {
		return model.Task{}, err
	}
	t.Status = model.Status(status)
	if runtimeTaskID != nil {
		t.RuntimeTaskID = *runtimeTaskID
	}
	if riverJobID != nil {
		t.RiverJobID = *riverJobID
	}
	t.Tenant = tenant
	return t, nil
}
func (r *PostgresRepository) loadDetails(ctx context.Context, t model.Task) (model.Task, error) {
	rows, err := r.DB.Query(ctx, `SELECT step_id,task_id,seq_start,title,status,started_at,completed_at,summary FROM ziggy_work_steps WHERE user_id=$1 AND workspace_id=$2 AND task_id=$3 ORDER BY seq_start`, t.Tenant.UserID, t.Tenant.WorkspaceID, t.TaskID)
	if err != nil {
		return model.Task{}, err
	}
	for rows.Next() {
		var s model.Step
		if err := rows.Scan(&s.StepID, &s.TaskID, &s.SeqStart, &s.Title, &s.Status, &s.StartedAt, &s.CompletedAt, &s.Summary); err != nil {
			rows.Close()
			return model.Task{}, err
		}
		t.Steps = append(t.Steps, s)
	}
	rows.Close()
	rows, err = r.DB.Query(ctx, `SELECT artifact_id,task_id,step_id,kind,name,mime,size_bytes,sha256,created_at,summary,path_rel,available,unavailable_reason FROM ziggy_work_artifacts WHERE user_id=$1 AND workspace_id=$2 AND task_id=$3 ORDER BY created_at`, t.Tenant.UserID, t.Tenant.WorkspaceID, t.TaskID)
	if err != nil {
		return model.Task{}, err
	}
	for rows.Next() {
		var a model.Artifact
		if err := rows.Scan(&a.ArtifactID, &a.TaskID, &a.StepID, &a.Kind, &a.Name, &a.MIME, &a.SizeBytes, &a.SHA256, &a.CreatedAt, &a.Summary, &a.PathRel, &a.Available, &a.UnavailableReason); err != nil {
			rows.Close()
			return model.Task{}, err
		}
		t.Artifacts = append(t.Artifacts, a)
	}
	rows.Close()
	t.ArtifactCount = len(t.Artifacts)
	return t, nil
}
func mapError(err error) error {
	if errors.Is(err, pgx.ErrNoRows) {
		return model.ErrNotFound
	}
	var e *pgconn.PgError
	if errors.As(err, &e) && e.Code == "23505" {
		return model.ErrConflict
	}
	return err
}

type idempotencyResult struct {
	taskID   string
	eventSeq *int64
}

func claimIdempotency(ctx context.Context, tx pgx.Tx, tenant model.Tenant, request model.RequestIdentity, operation, taskID string, eventSeq *int64, now time.Time) (bool, idempotencyResult, error) {
	tag, err := tx.Exec(ctx, `INSERT INTO ziggy_work_idempotency (user_id,workspace_id,idempotency_key,operation,payload_hash,task_id,event_seq,created_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT (user_id,workspace_id,idempotency_key) DO NOTHING`, tenant.UserID, tenant.WorkspaceID, request.Key, operation, request.PayloadHash, taskID, eventSeq, now)
	if err != nil {
		return false, idempotencyResult{}, err
	}
	if tag.RowsAffected() == 1 {
		return true, idempotencyResult{taskID: taskID, eventSeq: eventSeq}, nil
	}
	var existingOperation, existingHash, existingTaskID string
	var existingEventSeq *int64
	if err := tx.QueryRow(ctx, `SELECT operation,payload_hash,task_id,event_seq FROM ziggy_work_idempotency WHERE user_id=$1 AND workspace_id=$2 AND idempotency_key=$3`, tenant.UserID, tenant.WorkspaceID, request.Key).Scan(&existingOperation, &existingHash, &existingTaskID, &existingEventSeq); err != nil {
		return false, idempotencyResult{}, err
	}
	if existingOperation != operation || existingHash != request.PayloadHash || existingTaskID != taskID && operation != "create" {
		return false, idempotencyResult{}, model.ErrIdempotencyConflict
	}
	return false, idempotencyResult{taskID: existingTaskID, eventSeq: existingEventSeq}, nil
}

func (r *PostgresRepository) getEvent(ctx context.Context, tenant model.Tenant, taskID string, seq int64) (model.Event, error) {
	var event model.Event
	err := r.DB.QueryRow(ctx, `SELECT task_id,seq,type,actor,step_id,created_at,payload FROM ziggy_work_events WHERE user_id=$1 AND workspace_id=$2 AND task_id=$3 AND seq=$4`, tenant.UserID, tenant.WorkspaceID, taskID, seq).Scan(&event.TaskID, &event.Seq, &event.Type, &event.Actor, &event.StepID, &event.CreatedAt, &event.Payload)
	if errors.Is(err, pgx.ErrNoRows) {
		return model.Event{}, model.ErrNotFound
	}
	event.Tenant = tenant
	return event, err
}

// MemoryRepository is concurrency-safe and intentionally mirrors tenant and
// sequence constraints used by PostgreSQL for unit and race tests.
type MemoryRepository struct {
	mu          sync.RWMutex
	tasks       map[string]model.Task
	events      map[string][]model.Event
	steps       map[string][]model.Step
	artifacts   map[string][]model.Artifact
	idempotency map[string]memoryIdempotency
	notify      map[string]map[chan struct{}]struct{}
	now         func() time.Time
}

type memoryIdempotency struct {
	operation   string
	payloadHash string
	taskID      string
	eventSeq    int64
}

func NewMemory() *MemoryRepository {
	return &MemoryRepository{tasks: map[string]model.Task{}, events: map[string][]model.Event{}, steps: map[string][]model.Step{}, artifacts: map[string][]model.Artifact{}, idempotency: map[string]memoryIdempotency{}, notify: map[string]map[chan struct{}]struct{}{}, now: time.Now}
}
func memKey(t model.Tenant, id string) string            { return t.UserID + "\x00" + t.WorkspaceID + "\x00" + id }
func memRequestKey(t model.Tenant, key string) string    { return memKey(t, key) }
func (r *MemoryRepository) Health(context.Context) error { return nil }
func (r *MemoryRepository) CreateTask(ctx context.Context, t model.Tenant, i model.CreateInput, request model.RequestIdentity, q job.Enqueuer) (model.Task, error) {
	if i.Validate() != nil || !request.Valid() || q == nil {
		return model.Task{}, model.ErrInvalidInput
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	requestKey := memRequestKey(t, request.Key)
	if existing, ok := r.idempotency[requestKey]; ok {
		if existing.operation != "create" || existing.payloadHash != request.PayloadHash {
			return model.Task{}, model.ErrIdempotencyConflict
		}
		return r.tasks[memKey(t, existing.taskID)], nil
	}
	id := newWorkID()
	now := r.now().UTC()
	if i.Mode == "" {
		i.Mode = "background"
	}
	task := model.Task{TaskID: id, SessionKey: "work:" + id, ChatID: i.ChatID, Title: i.Title, PromptPreview: model.Preview(i.Content, 240), Status: model.Queued, Mode: i.Mode, Model: i.Model, CreatedAt: now, UpdatedAt: now, LastSeq: 1, Version: 1, Tenant: t}
	if task.Title == "" {
		task.Title = model.Preview(i.Content, 120)
	}
	k := memKey(t, id)
	r.tasks[k] = task
	r.events[k] = []model.Event{{TaskID: id, Seq: 1, Type: "task.created", Actor: "system", CreatedAt: now, Payload: map[string]any{"task_id": id, "title": task.Title, "status": task.Status}, Tenant: t}}
	r.idempotency[requestKey] = memoryIdempotency{operation: "create", payloadHash: request.PayloadHash, taskID: id}
	jobID, err := q.Enqueue(ctx, job.Job{TaskID: id, UserID: t.UserID, WorkspaceID: t.WorkspaceID, Content: i.Content, ChatID: i.ChatID, CommandID: id})
	if err != nil {
		delete(r.tasks, k)
		delete(r.events, k)
		delete(r.idempotency, requestKey)
		return model.Task{}, err
	}
	task.RiverJobID = jobID
	task.Version = 2
	r.tasks[k] = task
	return task, nil
}
func (r *MemoryRepository) ListTasks(_ context.Context, t model.Tenant, f model.ListFilter) ([]model.Task, error) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := []model.Task{}
	for _, v := range r.tasks {
		if v.Tenant == t && (f.Status == "" || v.Status == f.Status) {
			v.Steps = append([]model.Step(nil), r.steps[memKey(t, v.TaskID)]...)
			v.Artifacts = append([]model.Artifact(nil), r.artifacts[memKey(t, v.TaskID)]...)
			v.ArtifactCount = len(v.Artifacts)
			out = append(out, v)
		}
	}
	return out, nil
}
func (r *MemoryRepository) GetTask(_ context.Context, t model.Tenant, id string) (model.Task, error) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	v, ok := r.tasks[memKey(t, id)]
	if !ok {
		return model.Task{}, model.ErrNotFound
	}
	v.Steps = append([]model.Step(nil), r.steps[memKey(t, id)]...)
	v.Artifacts = append([]model.Artifact(nil), r.artifacts[memKey(t, id)]...)
	v.ArtifactCount = len(v.Artifacts)
	return v, nil
}
func (r *MemoryRepository) ListEvents(_ context.Context, t model.Tenant, id string, after int64, limit int) ([]model.Event, error) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	all := r.events[memKey(t, id)]
	out := []model.Event{}
	for _, e := range all {
		if e.Seq > after {
			out = append(out, e)
			if len(out) >= limit && limit > 0 {
				break
			}
		}
	}
	return out, nil
}
func (r *MemoryRepository) AppendEvent(_ context.Context, t model.Tenant, id, typ string, payload map[string]any, actor string) (model.Event, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	k := memKey(t, id)
	task, ok := r.tasks[k]
	if !ok {
		return model.Event{}, model.ErrNotFound
	}
	now := r.now().UTC()
	e := model.Event{TaskID: id, Seq: task.LastSeq + 1, Type: typ, Actor: actor, CreatedAt: now, Payload: payload, Tenant: t}
	task.LastSeq = e.Seq
	task.UpdatedAt = now
	task.Version++
	r.tasks[k] = task
	r.events[k] = append(r.events[k], e)
	r.signalLocked(k)
	return e, nil
}
func (r *MemoryRepository) AppendRuntimeEvent(_ context.Context, t model.Tenant, id, runtimeTaskID string, event model.Event) (model.Event, bool, error) {
	if runtimeTaskID == "" || event.Seq <= 0 {
		return model.Event{}, false, model.ErrInvalidInput
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	k := memKey(t, id)
	task, ok := r.tasks[k]
	if !ok {
		return model.Event{}, false, model.ErrNotFound
	}
	for _, existing := range r.events[k] {
		if existing.RuntimeTaskID == runtimeTaskID && existing.RuntimeSeq == event.Seq {
			return existing, false, nil
		}
	}
	runtimeSeq := event.Seq
	event.TaskID = id
	event.Seq = task.LastSeq + 1
	event.RuntimeTaskID = runtimeTaskID
	event.RuntimeSeq = runtimeSeq
	event.Tenant = t
	if event.CreatedAt.IsZero() {
		event.CreatedAt = r.now().UTC()
	}
	task.LastSeq = event.Seq
	task.UpdatedAt = event.CreatedAt
	task.Version++
	if event.Type == "status.changed" {
		if raw, ok := event.Payload["status"].(string); ok && model.Status(raw).Valid() {
			incoming := model.Status(raw)
			if task.Status != model.Cancelled && !(task.Status.Terminal() && !incoming.Terminal()) {
				task.Status = incoming
			}
			if task.Status == model.Running && task.StartedAt == nil {
				startedAt := event.CreatedAt
				task.StartedAt = &startedAt
			}
			if task.Status.Terminal() && task.CompletedAt == nil {
				completedAt := event.CreatedAt
				task.CompletedAt = &completedAt
			}
		}
		if value := payloadString(event.Payload, "error"); value != nil {
			task.Error = value
		}
		if value := payloadString(event.Payload, "result_summary"); value != nil {
			task.ResultSummary = value
		}
	}
	r.tasks[k] = task
	r.events[k] = append(r.events[k], event)
	r.signalLocked(k)
	return event, true, nil
}
func (r *MemoryRepository) UpdateStatus(_ context.Context, t model.Tenant, id string, status model.Status, message, summary *string) (model.Task, model.Event, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	k := memKey(t, id)
	task, ok := r.tasks[k]
	if !ok {
		return model.Task{}, model.Event{}, model.ErrNotFound
	}
	if task.Status.Terminal() {
		return model.Task{}, model.Event{}, model.ErrTerminal
	}
	now := r.now().UTC()
	from := task.Status
	task.Status = status
	task.UpdatedAt = now
	task.Version++
	if status == model.Running && task.StartedAt == nil {
		task.StartedAt = &now
	}
	if status.Terminal() {
		task.CompletedAt = &now
	}
	task.Error = message
	task.ResultSummary = summary
	task.LastSeq++
	e := model.Event{TaskID: id, Seq: task.LastSeq, Type: "status.changed", Actor: "system", CreatedAt: now, Payload: map[string]any{"status": status, "error": message, "result_summary": summary}, Tenant: t}
	r.tasks[k] = task
	r.events[k] = append(r.events[k], e)
	_ = from
	r.signalLocked(k)
	return task, e, nil
}
func (r *MemoryRepository) SetRuntimeTask(_ context.Context, t model.Tenant, id, rt string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	k := memKey(t, id)
	v, ok := r.tasks[k]
	if !ok {
		return model.ErrNotFound
	}
	v.RuntimeTaskID = rt
	v.Version++
	r.tasks[k] = v
	return nil
}
func (r *MemoryRepository) EnqueueFollowUp(ctx context.Context, t model.Tenant, id, content string, request model.RequestIdentity, enqueuer job.Enqueuer) (model.Task, model.Event, error) {
	if !request.Valid() || strings.TrimSpace(content) == "" || enqueuer == nil {
		return model.Task{}, model.Event{}, model.ErrInvalidInput
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	requestKey := memRequestKey(t, request.Key)
	if existing, ok := r.idempotency[requestKey]; ok {
		if existing.operation != "message" || existing.payloadHash != request.PayloadHash || existing.taskID != id {
			return model.Task{}, model.Event{}, model.ErrIdempotencyConflict
		}
		key := memKey(t, id)
		for _, event := range r.events[key] {
			if event.Seq == existing.eventSeq {
				return r.tasks[key], event, nil
			}
		}
		return model.Task{}, model.Event{}, model.ErrConflict
	}
	task, ok := r.tasks[memKey(t, id)]
	if !ok {
		return model.Task{}, model.Event{}, model.ErrNotFound
	}
	if task.Status.Terminal() {
		return model.Task{}, model.Event{}, model.ErrTerminal
	}
	jobID, err := enqueuer.Enqueue(ctx, job.Job{TaskID: id, UserID: t.UserID, WorkspaceID: t.WorkspaceID, Content: content, ChatID: task.ChatID, FollowUp: true, CommandID: newCommandID()})
	if err != nil {
		return model.Task{}, model.Event{}, err
	}
	k := memKey(t, id)
	task = r.tasks[k]
	if task.Status == model.Waiting {
		task.Status = model.Queued
		task.Version++
		task.LastSeq++
		task.UpdatedAt = r.now().UTC()
		r.events[k] = append(r.events[k], model.Event{TaskID: id, Seq: task.LastSeq, Type: "status.changed", Actor: "system", CreatedAt: task.UpdatedAt, Payload: map[string]any{"status": model.Queued}, Tenant: t})
	}
	task.RiverJobID = jobID
	task.LastSeq++
	task.Version++
	task.UpdatedAt = r.now().UTC()
	e := model.Event{TaskID: id, Seq: task.LastSeq, Type: "message.received", Actor: "user", CreatedAt: task.UpdatedAt, Payload: map[string]any{"content": model.Preview(content, 240)}, Tenant: t}
	r.tasks[k] = task
	r.events[k] = append(r.events[k], e)
	r.idempotency[requestKey] = memoryIdempotency{operation: "message", payloadHash: request.PayloadHash, taskID: id, eventSeq: e.Seq}
	r.signalLocked(k)
	return task, e, nil
}
func (r *MemoryRepository) CancelTask(_ context.Context, t model.Tenant, id string, request model.RequestIdentity) (model.Task, error) {
	if !request.Valid() {
		return model.Task{}, model.ErrInvalidInput
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	k := memKey(t, id)
	requestKey := memRequestKey(t, request.Key)
	if existing, ok := r.idempotency[requestKey]; ok {
		if existing.operation != "cancel" || existing.payloadHash != request.PayloadHash || existing.taskID != id {
			return model.Task{}, model.ErrIdempotencyConflict
		}
		return r.tasks[k], nil
	}
	task, ok := r.tasks[k]
	if !ok {
		return model.Task{}, model.ErrNotFound
	}
	if task.Status.Terminal() {
		return model.Task{}, model.ErrTerminal
	}
	now := r.now().UTC()
	for _, typ := range []string{"cancel.requested", "status.changed"} {
		task.LastSeq++
		payload := map[string]any{"status": model.Cancelled}
		if typ == "cancel.requested" {
			payload = map[string]any{"status": "cancelled"}
		}
		r.events[k] = append(r.events[k], model.Event{TaskID: id, Seq: task.LastSeq, Type: typ, Actor: "user", CreatedAt: now, Payload: payload, Tenant: t})
	}
	task.Status = model.Cancelled
	task.CompletedAt = &now
	task.UpdatedAt = now
	task.Version++
	r.tasks[k] = task
	r.idempotency[requestKey] = memoryIdempotency{operation: "cancel", payloadHash: request.PayloadHash, taskID: id}
	r.signalLocked(k)
	return task, nil
}
func (r *MemoryRepository) GetArtifact(_ context.Context, t model.Tenant, id string) (model.Artifact, error) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	for _, items := range r.artifacts {
		for _, a := range items {
			if a.ArtifactID == id && a.TaskID != "" {
				if v, ok := r.tasks[memKey(t, a.TaskID)]; ok && v.Tenant == t {
					return a, nil
				}
			}
		}
	}
	return model.Artifact{}, model.ErrNotFound
}
func (r *MemoryRepository) RuntimeCursor(_ context.Context, t model.Tenant, runtimeTaskID string) (int64, error) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	var cursor int64
	for _, events := range r.events {
		for _, event := range events {
			if event.Tenant == t && event.RuntimeTaskID == runtimeTaskID && event.RuntimeSeq > cursor {
				cursor = event.RuntimeSeq
			}
		}
	}
	return cursor, nil
}
func (r *MemoryRepository) UpsertRuntimeTask(_ context.Context, t model.Tenant, task model.Task, events []model.Event, artifacts []model.Artifact) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	var current model.Task
	foundCurrent := false
	for _, v := range r.tasks {
		if v.Tenant == t && v.RuntimeTaskID != "" && v.RuntimeTaskID == task.RuntimeTaskID {
			task.TaskID = v.TaskID
			current = v
			foundCurrent = true
			break
		}
	}
	k := memKey(t, task.TaskID)
	task.Tenant = t
	if foundCurrent {
		lastSeq := current.LastSeq
		riverJobID := current.RiverJobID
		version := current.Version
		if current.Status == model.Cancelled || current.Status.Terminal() && !task.Status.Terminal() {
			task.Status = current.Status
			task.CompletedAt = current.CompletedAt
		}
		task.LastSeq = lastSeq
		task.RiverJobID = riverJobID
		task.Version = version + 1
	}
	r.tasks[k] = task
	for _, e := range events {
		found := false
		for _, old := range r.events[k] {
			if old.RuntimeTaskID == task.RuntimeTaskID && old.RuntimeSeq == e.Seq {
				found = true
				break
			}
		}
		if !found {
			runtimeSeq := e.Seq
			task = r.tasks[k]
			e.TaskID = task.TaskID
			e.Seq = task.LastSeq + 1
			e.RuntimeTaskID = task.RuntimeTaskID
			e.RuntimeSeq = runtimeSeq
			e.Tenant = t
			r.events[k] = append(r.events[k], e)
			task.LastSeq = e.Seq
			task.UpdatedAt = e.CreatedAt
			task.Version++
			r.tasks[k] = task
		}
	}
	stepsByID := make(map[string]model.Step, len(r.steps[k])+len(task.Steps))
	for _, step := range r.steps[k] {
		stepsByID[step.StepID] = step
	}
	for _, step := range task.Steps {
		step.TaskID = task.TaskID
		stepsByID[step.StepID] = step
	}
	r.steps[k] = r.steps[k][:0]
	for _, step := range stepsByID {
		r.steps[k] = append(r.steps[k], step)
	}
	byID := make(map[string]model.Artifact, len(r.artifacts[k])+len(artifacts))
	for _, artifact := range r.artifacts[k] {
		byID[artifact.ArtifactID] = artifact
	}
	for _, artifact := range artifacts {
		artifact.TaskID = task.TaskID
		byID[artifact.ArtifactID] = artifact
	}
	r.artifacts[k] = r.artifacts[k][:0]
	for _, artifact := range byID {
		r.artifacts[k] = append(r.artifacts[k], artifact)
	}
	task = r.tasks[k]
	task.ArtifactCount = len(r.artifacts[k])
	r.tasks[k] = task
	r.signalLocked(k)
	return nil
}
func (r *MemoryRepository) Subscribe(t model.Tenant, id string) (<-chan struct{}, func()) {
	ch := make(chan struct{}, 1)
	key := memKey(t, id)
	r.mu.Lock()
	if r.notify[key] == nil {
		r.notify[key] = map[chan struct{}]struct{}{}
	}
	r.notify[key][ch] = struct{}{}
	r.mu.Unlock()
	return ch, func() {
		r.mu.Lock()
		if subscribers := r.notify[key]; subscribers != nil {
			delete(subscribers, ch)
			if len(subscribers) == 0 {
				delete(r.notify, key)
			}
		}
		close(ch)
		r.mu.Unlock()
	}
}
func (r *MemoryRepository) signalLocked(id string) {
	for ch := range r.notify[id] {
		select {
		case ch <- struct{}{}:
		default:
		}
	}
}
func (r *MemoryRepository) Import(_ context.Context, t model.Tenant, in LegacyImport) (ImportResult, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	res := ImportResult{}
	for _, task := range in.Tasks {
		k := memKey(t, task.TaskID)
		if old, ok := r.tasks[k]; ok && old.TaskID == task.TaskID {
			res.Skipped++
			continue
		}
		task.Tenant = t
		if !model.WorkIDPattern.MatchString(task.TaskID) || !task.Status.Valid() {
			return ImportResult{}, model.ErrInvalidInput
		}
		if task.SessionKey == "" {
			task.SessionKey = "work:" + task.TaskID
		}
		if task.Status == model.Scheduled || task.Status.Terminal() {
			task.RiverJobID = ""
		}
		r.tasks[k] = task
		res.Tasks++
	}
	for _, s := range in.Steps {
		k := memKey(t, s.TaskID)
		found := false
		for _, old := range r.steps[k] {
			if old.StepID == s.StepID {
				found = true
				break
			}
		}
		if !found {
			r.steps[k] = append(r.steps[k], s)
			res.Steps++
		}
	}
	for _, a := range in.Artifacts {
		k := memKey(t, a.TaskID)
		found := false
		for _, old := range r.artifacts[k] {
			if old.ArtifactID == a.ArtifactID {
				found = true
				break
			}
		}
		if !found {
			r.artifacts[k] = append(r.artifacts[k], a)
			res.Artifacts++
		}
	}
	for _, e := range in.Events {
		k := memKey(t, e.TaskID)
		e.Tenant = t
		found := false
		for _, old := range r.events[k] {
			if old.Seq == e.Seq {
				found = true
				break
			}
		}
		if !found {
			r.events[k] = append(r.events[k], e)
			res.Events++
		}
		if v, ok := r.tasks[k]; ok && e.Seq > v.LastSeq {
			v.LastSeq = e.Seq
			r.tasks[k] = v
		}
	}
	return res, nil
}

func newWorkID() string {
	b := make([]byte, 16)
	if _, err := cryptorand.Read(b); err != nil {
		panic("crypto/rand unavailable")
	}
	return "work_" + fmtHex(b)
}

func newCommandID() string {
	b := make([]byte, 16)
	if _, err := cryptorand.Read(b); err != nil {
		panic("crypto/rand unavailable")
	}
	return "cmd_" + fmtHex(b)
}
func fmtHex(b []byte) string {
	const h = "0123456789abcdef"
	out := make([]byte, len(b)*2)
	for i, v := range b {
		out[i*2], out[i*2+1] = h[v>>4], h[v&15]
	}
	return string(out)
}
