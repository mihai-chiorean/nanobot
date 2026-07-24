package config

import (
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"log/slog"
	"net"
	"net/mail"
	"net/netip"
	"net/url"
	"os"
	"path"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"time"
)

const (
	defaultListenAddr          = "127.0.0.1:8787"
	defaultShutdownTimeout     = 10 * time.Second
	defaultReadinessTimeout    = 2 * time.Second
	defaultReadinessCacheTTL   = 2 * time.Second
	defaultUpstreamReadyPath   = "/"
	defaultMaxRequestBodyBytes = int64(64 << 20)
	defaultHTTPInFlight        = int64(64)
	defaultSSEInFlight         = int64(8)
	defaultWebSocketInFlight   = int64(8)
	defaultOTelTraceSampleRate = 1.0
	defaultDeploymentEnv       = "production"
	defaultUpstreamPreflight   = true
)

var tailscaleCGNATPrefix = netip.MustParsePrefix("100.64.0.0/10")

type Config struct {
	ListenAddr        string
	UpstreamURL       *url.URL
	OwnerEmail        string
	OwnerSubject      string
	ClerkSecretKey    string
	AuthorizedParties []string
	TenantManifest    string
	TenantBindings    string
	TenantMode        string
	TenantDatabaseURL string
	ConnectorsURL     *url.URL
	ConnectorTrustKey []byte
	WorkURL           *url.URL
	WorkTrustKey      []byte
	BlockedPaths      map[string]struct{}
	ShutdownTimeout   time.Duration
	ReadinessTimeout  time.Duration
	ReadinessCacheTTL time.Duration
	UpstreamReadyPath string
	MaxRequestBody    int64
	HTTPInFlight      int64
	SSEInFlight       int64
	WebSocketInFlight int64
	UpstreamPreflight bool
	LogLevel          slog.Level
	OTelEndpoint      string
	OTelAuthFile      string
	OTelTraceSample   float64
	DeploymentEnv     string
}

type LookupEnv func(string) (string, bool)
type ReadFile func(string) ([]byte, error)

func Load() (Config, error) {
	return LoadFrom(os.LookupEnv)
}

func LoadFrom(lookup LookupEnv) (Config, error) {
	return loadFrom(lookup, os.ReadFile)
}

func loadFrom(lookup LookupEnv, readFile ReadFile) (Config, error) {
	upstreamValue, err := required(lookup, "ZIGGY_UPSTREAM_URL")
	if err != nil {
		return Config{}, err
	}
	upstream, err := parseUpstream(upstreamValue)
	if err != nil {
		return Config{}, fmt.Errorf("ZIGGY_UPSTREAM_URL: %w", err)
	}

	secret, err := clerkSecret(lookup, readFile)
	if err != nil {
		return Config{}, err
	}

	listenAddr := valueOrDefault(lookup, "ZIGGY_LISTEN_ADDR", defaultListenAddr)
	if _, _, err := net.SplitHostPort(listenAddr); err != nil {
		return Config{}, fmt.Errorf("ZIGGY_LISTEN_ADDR: %w", err)
	}

	shutdownTimeout, err := duration(lookup, "ZIGGY_SHUTDOWN_TIMEOUT", defaultShutdownTimeout)
	if err != nil {
		return Config{}, err
	}
	readinessTimeout, err := duration(lookup, "ZIGGY_UPSTREAM_READY_TIMEOUT", defaultReadinessTimeout)
	if err != nil {
		return Config{}, err
	}
	readinessCacheTTL, err := duration(lookup, "ZIGGY_UPSTREAM_READY_CACHE_TTL", defaultReadinessCacheTTL)
	if err != nil {
		return Config{}, err
	}
	readyPath, err := absolutePath(lookup, "ZIGGY_UPSTREAM_READY_PATH", defaultUpstreamReadyPath)
	if err != nil {
		return Config{}, err
	}
	maxRequestBody, err := positiveInt64(lookup, "ZIGGY_MAX_REQUEST_BODY_BYTES", defaultMaxRequestBodyBytes)
	if err != nil {
		return Config{}, err
	}
	httpInFlight, err := positiveInt64(lookup, "ZIGGY_MAX_HTTP_IN_FLIGHT", defaultHTTPInFlight)
	if err != nil {
		return Config{}, err
	}
	sseInFlight, err := positiveInt64(lookup, "ZIGGY_MAX_SSE_IN_FLIGHT", defaultSSEInFlight)
	if err != nil {
		return Config{}, err
	}
	webSocketInFlight, err := positiveInt64(lookup, "ZIGGY_MAX_WEBSOCKET_IN_FLIGHT", defaultWebSocketInFlight)
	if err != nil {
		return Config{}, err
	}
	logLevel, err := parseLogLevel(valueOrDefault(lookup, "ZIGGY_LOG_LEVEL", "info"))
	if err != nil {
		return Config{}, err
	}
	otelTraceSample, err := fraction(lookup, "ZIGGY_OTEL_TRACE_SAMPLE_RATIO", defaultOTelTraceSampleRate)
	if err != nil {
		return Config{}, err
	}
	deploymentEnv, err := deploymentEnvironment(valueOrDefault(lookup, "ZIGGY_DEPLOYMENT_ENVIRONMENT", defaultDeploymentEnv))
	if err != nil {
		return Config{}, err
	}
	authorizedParties := commaSeparated(lookup, "ZIGGY_AUTHORIZED_PARTIES", "")
	ownerSubject := optional(lookup, "ZIGGY_OWNER_SUBJECT")
	tenantManifest := optional(lookup, "ZIGGY_TENANTS_FILE")
	tenantBindings := optional(lookup, "ZIGGY_TENANT_BINDINGS_FILE")
	if (tenantManifest == "") != (tenantBindings == "") {
		return Config{}, fmt.Errorf("ZIGGY_TENANTS_FILE and ZIGGY_TENANT_BINDINGS_FILE must be set together")
	}
	ownerEmail := strings.ToLower(optional(lookup, "ZIGGY_OWNER_EMAIL"))
	if ownerEmail != "" {
		parsedEmail, err := mail.ParseAddress(ownerEmail)
		if err != nil || parsedEmail.Address != ownerEmail {
			return Config{}, fmt.Errorf("ZIGGY_OWNER_EMAIL: invalid email address")
		}
	}
	tenantMode, err := tenancyMode(lookup, tenantManifest)
	if err != nil {
		return Config{}, err
	}
	tenantDatabaseURL, err := tenantDatabaseConfig(lookup, readFile)
	if err != nil {
		return Config{}, err
	}
	switch tenantMode {
	case "manifest":
		if tenantManifest == "" {
			return Config{}, fmt.Errorf("ZIGGY_TENANTS_FILE and ZIGGY_TENANT_BINDINGS_FILE are required in manifest mode")
		}
		if tenantDatabaseURL != "" {
			return Config{}, fmt.Errorf("tenant database configuration is only valid in postgres mode")
		}
	case "postgres":
		if tenantManifest != "" {
			return Config{}, fmt.Errorf("tenant manifest configuration is not valid in postgres mode")
		}
		if tenantDatabaseURL == "" {
			return Config{}, fmt.Errorf("ZIGGY_TENANT_DATABASE_URL or ZIGGY_TENANT_DATABASE_URL_FILE is required in postgres mode")
		}
	case "legacy":
		if tenantManifest != "" || tenantDatabaseURL != "" || ownerEmail == "" {
			return Config{}, fmt.Errorf("legacy mode requires only ZIGGY_OWNER_EMAIL")
		}
	}
	connectorsURL, connectorTrustKey, err := connectorConfig(lookup, readFile)
	if err != nil {
		return Config{}, err
	}
	workURL, workTrustKey, err := workConfig(lookup, readFile)
	if err != nil {
		return Config{}, err
	}
	upstreamPreflight, err := boolean(lookup, "ZIGGY_UPSTREAM_PREFLIGHT", defaultUpstreamPreflight)
	if err != nil {
		return Config{}, err
	}
	if deploymentEnv == "production" {
		if len(authorizedParties) == 0 {
			return Config{}, fmt.Errorf("ZIGGY_AUTHORIZED_PARTIES is required in production")
		}
		if tenantMode == "legacy" {
			return Config{}, fmt.Errorf("ZIGGY_TENANCY_MODE=legacy is not allowed in production")
		}
		if !upstreamPreflight {
			return Config{}, fmt.Errorf("ZIGGY_UPSTREAM_PREFLIGHT cannot be disabled in production")
		}
	}

	return Config{
		ListenAddr:        listenAddr,
		UpstreamURL:       upstream,
		OwnerEmail:        ownerEmail,
		OwnerSubject:      ownerSubject,
		ClerkSecretKey:    secret,
		AuthorizedParties: authorizedParties,
		TenantManifest:    tenantManifest,
		TenantBindings:    tenantBindings,
		TenantMode:        tenantMode,
		TenantDatabaseURL: tenantDatabaseURL,
		ConnectorsURL:     connectorsURL,
		ConnectorTrustKey: connectorTrustKey,
		WorkURL:           workURL,
		WorkTrustKey:      workTrustKey,
		BlockedPaths:      blockedPaths(lookup),
		ShutdownTimeout:   shutdownTimeout,
		ReadinessTimeout:  readinessTimeout,
		ReadinessCacheTTL: readinessCacheTTL,
		UpstreamReadyPath: readyPath,
		MaxRequestBody:    maxRequestBody,
		HTTPInFlight:      httpInFlight,
		SSEInFlight:       sseInFlight,
		WebSocketInFlight: webSocketInFlight,
		UpstreamPreflight: upstreamPreflight,
		LogLevel:          logLevel,
		OTelEndpoint:      optional(lookup, "ZIGGY_OTEL_ENDPOINT"),
		OTelAuthFile:      optional(lookup, "ZIGGY_OTEL_AUTH_FILE"),
		OTelTraceSample:   otelTraceSample,
		DeploymentEnv:     deploymentEnv,
	}, nil
}

func tenancyMode(lookup LookupEnv, tenantManifest string) (string, error) {
	mode := strings.ToLower(optional(lookup, "ZIGGY_TENANCY_MODE"))
	if mode == "" {
		if tenantManifest != "" {
			return "manifest", nil
		}
		return "legacy", nil
	}
	switch mode {
	case "manifest", "postgres", "legacy":
		return mode, nil
	default:
		return "", fmt.Errorf("ZIGGY_TENANCY_MODE must be manifest, postgres, or legacy")
	}
}

func tenantDatabaseConfig(lookup LookupEnv, readFile ReadFile) (string, error) {
	inline := optional(lookup, "ZIGGY_TENANT_DATABASE_URL")
	filename := optional(lookup, "ZIGGY_TENANT_DATABASE_URL_FILE")
	if inline != "" && filename != "" {
		return "", fmt.Errorf("set only one of ZIGGY_TENANT_DATABASE_URL or ZIGGY_TENANT_DATABASE_URL_FILE")
	}
	if filename != "" {
		contents, err := readFile(filename)
		if err != nil {
			return "", fmt.Errorf("ZIGGY_TENANT_DATABASE_URL_FILE: %w", err)
		}
		inline = strings.TrimSpace(string(contents))
	}
	if inline == "" {
		return "", nil
	}
	parsed, err := url.Parse(inline)
	if err != nil || (parsed.Scheme != "postgres" && parsed.Scheme != "postgresql") || parsed.Host == "" {
		return "", fmt.Errorf("ZIGGY_TENANT_DATABASE_URL must be a PostgreSQL URL")
	}
	return inline, nil
}

func workConfig(lookup LookupEnv, readFile ReadFile) (*url.URL, []byte, error) {
	rawURL := optional(lookup, "ZIGGY_WORK_URL")
	keyFile := optional(lookup, "ZIGGY_WORK_TRUST_KEY_FILE")
	if rawURL == "" {
		if keyFile != "" {
			return nil, nil, fmt.Errorf("ZIGGY_WORK_URL and ZIGGY_WORK_TRUST_KEY_FILE must be set together")
		}
		return nil, nil, nil
	}
	if keyFile == "" {
		if credentialsDirectory := optional(lookup, "CREDENTIALS_DIRECTORY"); credentialsDirectory != "" {
			keyFile = filepath.Join(credentialsDirectory, "work-trust-key")
		}
	}
	if keyFile == "" {
		return nil, nil, fmt.Errorf("ZIGGY_WORK_URL and ZIGGY_WORK_TRUST_KEY_FILE must be set together")
	}
	parsed, err := parseUpstream(rawURL)
	if err != nil {
		return nil, nil, fmt.Errorf("ZIGGY_WORK_URL: %w", err)
	}
	contents, err := readFile(keyFile)
	if err != nil {
		return nil, nil, fmt.Errorf("ZIGGY_WORK_TRUST_KEY_FILE: %w", err)
	}
	key := decodeKeyMaterial(contents)
	if len(key) < 32 {
		return nil, nil, fmt.Errorf("ZIGGY_WORK_TRUST_KEY_FILE must contain at least 32 bytes")
	}
	return parsed, key, nil
}

func connectorConfig(lookup LookupEnv, readFile ReadFile) (*url.URL, []byte, error) {
	rawURL := optional(lookup, "ZIGGY_CONNECTORS_URL")
	keyFile := optional(lookup, "ZIGGY_CONNECTORS_TRUST_KEY_FILE")
	if rawURL == "" {
		if keyFile != "" {
			return nil, nil, fmt.Errorf("ZIGGY_CONNECTORS_URL and ZIGGY_CONNECTORS_TRUST_KEY_FILE must be set together")
		}
		return nil, nil, nil
	}
	if keyFile == "" {
		if credentialsDirectory := optional(lookup, "CREDENTIALS_DIRECTORY"); credentialsDirectory != "" {
			keyFile = filepath.Join(credentialsDirectory, "connector-trust-key")
		}
	}
	if keyFile == "" {
		return nil, nil, fmt.Errorf("ZIGGY_CONNECTORS_URL and ZIGGY_CONNECTORS_TRUST_KEY_FILE must be set together")
	}
	parsed, err := parseUpstream(rawURL)
	if err != nil {
		return nil, nil, fmt.Errorf("ZIGGY_CONNECTORS_URL: %w", err)
	}
	contents, err := readFile(keyFile)
	if err != nil {
		return nil, nil, fmt.Errorf("ZIGGY_CONNECTORS_TRUST_KEY_FILE: %w", err)
	}
	key := decodeKeyMaterial(contents)
	if len(key) < 32 {
		return nil, nil, fmt.Errorf("ZIGGY_CONNECTORS_TRUST_KEY_FILE must contain at least 32 bytes")
	}
	return parsed, key, nil
}

func decodeKeyMaterial(contents []byte) []byte {
	trimmed := strings.TrimSpace(string(contents))
	for _, decode := range []func(string) ([]byte, error){
		base64.StdEncoding.DecodeString,
		base64.RawStdEncoding.DecodeString,
		hex.DecodeString,
	} {
		if decoded, err := decode(trimmed); err == nil && len(decoded) >= 32 {
			return decoded
		}
	}
	return []byte(trimmed)
}

func absolutePath(lookup LookupEnv, key, fallback string) (string, error) {
	value := valueOrDefault(lookup, key, fallback)
	if !strings.HasPrefix(value, "/") || strings.ContainsAny(value, "?#") {
		return "", fmt.Errorf("%s must be an absolute path without query or fragment", key)
	}
	return path.Clean(value), nil
}

func positiveInt64(lookup LookupEnv, key string, fallback int64) (int64, error) {
	value := valueOrDefault(lookup, key, strconv.FormatInt(fallback, 10))
	parsed, err := strconv.ParseInt(value, 10, 64)
	if err != nil || parsed <= 0 {
		return 0, fmt.Errorf("%s must be a positive integer", key)
	}
	return parsed, nil
}

func fraction(lookup LookupEnv, key string, fallback float64) (float64, error) {
	value := valueOrDefault(lookup, key, strconv.FormatFloat(fallback, 'f', -1, 64))
	parsed, err := strconv.ParseFloat(value, 64)
	if err != nil || parsed < 0 || parsed > 1 {
		return 0, fmt.Errorf("%s must be between 0 and 1", key)
	}
	return parsed, nil
}

func deploymentEnvironment(value string) (string, error) {
	value = strings.ToLower(strings.TrimSpace(value))
	switch value {
	case "development", "local", "staging", "production", "test":
		return value, nil
	default:
		return "", fmt.Errorf("ZIGGY_DEPLOYMENT_ENVIRONMENT: unsupported environment")
	}
}

func clerkSecret(lookup LookupEnv, readFile ReadFile) (string, error) {
	inline := optional(lookup, "CLERK_SECRET_KEY")
	filename := optional(lookup, "CLERK_SECRET_KEY_FILE")
	if inline != "" && filename != "" {
		return "", fmt.Errorf("set only one of CLERK_SECRET_KEY or CLERK_SECRET_KEY_FILE")
	}
	if inline != "" {
		return inline, nil
	}
	if filename == "" {
		return "", fmt.Errorf("CLERK_SECRET_KEY or CLERK_SECRET_KEY_FILE is required")
	}

	contents, err := readFile(filename)
	if err != nil {
		return "", fmt.Errorf("CLERK_SECRET_KEY_FILE: %w", err)
	}
	secret := strings.TrimSpace(string(contents))
	if secret == "" {
		return "", fmt.Errorf("CLERK_SECRET_KEY_FILE is empty")
	}
	return secret, nil
}

func required(lookup LookupEnv, key string) (string, error) {
	value := optional(lookup, key)
	if value == "" {
		return "", fmt.Errorf("%s is required", key)
	}
	return value, nil
}

func optional(lookup LookupEnv, key string) string {
	value, _ := lookup(key)
	return strings.TrimSpace(value)
}

func valueOrDefault(lookup LookupEnv, key, fallback string) string {
	if value := optional(lookup, key); value != "" {
		return value
	}
	return fallback
}

func duration(lookup LookupEnv, key string, fallback time.Duration) (time.Duration, error) {
	value := valueOrDefault(lookup, key, fallback.String())
	parsed, err := time.ParseDuration(value)
	if err != nil || parsed <= 0 {
		return 0, fmt.Errorf("%s must be a positive duration", key)
	}
	return parsed, nil
}

func boolean(lookup LookupEnv, key string, fallback bool) (bool, error) {
	value := valueOrDefault(lookup, key, strconv.FormatBool(fallback))
	parsed, err := strconv.ParseBool(value)
	if err != nil {
		return false, fmt.Errorf("%s must be true or false", key)
	}
	return parsed, nil
}

func parseUpstream(value string) (*url.URL, error) {
	parsed, err := url.Parse(value)
	if err != nil {
		return nil, err
	}
	if parsed.Scheme != "http" && parsed.Scheme != "https" {
		return nil, fmt.Errorf("scheme must be http or https")
	}
	if parsed.Host == "" {
		return nil, fmt.Errorf("host is required")
	}
	if parsed.User != nil || parsed.RawQuery != "" || parsed.ForceQuery || parsed.Fragment != "" || parsed.RawFragment != "" {
		return nil, fmt.Errorf("userinfo, query, and fragment are not allowed")
	}
	host := parsed.Hostname()
	if !trustedUpstreamHost(host) {
		return nil, fmt.Errorf("host must be an explicit loopback, RFC1918, IPv6 ULA/link-local, or Tailscale CGNAT address")
	}
	parsed.Path = strings.TrimSuffix(parsed.Path, "/")
	return parsed, nil
}

// ParsePrivateUpstream validates a tenant runtime origin with the same private
// network policy as the primary upstream.
func ParsePrivateUpstream(value string) (*url.URL, error) {
	return parseUpstream(value)
}

func trustedUpstreamHost(host string) bool {
	if strings.EqualFold(host, "localhost") {
		return true
	}
	addressText := host
	if address, zone, found := strings.Cut(host, "%"); found {
		if zone == "" {
			return false
		}
		addressText = address
	}
	address, err := netip.ParseAddr(addressText)
	if err != nil {
		return false
	}
	address = address.Unmap()
	if address.IsLoopback() || address.IsPrivate() || address.IsLinkLocalUnicast() {
		return true
	}
	return tailscaleCGNATPrefix.Contains(address)
}

func commaSeparated(lookup LookupEnv, key, fallback string) []string {
	value := valueOrDefault(lookup, key, fallback)
	if value == "" {
		return nil
	}

	seen := make(map[string]struct{})
	result := make([]string, 0)
	for part := range strings.SplitSeq(value, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		if _, exists := seen[part]; exists {
			continue
		}
		seen[part] = struct{}{}
		result = append(result, part)
	}
	return result
}

func blockedPaths(lookup LookupEnv) map[string]struct{} {
	paths := commaSeparated(lookup, "ZIGGY_BLOCKED_PATHS", "/webui/bootstrap,/auth/token")
	result := make(map[string]struct{}, len(paths)+1)
	for _, blocked := range paths {
		if !strings.HasPrefix(blocked, "/") {
			blocked = "/" + blocked
		}
		result[path.Clean(blocked)] = struct{}{}
	}
	result["/webui/bootstrap"] = struct{}{}
	return result
}

func parseLogLevel(value string) (slog.Level, error) {
	var level slog.Level
	if err := level.UnmarshalText([]byte(strings.ToLower(strings.TrimSpace(value)))); err != nil {
		return 0, fmt.Errorf("ZIGGY_LOG_LEVEL: %w", err)
	}
	if !slices.Contains([]slog.Level{slog.LevelDebug, slog.LevelInfo, slog.LevelWarn, slog.LevelError}, level) {
		return 0, fmt.Errorf("ZIGGY_LOG_LEVEL: unsupported level")
	}
	return level, nil
}
