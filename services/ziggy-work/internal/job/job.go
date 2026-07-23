package job

import (
	"context"

	"github.com/jackc/pgx/v5"
)

// Job is the stable, tenant-scoped input persisted in River.
type Job struct {
	TaskID      string `json:"task_id"`
	UserID      string `json:"user_id"`
	WorkspaceID string `json:"workspace_id"`
	Content     string `json:"content"`
	ChatID      string `json:"chat_id"`
	FollowUp    bool   `json:"follow_up,omitempty"`
	CommandID   string `json:"command_id"`
	Attempt     int    `json:"-"`
	MaxAttempts int    `json:"-"`
}

// Enqueuer is deliberately smaller than River's client. Repositories can use
// it transactionally without exposing River to the HTTP or domain layers.
type Enqueuer interface {
	EnqueueTx(context.Context, pgx.Tx, Job) (string, error)
	Enqueue(context.Context, Job) (string, error)
}
