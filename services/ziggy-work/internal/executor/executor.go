package executor

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/gorilla/websocket"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/model"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/repository"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/telemetry"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/tenant"
)

type Executor interface {
	Execute(context.Context, model.Task, string) error
	Cancel(context.Context, model.Task) error
	Message(context.Context, model.Task, string, string) error
	Health(context.Context) error
}

type Nanobot struct {
	repo         repository.Repository
	tenants      *tenant.Registry
	artifactRoot string
	http         *http.Client
	timeout      time.Duration
	idleTimeout  time.Duration
	logger       *slog.Logger
	telemetry    telemetry.Telemetry
	dialer       websocket.Dialer
}

func NewNanobot(repo repository.Repository, tenants *tenant.Registry, artifactRoot string, timeout time.Duration, logger *slog.Logger, tel telemetry.Telemetry) *Nanobot {
	if tel == nil {
		tel = telemetry.Noop()
	}
	return &Nanobot{
		repo: repo, tenants: tenants, artifactRoot: artifactRoot,
		http: &http.Client{
			Timeout: timeout,
			CheckRedirect: func(*http.Request, []*http.Request) error {
				return http.ErrUseLastResponse
			},
		},
		timeout: timeout, idleTimeout: 10 * time.Minute, logger: logger,
		telemetry: tel, dialer: websocket.Dialer{HandshakeTimeout: timeout},
	}
}
func (n *Nanobot) Health(ctx context.Context) error {
	if n.tenants == nil {
		return errors.New("tenant registry unavailable")
	}
	return nil
}

type tokenResponse struct {
	Token     string `json:"token"`
	WSPath    string `json:"ws_path"`
	ExpiresIn int    `json:"expires_in"`
}
type frame struct {
	Event     string         `json:"event"`
	TaskID    string         `json:"task_id"`
	Seq       int64          `json:"seq"`
	Type      string         `json:"type"`
	Payload   map[string]any `json:"payload"`
	Actor     string         `json:"actor"`
	StepID    *string        `json:"step_id"`
	CreatedAt time.Time      `json:"created_at"`
	Task      *struct {
		TaskID string `json:"task_id"`
	} `json:"task"`
}

func (n *Nanobot) Execute(ctx context.Context, task model.Task, content string) error {
	ctx, span := n.telemetry.Start(ctx, "ziggy.work.execute")
	defer span.End()
	start := time.Now()
	err := n.run(ctx, task, content, "execute", "")
	n.telemetry.Execution(ctx, "execute", time.Since(start), err)
	return err
}

func (n *Nanobot) run(ctx context.Context, task model.Task, content, operation, commandID string) error {
	a, err := n.tenants.Resolve(task.Tenant)
	if err != nil {
		return err
	}
	token, wsPath, err := n.token(ctx, a)
	if err != nil {
		return err
	}
	u, err := url.Parse(a.UpstreamURL)
	if err != nil {
		return err
	}
	if u.Scheme == "http" {
		u.Scheme = "ws"
	} else {
		u.Scheme = "wss"
	}
	if wsPath == "" {
		wsPath = "/"
	}
	u.Path = wsPath
	query := u.Query()
	query.Set("client_id", "ziggy-work")
	query.Set("token", token)
	u.RawQuery = query.Encode()
	conn, _, err := n.dialer.DialContext(ctx, u.String(), nil)
	if err != nil {
		return fmt.Errorf("nanobot websocket: %w", err)
	}
	defer conn.Close()
	if operation == "cancel" {
		return n.send(ctx, conn, map[string]any{"type": "work.cancel", "task_id": task.RuntimeTaskID})
	}
	if operation == "message" {
		message := map[string]any{"type": "work.message", "task_id": task.RuntimeTaskID, "content": content}
		if commandID != "" {
			message["idempotency_key"] = commandID
		}
		if err := n.send(ctx, conn, message); err != nil {
			return err
		}
		if err := n.send(ctx, conn, map[string]any{"type": "work.subscribe", "task_id": task.RuntimeTaskID, "after_seq": 0}); err != nil {
			return err
		}
		return n.readEvents(ctx, conn, task)
	}
	if task.RuntimeTaskID == "" {
		current, err := n.repo.GetTask(ctx, task.Tenant, task.TaskID)
		if err != nil {
			return err
		}
		if current.Status.Terminal() {
			return nil
		}
		if err := n.send(ctx, conn, map[string]any{"type": "work.create", "chat_id": task.ChatID, "content": content, "mode": "background", "title": task.Title, "idempotency_key": task.TaskID}); err != nil {
			return err
		}
	} else if err := n.send(ctx, conn, map[string]any{"type": "work.subscribe", "task_id": task.RuntimeTaskID, "after_seq": 0}); err != nil {
		return err
	}
	return n.readEvents(ctx, conn, task)
}

func (n *Nanobot) Message(ctx context.Context, task model.Task, content, commandID string) error {
	ctx, span := n.telemetry.Start(ctx, "ziggy.work.message")
	defer span.End()
	return n.run(ctx, task, content, "message", commandID)
}
func (n *Nanobot) Cancel(ctx context.Context, task model.Task) error {
	ctx, span := n.telemetry.Start(ctx, "ziggy.work.cancel")
	defer span.End()
	if task.RuntimeTaskID == "" {
		return nil
	}
	return n.run(ctx, task, "", "cancel", "")
}

func (n *Nanobot) token(ctx context.Context, a tenant.Allocation) (string, string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(a.UpstreamURL, "/")+"/auth/token", nil)
	if err != nil {
		return "", "", err
	}
	req.Header.Set("Authorization", "Bearer "+a.UpstreamBootstrapSecret)
	resp, err := n.http.Do(req)
	if err != nil {
		return "", "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", "", fmt.Errorf("nanobot token endpoint returned %d", resp.StatusCode)
	}
	var out tokenResponse
	dec := json.NewDecoder(io.LimitReader(resp.Body, 64<<10))
	if err := dec.Decode(&out); err != nil {
		return "", "", err
	}
	if out.Token == "" {
		return "", "", errors.New("nanobot token response missing token")
	}
	return out.Token, out.WSPath, nil
}

func (n *Nanobot) send(ctx context.Context, c *websocket.Conn, v any) error {
	_ = c.SetWriteDeadline(time.Now().Add(n.timeout))
	b, err := json.Marshal(v)
	if err != nil {
		return err
	}
	return c.WriteMessage(websocket.TextMessage, b)
}
func (n *Nanobot) readEvents(ctx context.Context, c *websocket.Conn, task model.Task) error {
	c.SetReadLimit(4 << 20)
	stopClosing := context.AfterFunc(ctx, func() { _ = c.Close() })
	defer stopClosing()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}
		_ = c.SetReadDeadline(time.Now().Add(n.idleTimeout))
		_, r, err := c.ReadMessage()
		if err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			return err
		}
		var f frame
		if json.Unmarshal(r, &f) != nil {
			continue
		}
		if f.Event == "work.created" && f.Task != nil && f.Task.TaskID != "" {
			if err := n.repo.SetRuntimeTask(ctx, task.Tenant, task.TaskID, f.Task.TaskID); err != nil {
				return err
			}
			task.RuntimeTaskID = f.Task.TaskID
			current, err := n.repo.GetTask(ctx, task.Tenant, task.TaskID)
			if err != nil {
				return err
			}
			if current.Status == model.Cancelled {
				return n.send(ctx, c, map[string]any{"type": "work.cancel", "task_id": task.RuntimeTaskID})
			}
			continue
		}
		if f.Event != "work.event" {
			if f.Event == "error" {
				return errors.New("nanobot returned websocket error")
			}
			continue
		}
		if f.Payload == nil {
			f.Payload = map[string]any{}
		}
		runtimeEvent := model.Event{TaskID: task.TaskID, Seq: f.Seq, Type: f.Type, Payload: f.Payload, Actor: emptyDefault(f.Actor, "nanobot"), StepID: f.StepID, CreatedAt: f.CreatedAt}
		if _, _, err := n.repo.AppendRuntimeEvent(ctx, task.Tenant, task.TaskID, task.RuntimeTaskID, runtimeEvent); err != nil {
			return err
		}
		if f.Type == "status.changed" {
			if raw, ok := f.Payload["status"].(string); ok {
				status := model.Status(raw)
				if status.Valid() {
					n.telemetry.Transition(ctx, string(task.Status), string(status))
					task.Status = status
					if status.Terminal() {
						return nil
					}
					if status == model.Waiting {
						return nil
					}
					continue
				}
			}
		}
	}
}
func emptyDefault(v, d string) string {
	if strings.TrimSpace(v) == "" {
		return d
	}
	return v
}
