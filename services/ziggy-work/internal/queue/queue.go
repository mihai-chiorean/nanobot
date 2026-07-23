package queue

import (
	"context"
	"strconv"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/job"
	"github.com/riverqueue/river"
	"github.com/riverqueue/river/riverdriver/riverpgxv5"
)

const DefaultQueue = "ziggy_work_low"

type Handler func(context.Context, job.Job) error

type Queue interface {
	job.Enqueuer
	Start(context.Context) error
	Stop(context.Context) error
	Cancel(context.Context, string) error
}

type MemoryQueue struct {
	ch      chan job.Job
	handler Handler
	workers int
	wg      sync.WaitGroup
	stop    context.CancelFunc
	mu      sync.Mutex
	next    int64
}

func NewMemory(capacity, workers int, handler Handler) *MemoryQueue {
	if capacity < 1 {
		capacity = 1
	}
	if workers < 1 {
		workers = 1
	}
	return &MemoryQueue{ch: make(chan job.Job, capacity), workers: workers, handler: handler}
}
func (q *MemoryQueue) EnqueueTx(ctx context.Context, _ pgx.Tx, j job.Job) (string, error) {
	return q.Enqueue(ctx, j)
}
func (q *MemoryQueue) Enqueue(ctx context.Context, j job.Job) (string, error) {
	select {
	case q.ch <- j:
		q.mu.Lock()
		q.next++
		id := strconv.FormatInt(q.next, 10)
		q.mu.Unlock()
		return id, nil
	default:
		return "", ErrFull
	}
}
func (q *MemoryQueue) Start(ctx context.Context) error {
	runCtx, cancel := context.WithCancel(ctx)
	q.stop = cancel
	for i := 0; i < q.workers; i++ {
		q.wg.Add(1)
		go func() {
			defer q.wg.Done()
			for {
				select {
				case <-runCtx.Done():
					return
				case j := <-q.ch:
					if q.handler != nil {
						_ = q.handler(runCtx, j)
					}
				}
			}
		}()
	}
	return nil
}
func (q *MemoryQueue) Stop(ctx context.Context) error {
	if q.stop != nil {
		q.stop()
	}
	done := make(chan struct{})
	go func() { q.wg.Wait(); close(done) }()
	select {
	case <-done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}
func (*MemoryQueue) Cancel(context.Context, string) error { return nil }

var ErrFull = context.DeadlineExceeded

type RiverQueue struct {
	client    *river.Client[pgx.Tx]
	queueName string
	handler   Handler
}
type workArgs struct {
	TaskID      string `json:"task_id"`
	UserID      string `json:"user_id"`
	WorkspaceID string `json:"workspace_id"`
	Content     string `json:"content"`
	ChatID      string `json:"chat_id"`
	FollowUp    bool   `json:"follow_up,omitempty"`
	CommandID   string `json:"command_id"`
}

func (workArgs) Kind() string { return "ziggy_work_execute" }

type workWorker struct {
	river.WorkerDefaults[workArgs]
	handler Handler
}

func (w *workWorker) Work(ctx context.Context, j *river.Job[workArgs]) error {
	return w.handler(ctx, job.Job{TaskID: j.Args.TaskID, UserID: j.Args.UserID, WorkspaceID: j.Args.WorkspaceID, Content: j.Args.Content, ChatID: j.Args.ChatID, FollowUp: j.Args.FollowUp, CommandID: j.Args.CommandID, Attempt: j.Attempt, MaxAttempts: j.MaxAttempts})
}

func NewRiver(pool *pgxpool.Pool, queueName string, workers int, handler Handler) (*RiverQueue, error) {
	if queueName == "" {
		queueName = DefaultQueue
	}
	if workers < 1 {
		workers = 1
	}
	bundle := river.NewWorkers()
	river.AddWorker(bundle, &workWorker{handler: handler})
	client, err := river.NewClient(riverpgxv5.New(pool), &river.Config{Queues: map[string]river.QueueConfig{queueName: {MaxWorkers: workers}}, Workers: bundle, JobTimeout: 30 * time.Minute})
	if err != nil {
		return nil, err
	}
	return &RiverQueue{client: client, queueName: queueName, handler: handler}, nil
}
func (q *RiverQueue) EnqueueTx(ctx context.Context, tx pgx.Tx, j job.Job) (string, error) {
	r, err := q.client.InsertTx(ctx, tx, workArgs{TaskID: j.TaskID, UserID: j.UserID, WorkspaceID: j.WorkspaceID, Content: j.Content, ChatID: j.ChatID, FollowUp: j.FollowUp, CommandID: j.CommandID}, &river.InsertOpts{Queue: q.queueName, Priority: 4, MaxAttempts: 20})
	if err != nil {
		return "", err
	}
	return strconv.FormatInt(r.Job.ID, 10), nil
}
func (q *RiverQueue) Enqueue(ctx context.Context, j job.Job) (string, error) {
	r, err := q.client.Insert(ctx, workArgs{TaskID: j.TaskID, UserID: j.UserID, WorkspaceID: j.WorkspaceID, Content: j.Content, ChatID: j.ChatID, FollowUp: j.FollowUp, CommandID: j.CommandID}, &river.InsertOpts{Queue: q.queueName, Priority: 4, MaxAttempts: 20})
	if err != nil {
		return "", err
	}
	return strconv.FormatInt(r.Job.ID, 10), nil
}
func (q *RiverQueue) Start(ctx context.Context) error { return q.client.Start(ctx) }
func (q *RiverQueue) Stop(ctx context.Context) error  { return q.client.Stop(ctx) }
func (q *RiverQueue) Cancel(ctx context.Context, id string) error {
	n, err := strconv.ParseInt(id, 10, 64)
	if err != nil {
		return err
	}
	_, err = q.client.JobCancel(ctx, n)
	return err
}
