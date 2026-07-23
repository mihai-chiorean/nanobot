package repository

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/model"
)

func TestRuntimeEventIsIdempotentAndConvergesStatus(t *testing.T) {
	ctx := context.Background()
	repo := NewMemory()
	tenant := model.Tenant{UserID: "user", WorkspaceID: "workspace"}
	taskID := "work_00000000000000000000000000000001"
	runtimeID := "work_00000000000000000000000000000002"
	now := time.Now().UTC()
	task := model.Task{TaskID: taskID, RuntimeTaskID: runtimeID, SessionKey: "work:" + taskID, ChatID: "chat", Title: "title", Status: model.Running, Mode: "background", CreatedAt: now, UpdatedAt: now}
	if err := repo.UpsertRuntimeTask(ctx, tenant, task, nil, nil); err != nil {
		t.Fatal(err)
	}
	event := model.Event{Seq: 7, Type: "status.changed", Actor: "nanobot", CreatedAt: now, Payload: map[string]any{"status": "succeeded", "result_summary": "done"}}
	first, inserted, err := repo.AppendRuntimeEvent(ctx, tenant, taskID, runtimeID, event)
	if err != nil || !inserted {
		t.Fatalf("first append: inserted=%v err=%v", inserted, err)
	}
	second, inserted, err := repo.AppendRuntimeEvent(ctx, tenant, taskID, runtimeID, event)
	if err != nil || inserted || second.Seq != first.Seq {
		t.Fatalf("duplicate append: first=%d second=%d inserted=%v err=%v", first.Seq, second.Seq, inserted, err)
	}
	events, err := repo.ListEvents(ctx, tenant, taskID, 0, 100)
	if err != nil || len(events) != 1 {
		t.Fatalf("events=%d err=%v", len(events), err)
	}
	stored, err := repo.GetTask(ctx, tenant, taskID)
	if err != nil || stored.Status != model.Succeeded || stored.ResultSummary == nil || *stored.ResultSummary != "done" {
		t.Fatalf("stored=%+v err=%v", stored, err)
	}
	stale := model.Event{Seq: 8, Type: "status.changed", Actor: "nanobot", CreatedAt: now, Payload: map[string]any{"status": "running"}}
	if _, inserted, err := repo.AppendRuntimeEvent(ctx, tenant, taskID, runtimeID, stale); err != nil || !inserted {
		t.Fatalf("stale append: inserted=%v err=%v", inserted, err)
	}
	stored, err = repo.GetTask(ctx, tenant, taskID)
	if err != nil || stored.Status != model.Succeeded {
		t.Fatalf("terminal task regressed: stored=%+v err=%v", stored, err)
	}
}

func TestRuntimeReconcilePreservesCancellationAndUpsertsSteps(t *testing.T) {
	ctx := context.Background()
	repo := NewMemory()
	tenant := model.Tenant{UserID: "user", WorkspaceID: "workspace"}
	taskID := "work_00000000000000000000000000000001"
	runtimeID := "work_00000000000000000000000000000002"
	now := time.Now().UTC()
	task := model.Task{TaskID: taskID, RuntimeTaskID: runtimeID, SessionKey: "work:" + taskID, ChatID: "chat", Status: model.Running, Mode: "background", CreatedAt: now, UpdatedAt: now}
	if err := repo.UpsertRuntimeTask(ctx, tenant, task, nil, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := repo.CancelTask(ctx, tenant, taskID, model.RequestIdentity{Key: "cancel-request-0001", PayloadHash: strings.Repeat("a", 64)}); err != nil {
		t.Fatal(err)
	}
	task.Status = model.Succeeded
	task.Steps = []model.Step{{StepID: "step_1", TaskID: taskID, SeqStart: 1, Title: "step", Status: "succeeded"}}
	if err := repo.UpsertRuntimeTask(ctx, tenant, task, nil, nil); err != nil {
		t.Fatal(err)
	}
	stored, err := repo.GetTask(ctx, tenant, taskID)
	if err != nil || stored.Status != model.Cancelled || len(stored.Steps) != 1 {
		t.Fatalf("stored=%+v err=%v", stored, err)
	}
}
