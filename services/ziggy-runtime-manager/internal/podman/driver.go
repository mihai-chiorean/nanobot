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

	"github.com/mihai-chiorean/nanobot/services/ziggy-runtime-manager/internal/atomicfile"
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

// ValidateRequest authorizes policy membership without requiring capabilities
// to still be fresh, so stop/delete remain available after credential expiry.
func (d Driver) ValidateRequest(request runtime.Request) error {
	return d.validateRequest(request, false)
}

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
	if err := d.stopUnitForDeletion(ctx, UnitName(request)); err != nil {
		return err
	}
	if err := d.removeOwnedContainer(ctx, request); err != nil {
		return err
	}
	_, _, configVolume := tenantNames(request)
	if err := d.removeOwnedVolume(ctx, configVolume, lifecycleLabels(request)); err != nil {
		return err
	}
	if err := d.removeOwnedNetwork(ctx, request); err != nil {
		return err
	}
	if err := os.Remove(filepath.Join(d.QuadletDir, strings.TrimSuffix(UnitName(request), ".service")+".container")); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return err
	}
	return d.systemctl(ctx, "daemon-reload")
}

// DeleteTenantData is intentionally separate from generation cleanup. A caller
// must remove the active generation first, then make an explicit destructive
// request before the persistent workspace volume can be removed.
func (d Driver) DeleteTenantData(ctx context.Context, request runtime.Request) error {
	if err := d.validateRequest(request, false); err != nil {
		return err
	}
	actual, found, err := d.currentGeneration(ctx, request)
	if err != nil {
		return err
	}
	if found {
		if actual > request.Generation {
			return runtime.ErrStaleGeneration
		}
		if actual != request.Generation {
			return runtime.ErrGenerationConflict
		}
		return errors.New("runtime generation must be deleted before tenant data deletion")
	}
	inactive, err := d.unitInactiveOrAbsent(ctx, UnitName(request))
	if err != nil {
		return errors.New("runtime unit state could not be verified before tenant data deletion")
	}
	if !inactive {
		return errors.New("runtime unit remained active before tenant data deletion")
	}
	_, rootVolume, _ := tenantNames(request)
	return d.removeOwnedWorkspaceVolume(ctx, rootVolume, request)
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
	if err := d.ensureWorkspaceVolume(ctx, rootVolume, request); err != nil {
		return err
	}
	if err := d.ensureVolume(ctx, configVolume, lifecycleLabels(request)); err != nil {
		return err
	}
	stageArgs := []string{"run", "--rm", "--network", "none", "--read-only", "--cap-drop", "all", "--security-opt", "no-new-privileges", "--userns", "auto:size=65536", "--volume", configVolume + ":/run/ziggy/config:rw,U,Z", "--entrypoint", "/usr/local/bin/ziggy-runtime-stage-config", d.Policy.ImageDigest}
	result, err := d.Runner.Run(ctx, Command{Path: "podman", Args: stageArgs, Stdin: config})
	if err != nil || result.ExitCode != 0 {
		return errors.New("runtime config staging failed")
	}
	if err := os.MkdirAll(d.QuadletDir, 0o700); err != nil {
		return err
	}
	if err := os.Chmod(d.QuadletDir, 0o700); err != nil {
		return err
	}
	path := filepath.Join(d.QuadletDir, strings.TrimSuffix(UnitName(request), ".service")+".container")
	if err := atomicfile.Write(path, quadlet, 0o600); err != nil {
		return err
	}
	return d.systemctl(ctx, "daemon-reload")
}

func (d Driver) ensureNetwork(ctx context.Context, request runtime.Request) error {
	name := egressNetwork(request)
	args := append([]string{"network", "create", "--internal"}, labelArgs(lifecycleLabels(request))...)
	args = append(args, name)
	return d.createAndVerify(ctx, Command{Path: "podman", Args: args}, Command{Path: "podman", Args: []string{"network", "inspect", "--format", "{{json .}}", name}}, func(raw []byte) error {
		var inspection struct {
			Internal bool              `json:"internal"`
			Labels   map[string]string `json:"labels"`
		}
		if err := json.Unmarshal(raw, &inspection); err != nil || !inspection.Internal || !labelsMatch(inspection.Labels, lifecycleLabels(request)) {
			return errors.New("runtime network is not manager-owned and internal")
		}
		return nil
	})
}

func (d Driver) ensureVolume(ctx context.Context, name string, expectedLabels map[string]string) error {
	args := append([]string{"volume", "create"}, labelArgs(expectedLabels)...)
	args = append(args, name)
	return d.createAndVerify(ctx, Command{Path: "podman", Args: args}, Command{Path: "podman", Args: []string{"volume", "inspect", "--format", "{{json .}}", name}}, func(raw []byte) error {
		var inspection struct {
			Labels map[string]string `json:"Labels"`
		}
		if err := json.Unmarshal(raw, &inspection); err != nil || !labelsMatch(inspection.Labels, expectedLabels) {
			return errors.New("runtime volume is not manager-owned")
		}
		return nil
	})
}

func (d Driver) ensureWorkspaceVolume(ctx context.Context, name string, request runtime.Request) error {
	expectedLabels := workspaceVolumeLabels(request)
	args := append([]string{"volume", "create"}, labelArgs(expectedLabels)...)
	args = append(args, name)
	return d.createAndVerify(ctx, Command{Path: "podman", Args: args}, Command{Path: "podman", Args: []string{"volume", "inspect", "--format", "{{json .}}", name}}, func(raw []byte) error {
		var inspection struct {
			Labels map[string]string `json:"Labels"`
		}
		if err := json.Unmarshal(raw, &inspection); err != nil || !workspaceVolumeLabelsMatch(inspection.Labels, expectedLabels) {
			return errors.New("workspace volume is not manager-owned and generation-independent")
		}
		return nil
	})
}

func (d Driver) createAndVerify(ctx context.Context, create, inspect Command, verify func([]byte) error) error {
	if create.Path != "podman" || len(create.Args) < 3 {
		return errors.New("runtime prerequisite creation command is invalid")
	}
	resource := create.Args[0]
	name := create.Args[len(create.Args)-1]
	exists, err := d.resourceExists(ctx, resource, name)
	if err != nil {
		return err
	}
	if !exists {
		created, err := d.Runner.Run(ctx, create)
		if err != nil || created.ExitCode != 0 {
			return errors.New("runtime prerequisite creation failed")
		}
	}
	observed, err := d.Runner.Run(ctx, inspect)
	if err != nil || observed.ExitCode != 0 || verify([]byte(observed.Stdout)) != nil {
		return errors.New("runtime prerequisite ownership verification failed")
	}
	return nil
}

func labelArgs(labels map[string]string) []string {
	args := []string{"--label", managerOwnerLabel + "=" + labels[managerOwnerLabel], "--label", workspaceLabel + "=" + labels[workspaceLabel]}
	if generation, ok := labels[generationLabel]; ok {
		args = append(args, "--label", generationLabel+"="+generation)
	}
	return args
}

func lifecycleLabels(request runtime.Request) map[string]string {
	return map[string]string{
		managerOwnerLabel: managerOwnerValue,
		workspaceLabel:    request.WorkspaceID,
		generationLabel:   strconv.FormatUint(request.Generation, 10),
	}
}

func workspaceVolumeLabels(request runtime.Request) map[string]string {
	return map[string]string{
		managerOwnerLabel: managerOwnerValue,
		workspaceLabel:    request.WorkspaceID,
	}
}

func labelsMatch(actual, expected map[string]string) bool {
	for key, value := range expected {
		if actual[key] != value {
			return false
		}
	}
	return true
}

func workspaceVolumeLabelsMatch(actual, expected map[string]string) bool {
	_, hasGeneration := actual[generationLabel]
	return !hasGeneration && labelsMatch(actual, expected)
}

func (d Driver) removeOwnedVolume(ctx context.Context, name string, expectedLabels map[string]string) error {
	exists, err := d.resourceExists(ctx, "volume", name)
	if err != nil {
		return err
	}
	if !exists {
		return nil
	}
	inspection, err := d.Runner.Run(ctx, Command{Path: "podman", Args: []string{"volume", "inspect", "--format", "{{json .}}", name}})
	if err != nil {
		return err
	}
	var volume struct {
		Labels map[string]string `json:"Labels"`
	}
	if inspection.ExitCode != 0 || json.Unmarshal([]byte(inspection.Stdout), &volume) != nil || !labelsMatch(volume.Labels, expectedLabels) {
		return errors.New("runtime volume is not manager-owned")
	}
	removed, err := d.Runner.Run(ctx, Command{Path: "podman", Args: []string{"volume", "rm", name}})
	if err != nil || removed.ExitCode != 0 {
		return errors.New("runtime volume deletion failed")
	}
	return nil
}

func (d Driver) removeOwnedWorkspaceVolume(ctx context.Context, name string, request runtime.Request) error {
	exists, err := d.resourceExists(ctx, "volume", name)
	if err != nil {
		return err
	}
	if !exists {
		return nil
	}
	inspection, err := d.Runner.Run(ctx, Command{Path: "podman", Args: []string{"volume", "inspect", "--format", "{{json .}}", name}})
	if err != nil {
		return err
	}
	var volume struct {
		Labels map[string]string `json:"Labels"`
	}
	if inspection.ExitCode != 0 || json.Unmarshal([]byte(inspection.Stdout), &volume) != nil || !workspaceVolumeLabelsMatch(volume.Labels, workspaceVolumeLabels(request)) {
		return errors.New("workspace volume is not manager-owned and generation-independent")
	}
	removed, err := d.Runner.Run(ctx, Command{Path: "podman", Args: []string{"volume", "rm", name}})
	if err != nil || removed.ExitCode != 0 {
		return errors.New("workspace volume deletion failed")
	}
	return nil
}

func (d Driver) removeOwnedNetwork(ctx context.Context, request runtime.Request) error {
	name := egressNetwork(request)
	exists, err := d.resourceExists(ctx, "network", name)
	if err != nil {
		return err
	}
	if !exists {
		return nil
	}
	inspection, err := d.Runner.Run(ctx, Command{Path: "podman", Args: []string{"network", "inspect", "--format", "{{json .}}", name}})
	if err != nil {
		return err
	}
	var network struct {
		Internal bool              `json:"internal"`
		Labels   map[string]string `json:"labels"`
	}
	if inspection.ExitCode != 0 || json.Unmarshal([]byte(inspection.Stdout), &network) != nil || !network.Internal || !labelsMatch(network.Labels, lifecycleLabels(request)) {
		return errors.New("runtime network is not manager-owned and internal")
	}
	removed, err := d.Runner.Run(ctx, Command{Path: "podman", Args: []string{"network", "rm", name}})
	if err != nil || removed.ExitCode != 0 {
		return errors.New("runtime network deletion failed")
	}
	return nil
}

func (d Driver) currentGeneration(ctx context.Context, request runtime.Request) (uint64, bool, error) {
	container, _, _ := tenantNames(request)
	exists, err := d.resourceExists(ctx, "container", container)
	if err != nil {
		return 0, false, err
	}
	if !exists {
		return 0, false, nil
	}
	result, err := d.Runner.Run(ctx, Command{Path: "podman", Args: []string{"inspect", "--format", "{{json .Config.Labels}}", container}})
	if err != nil || result.ExitCode != 0 {
		return 0, false, errors.New("runtime container ownership inspect failed")
	}
	var labels map[string]string
	if err := json.Unmarshal([]byte(result.Stdout), &labels); err != nil ||
		labels[managerOwnerLabel] != managerOwnerValue ||
		labels[workspaceLabel] != request.WorkspaceID {
		return 0, false, errors.New("runtime container is not manager-owned")
	}
	generation, err := strconv.ParseUint(strings.TrimSpace(labels[generationLabel]), 10, 64)
	if err != nil || generation == 0 {
		return 0, false, errors.New("runtime generation label is invalid")
	}
	return generation, true, nil
}

func (d Driver) resourceExists(ctx context.Context, resource, name string) (bool, error) {
	result, err := d.Runner.Run(ctx, Command{Path: "podman", Args: []string{resource, "exists", name}})
	if err != nil {
		return false, err
	}
	switch result.ExitCode {
	case 0:
		return true, nil
	case 1:
		return false, nil
	default:
		return false, fmt.Errorf("podman %s existence check failed", resource)
	}
}

func (d Driver) removeOwnedContainer(ctx context.Context, request runtime.Request) error {
	actual, found, err := d.currentGeneration(ctx, request)
	if err != nil {
		return err
	}
	if !found {
		return nil
	}
	if actual > request.Generation {
		return runtime.ErrStaleGeneration
	}
	if actual != request.Generation {
		return runtime.ErrGenerationConflict
	}
	container, _, _ := tenantNames(request)
	result, err := d.Runner.Run(ctx, Command{Path: "podman", Args: []string{"rm", "--force", "--time", "30", container}})
	if err != nil || result.ExitCode != 0 {
		return errors.New("runtime deletion command failed")
	}
	return nil
}

func (d Driver) stopUnitForDeletion(ctx context.Context, unit string) error {
	result, err := d.Runner.Run(ctx, Command{Path: "systemctl", Args: []string{"--user", "stop", unit}})
	if err == nil && result.ExitCode == 0 {
		return nil
	}
	inactive, stateErr := d.unitInactiveOrAbsent(ctx, unit)
	if stateErr != nil {
		return errors.New("runtime systemd stop failed and unit state could not be verified")
	}
	if !inactive {
		return errors.New("runtime systemd stop failed while unit remained active")
	}
	return nil
}

func (d Driver) unitInactiveOrAbsent(ctx context.Context, unit string) (bool, error) {
	result, err := d.Runner.Run(ctx, Command{
		Path: "systemctl",
		Args: []string{"--user", "show", unit, "--property=LoadState", "--property=ActiveState", "--no-pager"},
	})
	if err != nil || result.ExitCode != 0 {
		return false, errors.New("runtime systemd state query failed")
	}
	properties := map[string]string{}
	for _, line := range strings.Split(result.Stdout, "\n") {
		key, value, found := strings.Cut(strings.TrimSpace(line), "=")
		if found {
			properties[key] = value
		}
	}
	loadState, hasLoadState := properties["LoadState"]
	activeState, hasActiveState := properties["ActiveState"]
	if !hasLoadState || !hasActiveState || loadState == "" || activeState == "" {
		return false, errors.New("runtime systemd state response was incomplete")
	}
	return activeState == "inactive", nil
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
