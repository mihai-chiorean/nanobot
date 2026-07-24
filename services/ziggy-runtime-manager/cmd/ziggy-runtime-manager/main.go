package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"os/user"
	"path/filepath"
	"strconv"
	"syscall"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-runtime-manager/internal/api"
	"github.com/mihai-chiorean/nanobot/services/ziggy-runtime-manager/internal/podman"
	lifecycle "github.com/mihai-chiorean/nanobot/services/ziggy-runtime-manager/runtime"
)

var version = "dev"

func main() {
	var policyPath, quadletDir, socket, socketGroup, stateDir string
	flag.StringVar(&policyPath, "policy", "", "manager-owned policy JSON (mode 0600)")
	flag.StringVar(&quadletDir, "quadlet-dir", "", "rootless Quadlet directory")
	flag.StringVar(&stateDir, "state-dir", "", "manager-owned lifecycle state directory")
	flag.StringVar(&socket, "socket", "", "manager-owned Unix socket")
	flag.StringVar(&socketGroup, "socket-group", "", "primary group authorized for lifecycle requests")
	flag.Parse()
	if policyPath == "" || quadletDir == "" || stateDir == "" || socket == "" || socketGroup == "" {
		fmt.Fprintln(os.Stderr, "usage: ziggy-runtime-manager -policy FILE -quadlet-dir DIR -state-dir DIR -socket FILE -socket-group GROUP")
		os.Exit(2)
	}
	policy, err := loadPolicy(policyPath)
	if err != nil || policy.Validate(time.Now()) != nil {
		fmt.Fprintln(os.Stderr, "invalid runtime manager policy")
		os.Exit(1)
	}
	group, err := user.LookupGroup(socketGroup)
	if err != nil {
		fmt.Fprintln(os.Stderr, "invalid runtime manager socket group")
		os.Exit(1)
	}
	groupID, err := strconv.Atoi(group.Gid)
	if err != nil || groupID < 0 {
		fmt.Fprintln(os.Stderr, "invalid runtime manager socket group")
		os.Exit(1)
	}
	driver := &podman.Driver{Policy: policy, QuadletDir: quadletDir, Runner: podman.ExecRunner{}}
	manager, err := lifecycle.NewManager(driver, stateDir)
	if err != nil {
		fmt.Fprintln(os.Stderr, "invalid runtime manager state directory")
		os.Exit(1)
	}
	logger := log.New(os.Stderr, "ziggy-runtime-manager ", log.LstdFlags|log.LUTC)
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	logger.Printf("runtime_manager_started version=%s", version)
	server := api.Server{
		Driver:         manager,
		Logger:         logger,
		SocketGroupID:  groupID,
		SocketGroupSet: true,
		DestructiveUID: os.Geteuid(),
		DestructiveSet: true,
	}
	if err := server.ListenAndServe(ctx, socket); err != nil {
		logger.Printf("runtime_manager_stopped result=failed")
		os.Exit(1)
	}
}

func loadPolicy(path string) (podman.Policy, error) {
	info, err := os.Stat(path)
	if err != nil {
		return podman.Policy{}, err
	}
	if info.Mode().Perm()&0o077 != 0 {
		return podman.Policy{}, fmt.Errorf("policy %s must not be group or world readable", filepath.Base(path))
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return podman.Policy{}, err
	}
	var policy podman.Policy
	if err := json.Unmarshal(raw, &policy); err != nil {
		return podman.Policy{}, err
	}
	return policy, nil
}
