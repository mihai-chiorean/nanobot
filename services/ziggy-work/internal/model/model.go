package model

import (
	"errors"
	"regexp"
	"strings"
	"time"
)

var WorkIDPattern = regexp.MustCompile(`^work_[a-f0-9]{32}$`)

var (
	ErrNotFound            = errors.New("work task not found")
	ErrConflict            = errors.New("work task conflict")
	ErrIdempotencyConflict = errors.New("idempotency key was reused for a different request")
	ErrTerminal            = errors.New("work task is terminal")
	ErrInvalidInput        = errors.New("invalid work input")
	ErrQueueFull           = errors.New("work queue is full")
)

type Tenant struct {
	UserID      string
	WorkspaceID string
}

func (t Tenant) Valid() bool {
	return strings.TrimSpace(t.UserID) != "" && strings.TrimSpace(t.WorkspaceID) != ""
}

type Status string

const (
	Scheduled   Status = "scheduled"
	Queued      Status = "queued"
	Running     Status = "running"
	Waiting     Status = "waiting"
	Succeeded   Status = "succeeded"
	Failed      Status = "failed"
	Cancelled   Status = "cancelled"
	Interrupted Status = "interrupted"
)

func (s Status) Terminal() bool {
	return s == Succeeded || s == Failed || s == Cancelled || s == Interrupted
}

func (s Status) Valid() bool {
	switch s {
	case Scheduled, Queued, Running, Waiting, Succeeded, Failed, Cancelled, Interrupted:
		return true
	default:
		return false
	}
}

type Task struct {
	TaskID        string     `json:"task_id"`
	SessionKey    string     `json:"session_key"`
	ChatID        string     `json:"chat_id"`
	Title         string     `json:"title"`
	PromptPreview string     `json:"prompt_preview"`
	Status        Status     `json:"status"`
	Mode          string     `json:"mode"`
	Model         string     `json:"model"`
	CreatedAt     time.Time  `json:"created_at"`
	UpdatedAt     time.Time  `json:"updated_at"`
	StartedAt     *time.Time `json:"started_at,omitempty"`
	CompletedAt   *time.Time `json:"completed_at,omitempty"`
	LastSeq       int64      `json:"last_seq"`
	ResultSummary *string    `json:"result_summary,omitempty"`
	Error         *string    `json:"error,omitempty"`
	ArtifactCount int        `json:"artifact_count"`
	Steps         []Step     `json:"steps,omitempty"`
	Artifacts     []Artifact `json:"artifacts,omitempty"`

	// Internal routing fields are never serialized by the public API.
	Tenant        Tenant `json:"-"`
	RuntimeTaskID string `json:"-"`
	RiverJobID    string `json:"-"`
	Version       int64  `json:"-"`
}

type Event struct {
	TaskID        string         `json:"task_id"`
	Seq           int64          `json:"seq"`
	Type          string         `json:"type"`
	Actor         string         `json:"actor,omitempty"`
	StepID        *string        `json:"step_id,omitempty"`
	CreatedAt     time.Time      `json:"created_at"`
	Payload       map[string]any `json:"payload"`
	Tenant        Tenant         `json:"-"`
	RuntimeTaskID string         `json:"-"`
	RuntimeSeq    int64          `json:"-"`
}

type Step struct {
	StepID      string     `json:"step_id"`
	TaskID      string     `json:"task_id"`
	SeqStart    int64      `json:"seq_start"`
	Title       string     `json:"title"`
	Status      string     `json:"status"`
	StartedAt   *time.Time `json:"started_at,omitempty"`
	CompletedAt *time.Time `json:"completed_at,omitempty"`
	Summary     *string    `json:"summary,omitempty"`
}

type Artifact struct {
	ArtifactID        string    `json:"artifact_id"`
	TaskID            string    `json:"task_id"`
	StepID            *string   `json:"step_id,omitempty"`
	Kind              string    `json:"kind"`
	Name              string    `json:"name"`
	MIME              string    `json:"mime"`
	SizeBytes         int64     `json:"size_bytes"`
	SHA256            string    `json:"sha256"`
	CreatedAt         time.Time `json:"created_at"`
	Summary           *string   `json:"summary,omitempty"`
	URL               string    `json:"url,omitempty"`
	Available         bool      `json:"available"`
	UnavailableReason string    `json:"unavailable_reason,omitempty"`
	PathRel           string    `json:"-"`
}

type CreateInput struct {
	ChatID  string
	Content string
	Title   string
	Mode    string
	Model   string
}

type RequestIdentity struct {
	Key         string
	PayloadHash string
}

func (i RequestIdentity) Valid() bool {
	return len(i.Key) >= 16 && len(i.Key) <= 128 && len(i.PayloadHash) == 64
}

type ListFilter struct {
	Status Status
	Limit  int
}

func (i CreateInput) Validate() error {
	if strings.TrimSpace(i.ChatID) == "" || len(i.ChatID) > 256 || strings.TrimSpace(i.Content) == "" || len(i.Content) > 1<<20 {
		return ErrInvalidInput
	}
	return nil
}

func Preview(value string, max int) string {
	value = strings.Join(strings.Fields(value), " ")
	if len(value) > max {
		return value[:max]
	}
	return value
}
