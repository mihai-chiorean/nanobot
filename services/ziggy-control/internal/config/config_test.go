package config

import (
	"errors"
	"log/slog"
	"testing"
	"time"
)

func TestLoadFromDefaults(t *testing.T) {
	environment := map[string]string{
		"ZIGGY_UPSTREAM_URL":         "http://127.0.0.1:8765/",
		"ZIGGY_AUTHORIZED_PARTIES":   "https://chat.example.com",
		"ZIGGY_TENANTS_FILE":         "/etc/ziggy/tenants.json",
		"ZIGGY_TENANT_BINDINGS_FILE": "/var/lib/ziggy-control/tenant-bindings.json",
		"CLERK_SECRET_KEY":           "secret",
	}

	config, err := LoadFrom(mapLookup(environment))
	if err != nil {
		t.Fatalf("LoadFrom() error = %v", err)
	}
	if config.ListenAddr != defaultListenAddr {
		t.Errorf("ListenAddr = %q, want %q", config.ListenAddr, defaultListenAddr)
	}
	if config.UpstreamURL.String() != "http://127.0.0.1:8765" {
		t.Errorf("UpstreamURL = %q", config.UpstreamURL)
	}
	if config.OwnerEmail != "" || config.OwnerSubject != "" {
		t.Errorf("tenant mode loaded legacy owner config: %+v", config)
	}
	if config.TenantManifest != "/etc/ziggy/tenants.json" || config.TenantBindings != "/var/lib/ziggy-control/tenant-bindings.json" {
		t.Errorf("tenant files not loaded: %+v", config)
	}
	if config.ShutdownTimeout != 10*time.Second {
		t.Errorf("ShutdownTimeout = %s", config.ShutdownTimeout)
	}
	if config.ReadinessTimeout != 2*time.Second {
		t.Errorf("ReadinessTimeout = %s", config.ReadinessTimeout)
	}
	if config.ReadinessCacheTTL != 2*time.Second {
		t.Errorf("ReadinessCacheTTL = %s", config.ReadinessCacheTTL)
	}
	if config.UpstreamReadyPath != "/" {
		t.Errorf("UpstreamReadyPath = %q", config.UpstreamReadyPath)
	}
	if config.MaxRequestBody != 64<<20 {
		t.Errorf("MaxRequestBody = %d", config.MaxRequestBody)
	}
	if config.LogLevel != slog.LevelInfo {
		t.Errorf("LogLevel = %s", config.LogLevel)
	}
	if config.OTelEndpoint != "" || config.OTelAuthFile != "" || config.OTelTraceSample != 1.0 {
		t.Errorf("OpenTelemetry defaults = endpoint %q, auth file %q, sample %v", config.OTelEndpoint, config.OTelAuthFile, config.OTelTraceSample)
	}
	if config.DeploymentEnv != "production" {
		t.Errorf("DeploymentEnv = %q, want production", config.DeploymentEnv)
	}
	for _, blocked := range []string{"/webui/bootstrap", "/auth/token"} {
		if _, ok := config.BlockedPaths[blocked]; !ok {
			t.Errorf("BlockedPaths does not contain %q", blocked)
		}
	}
}

func TestLoadFromRequiresOwnerOnlyForSingleTenantFallback(t *testing.T) {
	environment := map[string]string{
		"ZIGGY_UPSTREAM_URL":           "http://127.0.0.1:8765",
		"CLERK_SECRET_KEY":             "secret",
		"ZIGGY_DEPLOYMENT_ENVIRONMENT": "development",
	}
	if _, err := LoadFrom(mapLookup(environment)); err == nil {
		t.Fatal("LoadFrom() error = nil without a tenant manifest or fallback owner")
	}

	environment["ZIGGY_OWNER_EMAIL"] = "owner@example.com"
	config, err := LoadFrom(mapLookup(environment))
	if err != nil {
		t.Fatalf("LoadFrom() single-tenant fallback error = %v", err)
	}
	if config.OwnerEmail != "owner@example.com" {
		t.Errorf("OwnerEmail = %q", config.OwnerEmail)
	}
}

func TestLoadFromSecretFile(t *testing.T) {
	environment := map[string]string{
		"ZIGGY_UPSTREAM_URL":         "http://127.0.0.1:8765",
		"ZIGGY_OWNER_EMAIL":          "owner@example.com",
		"ZIGGY_OWNER_SUBJECT":        "user_123",
		"ZIGGY_AUTHORIZED_PARTIES":   "https://chat.example.com",
		"ZIGGY_TENANTS_FILE":         "/etc/ziggy/tenants.json",
		"ZIGGY_TENANT_BINDINGS_FILE": "/var/lib/ziggy-control/tenant-bindings.json",
		"CLERK_SECRET_KEY_FILE":      "/run/credentials/clerk-secret-key",
	}
	readFile := func(filename string) ([]byte, error) {
		if filename != environment["CLERK_SECRET_KEY_FILE"] {
			t.Fatalf("read filename = %q", filename)
		}
		return []byte("file-secret\n"), nil
	}

	config, err := loadFrom(mapLookup(environment), readFile)
	if err != nil {
		t.Fatalf("loadFrom() error = %v", err)
	}
	if config.ClerkSecretKey != "file-secret" {
		t.Errorf("ClerkSecretKey = %q", config.ClerkSecretKey)
	}
}

func TestLoadFromConnectorTrustFile(t *testing.T) {
	environment := map[string]string{
		"ZIGGY_UPSTREAM_URL":              "http://127.0.0.1:8765",
		"ZIGGY_OWNER_EMAIL":               "owner@example.com",
		"ZIGGY_OWNER_SUBJECT":             "user_123",
		"ZIGGY_AUTHORIZED_PARTIES":        "https://chat.example.com",
		"ZIGGY_TENANTS_FILE":              "/etc/ziggy/tenants.json",
		"ZIGGY_TENANT_BINDINGS_FILE":      "/var/lib/ziggy-control/tenant-bindings.json",
		"ZIGGY_CONNECTORS_URL":            "http://127.0.0.1:8790",
		"ZIGGY_CONNECTORS_TRUST_KEY_FILE": "/run/credentials/connector-trust-key",
		"CLERK_SECRET_KEY":                "secret",
	}
	readFile := func(filename string) ([]byte, error) {
		if filename != environment["ZIGGY_CONNECTORS_TRUST_KEY_FILE"] {
			t.Fatalf("read filename = %q", filename)
		}
		return []byte("01234567890123456789012345678901"), nil
	}

	loaded, err := loadFrom(mapLookup(environment), readFile)
	if err != nil {
		t.Fatal(err)
	}
	if loaded.ConnectorsURL.String() != "http://127.0.0.1:8790" || len(loaded.ConnectorTrustKey) != 32 {
		t.Fatalf("connector config = %+v", loaded)
	}
}

func TestLoadFromConnectorTrustSystemdCredential(t *testing.T) {
	environment := map[string]string{
		"ZIGGY_UPSTREAM_URL":         "http://127.0.0.1:8765",
		"ZIGGY_OWNER_EMAIL":          "owner@example.com",
		"ZIGGY_OWNER_SUBJECT":        "user_123",
		"ZIGGY_AUTHORIZED_PARTIES":   "https://chat.example.com",
		"ZIGGY_TENANTS_FILE":         "/etc/ziggy/tenants.json",
		"ZIGGY_TENANT_BINDINGS_FILE": "/var/lib/ziggy-control/tenant-bindings.json",
		"ZIGGY_CONNECTORS_URL":       "http://127.0.0.1:8790",
		"CREDENTIALS_DIRECTORY":      "/run/credentials/ziggy-control.service",
		"CLERK_SECRET_KEY":           "secret",
	}
	readFile := func(filename string) ([]byte, error) {
		want := "/run/credentials/ziggy-control.service/connector-trust-key"
		if filename != want {
			t.Fatalf("read filename = %q, want %q", filename, want)
		}
		return []byte("01234567890123456789012345678901"), nil
	}

	loaded, err := loadFrom(mapLookup(environment), readFile)
	if err != nil {
		t.Fatal(err)
	}
	if loaded.ConnectorsURL.String() != "http://127.0.0.1:8790" || len(loaded.ConnectorTrustKey) != 32 {
		t.Fatalf("connector config = %+v", loaded)
	}
}

func TestLoadFromRejectsInvalidSecretSources(t *testing.T) {
	base := map[string]string{
		"ZIGGY_UPSTREAM_URL": "http://127.0.0.1:8765",
		"ZIGGY_OWNER_EMAIL":  "owner@example.com",
	}
	tests := []struct {
		name        string
		environment map[string]string
		readFile    ReadFile
	}{
		{
			name:        "missing",
			environment: map[string]string{},
			readFile:    func(string) ([]byte, error) { return nil, errors.New("unexpected read") },
		},
		{
			name: "both",
			environment: map[string]string{
				"CLERK_SECRET_KEY":      "inline-secret",
				"CLERK_SECRET_KEY_FILE": "/secret",
			},
			readFile: func(string) ([]byte, error) { return nil, errors.New("unexpected read") },
		},
		{
			name:        "unreadable file",
			environment: map[string]string{"CLERK_SECRET_KEY_FILE": "/secret"},
			readFile:    func(string) ([]byte, error) { return nil, errors.New("denied") },
		},
		{
			name:        "empty file",
			environment: map[string]string{"CLERK_SECRET_KEY_FILE": "/secret"},
			readFile:    func(string) ([]byte, error) { return []byte(" \n"), nil },
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			environment := make(map[string]string, len(base)+len(test.environment))
			for key, value := range base {
				environment[key] = value
			}
			for key, value := range test.environment {
				environment[key] = value
			}
			if _, err := loadFrom(mapLookup(environment), test.readFile); err == nil {
				t.Fatal("loadFrom() error = nil, want error")
			}
		})
	}
}

func TestLoadFromOverrides(t *testing.T) {
	environment := map[string]string{
		"ZIGGY_UPSTREAM_URL":             "https://10.20.30.40/base",
		"ZIGGY_OWNER_EMAIL":              "owner@example.com",
		"ZIGGY_OWNER_SUBJECT":            "user_123",
		"CLERK_SECRET_KEY":               "secret",
		"ZIGGY_LISTEN_ADDR":              ":9000",
		"ZIGGY_AUTHORIZED_PARTIES":       "https://app.example, native://ziggy,https://app.example",
		"ZIGGY_BLOCKED_PATHS":            "private,/internal",
		"ZIGGY_SHUTDOWN_TIMEOUT":         "3s",
		"ZIGGY_UPSTREAM_READY_TIMEOUT":   "750ms",
		"ZIGGY_UPSTREAM_READY_CACHE_TTL": "250ms",
		"ZIGGY_UPSTREAM_READY_PATH":      "/healthz",
		"ZIGGY_MAX_REQUEST_BODY_BYTES":   "1024",
		"ZIGGY_LOG_LEVEL":                "debug",
		"ZIGGY_OTEL_ENDPOINT":            "http://127.0.0.1:4318",
		"ZIGGY_OTEL_AUTH_FILE":           "/run/credentials/otel-local-auth",
		"ZIGGY_OTEL_TRACE_SAMPLE_RATIO":  "0.25",
		"ZIGGY_DEPLOYMENT_ENVIRONMENT":   "staging",
		"ZIGGY_MAX_HTTP_IN_FLIGHT":       "12",
		"ZIGGY_MAX_SSE_IN_FLIGHT":        "3",
		"ZIGGY_MAX_WEBSOCKET_IN_FLIGHT":  "2",
		"ZIGGY_UPSTREAM_PREFLIGHT":       "false",
	}

	config, err := LoadFrom(mapLookup(environment))
	if err != nil {
		t.Fatalf("LoadFrom() error = %v", err)
	}
	if config.ListenAddr != ":9000" || config.OwnerSubject != "user_123" {
		t.Errorf("overrides not applied: %+v", config)
	}
	if got := len(config.AuthorizedParties); got != 2 {
		t.Errorf("len(AuthorizedParties) = %d, want 2", got)
	}
	if _, ok := config.BlockedPaths["/private"]; !ok {
		t.Error("BlockedPaths does not contain normalized /private")
	}
	if _, ok := config.BlockedPaths["/webui/bootstrap"]; !ok {
		t.Error("mandatory /webui/bootstrap block was removed")
	}
	if config.ReadinessCacheTTL != 250*time.Millisecond || config.UpstreamReadyPath != "/healthz" {
		t.Errorf("readiness overrides not applied: %+v", config)
	}
	if config.MaxRequestBody != 1024 {
		t.Errorf("MaxRequestBody = %d, want 1024", config.MaxRequestBody)
	}
	if config.HTTPInFlight != 12 || config.SSEInFlight != 3 || config.WebSocketInFlight != 2 || config.UpstreamPreflight {
		t.Errorf("admission/preflight overrides not applied: %+v", config)
	}
	if config.OTelEndpoint != "http://127.0.0.1:4318" || config.OTelAuthFile != "/run/credentials/otel-local-auth" || config.OTelTraceSample != 0.25 {
		t.Errorf("OpenTelemetry overrides not applied: %+v", config)
	}
	if config.DeploymentEnv != "staging" {
		t.Errorf("DeploymentEnv = %q, want staging", config.DeploymentEnv)
	}
}

func TestLoadFromRejectsInvalidConfiguration(t *testing.T) {
	base := map[string]string{
		"ZIGGY_UPSTREAM_URL":         "http://127.0.0.1:8765",
		"ZIGGY_OWNER_EMAIL":          "owner@example.com",
		"ZIGGY_OWNER_SUBJECT":        "user_123",
		"ZIGGY_AUTHORIZED_PARTIES":   "https://chat.example.com",
		"ZIGGY_TENANTS_FILE":         "/etc/ziggy/tenants.json",
		"ZIGGY_TENANT_BINDINGS_FILE": "/var/lib/ziggy-control/tenant-bindings.json",
		"CLERK_SECRET_KEY":           "secret",
	}
	tests := []struct {
		name   string
		key    string
		value  string
		remove string
	}{
		{name: "missing upstream", remove: "ZIGGY_UPSTREAM_URL"},
		{name: "missing tenant bindings", remove: "ZIGGY_TENANT_BINDINGS_FILE"},
		{name: "connector URL without trust key", key: "ZIGGY_CONNECTORS_URL", value: "http://127.0.0.1:8790"},
		{name: "unsupported scheme", key: "ZIGGY_UPSTREAM_URL", value: "ftp://example.com"},
		{name: "public upstream", key: "ZIGGY_UPSTREAM_URL", value: "https://8.8.8.8:443"},
		{name: "upstream credentials", key: "ZIGGY_UPSTREAM_URL", value: "http://user:pass@example.com"},
		{name: "upstream query", key: "ZIGGY_UPSTREAM_URL", value: "http://127.0.0.1:8765/?probe=1"},
		{name: "upstream fragment", key: "ZIGGY_UPSTREAM_URL", value: "http://127.0.0.1:8765/#probe"},
		{name: "bad email", key: "ZIGGY_OWNER_EMAIL", value: "owner"},
		{name: "malformed email", key: "ZIGGY_OWNER_EMAIL", value: "@example.com"},
		{name: "bad listen address", key: "ZIGGY_LISTEN_ADDR", value: "localhost"},
		{name: "non-positive timeout", key: "ZIGGY_SHUTDOWN_TIMEOUT", value: "0s"},
		{name: "relative readiness path", key: "ZIGGY_UPSTREAM_READY_PATH", value: "healthz"},
		{name: "readiness path query", key: "ZIGGY_UPSTREAM_READY_PATH", value: "/healthz?full=1"},
		{name: "non-positive body limit", key: "ZIGGY_MAX_REQUEST_BODY_BYTES", value: "0"},
		{name: "bad log level", key: "ZIGGY_LOG_LEVEL", value: "verbose"},
		{name: "negative trace ratio", key: "ZIGGY_OTEL_TRACE_SAMPLE_RATIO", value: "-0.1"},
		{name: "high trace ratio", key: "ZIGGY_OTEL_TRACE_SAMPLE_RATIO", value: "1.1"},
		{name: "bad deployment environment", key: "ZIGGY_DEPLOYMENT_ENVIRONMENT", value: "tenant-123"},
		{name: "bad HTTP admission limit", key: "ZIGGY_MAX_HTTP_IN_FLIGHT", value: "0"},
		{name: "bad preflight boolean", key: "ZIGGY_UPSTREAM_PREFLIGHT", value: "maybe"},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			environment := make(map[string]string, len(base)+1)
			for key, value := range base {
				environment[key] = value
			}
			if test.remove != "" {
				delete(environment, test.remove)
			}
			if test.key != "" {
				environment[test.key] = test.value
			}
			if _, err := LoadFrom(mapLookup(environment)); err == nil {
				t.Fatal("LoadFrom() error = nil, want error")
			}
		})
	}
}

func TestParseUpstreamAcceptsTrustedAddressClasses(t *testing.T) {
	for _, value := range []string{
		"http://localhost:8765",
		"http://127.0.0.1:8765",
		"http://10.0.0.8:8765",
		"http://172.16.1.8:8765",
		"http://192.168.1.8:8765",
		"http://100.100.10.8:8765",
		"http://[::1]:8765",
		"http://[fd00::8]:8765",
		"http://[fe80::8]:8765",
	} {
		if _, err := parseUpstream(value); err != nil {
			t.Errorf("parseUpstream(%q) error = %v", value, err)
		}
	}
}

func mapLookup(values map[string]string) LookupEnv {
	return func(key string) (string, bool) {
		value, ok := values[key]
		return value, ok
	}
}
