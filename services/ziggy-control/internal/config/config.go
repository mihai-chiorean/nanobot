package config

import (
	"fmt"
	"log/slog"
	"net"
	"net/url"
	"os"
	"path"
	"slices"
	"strings"
	"time"
)

const (
	defaultListenAddr       = "127.0.0.1:8787"
	defaultShutdownTimeout  = 10 * time.Second
	defaultReadinessTimeout = 2 * time.Second
)

type Config struct {
	ListenAddr        string
	UpstreamURL       *url.URL
	OwnerEmail        string
	OwnerSubject      string
	ClerkSecretKey    string
	AuthorizedParties []string
	BlockedPaths      map[string]struct{}
	ShutdownTimeout   time.Duration
	ReadinessTimeout  time.Duration
	LogLevel          slog.Level
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

	ownerEmail, err := required(lookup, "ZIGGY_OWNER_EMAIL")
	if err != nil {
		return Config{}, err
	}
	ownerEmail = strings.ToLower(strings.TrimSpace(ownerEmail))
	if !strings.Contains(ownerEmail, "@") {
		return Config{}, fmt.Errorf("ZIGGY_OWNER_EMAIL: invalid email address")
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
	logLevel, err := parseLogLevel(valueOrDefault(lookup, "ZIGGY_LOG_LEVEL", "info"))
	if err != nil {
		return Config{}, err
	}

	return Config{
		ListenAddr:        listenAddr,
		UpstreamURL:       upstream,
		OwnerEmail:        ownerEmail,
		OwnerSubject:      optional(lookup, "ZIGGY_OWNER_SUBJECT"),
		ClerkSecretKey:    secret,
		AuthorizedParties: commaSeparated(lookup, "ZIGGY_AUTHORIZED_PARTIES", ""),
		BlockedPaths:      blockedPaths(lookup),
		ShutdownTimeout:   shutdownTimeout,
		ReadinessTimeout:  readinessTimeout,
		LogLevel:          logLevel,
	}, nil
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
	if parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return nil, fmt.Errorf("userinfo, query, and fragment are not allowed")
	}
	parsed.Path = strings.TrimSuffix(parsed.Path, "/")
	return parsed, nil
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
