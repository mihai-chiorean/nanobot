package podman

import (
	"context"
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
	if _, err := os.Stat(filepath.Join(driver.QuadletDir, "ziggy-tenant-tenant-alpha.container")); err != nil {
		t.Fatalf("Quadlet was not staged: %v", err)
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
