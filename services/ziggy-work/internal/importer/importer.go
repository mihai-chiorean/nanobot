package importer

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/artifactstore"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/model"
	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/repository"
)

const (
	MaxJSONBytes          int64 = 64 << 20
	MaxTasks                    = 10000
	MaxEvents                   = 1000000
	MaxArtifacts                = 100000
	MaxArtifactBytes      int64 = 100 << 20
	MaxTotalArtifactBytes int64 = 1 << 30
)

type Options struct {
	ArtifactRoot        string
	ArtifactDestination string
	ArtifactMap         map[string]string
}
type rawDocument struct {
	Tasks     []json.RawMessage `json:"tasks"`
	Events    []json.RawMessage `json:"events"`
	Steps     []json.RawMessage `json:"steps"`
	Artifacts []json.RawMessage `json:"artifacts"`
}
type parsed struct {
	data            repository.LegacyImport
	artifactSources map[string]string
}

func Parse(r io.Reader) (repository.LegacyImport, error) {
	p, err := parse(r)
	if err != nil {
		return repository.LegacyImport{}, err
	}
	return p.data, nil
}
func Import(ctx context.Context, repo repository.Repository, tenant model.Tenant, r io.Reader, opts Options) (repository.ImportResult, error) {
	p, err := parse(r)
	if err != nil {
		return repository.ImportResult{}, err
	}
	maxSequence := make(map[string]int64, len(p.data.Tasks))
	for _, event := range p.data.Events {
		if event.Seq > maxSequence[event.TaskID] {
			maxSequence[event.TaskID] = event.Seq
		}
	}
	for i := range p.data.Tasks {
		if p.data.Tasks[i].LastSeq > maxSequence[p.data.Tasks[i].TaskID] {
			maxSequence[p.data.Tasks[i].TaskID] = p.data.Tasks[i].LastSeq
		}
		if p.data.Tasks[i].Status == model.Queued || p.data.Tasks[i].Status == model.Running || p.data.Tasks[i].Status == model.Waiting {
			message := "interrupted during migration; submit a new task to retry"
			now := time.Now().UTC()
			p.data.Tasks[i].Status = model.Interrupted
			p.data.Tasks[i].Error = &message
			p.data.Tasks[i].CompletedAt = &now
			sequence := maxSequence[p.data.Tasks[i].TaskID] + 1
			maxSequence[p.data.Tasks[i].TaskID] = sequence
			p.data.Events = append(p.data.Events, model.Event{
				TaskID:    p.data.Tasks[i].TaskID,
				Seq:       sequence,
				Type:      "status.changed",
				Actor:     "migration",
				CreatedAt: now,
				Payload:   map[string]any{"status": model.Interrupted, "error": message},
			})
		}
	}
	cleanup, err := prepareArtifacts(&p, tenant, opts)
	if err != nil {
		return repository.ImportResult{}, err
	}
	res, err := repo.Import(ctx, tenant, p.data)
	if err != nil {
		cleanup()
		return repository.ImportResult{}, err
	}
	return res, nil
}

func parse(r io.Reader) (parsed, error) {
	b, err := io.ReadAll(io.LimitReader(r, MaxJSONBytes+1))
	if err != nil {
		return parsed{}, err
	}
	if int64(len(b)) > MaxJSONBytes {
		return parsed{}, errors.New("legacy export exceeds 64 MiB")
	}
	var d rawDocument
	dec := json.NewDecoder(bytes.NewReader(b))
	if err := dec.Decode(&d); err != nil {
		return parsed{}, fmt.Errorf("decode legacy export: %w", err)
	}
	var trailing any
	if err := dec.Decode(&trailing); err != io.EOF {
		return parsed{}, errors.New("legacy export contains trailing JSON")
	}
	if len(d.Tasks) > MaxTasks || len(d.Events) > MaxEvents || len(d.Artifacts) > MaxArtifacts {
		return parsed{}, errors.New("legacy export exceeds record limits")
	}
	p := parsed{data: repository.LegacyImport{}, artifactSources: map[string]string{}}
	taskIDs := map[string]bool{}
	for _, raw := range d.Tasks {
		t, err := parseTask(raw)
		if err != nil {
			return parsed{}, err
		}
		if !model.WorkIDPattern.MatchString(t.TaskID) || !t.Status.Valid() {
			return parsed{}, errors.New("legacy task has invalid work_id or status")
		}
		if taskIDs[t.TaskID] {
			return parsed{}, errors.New("duplicate legacy task id")
		}
		taskIDs[t.TaskID] = true
		p.data.Tasks = append(p.data.Tasks, t)
		p.data.Steps = append(p.data.Steps, t.Steps...)
		p.data.Artifacts = append(p.data.Artifacts, t.Artifacts...)
	}
	for _, raw := range d.Events {
		e, err := parseEvent(raw)
		if err != nil {
			return parsed{}, err
		}
		if !taskIDs[e.TaskID] {
			return parsed{}, fmt.Errorf("event references unknown task %q", e.TaskID)
		}
		if e.Seq <= 0 {
			return parsed{}, errors.New("legacy event sequence must be positive")
		}
		p.data.Events = append(p.data.Events, e)
	}
	for _, raw := range d.Steps {
		s, err := parseStep(raw)
		if err != nil {
			return parsed{}, err
		}
		if !taskIDs[s.TaskID] {
			return parsed{}, errors.New("step references unknown task")
		}
		p.data.Steps = append(p.data.Steps, s)
	}
	for _, raw := range d.Artifacts {
		a, path, err := parseArtifact(raw)
		if err != nil {
			return parsed{}, err
		}
		if !taskIDs[a.TaskID] {
			return parsed{}, errors.New("artifact references unknown task")
		}
		p.data.Artifacts = append(p.data.Artifacts, a)
		p.artifactSources[a.ArtifactID] = path
	}
	seenEvents := map[string]struct{}{}
	for _, e := range p.data.Events {
		key := e.TaskID + "\x00" + fmt.Sprint(e.Seq)
		if _, ok := seenEvents[key]; ok {
			return parsed{}, errors.New("duplicate legacy event sequence")
		}
		seenEvents[key] = struct{}{}
	}
	seenSteps := map[string]struct{}{}
	for _, s := range p.data.Steps {
		if s.StepID == "" {
			return parsed{}, errors.New("legacy step id is required")
		}
		if _, ok := seenSteps[s.StepID]; ok {
			return parsed{}, errors.New("duplicate legacy step id")
		}
		seenSteps[s.StepID] = struct{}{}
	}
	seenArtifacts := map[string]struct{}{}
	for _, a := range p.data.Artifacts {
		if a.ArtifactID == "" {
			return parsed{}, errors.New("legacy artifact id is required")
		}
		if _, ok := seenArtifacts[a.ArtifactID]; ok {
			return parsed{}, errors.New("duplicate legacy artifact id")
		}
		seenArtifacts[a.ArtifactID] = struct{}{}
	}
	sort.Slice(p.data.Events, func(i, j int) bool {
		if p.data.Events[i].TaskID == p.data.Events[j].TaskID {
			return p.data.Events[i].Seq < p.data.Events[j].Seq
		}
		return p.data.Events[i].TaskID < p.data.Events[j].TaskID
	})
	runtimeTaskIDs := make(map[string]string, len(p.data.Tasks))
	for i := range p.data.Tasks {
		if p.data.Tasks[i].RuntimeTaskID == "" {
			p.data.Tasks[i].RuntimeTaskID = p.data.Tasks[i].TaskID
		}
		runtimeTaskIDs[p.data.Tasks[i].TaskID] = p.data.Tasks[i].RuntimeTaskID
	}
	for i := range p.data.Events {
		p.data.Events[i].RuntimeTaskID = runtimeTaskIDs[p.data.Events[i].TaskID]
		p.data.Events[i].RuntimeSeq = p.data.Events[i].Seq
	}
	return p, nil
}

func parseTask(raw json.RawMessage) (model.Task, error) {
	var t model.Task
	if err := json.Unmarshal(raw, &t); err != nil {
		return t, err
	}
	var m map[string]json.RawMessage
	_ = json.Unmarshal(raw, &m)
	if t.TaskID == "" {
		t.TaskID = stringValue(m, "id")
	}
	if t.Status == "" {
		t.Status = model.Queued
		if v := stringValue(m, "state"); v != "" {
			t.Status = model.Status(v)
		}
	}
	if t.SessionKey == "" {
		t.SessionKey = stringValue(m, "chat_key")
	}
	if t.ChatID == "" {
		t.ChatID = stringValue(m, "chat_id")
	}
	t.RuntimeTaskID = stringValue(m, "runtime_task_id")
	return t, nil
}
func parseEvent(raw json.RawMessage) (model.Event, error) {
	var e model.Event
	if err := json.Unmarshal(raw, &e); err != nil {
		return e, err
	}
	var m map[string]json.RawMessage
	_ = json.Unmarshal(raw, &m)
	if e.TaskID == "" {
		e.TaskID = stringValue(m, "task_id")
	}
	if e.Seq == 0 {
		e.Seq = int64Value(m, "sequence")
	}
	if e.Payload == nil {
		var v map[string]any
		_ = json.Unmarshal(m["data"], &v)
		e.Payload = v
	}
	if e.Payload == nil {
		e.Payload = map[string]any{}
	}
	return e, nil
}
func parseStep(raw json.RawMessage) (model.Step, error) {
	var s model.Step
	if err := json.Unmarshal(raw, &s); err != nil {
		return s, err
	}
	return s, nil
}
func parseArtifact(raw json.RawMessage) (model.Artifact, string, error) {
	var a model.Artifact
	if err := json.Unmarshal(raw, &a); err != nil {
		return a, "", err
	}
	var m map[string]json.RawMessage
	_ = json.Unmarshal(raw, &m)
	if a.ArtifactID == "" {
		a.ArtifactID = stringValue(m, "id")
	}
	if a.TaskID == "" {
		a.TaskID = stringValue(m, "task_id")
	}
	path := stringValue(m, "path_rel")
	if path == "" {
		path = stringValue(m, "path")
	}
	a.PathRel = path
	if a.Available == false && path == "" {
		a.UnavailableReason = "legacy export did not include artifact bytes"
	}
	return a, path, nil
}
func stringValue(m map[string]json.RawMessage, k string) string {
	var v string
	_ = json.Unmarshal(m[k], &v)
	return strings.TrimSpace(v)
}
func int64Value(m map[string]json.RawMessage, k string) int64 {
	var v int64
	_ = json.Unmarshal(m[k], &v)
	return v
}

func prepareArtifacts(p *parsed, tenant model.Tenant, opts Options) (func(), error) {
	cleanups := []func(){}
	cleanup := func() {
		for i := len(cleanups) - 1; i >= 0; i-- {
			cleanups[i]()
		}
	}
	if len(p.data.Artifacts) == 0 {
		return cleanup, nil
	}
	if len(opts.ArtifactMap) > 0 && opts.ArtifactRoot == "" {
		return cleanup, errors.New("--artifact-root is required with --artifact-map")
	}
	if opts.ArtifactRoot == "" && len(opts.ArtifactMap) == 0 {
		for i := range p.data.Artifacts {
			p.data.Artifacts[i].Available = false
			p.data.Artifacts[i].UnavailableReason = "artifact bytes not imported; rerun with --artifact-root or --artifact-map"
		}
		return cleanup, nil
	}
	if opts.ArtifactDestination == "" {
		return cleanup, errors.New("artifact destination is required when copying legacy artifact bytes")
	}
	root, err := filepath.Abs(opts.ArtifactRoot)
	if err != nil {
		return cleanup, err
	}
	rootInfo, err := os.Stat(root)
	if err != nil || !rootInfo.IsDir() {
		return cleanup, errors.New("artifact root must be an existing directory")
	}
	var total int64
	for i := range p.data.Artifacts {
		a := &p.data.Artifacts[i]
		source := opts.ArtifactMap[a.ArtifactID]
		if source == "" {
			source = p.artifactSources[a.ArtifactID]
		}
		if source == "" {
			if len(opts.ArtifactMap) > 0 {
				return cleanup, fmt.Errorf("artifact source mapping missing for %s", a.ArtifactID)
			}
			a.Available = false
			a.UnavailableReason = "artifact source path missing"
			continue
		}
		if filepath.IsAbs(source) {
			return cleanup, errors.New("artifact source path must be relative")
		}
		clean := filepath.Clean(source)
		if clean == "." || clean == ".." || strings.HasPrefix(clean, ".."+string(filepath.Separator)) {
			return cleanup, errors.New("artifact source path traversal rejected")
		}
		full := filepath.Join(root, clean)
		real, err := filepath.EvalSymlinks(full)
		if err != nil {
			return cleanup, fmt.Errorf("artifact source unavailable: %w", err)
		}
		rel, err := filepath.Rel(root, real)
		if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
			return cleanup, errors.New("artifact symlink escapes artifact root")
		}
		info, err := os.Stat(real)
		if err != nil || !info.Mode().IsRegular() {
			return cleanup, errors.New("artifact source is not a regular file")
		}
		if info.Size() > MaxArtifactBytes || total+info.Size() > MaxTotalArtifactBytes {
			return cleanup, errors.New("artifact bytes exceed import limits")
		}
		total += info.Size()
		in, err := os.Open(real)
		if err != nil {
			return cleanup, err
		}
		relative, size, digest, remove, putErr := artifactstore.Put(
			opts.ArtifactDestination,
			tenant,
			a.TaskID,
			a.ArtifactID,
			in,
			MaxArtifactBytes,
			a.SizeBytes,
			a.SHA256,
		)
		_ = in.Close()
		if putErr != nil {
			return cleanup, fmt.Errorf("store artifact %s: %w", a.ArtifactID, putErr)
		}
		cleanups = append(cleanups, remove)
		a.SizeBytes = size
		a.SHA256 = digest
		a.PathRel, a.Available, a.UnavailableReason = relative, true, ""
	}
	return cleanup, nil
}
