package telemetry

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"net"
	"net/url"
	"os"
	"strings"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/propagation"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

const (
	metricExportInterval = 30 * time.Second
	exportTimeout        = 3 * time.Second
	traceBatchTimeout    = 5 * time.Second
	traceQueueSize       = 256
	traceBatchSize       = 128
	maxCredentialBytes   = 4 << 10
)

type Config struct {
	Endpoint              string
	AuthorizationFile     string
	ServiceVersion        string
	DeploymentEnvironment string
	TraceSampleRatio      float64
}

// New creates OTLP/HTTP providers only when both the endpoint and local
// collector credential are configured. The endpoint is deliberately restricted
// to loopback so the local collector remains the only export boundary.
func New(ctx context.Context, config Config) (*Recorder, error) {
	return newWithCredentialReader(ctx, config, readCredentialFile)
}

type credentialReader func(string) ([]byte, error)

func newWithCredentialReader(ctx context.Context, config Config, readCredential credentialReader) (*Recorder, error) {
	endpointConfigured := strings.TrimSpace(config.Endpoint) != ""
	authConfigured := strings.TrimSpace(config.AuthorizationFile) != ""
	if !endpointConfigured && !authConfigured {
		return Noop(), nil
	}
	if !endpointConfigured || !authConfigured {
		return nil, errors.New("telemetry endpoint and authorization file must both be configured")
	}
	endpoint, err := loopbackEndpoint(config.Endpoint)
	if err != nil {
		return nil, err
	}
	credential, err := readCredential(config.AuthorizationFile)
	if err != nil {
		return nil, errors.New("read telemetry authorization credential")
	}
	authorization, err := basicAuthorization(credential)
	if err != nil {
		return nil, err
	}
	if config.TraceSampleRatio < 0 || config.TraceSampleRatio > 1 {
		return nil, errors.New("trace sample ratio must be between 0 and 1")
	}

	res := resource.NewSchemaless(
		attribute.String("service.name", "ziggy-control"),
		attribute.String("service.namespace", "ziggy"),
		attribute.String("service.version", config.ServiceVersion),
		attribute.String("deployment.environment", config.DeploymentEnvironment),
	)
	metricExporter, err := otlpmetrichttp.New(ctx,
		otlpmetrichttp.WithEndpointURL(endpoint),
		otlpmetrichttp.WithHeaders(map[string]string{"Authorization": authorization}),
		otlpmetrichttp.WithTimeout(exportTimeout),
		otlpmetrichttp.WithRetry(otlpmetrichttp.RetryConfig{Enabled: false}),
	)
	if err != nil {
		return nil, fmt.Errorf("create metric exporter: %w", err)
	}
	traceExporter, err := otlptracehttp.New(ctx,
		otlptracehttp.WithEndpointURL(endpoint),
		otlptracehttp.WithHeaders(map[string]string{"Authorization": authorization}),
		otlptracehttp.WithTimeout(exportTimeout),
		otlptracehttp.WithRetry(otlptracehttp.RetryConfig{Enabled: false}),
	)
	if err != nil {
		_ = metricExporter.Shutdown(ctx)
		return nil, fmt.Errorf("create trace exporter: %w", err)
	}

	meterProvider := sdkmetric.NewMeterProvider(
		sdkmetric.WithResource(res),
		sdkmetric.WithReader(sdkmetric.NewPeriodicReader(
			metricExporter,
			sdkmetric.WithInterval(metricExportInterval),
			sdkmetric.WithTimeout(exportTimeout),
		)),
	)
	tracerProvider := sdktrace.NewTracerProvider(
		sdktrace.WithResource(res),
		sdktrace.WithSampler(controlPlaneSampler(config.TraceSampleRatio)),
		sdktrace.WithSpanProcessor(sdktrace.NewBatchSpanProcessor(
			traceExporter,
			sdktrace.WithMaxQueueSize(traceQueueSize),
			sdktrace.WithMaxExportBatchSize(traceBatchSize),
			sdktrace.WithBatchTimeout(traceBatchTimeout),
			sdktrace.WithExportTimeout(exportTimeout),
		)),
	)

	recorder, err := newRecorder(meterProvider, tracerProvider, propagation.TraceContext{}, time.Now())
	if err != nil {
		_ = meterProvider.Shutdown(ctx)
		_ = tracerProvider.Shutdown(ctx)
		return nil, err
	}
	recorder.enabled = true
	recorder.shutdown = func(ctx context.Context) error {
		return errors.Join(tracerProvider.Shutdown(ctx), meterProvider.Shutdown(ctx))
	}
	return recorder, nil
}

func controlPlaneSampler(ratio float64) sdktrace.Sampler {
	local := sdktrace.TraceIDRatioBased(ratio)
	return sdktrace.ParentBased(
		local,
		sdktrace.WithRemoteParentSampled(local),
		sdktrace.WithRemoteParentNotSampled(local),
	)
}

func readCredentialFile(filename string) ([]byte, error) {
	file, err := os.Open(filename)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	contents, err := io.ReadAll(io.LimitReader(file, maxCredentialBytes+1))
	if err != nil {
		return nil, err
	}
	if len(contents) > maxCredentialBytes {
		return nil, errors.New("credential exceeds size limit")
	}
	return contents, nil
}

func basicAuthorization(contents []byte) (string, error) {
	if len(contents) == 0 || len(contents) > maxCredentialBytes {
		return "", errors.New("telemetry authorization credential is invalid")
	}
	value := strings.TrimSpace(string(contents))
	if value == "" || strings.ContainsAny(value, "\r\n\x00") {
		return "", errors.New("telemetry authorization credential is invalid")
	}

	if encoded, found := strings.CutPrefix(value, "Basic "); found {
		decoded, err := base64.StdEncoding.DecodeString(encoded)
		if err != nil || !validUserPassword(string(decoded)) {
			return "", errors.New("telemetry authorization credential is invalid")
		}
		return "Basic " + encoded, nil
	}
	if !validUserPassword(value) {
		return "", errors.New("telemetry authorization credential is invalid")
	}
	return "Basic " + base64.StdEncoding.EncodeToString([]byte(value)), nil
}

func validUserPassword(value string) bool {
	username, password, found := strings.Cut(value, ":")
	if !found || username == "" || password == "" || strings.ContainsAny(value, "\r\n\x00") {
		return false
	}
	for _, character := range value {
		if character < 0x20 || character > 0x7e {
			return false
		}
	}
	return true
}

func loopbackEndpoint(raw string) (string, error) {
	endpoint, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || endpoint.Scheme == "" || endpoint.Host == "" {
		return "", errors.New("telemetry endpoint must be an absolute HTTP URL")
	}
	if endpoint.Scheme != "http" && endpoint.Scheme != "https" {
		return "", errors.New("telemetry endpoint scheme must be http or https")
	}
	if endpoint.User != nil || endpoint.RawQuery != "" || endpoint.Fragment != "" {
		return "", errors.New("telemetry endpoint must not contain credentials, query, or fragment")
	}
	if endpoint.Path != "" && endpoint.Path != "/" {
		return "", errors.New("telemetry endpoint must not contain a path")
	}
	host := endpoint.Hostname()
	address := net.ParseIP(host)
	if !strings.EqualFold(host, "localhost") && (address == nil || !address.IsLoopback()) {
		return "", errors.New("telemetry endpoint must resolve explicitly to loopback")
	}
	endpoint.Path = ""
	return strings.TrimSuffix(endpoint.String(), "/"), nil
}
