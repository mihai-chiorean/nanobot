package config

import (
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"os"
	"strconv"
	"strings"
	"time"
)

type Config struct {
	Environment       string
	Version           string
	ListenAddr        string
	ShutdownTimeout   time.Duration
	RequestTimeout    time.Duration
	UpstreamTimeout   time.Duration
	DatabaseURL       string
	PrincipalKey      []byte
	TenantManifest    string
	ArtifactRoot      string
	QueueName         string
	QueueWorkers      int
	QueueCapacity     int
	StreamLimit       int
	TenantStreamLimit int
	BodyLimit         int64
	EventLimit        int64
	OTLPEndpoint      string
	ReconcileInterval time.Duration
}

type LookupEnv func(string) (string, bool)
type ReadFile func(string) ([]byte, error)

func Load() (Config, error) { return LoadFrom(os.LookupEnv, os.ReadFile) }

func LoadFrom(lookup LookupEnv, readFile ReadFile) (Config, error) {
	env := strings.ToLower(value(lookup, "ZIGGY_WORK_ENV", "development"))
	if env != "development" && env != "test" && env != "staging" && env != "production" {
		return Config{}, errors.New("ZIGGY_WORK_ENV must be development, test, staging, or production")
	}
	key, err := secretBytes(lookup, readFile, "ZIGGY_WORK_TRUST_KEY", 32, env == "test")
	if err != nil {
		return Config{}, err
	}
	db, err := secretText(lookup, readFile, "ZIGGY_WORK_DATABASE_URL", env != "production")
	if err != nil {
		return Config{}, err
	}
	manifest := strings.TrimSpace(value(lookup, "ZIGGY_WORK_TENANTS_FILE", ""))
	if env == "production" && manifest == "" {
		return Config{}, errors.New("ZIGGY_WORK_TENANTS_FILE is required in production")
	}
	shutdownTimeout, err := duration(lookup, "ZIGGY_WORK_SHUTDOWN_TIMEOUT", 10*time.Second, true)
	if err != nil {
		return Config{}, err
	}
	requestTimeout, err := duration(lookup, "ZIGGY_WORK_REQUEST_TIMEOUT", 15*time.Second, true)
	if err != nil {
		return Config{}, err
	}
	upstreamTimeout, err := duration(lookup, "ZIGGY_WORK_UPSTREAM_TIMEOUT", 20*time.Second, true)
	if err != nil {
		return Config{}, err
	}
	reconcileInterval, err := duration(lookup, "ZIGGY_WORK_RECONCILE_INTERVAL", 0, false)
	if err != nil {
		return Config{}, err
	}
	workers, err := integer(lookup, "ZIGGY_WORK_QUEUE_WORKERS", 2, 1, 100)
	if err != nil {
		return Config{}, err
	}
	capacity, err := integer(lookup, "ZIGGY_WORK_QUEUE_CAPACITY", 256, 1, 10000)
	if err != nil {
		return Config{}, err
	}
	streamLimit, err := integer(lookup, "ZIGGY_WORK_STREAM_LIMIT", 100, 1, 10000)
	if err != nil {
		return Config{}, err
	}
	tenantStreamLimit, err := integer(lookup, "ZIGGY_WORK_TENANT_STREAM_LIMIT", 4, 1, 1000)
	if err != nil {
		return Config{}, err
	}
	if tenantStreamLimit > streamLimit {
		return Config{}, errors.New("ZIGGY_WORK_TENANT_STREAM_LIMIT cannot exceed ZIGGY_WORK_STREAM_LIMIT")
	}
	bodyLimit, err := integer(lookup, "ZIGGY_WORK_BODY_LIMIT", 1<<20, 1024, 10<<20)
	if err != nil {
		return Config{}, err
	}
	eventLimit, err := integer(lookup, "ZIGGY_WORK_EVENT_LIMIT", 256<<10, 1024, 4<<20)
	if err != nil {
		return Config{}, err
	}
	c := Config{
		Environment:       env,
		Version:           value(lookup, "ZIGGY_WORK_VERSION", "dev"),
		ListenAddr:        value(lookup, "ZIGGY_WORK_LISTEN_ADDR", "127.0.0.1:8791"),
		ShutdownTimeout:   shutdownTimeout,
		RequestTimeout:    requestTimeout,
		UpstreamTimeout:   upstreamTimeout,
		DatabaseURL:       db,
		PrincipalKey:      key,
		TenantManifest:    manifest,
		ArtifactRoot:      strings.TrimSpace(value(lookup, "ZIGGY_WORK_ARTIFACT_ROOT", "")),
		QueueName:         value(lookup, "ZIGGY_WORK_QUEUE", "ziggy_work_low"),
		QueueWorkers:      workers,
		QueueCapacity:     capacity,
		StreamLimit:       streamLimit,
		TenantStreamLimit: tenantStreamLimit,
		BodyLimit:         int64(bodyLimit),
		EventLimit:        int64(eventLimit),
		OTLPEndpoint:      strings.TrimSpace(value(lookup, "ZIGGY_WORK_OTLP_HTTP_ENDPOINT", "")),
		ReconcileInterval: reconcileInterval,
	}
	if _, _, err := net.SplitHostPort(c.ListenAddr); err != nil {
		return Config{}, fmt.Errorf("ZIGGY_WORK_LISTEN_ADDR: %w", err)
	}
	if c.OTLPEndpoint != "" && !loopbackEndpoint(c.OTLPEndpoint) {
		return Config{}, errors.New("ZIGGY_WORK_OTLP_HTTP_ENDPOINT must target a loopback collector")
	}
	return c, nil
}

func value(lookup LookupEnv, name, fallback string) string {
	if v, ok := lookup(name); ok && strings.TrimSpace(v) != "" {
		return v
	}
	return fallback
}

func duration(lookup LookupEnv, name string, fallback time.Duration, positive bool) (time.Duration, error) {
	raw := strings.TrimSpace(value(lookup, name, ""))
	if raw == "" {
		return fallback, nil
	}
	d, err := time.ParseDuration(raw)
	if err != nil {
		return 0, fmt.Errorf("%s must be a valid duration: %w", name, err)
	}
	if positive && d <= 0 {
		return 0, fmt.Errorf("%s must be a positive duration", name)
	}
	if !positive && d < 0 {
		return 0, fmt.Errorf("%s must be zero or a positive duration", name)
	}
	return d, nil
}

func integer(lookup LookupEnv, name string, fallback, min, max int) (int, error) {
	raw := strings.TrimSpace(value(lookup, name, ""))
	if raw == "" {
		return fallback, nil
	}
	n, err := strconv.Atoi(raw)
	if err != nil {
		return 0, fmt.Errorf("%s must be an integer between %d and %d: %w", name, min, max, err)
	}
	if n < min || n > max {
		return 0, fmt.Errorf("%s must be an integer between %d and %d", name, min, max)
	}
	return n, nil
}

func secretText(lookup LookupEnv, readFile ReadFile, name string, optional bool) (string, error) {
	inline, file := strings.TrimSpace(value(lookup, name, "")), strings.TrimSpace(value(lookup, name+"_FILE", ""))
	if inline != "" && file != "" {
		return "", fmt.Errorf("set only one of %s or %s", name, name+"_FILE")
	}
	if inline != "" {
		return inline, nil
	}
	if file == "" {
		if optional {
			return "", nil
		}
		return "", fmt.Errorf("%s_FILE is required", name)
	}
	b, err := readFile(file)
	if err != nil {
		return "", fmt.Errorf("%s_FILE: %w", name, err)
	}
	v := strings.TrimSpace(string(b))
	if v == "" {
		return "", fmt.Errorf("%s_FILE is empty", name)
	}
	return v, nil
}

func secretBytes(lookup LookupEnv, readFile ReadFile, name string, minimum int, optional bool) ([]byte, error) {
	v, err := secretText(lookup, readFile, name, optional)
	if err != nil {
		return nil, err
	}
	if v == "" {
		return nil, nil
	}
	for _, decode := range []func(string) ([]byte, error){base64.StdEncoding.DecodeString, base64.RawStdEncoding.DecodeString, hex.DecodeString} {
		if decoded, e := decode(v); e == nil && len(decoded) >= minimum {
			return decoded, nil
		}
	}
	if len(v) < minimum {
		return nil, fmt.Errorf("%s must contain at least %d bytes", name, minimum)
	}
	return []byte(v), nil
}

func loopbackEndpoint(raw string) bool {
	host := raw
	if strings.Contains(raw, "://") {
		parts := strings.SplitN(raw, "://", 2)
		host = parts[1]
	}
	host = strings.TrimSuffix(host, "/")
	if h, _, err := net.SplitHostPort(host); err == nil {
		host = h
	} else {
		host = strings.Split(host, ":")[0]
	}
	if strings.EqualFold(host, "localhost") {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}
