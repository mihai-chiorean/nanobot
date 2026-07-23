package executor

import (
	"context"
	cryptorand "crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/artifactstore"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/model"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/tenant"
)

const (
	runtimeTaskPageSize  = 200
	runtimeEventPageSize = 500
	maxEventsPerTaskRun  = 2000
	maxArtifactBytes     = 100 << 20
)

type runtimeDocument struct {
	Tasks      []json.RawMessage `json:"tasks"`
	HasMore    bool              `json:"has_more"`
	NextOffset int               `json:"next_offset"`
	NextTaskID string            `json:"next_task_id"`
}

type runtimeTask struct {
	model.Task
	Events    []model.Event    `json:"events,omitempty"`
	Artifacts []model.Artifact `json:"artifacts,omitempty"`
}

func (n *Nanobot) Reconcile(ctx context.Context) error {
	if n.tenants == nil {
		return errors.New("tenant registry unavailable")
	}
	allocations := n.tenants.Allocations()
	sem := make(chan struct{}, 2)
	errCh := make(chan error, len(allocations))
	for _, a := range allocations {
		a := a
		select {
		case sem <- struct{}{}:
		case <-ctx.Done():
			return ctx.Err()
		}
		go func() {
			defer func() { <-sem }()
			errCh <- n.reconcileTenant(ctx, a)
		}()
	}
	var errs []error
	for range allocations {
		if err := <-errCh; err != nil {
			errs = append(errs, err)
		}
	}
	return errors.Join(errs...)
}

func (n *Nanobot) reconcileTenant(ctx context.Context, a tenant.Allocation) error {
	token, _, err := n.token(ctx, a)
	if err != nil {
		return err
	}
	tenantID := model.Tenant{UserID: a.UserID, WorkspaceID: a.WorkspaceID}
	for cursor := ""; ; {
		var page runtimeDocument
		query := url.Values{"limit": {fmt.Sprint(runtimeTaskPageSize)}, "order": {"task_id"}}
		if cursor != "" {
			query.Set("after_task_id", cursor)
		}
		path := strings.TrimRight(a.UpstreamURL, "/") + "/api/work?" + query.Encode()
		if err := n.getJSON(ctx, path, token, 4<<20, &page); err != nil {
			return fmt.Errorf("nanobot work list: %w", err)
		}
		for _, raw := range page.Tasks {
			if err := n.reconcileRuntimeTask(ctx, a, token, tenantID, raw); err != nil {
				return err
			}
		}
		if !page.HasMore || len(page.Tasks) == 0 {
			return nil
		}
		next := page.NextTaskID
		if next == "" {
			var last runtimeTask
			if err := json.Unmarshal(page.Tasks[len(page.Tasks)-1], &last); err == nil {
				next = last.TaskID
			}
		}
		if next == "" || next <= cursor {
			return errors.New("nanobot task cursor did not advance")
		}
		cursor = next
	}
}

func (n *Nanobot) reconcileRuntimeTask(ctx context.Context, a tenant.Allocation, token string, tenantID model.Tenant, raw json.RawMessage) error {
	var summary runtimeTask
	if err := json.Unmarshal(raw, &summary); err != nil {
		return nil
	}
	runtimeID := runtimeTaskID(raw, summary)
	if runtimeID == "" || !model.WorkIDPattern.MatchString(runtimeID) {
		return nil
	}

	var detail struct {
		Task runtimeTask `json:"task"`
	}
	path := strings.TrimRight(a.UpstreamURL, "/") + "/api/work/" + url.PathEscape(runtimeID)
	if err := n.getJSON(ctx, path, token, 8<<20, &detail); err != nil {
		return fmt.Errorf("nanobot work detail: %w", err)
	}
	rt := detail.Task
	rt.RuntimeTaskID = runtimeID
	rt.Tenant = tenantID
	if !rt.Status.Valid() {
		rt.Status = model.Queued
	}
	if rt.TaskID == "" {
		rt.TaskID = summary.TaskID
	}
	if !model.WorkIDPattern.MatchString(rt.TaskID) {
		rt.TaskID = newReconcileID()
	}
	if rt.CreatedAt.IsZero() {
		rt.CreatedAt = time.Now().UTC()
	}
	if rt.UpdatedAt.IsZero() {
		rt.UpdatedAt = rt.CreatedAt
	}

	artifacts, cleanup := n.runtimeArtifacts(ctx, a, token, tenantID, rt.TaskID, rt.Artifacts)
	if err := n.repo.UpsertRuntimeTask(ctx, tenantID, rt.Task, nil, artifacts); err != nil {
		cleanup()
		return err
	}
	cursor, err := n.repo.RuntimeCursor(ctx, tenantID, runtimeID)
	if err != nil {
		return err
	}
	events, err := n.runtimeEvents(ctx, a, token, runtimeID, cursor, maxEventsPerTaskRun)
	if err != nil {
		return err
	}
	for i := range events {
		events[i].TaskID = rt.TaskID
		events[i].Tenant = tenantID
	}
	if len(events) > 0 {
		if err := n.repo.UpsertRuntimeTask(ctx, tenantID, rt.Task, events, nil); err != nil {
			return err
		}
	}
	return nil
}

func runtimeTaskID(raw json.RawMessage, rt runtimeTask) string {
	if rt.RuntimeTaskID != "" {
		return rt.RuntimeTaskID
	}
	var fields map[string]json.RawMessage
	_ = json.Unmarshal(raw, &fields)
	var value string
	_ = json.Unmarshal(fields["runtime_task_id"], &value)
	if value != "" {
		return value
	}
	return rt.TaskID
}

func (n *Nanobot) runtimeEvents(ctx context.Context, a tenant.Allocation, token, runtimeID string, after int64, maximum int) ([]model.Event, error) {
	if maximum <= 0 {
		return nil, nil
	}
	all := make([]model.Event, 0, min(runtimeEventPageSize, maximum))
	for len(all) < maximum {
		pageSize := min(runtimeEventPageSize, maximum-len(all))
		path := fmt.Sprintf("%s/api/work/%s/events?after_seq=%d&limit=%d", strings.TrimRight(a.UpstreamURL, "/"), url.PathEscape(runtimeID), after, pageSize)
		var result struct {
			Events       []model.Event `json:"events"`
			HasMore      bool          `json:"has_more"`
			NextAfterSeq int64         `json:"next_after_seq"`
		}
		if err := n.getJSON(ctx, path, token, 4<<20, &result); err != nil {
			return nil, fmt.Errorf("nanobot events: %w", err)
		}
		all = append(all, result.Events...)
		if !result.HasMore || len(result.Events) == 0 {
			return all, nil
		}
		next := result.NextAfterSeq
		if next <= after {
			next = result.Events[len(result.Events)-1].Seq
		}
		if next <= after {
			return nil, errors.New("nanobot events cursor did not advance")
		}
		after = next
	}
	return all, nil
}

func (n *Nanobot) runtimeArtifacts(ctx context.Context, a tenant.Allocation, token string, tenantID model.Tenant, taskID string, artifacts []model.Artifact) ([]model.Artifact, func()) {
	cleanups := make([]func(), 0, len(artifacts))
	cleanup := func() {
		for i := len(cleanups) - 1; i >= 0; i-- {
			cleanups[i]()
		}
	}
	if n.artifactRoot == "" {
		return nil, cleanup
	}
	available := make([]model.Artifact, 0, len(artifacts))
	for _, artifact := range artifacts {
		artifact.TaskID = taskID
		if existing, err := n.repo.GetArtifact(ctx, tenantID, artifact.ArtifactID); err == nil && existing.Available && artifactMatches(existing, artifact) {
			artifact.PathRel = existing.PathRel
			artifact.Available = true
			artifact.SizeBytes = existing.SizeBytes
			artifact.SHA256 = existing.SHA256
			available = append(available, artifact)
			continue
		}
		response, err := n.getArtifact(ctx, a.UpstreamURL, artifact.URL, token)
		if err != nil {
			n.logger.Warn("runtime artifact download failed", "failure_class", "download")
			continue
		}
		pathRel, size, digest, remove, putErr := artifactstore.Put(n.artifactRoot, tenantID, taskID, artifact.ArtifactID, response.Body, maxArtifactBytes, artifact.SizeBytes, artifact.SHA256)
		_ = response.Body.Close()
		if putErr != nil {
			n.logger.Warn("runtime artifact storage failed", "failure_class", "validation_or_storage")
			continue
		}
		artifact.PathRel = pathRel
		artifact.SizeBytes = size
		artifact.SHA256 = digest
		artifact.Available = true
		artifact.UnavailableReason = ""
		artifact.URL = ""
		available = append(available, artifact)
		cleanups = append(cleanups, remove)
	}
	return available, cleanup
}

func artifactMatches(existing, runtime model.Artifact) bool {
	return (runtime.SizeBytes <= 0 || runtime.SizeBytes == existing.SizeBytes) &&
		(runtime.SHA256 == "" || strings.EqualFold(runtime.SHA256, existing.SHA256))
}

func (n *Nanobot) getArtifact(ctx context.Context, upstream, artifactURL, token string) (*http.Response, error) {
	base, err := url.Parse(strings.TrimRight(upstream, "/"))
	if err != nil {
		return nil, err
	}
	reference, err := url.Parse(artifactURL)
	if err != nil {
		return nil, err
	}
	resolved := base.ResolveReference(reference)
	if resolved.Scheme != base.Scheme || resolved.Host != base.Host || !strings.HasPrefix(resolved.Path, "/api/work/artifacts/") {
		return nil, errors.New("nanobot artifact URL rejected")
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, resolved.String(), nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	resp, err := n.http.Do(req)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusOK {
		_ = resp.Body.Close()
		return nil, fmt.Errorf("nanobot artifact returned %d", resp.StatusCode)
	}
	return resp, nil
}

func (n *Nanobot) getJSON(ctx context.Context, path, token string, limit int64, destination any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, path, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	resp, err := n.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("returned %d", resp.StatusCode)
	}
	return json.NewDecoder(io.LimitReader(resp.Body, limit)).Decode(destination)
}

func newReconcileID() string {
	b := make([]byte, 16)
	if _, err := cryptorand.Read(b); err != nil {
		panic("crypto/rand unavailable")
	}
	const h = "0123456789abcdef"
	out := make([]byte, 32)
	for i, v := range b {
		out[i*2], out[i*2+1] = h[v>>4], h[v&15]
	}
	return "work_" + string(out)
}
