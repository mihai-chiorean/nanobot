package podman

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-runtime-manager/runtime"
)

var fixtureNow = time.Date(2026, 7, 24, 12, 0, 0, 0, time.UTC)

func fixturePolicy() Policy {
	return Policy{
		ImageDigest: "registry.example.invalid/ziggy/nanobot-runtime@sha256:" + strings.Repeat("a", 64),
		Tenants: map[string]TenantPolicy{
			"tenant-alpha": {
				HostPort: 21001, Model: "local/model",
				ModelCapability: Capability{Value: strings.Repeat("m", 43), ExpiresAt: fixtureNow.Add(10 * time.Minute)},
				MCPCapability:   Capability{Value: strings.Repeat("n", 43), ExpiresAt: fixtureNow.Add(10 * time.Minute)},
			},
			"tenant-bravo": {
				HostPort: 21002, Model: "local/model",
				ModelCapability: Capability{Value: strings.Repeat("p", 43), ExpiresAt: fixtureNow.Add(10 * time.Minute)},
				MCPCapability:   Capability{Value: strings.Repeat("q", 43), ExpiresAt: fixtureNow.Add(10 * time.Minute)},
			},
		},
	}
}

func TestRenderNanobotConfigIsDeterministicAndRestricted(t *testing.T) {
	policy := fixturePolicy()
	request := runtime.Request{WorkspaceID: "tenant-alpha", Generation: 7}
	first, err := RenderNanobotConfig(policy, request)
	if err != nil {
		t.Fatal(err)
	}
	second, err := RenderNanobotConfig(policy, request)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(first, second) {
		t.Fatal("config generation must be deterministic for one generation policy")
	}
	if err := ValidateRenderedConfig(first); err != nil {
		t.Fatalf("generated config rejected: %v", err)
	}
	var config map[string]any
	if err := json.Unmarshal(first, &config); err != nil {
		t.Fatal(err)
	}
	tools := config["tools"].(map[string]any)
	tools["exec"].(map[string]any)["enable"] = true
	tampered, _ := json.Marshal(config)
	if err := ValidateRenderedConfig(tampered); err == nil {
		t.Fatal("enabled Nanobot exec was accepted")
	}
	providers := config["providers"].(map[string]any)
	providers["custom"].(map[string]any)["apiBase"] = "https://internet.invalid/v1"
	tampered, _ = json.Marshal(config)
	if err := ValidateRenderedConfig(tampered); err == nil {
		t.Fatal("direct model egress was accepted")
	}
}

func TestRenderQuadletRejectsUnsafeNetworkAndMounts(t *testing.T) {
	quadlet, err := RenderQuadlet(fixturePolicy(), runtime.Request{WorkspaceID: "tenant-alpha", Generation: 7})
	if err != nil {
		t.Fatal(err)
	}
	if err := ValidateRenderedQuadlet(string(quadlet)); err != nil {
		t.Fatalf("generated quadlet rejected: %v", err)
	}
	if strings.Contains(string(quadlet), "socket.create_connection") {
		t.Fatal("raw TCP health probe remained in generated Quadlet")
	}
	for _, required := range []string{
		`c.request("GET","/health"`,
		`json.loads(b) == {"status":"ok"}`,
		"ProtectHome=read-only",
		"ReadWritePaths=%h/.local/share/containers",
		"ReadWritePaths=%t/containers",
	} {
		if !strings.Contains(string(quadlet), required) {
			t.Fatalf("generated Quadlet missing %q", required)
		}
	}
	for _, unsafe := range []struct {
		name   string
		change func(string) string
	}{
		{"host network", func(value string) string {
			return strings.Replace(value, "Network=ziggy-tenant-tenant-alpha-g7", "Network=host", 1)
		}},
		{"host bind", func(value string) string {
			return strings.Replace(value, "Volume=ziggy-tenant-tenant-alpha-root", "Volume=/home/operator/repo", 1)
		}},
		{"environment file", func(value string) string { return value + "EnvironmentFile=/home/operator/.env\n" }},
		{"capability add", func(value string) string { return value + "AddCapability=SYS_ADMIN\n" }},
	} {
		t.Run(unsafe.name, func(t *testing.T) {
			if err := ValidateRenderedQuadlet(unsafe.change(string(quadlet))); err == nil {
				t.Fatal("unsafe Quadlet was accepted")
			}
		})
	}
}

func TestPolicyCapsPhaseOneFleetAtFourWorkspaces(t *testing.T) {
	policy := fixturePolicy()
	for index := 0; index < 3; index++ {
		policy.Tenants[fmt.Sprintf("tenant-extra-%d", index)] = TenantPolicy{
			HostPort: 22000 + index,
			Model:    "local/model",
			ModelCapability: Capability{
				Value:     strings.Repeat(string(rune('A'+index*2)), 43),
				ExpiresAt: fixtureNow.Add(10 * time.Minute),
			},
			MCPCapability: Capability{
				Value:     strings.Repeat(string(rune('B'+index*2)), 43),
				ExpiresAt: fixtureNow.Add(10 * time.Minute),
			},
		}
	}
	if err := policy.Validate(fixtureNow); err == nil || !strings.Contains(err.Error(), "4 workspace") {
		t.Fatalf("five-workspace policy error=%v", err)
	}
	delete(policy.Tenants, "tenant-extra-2")
	if err := policy.Validate(fixtureNow); err != nil {
		t.Fatalf("four-workspace canary policy rejected: %v", err)
	}
}

func TestPolicyRejectsMutableImageSharedPortsAndCredentials(t *testing.T) {
	policy := fixturePolicy()
	policy.ImageDigest = "registry.example.invalid/ziggy/nanobot-runtime:latest"
	if err := policy.Validate(fixtureNow); err == nil {
		t.Fatal("mutable image was accepted")
	}
	policy = fixturePolicy()
	bravo := policy.Tenants["tenant-bravo"]
	bravo.HostPort = 21001
	policy.Tenants["tenant-bravo"] = bravo
	if err := policy.Validate(fixtureNow); err == nil {
		t.Fatal("duplicate port was accepted")
	}
	policy = fixturePolicy()
	bravo = policy.Tenants["tenant-bravo"]
	bravo.MCPCapability = policy.Tenants["tenant-alpha"].ModelCapability
	policy.Tenants["tenant-bravo"] = bravo
	if err := policy.Validate(fixtureNow); err == nil {
		t.Fatal("reused capability was accepted")
	}
}
