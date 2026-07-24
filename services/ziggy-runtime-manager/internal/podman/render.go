package podman

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"github.com/mihai-chiorean/nanobot/services/ziggy-runtime-manager/runtime"
)

type renderedConfig struct {
	Agents struct {
		Defaults struct {
			Workspace string `json:"workspace"`
			Model     string `json:"model"`
			Provider  string `json:"provider"`
		} `json:"defaults"`
	} `json:"agents"`
	Channels  map[string]any `json:"channels"`
	Gateway   map[string]any `json:"gateway"`
	Providers map[string]any `json:"providers"`
	Tools     map[string]any `json:"tools"`
}

func tenantNames(r runtime.Request) (string, string, string) {
	// The stable name is the local single-writer fence. A new generation cannot
	// create a second container until the old generation has been drained and deleted.
	base := "ziggy-tenant-" + r.WorkspaceID
	// Workspace data outlives a runtime generation. Config material is a
	// generation capability and is deleted with the generation that used it.
	return base, base + "-root", base + "-g" + fmt.Sprintf("%d", r.Generation) + "-config"
}

func egressNetwork(r runtime.Request) string {
	return "ziggy-tenant-" + r.WorkspaceID + "-g" + fmt.Sprintf("%d", r.Generation)
}

func UnitName(r runtime.Request) string {
	base, _, _ := tenantNames(r)
	return base + ".service"
}

// RenderNanobotConfig is deterministic for an allocation, generation, policy and
// short-lived capabilities. It does not inherit ambient environment or source config.
func RenderNanobotConfig(policy Policy, r runtime.Request) ([]byte, error) {
	if !r.Valid() || !workspaceIDPattern.MatchString(r.WorkspaceID) {
		return nil, errors.New("invalid runtime request")
	}
	tenant, ok := policy.Tenants[r.WorkspaceID]
	if !ok {
		return nil, runtime.ErrUnknownWorkspace
	}
	config := renderedConfig{Channels: map[string]any{}, Gateway: map[string]any{"host": "0.0.0.0", "port": 18790, "heartbeat": map[string]any{"enabled": false}}}
	config.Agents.Defaults.Workspace = "/home/nanobot/.nanobot/workspace"
	config.Agents.Defaults.Model = tenant.Model
	config.Agents.Defaults.Provider = "custom"
	config.Providers = map[string]any{
		"custom": map[string]any{
			"apiBase": defaultEgressProxy + "/model/v1",
			"apiKey":  tenant.ModelCapability.Value,
		},
	}
	config.Tools = map[string]any{
		"restrictToWorkspace": true,
		"exec": map[string]any{
			"enable":         false,
			"allowedEnvKeys": []string{},
		},
		"web": map[string]any{"enable": false},
		"my":  map[string]any{"enable": false},
		"rag": map[string]any{"enable": false},
		"mcpServers": map[string]any{
			"ziggy_tenant": map[string]any{
				"type":         "streamableHttp",
				"url":          defaultEgressProxy + "/mcp",
				"headers":      map[string]string{"Authorization": "Bearer " + tenant.MCPCapability.Value},
				"enabledTools": []string{"*"},
				"toolTimeout":  30,
			},
		},
	}
	return json.MarshalIndent(config, "", "  ")
}

// RenderQuadlet emits a rootless-only tenant profile. The stable workspace unit
// is rewritten only after the previous generation has been deleted.
func RenderQuadlet(policy Policy, r runtime.Request) ([]byte, error) {
	if !r.Valid() || !workspaceIDPattern.MatchString(r.WorkspaceID) {
		return nil, errors.New("invalid runtime request")
	}
	tenant, ok := policy.Tenants[r.WorkspaceID]
	if !ok {
		return nil, runtime.ErrUnknownWorkspace
	}
	container, rootVolume, configVolume := tenantNames(r)
	network := egressNetwork(r)
	quadlet := fmt.Sprintf(`[Unit]
Description=Ziggy isolated Nanobot runtime %s generation %d
After=network-online.target
Wants=network-online.target

[Container]
Image=%s
ContainerName=%s
Network=%s
Volume=%s:/home/nanobot/.nanobot:rw,U,Z
Volume=%s:/run/ziggy/config:ro,U,Z
ReadOnly=true
ReadOnlyTmpfs=false
ImageVolume=ignore
Pull=never
LogDriver=journald
User=1000:1000
UserNS=auto:size=65536
DropCapability=all
NoNewPrivileges=true
SecurityLabelDisable=false
PidsLimit=256
Memory=1G
PodmanArgs=--cpus=1.0
PublishPort=127.0.0.1:%d:18790/tcp
Tmpfs=/tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777
Tmpfs=/run:rw,nosuid,nodev,noexec,size=16m,mode=755
Tmpfs=/dev/shm:rw,nosuid,nodev,noexec,size=64m,mode=1777
Environment=HOME=/home/nanobot
Environment=LANG=C.UTF-8
Environment=PYTHONDONTWRITEBYTECODE=1
HealthCmd=CMD-SHELL python3 -c 'import socket; s=socket.create_connection(("127.0.0.1",18790),2); s.close()'
HealthInterval=30s
HealthTimeout=5s
HealthRetries=3
Label=io.ziggy.workspace=%s
Label=io.ziggy.generation=%d
Label=io.ziggy.owner=ziggy-runtime-manager
Label=io.ziggy.egress=enforce-required
Exec=nanobot gateway --config /run/ziggy/config/config.json

[Service]
Slice=ziggy-tenant.slice
Restart=on-failure
RestartSec=5s
TimeoutStartSec=90s
TimeoutStopSec=30s
KillMode=control-group
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
RestrictSUIDSGID=true
LockPersonality=true
RestrictRealtime=true
MemoryMax=1G
TasksMax=256
CPUQuota=100%%
LimitCORE=0
UMask=0077
StandardOutput=journal
StandardError=journal
SyslogIdentifier=%s

[Install]
WantedBy=default.target
`, r.WorkspaceID, r.Generation, policy.ImageDigest, container, network, rootVolume, configVolume, tenant.HostPort, r.WorkspaceID, r.Generation, container)
	if err := ValidateRenderedQuadlet(quadlet); err != nil {
		return nil, err
	}
	return []byte(quadlet), nil
}

// ValidateRenderedQuadlet is a belt-and-suspenders guard against accidental
// weakening when the renderer evolves. It intentionally rejects host paths and
// any setting that would make the tenant runtime privileged or host-networked.
func ValidateRenderedQuadlet(quadlet string) error {
	required := []string{
		"ReadOnly=true", "UserNS=auto:size=65536", "DropCapability=all", "NoNewPrivileges=true",
		"PidsLimit=256", "Memory=1G", "PodmanArgs=--cpus=1.0", "PublishPort=127.0.0.1:",
		"ReadOnlyTmpfs=false", "ImageVolume=ignore", "Pull=never", "LogDriver=journald", "SecurityLabelDisable=false",
		"Volume=ziggy-tenant-", "Network=ziggy-tenant-", "io.ziggy.egress=enforce-required",
	}
	for _, value := range required {
		if !strings.Contains(quadlet, value) {
			return fmt.Errorf("quadlet missing required control %q", value)
		}
	}
	for _, forbidden := range []string{"Network=host", "Privileged=true", "AddCapability=", "/Users/", "/etc/", "/var/run/docker.sock", "/run/podman/podman.sock", "EnvironmentFile="} {
		if strings.Contains(quadlet, forbidden) {
			return fmt.Errorf("quadlet contains unsafe setting %q", forbidden)
		}
	}
	for _, line := range strings.Split(quadlet, "\n") {
		if !strings.HasPrefix(line, "Volume=") {
			continue
		}
		source, _, found := strings.Cut(strings.TrimPrefix(line, "Volume="), ":")
		if !found || strings.HasPrefix(source, "/") || strings.Contains(source, "..") {
			return errors.New("quadlet volume source must be a named Podman volume")
		}
	}
	return nil
}

// ValidateRenderedConfig checks the generated JSON rather than trusting the
// generator. It gives tests and release tooling a stable safety assertion.
func ValidateRenderedConfig(raw []byte) error {
	var config map[string]any
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&config); err != nil {
		return err
	}
	tools, ok := config["tools"].(map[string]any)
	if !ok || tools["restrictToWorkspace"] != true {
		return errors.New("restrictToWorkspace must be enabled")
	}
	exec, ok := tools["exec"].(map[string]any)
	if !ok || exec["enable"] != false {
		return errors.New("Nanobot exec must be disabled")
	}
	allowed, ok := exec["allowedEnvKeys"].([]any)
	if !ok || len(allowed) != 0 {
		return errors.New("Nanobot inherited exec environment must be empty")
	}
	providers, ok := config["providers"].(map[string]any)
	if !ok || len(providers) != 1 {
		return errors.New("only the controlled model endpoint is allowed")
	}
	custom, ok := providers["custom"].(map[string]any)
	if !ok || custom["apiBase"] != defaultEgressProxy+"/model/v1" {
		return errors.New("model endpoint bypasses enforcing egress proxy")
	}
	mcp, ok := tools["mcpServers"].(map[string]any)
	if !ok || len(mcp) != 1 {
		return errors.New("only the tenant MCP broker is allowed")
	}
	server, ok := mcp["ziggy_tenant"].(map[string]any)
	if !ok || server["url"] != defaultEgressProxy+"/mcp" {
		return errors.New("MCP endpoint bypasses enforcing egress proxy")
	}
	return nil
}
