package telemetry

import (
	"context"
	"encoding/base64"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/propagation"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (roundTrip roundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return roundTrip(request)
}

func TestLoopbackEndpoint(t *testing.T) {
	tests := []struct {
		name    string
		value   string
		want    string
		wantErr bool
	}{
		{name: "IPv4", value: "http://127.0.0.1:4318/", want: "http://127.0.0.1:4318"},
		{name: "IPv6", value: "http://[::1]:4318", want: "http://[::1]:4318"},
		{name: "localhost TLS", value: "https://localhost:4318", want: "https://localhost:4318"},
		{name: "remote", value: "https://otlp.example.com", wantErr: true},
		{name: "credentials", value: "http://user:pass@127.0.0.1:4318", wantErr: true},
		{name: "path", value: "http://127.0.0.1:4318/v1/metrics", wantErr: true},
		{name: "missing scheme", value: "127.0.0.1:4318", wantErr: true},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			got, err := loopbackEndpoint(test.value)
			if (err != nil) != test.wantErr {
				t.Fatalf("loopbackEndpoint() error = %v, wantErr %v", err, test.wantErr)
			}
			if got != test.want {
				t.Errorf("loopbackEndpoint() = %q, want %q", got, test.want)
			}
		})
	}
}

func TestBasicAuthorization(t *testing.T) {
	encoded := base64.StdEncoding.EncodeToString([]byte("ziggy-control:local-password"))
	tests := []struct {
		name    string
		value   string
		want    string
		wantErr bool
	}{
		{name: "user password", value: "ziggy-control:local-password\n", want: "Basic " + encoded},
		{name: "Basic value", value: "Basic " + encoded, want: "Basic " + encoded},
		{name: "Bearer rejected", value: "Bearer secret-canary", wantErr: true},
		{name: "missing password", value: "collector:", wantErr: true},
		{name: "multiline", value: "collector:password\nsecret-canary", wantErr: true},
		{name: "empty", value: "", wantErr: true},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			got, err := basicAuthorization([]byte(test.value))
			if (err != nil) != test.wantErr {
				t.Fatalf("basicAuthorization() error = %v, wantErr %v", err, test.wantErr)
			}
			if got != test.want {
				t.Errorf("basicAuthorization() = %q, want %q", got, test.want)
			}
			if err != nil && strings.Contains(err.Error(), "secret-canary") {
				t.Errorf("error exposes credential: %v", err)
			}
		})
	}

	if _, err := basicAuthorization(make([]byte, maxCredentialBytes+1)); err == nil {
		t.Fatal("oversized credential accepted")
	}
}

func TestRemoteSampledParentCannotOverrideLocalRatio(t *testing.T) {
	remoteParent := trace.NewSpanContext(trace.SpanContextConfig{
		TraceID:    trace.TraceID{1},
		SpanID:     trace.SpanID{1},
		TraceFlags: trace.FlagsSampled,
		Remote:     true,
	})
	result := controlPlaneSampler(0).ShouldSample(sdktrace.SamplingParameters{
		ParentContext: trace.ContextWithRemoteSpanContext(context.Background(), remoteParent),
		TraceID:       trace.TraceID{2},
		Name:          "HTTP proxy",
		Kind:          trace.SpanKindServer,
	})
	if result.Decision != sdktrace.Drop {
		t.Fatalf("remote sampled parent decision = %v, want drop at local ratio zero", result.Decision)
	}
}

func TestNewRequiresEndpointAndCredentialWithoutExposingReadErrors(t *testing.T) {
	tests := []struct {
		name   string
		config Config
		read   credentialReader
	}{
		{
			name:   "endpoint only",
			config: Config{Endpoint: "http://127.0.0.1:4318", TraceSampleRatio: 1},
			read:   func(string) ([]byte, error) { return nil, errors.New("unexpected") },
		},
		{
			name:   "credential only",
			config: Config{AuthorizationFile: "/run/credentials/otel", TraceSampleRatio: 1},
			read:   func(string) ([]byte, error) { return nil, errors.New("unexpected") },
		},
		{
			name: "read failure",
			config: Config{
				Endpoint:          "http://127.0.0.1:4318",
				AuthorizationFile: "/run/credentials/otel",
				TraceSampleRatio:  1,
			},
			read: func(string) ([]byte, error) { return nil, errors.New("secret-canary") },
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			_, err := newWithCredentialReader(context.Background(), test.config, test.read)
			if err == nil {
				t.Fatal("newWithCredentialReader() error = nil")
			}
			containsFilename := test.config.AuthorizationFile != "" && strings.Contains(err.Error(), test.config.AuthorizationFile)
			if strings.Contains(err.Error(), "secret-canary") || containsFilename {
				t.Errorf("error exposes credential input: %v", err)
			}
		})
	}
}

func TestExporterUsesCredentialForMetricsAndTraces(t *testing.T) {
	authorizations := make(chan string, 4)
	collector := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		authorizations <- r.Header.Get("Authorization")
		w.WriteHeader(http.StatusOK)
	}))
	defer collector.Close()

	recorder, err := newWithCredentialReader(context.Background(), Config{
		Endpoint:              collector.URL,
		AuthorizationFile:     "/run/credentials/otel",
		ServiceVersion:        "test",
		DeploymentEnvironment: "test",
		TraceSampleRatio:      1,
	}, func(string) ([]byte, error) {
		return []byte("ziggy-control:local-password"), nil
	})
	if err != nil {
		t.Fatalf("newWithCredentialReader() error = %v", err)
	}
	ctx, finish := recorder.StartRequest(context.Background(), nil, "health", http.MethodGet)
	_ = ctx
	finish(http.StatusOK, 1)
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := recorder.Shutdown(shutdownCtx); err != nil {
		t.Fatalf("Shutdown() error = %v", err)
	}

	want := "Basic " + base64.StdEncoding.EncodeToString([]byte("ziggy-control:local-password"))
	for range 2 {
		select {
		case got := <-authorizations:
			if got != want {
				t.Errorf("Authorization = %q, want normalized Basic value", got)
			}
		case <-time.After(5 * time.Second):
			t.Fatal("timed out waiting for OTLP export")
		}
	}
}

func TestRecorderEmitsBoundedMetricsAndContentFreeSpans(t *testing.T) {
	recorder, reader, spans := newTestRecorder(t)

	headers := http.Header{
		"Authorization": []string{"Bearer secret-token"},
		"Cookie":        []string{"session=secret-cookie"},
		"Baggage":       []string{"tenant.id=tenant-secret"},
		"Traceparent":   []string{"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"},
	}
	ctx, finish := recorder.StartRequest(context.Background(), headers, "bootstrap", http.MethodGet)

	request, err := http.NewRequestWithContext(
		ctx,
		http.MethodGet,
		"http://nanobot.local/users/user-secret/conversations/conversation-secret?token=secret-token",
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Authorization", "Bearer upstream-secret")
	response, err := recorder.WrapTransport(roundTripFunc(func(outgoing *http.Request) (*http.Response, error) {
		if outgoing.Header.Get("Traceparent") == "" {
			t.Error("outgoing trace context was not propagated")
		}
		return &http.Response{
			StatusCode: http.StatusBadGateway,
			Header:     make(http.Header),
			Body:       io.NopCloser(strings.NewReader("private response content")),
			Request:    outgoing,
		}, nil
	}), "").RoundTrip(request)
	if err != nil {
		t.Fatal(err)
	}
	_ = response.Body.Close()
	finish(http.StatusForbidden, 37)

	var metrics metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &metrics); err != nil {
		t.Fatalf("collect metrics: %v", err)
	}
	requestCounter := findMetric(t, metrics, "ziggy.control.http.server.requests")
	requestSum, ok := requestCounter.Data.(metricdata.Sum[int64])
	if !ok || len(requestSum.DataPoints) != 1 || requestSum.DataPoints[0].Value != 1 {
		t.Fatalf("request counter = %#v", requestCounter.Data)
	}
	assertAttributeSet(t, requestSum.DataPoints[0].Attributes, map[string]string{
		"http.request.method":        http.MethodGet,
		"http.response.status_class": "4xx",
		"ziggy.route":                "bootstrap",
	})

	authCounter := findMetric(t, metrics, "ziggy.control.auth.bootstrap.attempts")
	authSum := authCounter.Data.(metricdata.Sum[int64])
	assertAttributeSet(t, authSum.DataPoints[0].Attributes, map[string]string{"ziggy.outcome": "denied"})

	upstreamCounter := findMetric(t, metrics, "ziggy.control.upstream.requests")
	upstreamSum := upstreamCounter.Data.(metricdata.Sum[int64])
	assertAttributeSet(t, upstreamSum.DataPoints[0].Attributes, map[string]string{
		"http.response.status_class": "5xx",
		"ziggy.operation":            "nanobot_bootstrap",
		"ziggy.outcome":              "http_5xx",
	})

	ended := spans.Ended()
	if len(ended) != 2 {
		t.Fatalf("ended spans = %d, want 2", len(ended))
	}
	for _, span := range ended {
		if len(span.Events()) != 0 || len(span.Links()) != 0 {
			t.Errorf("span %q contains events or links", span.Name())
		}
		serialized := span.Name()
		for _, item := range span.Attributes() {
			serialized += " " + string(item.Key) + "=" + item.Value.Emit()
		}
		for _, forbidden := range []string{
			"secret-token", "secret-cookie", "tenant-secret", "user-secret",
			"conversation-secret", "nanobot.local", "private response content",
			"url.", "http.request.header", "http.response.header", "enduser.", "user.id",
		} {
			if strings.Contains(serialized, forbidden) {
				t.Errorf("span %q contains forbidden telemetry value %q: %s", span.Name(), forbidden, serialized)
			}
		}
	}
}

func TestRecorderReducesUntrustedDimensions(t *testing.T) {
	recorder, reader, _ := newTestRecorder(t)
	_, finish := recorder.StartRequest(context.Background(), nil, "/tenants/tenant-secret", "CUSTOM-tenant-secret")
	finish(999, 0)

	var metrics metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &metrics); err != nil {
		t.Fatal(err)
	}
	requestSum := findMetric(t, metrics, "ziggy.control.http.server.requests").Data.(metricdata.Sum[int64])
	assertAttributeSet(t, requestSum.DataPoints[0].Attributes, map[string]string{
		"http.request.method":        "OTHER",
		"http.response.status_class": "unset",
		"ziggy.route":                "other",
	})
}

func newTestRecorder(t *testing.T) (*Recorder, *sdkmetric.ManualReader, *tracetest.SpanRecorder) {
	t.Helper()
	reader := sdkmetric.NewManualReader()
	meterProvider := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	spanRecorder := tracetest.NewSpanRecorder()
	tracerProvider := sdktrace.NewTracerProvider(
		sdktrace.WithSampler(sdktrace.AlwaysSample()),
		sdktrace.WithSpanProcessor(spanRecorder),
	)
	recorder, err := newRecorder(
		meterProvider,
		tracerProvider,
		propagation.TraceContext{},
		time.Now().Add(-time.Second),
	)
	if err != nil {
		t.Fatalf("newRecorder() error = %v", err)
	}
	t.Cleanup(func() {
		_ = recorder.Shutdown(context.Background())
		_ = meterProvider.Shutdown(context.Background())
		_ = tracerProvider.Shutdown(context.Background())
	})
	return recorder, reader, spanRecorder
}

func findMetric(t *testing.T, metrics metricdata.ResourceMetrics, name string) metricdata.Metrics {
	t.Helper()
	for _, scope := range metrics.ScopeMetrics {
		for _, candidate := range scope.Metrics {
			if candidate.Name == name {
				return candidate
			}
		}
	}
	t.Fatalf("metric %q not found", name)
	return metricdata.Metrics{}
}

func assertAttributeSet(t *testing.T, got attribute.Set, want map[string]string) {
	t.Helper()
	attributes := got.ToSlice()
	if len(attributes) != len(want) {
		t.Fatalf("attributes = %v, want exactly %v", attributes, want)
	}
	for _, item := range attributes {
		value, ok := want[string(item.Key)]
		if !ok || item.Value.AsString() != value {
			t.Errorf("attribute %q = %q, want %q (present %v)", item.Key, item.Value.AsString(), value, ok)
		}
	}
}
