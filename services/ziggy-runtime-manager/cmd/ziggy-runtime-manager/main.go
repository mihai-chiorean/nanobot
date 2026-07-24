package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-runtime-manager/internal/api"
	"github.com/mihai-chiorean/nanobot/services/ziggy-runtime-manager/internal/podman"
)

var version = "dev"

func main() {
	var policyPath, quadletDir, socket string
	flag.StringVar(&policyPath, "policy", "", "manager-owned policy JSON (mode 0600)")
	flag.StringVar(&quadletDir, "quadlet-dir", "", "rootless Quadlet directory")
	flag.StringVar(&socket, "socket", "", "manager-owned Unix socket")
	flag.Parse()
	if policyPath == "" || quadletDir == "" || socket == "" {
		fmt.Fprintln(os.Stderr, "usage: ziggy-runtime-manager -policy FILE -quadlet-dir DIR -socket FILE")
		os.Exit(2)
	}
	policy, err := loadPolicy(policyPath)
	if err != nil || policy.Validate(time.Now()) != nil {
		fmt.Fprintln(os.Stderr, "invalid runtime manager policy")
		os.Exit(1)
	}
	driver := podman.Driver{Policy: policy, QuadletDir: quadletDir, Runner: podman.ExecRunner{}}
	logger := log.New(os.Stderr, "ziggy-runtime-manager ", log.LstdFlags|log.LUTC)
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	logger.Printf("runtime_manager_started version=%s", version)
	if err := (api.Server{Driver: driver, Logger: logger}).ListenAndServe(ctx, socket); err != nil {
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
