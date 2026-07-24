package podman

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-runtime-manager/runtime"
)

type fakeRunner struct {
	generation string
	state      string
	commands   []Command
}

func (f *fakeRunner) Run(_ context.Context, command Command) (Result, error) {
	f.commands = append(f.commands, command)
	if command.Path == "systemctl" && len(command.Args) >= 3 && command.Args[1] == "start" {
		f.generation = "7"
	}
	if command.Path == "podman" && len(command.Args) > 1 && command.Args[0] == "network" && command.Args[1] == "inspect" {
		return Result{Stdout: prerequisiteJSON(true, lifecycleLabels(runtime.Request{WorkspaceID: "tenant-alpha", Generation: 7}))}, nil
	}
	if command.Path == "podman" && len(command.Args) > 1 && command.Args[0] == "volume" && command.Args[1] == "inspect" {
		if strings.HasSuffix(command.Args[len(command.Args)-1], "-root") {
			return Result{Stdout: prerequisiteJSON(false, workspaceVolumeLabels(runtime.Request{WorkspaceID: "tenant-alpha", Generation: 7}))}, nil
		}
		return Result{Stdout: prerequisiteJSON(false, lifecycleLabels(runtime.Request{WorkspaceID: "tenant-alpha", Generation: 7}))}, nil
	}
	if command.Path == "podman" && len(command.Args) > 1 && command.Args[0] == "inspect" {
		if strings.Contains(command.Args[2], "io.ziggy.generation") {
			if f.generation == "" {
				return Result{ExitCode: 125}, nil
			}
			return Result{Stdout: f.generation}, nil
		}
		return Result{Stdout: f.state}, nil
	}
	return Result{}, nil
}

func prerequisiteJSON(network bool, labels map[string]string) string {
	value := map[string]any{"Labels": labels}
	if network {
		value["internal"] = true
		value["labels"] = labels
	}
	raw, _ := json.Marshal(value)
	return string(raw)
}

func TestEnsureRunningStagesOnlyNamedVolumesAndNetworklessConfig(t *testing.T) {
	runner := &fakeRunner{state: `{"Status":"running","Health":{"Status":"healthy"}}`}
	driver := Driver{Policy: fixturePolicy(), QuadletDir: t.TempDir(), Runner: runner, Now: func() time.Time { return fixtureNow }}
	request := runtime.Request{WorkspaceID: "tenant-alpha", Generation: 7}
	status, err := driver.EnsureRunning(context.Background(), request)
	if err != nil {
		t.Fatal(err)
	}
	if status.State != runtime.StateRunning || status.Endpoint != "http://127.0.0.1:21001" {
		t.Fatalf("unexpected status: %+v", status)
	}
	var staged bool
	for _, command := range runner.commands {
		args := strings.Join(command.Args, " ")
		if strings.Contains(args, "ziggy-runtime-stage-config") {
			staged = true
			if !strings.Contains(args, "--network none") || !strings.Contains(args, "--cap-drop all") || strings.Contains(args, fixturePolicy().Tenants["tenant-alpha"].ModelCapability.Value) {
				t.Fatalf("unsafe config stage command: %q", args)
			}
		}
		if strings.Contains(args, "--volume") && strings.Contains(args, "/home/") && strings.Contains(args, "operator") {
			t.Fatalf("host mount appeared in command: %q", args)
		}
	}
	if !staged {
		t.Fatal("config was not staged through the restricted helper")
	}
	quadletPath := filepath.Join(driver.QuadletDir, "ziggy-tenant-tenant-alpha.container")
	info, err := os.Stat(quadletPath)
	if err != nil {
		t.Fatalf("Quadlet was not staged: %v", err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("Quadlet mode=%o want=600", info.Mode().Perm())
	}
	entries, err := os.ReadDir(driver.QuadletDir)
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		if strings.Contains(entry.Name(), ".tmp-") {
			t.Fatalf("temporary Quadlet was published: %s", entry.Name())
		}
	}
}

func TestGenerationFenceRejectsStaleAndReplacement(t *testing.T) {
	request := runtime.Request{WorkspaceID: "tenant-alpha", Generation: 7}
	for _, test := range []struct {
		name       string
		generation string
		want       error
	}{
		{"newer live generation", "8", runtime.ErrStaleGeneration},
		{"older live generation", "6", runtime.ErrGenerationConflict},
	} {
		t.Run(test.name, func(t *testing.T) {
			driver := Driver{Policy: fixturePolicy(), QuadletDir: t.TempDir(), Runner: &fakeRunner{generation: test.generation}, Now: func() time.Time { return fixtureNow }}
			_, err := driver.EnsureRunning(context.Background(), request)
			if !errors.Is(err, test.want) {
				t.Fatalf("error=%v want=%v", err, test.want)
			}
		})
	}
}

func TestStopRemainsAvailableAfterCapabilityExpiry(t *testing.T) {
	policy := fixturePolicy()
	alpha := policy.Tenants["tenant-alpha"]
	alpha.ModelCapability.ExpiresAt = fixtureNow.Add(-time.Minute)
	alpha.MCPCapability.ExpiresAt = fixtureNow.Add(-time.Minute)
	policy.Tenants["tenant-alpha"] = alpha
	driver := Driver{Policy: policy, QuadletDir: t.TempDir(), Runner: &fakeRunner{generation: "7"}, Now: func() time.Time { return fixtureNow }}
	status, err := driver.Stop(context.Background(), runtime.Request{WorkspaceID: "tenant-alpha", Generation: 7})
	if err != nil || status.State != runtime.StateStopped {
		t.Fatalf("stop must work after capability expiry: status=%+v err=%v", status, err)
	}
}

func TestPrerequisitesRejectUnownedExistingResources(t *testing.T) {
	request := runtime.Request{WorkspaceID: "tenant-alpha", Generation: 7}
	for _, test := range []struct {
		name    string
		network bool
		payload string
	}{
		{"network must have ownership labels", true, prerequisiteJSON(true, map[string]string{managerOwnerLabel: managerOwnerValue})},
		{"volume must have ownership labels", false, prerequisiteJSON(false, map[string]string{managerOwnerLabel: managerOwnerValue})},
	} {
		t.Run(test.name, func(t *testing.T) {
			runner := &prerequisiteRunner{createExit: 125, inspect: test.payload}
			driver := Driver{Runner: runner}
			var err error
			if test.network {
				err = driver.ensureNetwork(context.Background(), request)
			} else {
				err = driver.ensureWorkspaceVolume(context.Background(), "ziggy-tenant-tenant-alpha-root", request)
			}
			if err == nil {
				t.Fatal("pre-existing unowned prerequisite was accepted")
			}
		})
	}

	runner := &prerequisiteRunner{createExit: 125, inspect: `{"internal":false,"labels":{"io.ziggy.owner":"ziggy-runtime-manager","io.ziggy.workspace":"tenant-alpha","io.ziggy.generation":"7"}}`}
	if err := (Driver{Runner: runner}).ensureNetwork(context.Background(), request); err == nil {
		t.Fatal("external pre-existing network was accepted")
	}
}

func TestPrerequisitesAcceptLabeledExistingResourcesAfterCreateConflict(t *testing.T) {
	request := runtime.Request{WorkspaceID: "tenant-alpha", Generation: 7}
	runner := &prerequisiteRunner{createExit: 125, inspect: prerequisiteJSON(true, lifecycleLabels(request))}
	driver := Driver{Runner: runner}
	if err := driver.ensureNetwork(context.Background(), request); err != nil {
		t.Fatal(err)
	}
	if len(runner.commands) != 2 || runner.commands[1].Args[0] != "network" || runner.commands[1].Args[1] != "inspect" {
		t.Fatalf("existing network was not inspected: %+v", runner.commands)
	}
	if !strings.Contains(strings.Join(runner.commands[0].Args, " "), "io.ziggy.owner=ziggy-runtime-manager") || !strings.Contains(strings.Join(runner.commands[0].Args, " "), "io.ziggy.generation=7") {
		t.Fatalf("network create did not carry manager labels: %+v", runner.commands[0])
	}

	volumeRunner := &prerequisiteRunner{createExit: 125, inspect: prerequisiteJSON(false, workspaceVolumeLabels(request))}
	if err := (Driver{Runner: volumeRunner}).ensureWorkspaceVolume(context.Background(), "ziggy-tenant-tenant-alpha-root", request); err != nil {
		t.Fatal(err)
	}
	if len(volumeRunner.commands) != 2 || volumeRunner.commands[1].Args[0] != "volume" || volumeRunner.commands[1].Args[1] != "inspect" {
		t.Fatalf("existing volume was not inspected: %+v", volumeRunner.commands)
	}
}

func TestMalformedWorkspaceIsNotAnInvalidGeneration(t *testing.T) {
	driver := Driver{Policy: fixturePolicy(), QuadletDir: t.TempDir(), Runner: &fakeRunner{}, Now: func() time.Time { return fixtureNow }}
	_, err := driver.EnsureRunning(context.Background(), runtime.Request{WorkspaceID: "../tenant-alpha", Generation: 7})
	if !errors.Is(err, runtime.ErrInvalidWorkspace) {
		t.Fatalf("error=%v want invalid workspace", err)
	}
}

func TestGenerationReplacementPreservesWorkspaceVolume(t *testing.T) {
	runner := &upgradeRunner{volumes: map[string]map[string]string{}, networks: map[string]map[string]string{}}
	driver := Driver{Policy: fixturePolicy(), QuadletDir: t.TempDir(), Runner: runner, Now: func() time.Time { return fixtureNow }}
	generationOne := runtime.Request{WorkspaceID: "tenant-alpha", Generation: 1}
	generationTwo := runtime.Request{WorkspaceID: "tenant-alpha", Generation: 2}
	if _, err := driver.EnsureRunning(context.Background(), generationOne); err != nil {
		t.Fatal(err)
	}
	if _, err := driver.Stop(context.Background(), generationOne); err != nil {
		t.Fatal(err)
	}
	if err := driver.Delete(context.Background(), generationOne); err != nil {
		t.Fatal(err)
	}
	if _, found := runner.volumes["ziggy-tenant-tenant-alpha-root"]; !found {
		t.Fatal("generation cleanup removed the persistent workspace volume")
	}
	if _, found := runner.volumes["ziggy-tenant-tenant-alpha-g1-config"]; found {
		t.Fatal("generation cleanup retained generation-one config")
	}
	if _, err := driver.EnsureRunning(context.Background(), generationTwo); err != nil {
		t.Fatal(err)
	}
	rootLabels := runner.volumes["ziggy-tenant-tenant-alpha-root"]
	if _, generationLabeled := rootLabels[generationLabel]; generationLabeled {
		t.Fatal("workspace volume must not be generation-labeled")
	}
	if _, found := runner.volumes["ziggy-tenant-tenant-alpha-g2-config"]; !found {
		t.Fatal("generation-two config volume was not created")
	}
	if _, err := driver.EnsureRunning(context.Background(), generationOne); !errors.Is(err, runtime.ErrStaleGeneration) {
		t.Fatalf("stale generation error=%v", err)
	}
}

type prerequisiteRunner struct {
	createExit int
	inspect    string
	commands   []Command
}

type upgradeRunner struct {
	containerGeneration string
	pendingGeneration   string
	volumes             map[string]map[string]string
	networks            map[string]map[string]string
}

func (r *upgradeRunner) Run(_ context.Context, command Command) (Result, error) {
	if command.Path == "systemctl" {
		if len(command.Args) > 1 && command.Args[1] == "start" {
			r.containerGeneration = r.pendingGeneration
		}
		return Result{}, nil
	}
	if command.Path != "podman" || len(command.Args) == 0 {
		return Result{}, nil
	}
	switch command.Args[0] {
	case "inspect":
		if strings.Contains(command.Args[2], "io.ziggy.generation") {
			if r.containerGeneration == "" {
				return Result{ExitCode: 125}, nil
			}
			return Result{Stdout: r.containerGeneration}, nil
		}
		return Result{Stdout: `{"Status":"running","Health":{"Status":"healthy"}}`}, nil
	case "rm":
		r.containerGeneration = ""
		return Result{}, nil
	case "network":
		return r.runNetwork(command.Args)
	case "volume":
		return r.runVolume(command.Args)
	case "run":
		return Result{}, nil
	default:
		return Result{}, nil
	}
}

func (r *upgradeRunner) runNetwork(args []string) (Result, error) {
	name := args[len(args)-1]
	switch args[1] {
	case "create":
		if _, exists := r.networks[name]; exists {
			return Result{ExitCode: 125}, nil
		}
		r.networks[name] = commandLabels(args)
		r.pendingGeneration = r.networks[name][generationLabel]
		return Result{}, nil
	case "inspect":
		labels, exists := r.networks[name]
		if !exists {
			return Result{ExitCode: 125}, nil
		}
		return Result{Stdout: prerequisiteJSON(true, labels)}, nil
	case "rm":
		delete(r.networks, name)
		return Result{}, nil
	}
	return Result{}, nil
}

func (r *upgradeRunner) runVolume(args []string) (Result, error) {
	name := args[len(args)-1]
	switch args[1] {
	case "create":
		if _, exists := r.volumes[name]; exists {
			return Result{ExitCode: 125}, nil
		}
		r.volumes[name] = commandLabels(args)
		return Result{}, nil
	case "inspect":
		labels, exists := r.volumes[name]
		if !exists {
			return Result{ExitCode: 125}, nil
		}
		return Result{Stdout: prerequisiteJSON(false, labels)}, nil
	case "rm":
		delete(r.volumes, name)
		return Result{}, nil
	}
	return Result{}, nil
}

func commandLabels(args []string) map[string]string {
	labels := map[string]string{}
	for index := 0; index+1 < len(args); index++ {
		if args[index] != "--label" {
			continue
		}
		key, value, found := strings.Cut(args[index+1], "=")
		if found {
			labels[key] = value
		}
	}
	return labels
}

func (r *prerequisiteRunner) Run(_ context.Context, command Command) (Result, error) {
	r.commands = append(r.commands, command)
	if len(command.Args) >= 2 && command.Args[1] == "create" {
		return Result{ExitCode: r.createExit}, nil
	}
	if len(command.Args) >= 2 && command.Args[1] == "inspect" {
		return Result{Stdout: r.inspect}, nil
	}
	return Result{}, nil
}
