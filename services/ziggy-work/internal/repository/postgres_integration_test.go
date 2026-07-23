package repository

import (
	"context"
	"errors"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/job"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/model"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/queue"
)

func TestPostgresRuntimePersistence(t *testing.T) {
	databaseURL := os.Getenv("ZIGGY_WORK_TEST_DATABASE_URL")
	if databaseURL == "" {
		t.Skip("ZIGGY_WORK_TEST_DATABASE_URL is not set")
	}
	ctx := context.Background()
	pool, err := pgxpool.New(ctx, databaseURL)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	repo := NewPostgres(pool)
	tenant := model.Tenant{UserID: "integration-user", WorkspaceID: "integration-workspace"}
	taskID := "work_10000000000000000000000000000001"
	runtimeID := "work_10000000000000000000000000000002"
	_, _ = pool.Exec(ctx, `DELETE FROM ziggy_work_tasks WHERE user_id=$1 AND workspace_id=$2`, tenant.UserID, tenant.WorkspaceID)
	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(), `DELETE FROM ziggy_work_tasks WHERE user_id=$1 AND workspace_id=$2`, tenant.UserID, tenant.WorkspaceID)
	})
	now := time.Now().UTC()
	task := model.Task{
		TaskID: taskID, RuntimeTaskID: runtimeID, SessionKey: "work:" + taskID,
		ChatID: "chat", Title: "integration", Status: model.Running,
		Mode: "background", CreatedAt: now, UpdatedAt: now,
		Steps: []model.Step{{StepID: "step_integration", TaskID: taskID, SeqStart: 1, Title: "Step", Status: "running"}},
	}
	if err := repo.UpsertRuntimeTask(ctx, tenant, task, nil, nil); err != nil {
		t.Fatal(err)
	}
	event := model.Event{Seq: 1, Type: "status.changed", Actor: "nanobot", CreatedAt: now, Payload: map[string]any{"status": "succeeded"}}
	results := make(chan bool, 2)
	errors := make(chan error, 2)
	var wait sync.WaitGroup
	for range 2 {
		wait.Add(1)
		go func() {
			defer wait.Done()
			_, inserted, appendErr := repo.AppendRuntimeEvent(ctx, tenant, taskID, runtimeID, event)
			results <- inserted
			errors <- appendErr
		}()
	}
	wait.Wait()
	close(results)
	close(errors)
	insertedCount := 0
	for inserted := range results {
		if inserted {
			insertedCount++
		}
	}
	for appendErr := range errors {
		if appendErr != nil {
			t.Fatal(appendErr)
		}
	}
	if insertedCount != 1 {
		t.Fatalf("inserted duplicate runtime events: %d", insertedCount)
	}
	stale := model.Event{Seq: 2, Type: "status.changed", Actor: "nanobot", CreatedAt: now, Payload: map[string]any{"status": "running"}}
	if _, _, err := repo.AppendRuntimeEvent(ctx, tenant, taskID, runtimeID, stale); err != nil {
		t.Fatal(err)
	}
	stored, err := repo.GetTask(ctx, tenant, taskID)
	if err != nil || stored.Status != model.Succeeded || len(stored.Steps) != 1 {
		t.Fatalf("stored=%+v err=%v", stored, err)
	}
	cursor, err := repo.RuntimeCursor(ctx, tenant, runtimeID)
	if err != nil || cursor != 2 {
		t.Fatalf("cursor=%d err=%v", cursor, err)
	}
}

func TestPostgresMutationsAreIdempotentWithRiverEnqueue(t *testing.T) {
	databaseURL := os.Getenv("ZIGGY_WORK_TEST_DATABASE_URL")
	if databaseURL == "" {
		t.Skip("ZIGGY_WORK_TEST_DATABASE_URL is not set")
	}
	ctx := context.Background()
	pool, err := pgxpool.New(ctx, databaseURL)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	repo := NewPostgres(pool)
	workQueue, err := queue.NewRiver(pool, "ziggy_work_integration", 1, func(context.Context, job.Job) error { return nil })
	if err != nil {
		t.Fatal(err)
	}
	tenant := model.Tenant{UserID: "idempotency-user", WorkspaceID: "idempotency-workspace"}
	otherTenant := model.Tenant{UserID: "idempotency-other", WorkspaceID: "idempotency-workspace"}
	for _, item := range []model.Tenant{tenant, otherTenant} {
		_, _ = pool.Exec(ctx, `DELETE FROM ziggy_work_tasks WHERE user_id=$1 AND workspace_id=$2`, item.UserID, item.WorkspaceID)
	}
	t.Cleanup(func() {
		for _, item := range []model.Tenant{tenant, otherTenant} {
			_, _ = pool.Exec(context.Background(), `DELETE FROM ziggy_work_tasks WHERE user_id=$1 AND workspace_id=$2`, item.UserID, item.WorkspaceID)
		}
	})
	identity := model.RequestIdentity{Key: "create-request-0001", PayloadHash: strings.Repeat("a", 64)}
	input := model.CreateInput{ChatID: "chat", Content: "prepare report"}
	first, err := repo.CreateTask(ctx, tenant, input, identity, workQueue)
	if err != nil {
		t.Fatal(err)
	}
	replay, err := repo.CreateTask(ctx, tenant, input, identity, workQueue)
	if err != nil || replay.TaskID != first.TaskID || replay.RiverJobID != first.RiverJobID {
		t.Fatalf("replay=%+v first=%+v err=%v", replay, first, err)
	}
	changed := identity
	changed.PayloadHash = strings.Repeat("b", 64)
	if _, err := repo.CreateTask(ctx, tenant, input, changed, workQueue); !errors.Is(err, model.ErrIdempotencyConflict) {
		t.Fatalf("changed request error=%v", err)
	}
	if _, err := repo.CreateTask(ctx, otherTenant, input, identity, workQueue); err != nil {
		t.Fatalf("same key in another tenant: %v", err)
	}

	messageIdentity := model.RequestIdentity{Key: "message-request-0001", PayloadHash: strings.Repeat("c", 64)}
	_, firstEvent, err := repo.EnqueueFollowUp(ctx, tenant, first.TaskID, "continue", messageIdentity, workQueue)
	if err != nil {
		t.Fatal(err)
	}
	_, replayEvent, err := repo.EnqueueFollowUp(ctx, tenant, first.TaskID, "continue", messageIdentity, workQueue)
	if err != nil || replayEvent.Seq != firstEvent.Seq {
		t.Fatalf("message replay=%+v first=%+v err=%v", replayEvent, firstEvent, err)
	}
	var taskCount, requestCount, riverJobCount int
	if err := pool.QueryRow(ctx, `SELECT COUNT(*) FROM ziggy_work_tasks WHERE user_id=$1 AND workspace_id=$2`, tenant.UserID, tenant.WorkspaceID).Scan(&taskCount); err != nil {
		t.Fatal(err)
	}
	if err := pool.QueryRow(ctx, `SELECT COUNT(*) FROM ziggy_work_idempotency WHERE user_id=$1 AND workspace_id=$2`, tenant.UserID, tenant.WorkspaceID).Scan(&requestCount); err != nil {
		t.Fatal(err)
	}
	if err := pool.QueryRow(ctx, `SELECT COUNT(*) FROM river_job WHERE id IN ($1::bigint,$2::bigint)`, first.RiverJobID, replay.RiverJobID).Scan(&riverJobCount); err != nil {
		t.Fatal(err)
	}
	if taskCount != 1 || requestCount != 2 || riverJobCount != 1 {
		t.Fatalf("tasks=%d requests=%d river_jobs=%d", taskCount, requestCount, riverJobCount)
	}
}

func TestPostgresImportStoresMissingOptionalIDsAsNull(t *testing.T) {
	databaseURL := os.Getenv("ZIGGY_WORK_TEST_DATABASE_URL")
	if databaseURL == "" {
		t.Skip("ZIGGY_WORK_TEST_DATABASE_URL is not set")
	}
	ctx := context.Background()
	pool, err := pgxpool.New(ctx, databaseURL)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	repo := NewPostgres(pool)
	tenant := model.Tenant{UserID: "import-user", WorkspaceID: "import-workspace"}
	_, _ = pool.Exec(ctx, `DELETE FROM ziggy_work_tasks WHERE user_id=$1 AND workspace_id=$2`, tenant.UserID, tenant.WorkspaceID)
	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(), `DELETE FROM ziggy_work_tasks WHERE user_id=$1 AND workspace_id=$2`, tenant.UserID, tenant.WorkspaceID)
	})
	now := time.Now().UTC()
	input := LegacyImport{Tasks: []model.Task{
		{TaskID: "work_20000000000000000000000000000001", Status: model.Succeeded, CreatedAt: now, UpdatedAt: now},
		{TaskID: "work_20000000000000000000000000000002", Status: model.Succeeded, CreatedAt: now, UpdatedAt: now},
	}}
	result, err := repo.Import(ctx, tenant, input)
	if err != nil || result.Tasks != 2 {
		t.Fatalf("result=%+v err=%v", result, err)
	}
	var tasks, nullRuntimeIDs, nullRiverJobIDs int
	if err := pool.QueryRow(ctx, `SELECT count(*),count(*) FILTER (WHERE runtime_task_id IS NULL),count(*) FILTER (WHERE river_job_id IS NULL) FROM ziggy_work_tasks WHERE user_id=$1 AND workspace_id=$2`, tenant.UserID, tenant.WorkspaceID).Scan(&tasks, &nullRuntimeIDs, &nullRiverJobIDs); err != nil {
		t.Fatal(err)
	}
	if tasks != 2 || nullRuntimeIDs != 2 || nullRiverJobIDs != 2 {
		t.Fatalf("tasks=%d null_runtime_ids=%d null_river_job_ids=%d", tasks, nullRuntimeIDs, nullRiverJobIDs)
	}
}
