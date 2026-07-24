package podman

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-runtime-manager/runtime"
)

type Command struct {
	Path  string
	Args  []string
	Stdin []byte
}

type Result struct {
	Stdout   string
	ExitCode int
}

type Runner interface {
	Run(context.Context, Command) (Result, error)
}

type ExecRunner struct{}

func (ExecRunner) Run(ctx context.Context, command Command) (Result, error) {
	process := exec.CommandContext(ctx, command.Path, command.Args...)
	process.Stdin = bytes.NewReader(command.Stdin)
	output, err := process.Output()
	if err == nil {
		return Result{Stdout: string(output)}, nil
	}
	var exitError interface{ ExitCode() int }
	if errors.As(err, &exitError) {
		return Result{Stdout: string(output), ExitCode: exitError.ExitCode()}, nil
	}
	return Result{}, err
}

type Driver struct {
	Policy     Policy
	QuadletDir string
	Runner     Runner
	Now        func() time.Time
}

const (
	managerOwnerLabel = "io.ziggy.owner"
	managerOwnerValue = "ziggy-runtime-manager"
	workspaceLabel    = "io.ziggy.workspace"
	generationLabel   = "io.ziggy.generation"
)

func (d Driver) EnsureRunning(ctx context.Context, request runtime.Request) (runtime.Status, error) {
	if err := d.validateRequest(request, true); err != nil {
		return runtime.Status{}, err
	}
	actual, found, err := d.currentGeneration(ctx, request)
	if err != nil {
		return runtime.Status{}, err
	}
	if found && actual > request.Generation {
		return runtime.Status{}, runtime.ErrStaleGeneration
	}
	if found && actual < request.Generation {
		return runtime.Status{}, runtime.ErrGenerationConflict
	}
	if !found {
		if err := d.stage(ctx, request); err != nil {
			return runtime.Status{}, err
		}
	}
	if err := d.systemctl(ctx, "start", UnitName(request)); err != nil {
		return runtime.Status{}, err
	}
	return d.Health(ctx, request)
}

func (d Driver) Health(ctx context.Context, request runtime.Request) (runtime.Status, error) {
	if err := d.validateRequest(request, false); err != nil {
		return runtime.Status{}, err
	}
	actual, found, err := d.currentGeneration(ctx, request)
	if err != nil {
		return runtime.Status{}, err
	}
	if !found {
		return runtime.Status{WorkspaceID: request.WorkspaceID, Generation: request.Generation, State: runtime.StateAbsent, ObservedAt: d.now()}, runtime.ErrNotFound
	}
	if actual > request.Generation {
		return runtime.Status{}, runtime.ErrStaleGeneration
	}
	if actual != request.Generation {
		return runtime.Status{}, runtime.ErrGenerationConflict
	}
	container, _, _ := tenantNames(request)
	result, err := d.Runner.Run(ctx, Command{Path: "podman", Args: []string{"inspect", "--format", "{{json .State}}", container}})
	if err != nil || result.ExitCode != 0 {
		return runtime.Status{}, errors.New("runtime inspect failed")
	}
	var state struct {
		Status string `json:"Status"`
		Health *struct {
			Status string `json:"Status"`
		} `json:"Health"`
	}
	if err := json.Unmarshal([]byte(result.Stdout), &state); err != nil {
		return runtime.Status{}, errors.New("runtime state was not valid JSON")
	}
	status := runtime.Status{WorkspaceID: request.WorkspaceID, Generation: request.Generation, Endpoint: d.endpoint(request), ObservedAt: d.now()}
	if state.Status != "running" {
		status.State = runtime.StateFailed
		return status, errors.New("runtime is not running")
	}
	if state.Health == nil || state.Health.Status != "healthy" {
		status.State = runtime.StateStarting
		return status, errors.New("runtime health check has not passed")
	}
	status.State = runtime.StateRunning
	return status, nil
}

func (d Driver) Drain(ctx context.Context, request runtime.Request) (runtime.Status, error) {
	if err := d.assertCurrent(ctx, request); err != nil {
		return runtime.Status{}, err
	}
	// Routing withdrawal is performed by ziggy-control before this call. Stop
	// accepting new work by stopping the process group with a bounded timeout.
	if err := d.systemctl(ctx, "stop", UnitName(request)); err != nil {
		return runtime.Status{}, err
	}
	return runtime.Status{WorkspaceID: request.WorkspaceID, Generation: request.Generation, State: runtime.StateDraining, ObservedAt: d.now()}, nil
}

func (d Driver) Stop(ctx context.Context, request runtime.Request) (runtime.Status, error) {
	if err := d.assertCurrent(ctx, request); err != nil {
		return runtime.Status{}, err
	}
	if err := d.systemctl(ctx, "stop", UnitName(request)); err != nil {
		return runtime.Status{}, err
	}
	return runtime.Status{WorkspaceID: request.WorkspaceID, Generation: request.Generation, State: runtime.StateStopped, ObservedAt: d.now()}, nil
}

func (d Driver) Delete(ctx context.Context, request runtime.Request) error {
	if err := d.assertCurrent(ctx, request); err != nil && !errors.Is(err, runtime.ErrNotFound) {
		return err
	}
	_ = d.systemctl(ctx, "stop", UnitName(request))
	container, rootVolume, configVolume := tenantNames(request)
	for _, args := range [][]string{{"rm", "--force", "--time", "30", container}, {"volume", "rm", rootVolume}, {"volume", "rm", configVolume}, {"network", "rm", egressNetwork(request)}} {
		result, err := d.Runner.Run(ctx, Command{Path: "podman", Args: args})
		if err != nil || (result.ExitCode != 0 && result.ExitCode != 1) {
			return errors.New("runtime deletion command failed")
		}
	}
	if err := os.Remove(filepath.Join(d.QuadletDir, strings.TrimSuffix(UnitName(request), ".service")+".container")); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return err
	}
	return d.systemctl(ctx, "daemon-reload")
}

func (d Driver) stage(ctx context.Context, request runtime.Request) error {
	config, err := RenderNanobotConfig(d.Policy, request)
	if err != nil {
		return err
	}
	if err := ValidateRenderedConfig(config); err != nil {
		return err
	}
	quadlet, err := RenderQuadlet(d.Policy, request)
	if err != nil {
		return err
	}
	_, rootVolume, configVolume := tenantNames(request)
	if err := d.ensureNetwork(ctx, request); err != nil {
		return err
	}
	for _, volume := range []string{rootVolume, configVolume} {
		if err := d.ensureVolume(ctx, request, volume); err != nil {
			return err
		}
	}
	stageArgs := []string{"run", "--rm", "--network", "none", "--read-only", "--cap-drop", "all", "--security-opt", "no-new-privileges", "--userns", "auto:size=65536", "--volume", configVolume + ":/run/ziggy/config:rw,U,Z", "--entrypoint", "/usr/local/bin/ziggy-runtime-stage-config", d.Policy.ImageDigest}
	result, err := d.Runner.Run(ctx, Command{Path: "podman", Args: stageArgs, Stdin: config})
	if err != nil || result.ExitCode != 0 {
		return errors.New("runtime config staging failed")
	}
	if err := os.MkdirAll(d.QuadletDir, 0o700); err != nil {
		return err
	}
	path := filepath.Join(d.QuadletDir, strings.TrimSuffix(UnitName(request), ".service")+".container")
	if err := os.WriteFile(path, quadlet, 0o600); err != nil {
		return err
	}
	return d.systemctl(ctx, "daemon-reload")
}

func (d Driver) ensureNetwork(ctx context.Context, request runtime.Request) error {
	name := egressNetwork(request)
	args := append([]string{"network", "create", "--internal"}, labelArgs(request)...)
	args = append(args, name)
	return d.createAndVerify(ctx, Command{Path: "podman", Args: args}, Command{Path: "podman", Args: []string{"network", "inspect", "--format", "{{json .}}", name}}, func(raw []byte) error {
		var inspection struct {
			Internal bool              `json:"internal"`
			Labels   map[string]string `json:"labels"`
		}
		if err := json.Unmarshal(raw, &inspection); err != nil || !inspection.Internal || !labelsMatch(inspection.Labels, request) {
			return errors.New("runtime network is not manager-owned and internal")
		}
		return nil
	})
}

func (d Driver) ensureVolume(ctx context.Context, request runtime.Request, name string) error {
	args := append([]string{"volume", "create"}, labelArgs(request)...)
	args = append(args, name)
	return d.createAndVerify(ctx, Command{Path: "podman", Args: args}, Command{Path: "podman", Args: []string{"volume", "inspect", "--format", "{{json .}}", name}}, func(raw []byte) error {
		var inspection struct {
			Labels map[string]string `json:"Labels"`
		}
		if err := json.Unmarshal(raw, &inspection); err != nil || !labelsMatch(inspection.Labels, request) {
			return errors.New("runtime volume is not manager-owned")
		}
		return nil
	})
}

func (d Driver) createAndVerify(ctx context.Context, create, inspect Command, verify func([]byte) error) error {
	created, err := d.Runner.Run(ctx, create)
	if err != nil || (created.ExitCode != 0 && created.ExitCode != 125) {
		return errors.New("runtime prerequisite creation failed")
	}
	observed, err := d.Runner.Run(ctx, inspect)
	if err != nil || observed.ExitCode != 0 || verify([]byte(observed.Stdout)) != nil {
		return errors.New("runtime prerequisite ownership verification failed")
	}
	return nil
}

func labelArgs(request runtime.Request) []string {
	labels := lifecycleLabels(request)
	return []string{
		"--label", managerOwnerLabel + "=" + labels[managerOwnerLabel],
		"--label", workspaceLabel + "=" + labels[workspaceLabel],
		"--label", generationLabel + "=" + labels[generationLabel],
	}
}

func lifecycleLabels(request runtime.Request) map[string]string {
	return map[string]string{
		managerOwnerLabel: managerOwnerValue,
		workspaceLabel:    request.WorkspaceID,
		generationLabel:   strconv.FormatUint(request.Generation, 10),
	}
}

func labelsMatch(actual map[string]string, request runtime.Request) bool {
	for key, value := range lifecycleLabels(request) {
		if actual[key] != value {
			return false
		}
	}
	return true
}

func (d Driver) currentGeneration(ctx context.Context, request runtime.Request) (uint64, bool, error) {
	container, _, _ := tenantNames(request)
	result, err := d.Runner.Run(ctx, Command{Path: "podman", Args: []string{"inspect", "--format", "{{index .Config.Labels \"io.ziggy.generation\"}}", container}})
	if err != nil {
		return 0, false, err
	}
	if result.ExitCode != 0 {
		if result.ExitCode == 125 || result.ExitCode == 1 {
			return 0, false, nil
		}
		return 0, false, errors.New("runtime generation inspect failed")
	}
	generation, err := strconv.ParseUint(strings.TrimSpace(result.Stdout), 10, 64)
	if err != nil || generation == 0 {
		return 0, false, errors.New("runtime generation label is invalid")
	}
	return generation, true, nil
}

func (d Driver) assertCurrent(ctx context.Context, request runtime.Request) error {
	if err := d.validateRequest(request, false); err != nil {
		return err
	}
	actual, found, err := d.currentGeneration(ctx, request)
	if err != nil {
		return err
	}
	if !found {
		return runtime.ErrNotFound
	}
	if actual > request.Generation {
		return runtime.ErrStaleGeneration
	}
	if actual != request.Generation {
		return runtime.ErrGenerationConflict
	}
	return nil
}

func (d Driver) validateRequest(request runtime.Request, requireFreshCapabilities bool) error {
	if !workspaceIDPattern.MatchString(request.WorkspaceID) {
		return runtime.ErrInvalidWorkspace
	}
	if request.Generation == 0 {
		return runtime.ErrInvalidGeneration
	}
	if err := d.Policy.validate(d.now(), requireFreshCapabilities); err != nil {
		return err
	}
	if _, ok := d.Policy.Tenants[request.WorkspaceID]; !ok {
		return runtime.ErrUnknownWorkspace
	}
	if d.Runner == nil || d.QuadletDir == "" {
		return errors.New("runtime driver is not configured")
	}
	return nil
}

func (d Driver) now() time.Time {
	if d.Now != nil {
		return d.Now().UTC()
	}
	return time.Now().UTC()
}

func (d Driver) endpoint(request runtime.Request) string {
	return "http://127.0.0.1:" + strconv.Itoa(d.Policy.Tenants[request.WorkspaceID].HostPort)
}

func (d Driver) systemctl(ctx context.Context, args ...string) error {
	result, err := d.Runner.Run(ctx, Command{Path: "systemctl", Args: append([]string{"--user"}, args...)})
	if err != nil || result.ExitCode != 0 {
		return fmt.Errorf("runtime systemd operation %q failed", strings.Join(args, " "))
	}
	return nil
}
