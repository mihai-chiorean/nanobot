package telemetry

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"strings"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/metric"
	metricnoop "go.opentelemetry.io/otel/metric/noop"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"
	tracenoop "go.opentelemetry.io/otel/trace/noop"
)

const instrumentationName = "github.com/mihai-chiorean/nanobot/services/ziggy-control"

var (
	durationBuckets = []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300}
	sizeBuckets     = []float64{0, 512, 4 << 10, 16 << 10, 64 << 10, 256 << 10, 1 << 20, 4 << 20, 16 << 20, 64 << 20}
)

type routeContextKey struct{}

// Recorder owns Ziggy's bounded operational telemetry instruments. It never
// receives request paths, identities, headers other than trace context, or bodies.
type Recorder struct {
	tracer     trace.Tracer
	propagator propagation.TextMapPropagator

	httpRequests       metric.Int64Counter
	httpDuration       metric.Float64Histogram
	httpResponseSize   metric.Int64Histogram
	httpActive         metric.Int64UpDownCounter
	bootstrapAttempts  metric.Int64Counter
	upstreamRequests   metric.Int64Counter
	upstreamDuration   metric.Float64Histogram
	upstreamActive     metric.Int64UpDownCounter
	uptime             metric.Float64ObservableGauge
	uptimeRegistration metric.Registration

	enabled  bool
	shutdown func(context.Context) error
}

// Noop returns a concurrency-safe recorder that emits no telemetry.
func Noop() *Recorder {
	recorder, err := newRecorder(
		metricnoop.NewMeterProvider(),
		tracenoop.NewTracerProvider(),
		propagation.TraceContext{},
		time.Now(),
	)
	if err != nil {
		panic(fmt.Sprintf("create no-op telemetry: %v", err))
	}
	return recorder
}

func newRecorder(
	meterProvider metric.MeterProvider,
	tracerProvider trace.TracerProvider,
	propagator propagation.TextMapPropagator,
	started time.Time,
) (*Recorder, error) {
	meter := meterProvider.Meter(instrumentationName)
	recorder := &Recorder{
		tracer:     tracerProvider.Tracer(instrumentationName),
		propagator: propagator,
		shutdown:   func(context.Context) error { return nil },
	}

	var err error
	if recorder.httpRequests, err = meter.Int64Counter(
		"ziggy.control.http.server.requests",
		metric.WithDescription("Completed HTTP server requests."),
		metric.WithUnit("{request}"),
	); err != nil {
		return nil, fmt.Errorf("create HTTP request counter: %w", err)
	}
	if recorder.httpDuration, err = meter.Float64Histogram(
		"ziggy.control.http.server.duration",
		metric.WithDescription("HTTP server request duration."),
		metric.WithUnit("s"),
		metric.WithExplicitBucketBoundaries(durationBuckets...),
	); err != nil {
		return nil, fmt.Errorf("create HTTP duration histogram: %w", err)
	}
	if recorder.httpResponseSize, err = meter.Int64Histogram(
		"ziggy.control.http.server.response.size",
		metric.WithDescription("HTTP response bytes written by ziggy-control."),
		metric.WithUnit("By"),
		metric.WithExplicitBucketBoundaries(sizeBuckets...),
	); err != nil {
		return nil, fmt.Errorf("create HTTP response size histogram: %w", err)
	}
	if recorder.httpActive, err = meter.Int64UpDownCounter(
		"ziggy.control.http.server.active_requests",
		metric.WithDescription("HTTP requests currently handled by ziggy-control."),
		metric.WithUnit("{request}"),
	); err != nil {
		return nil, fmt.Errorf("create active HTTP request counter: %w", err)
	}
	if recorder.bootstrapAttempts, err = meter.Int64Counter(
		"ziggy.control.auth.bootstrap.attempts",
		metric.WithDescription("Bootstrap authentication and authorization outcomes."),
		metric.WithUnit("{attempt}"),
	); err != nil {
		return nil, fmt.Errorf("create bootstrap attempt counter: %w", err)
	}
	if recorder.upstreamRequests, err = meter.Int64Counter(
		"ziggy.control.upstream.requests",
		metric.WithDescription("Completed requests to bounded upstream classes."),
		metric.WithUnit("{request}"),
	); err != nil {
		return nil, fmt.Errorf("create upstream request counter: %w", err)
	}
	if recorder.upstreamDuration, err = meter.Float64Histogram(
		"ziggy.control.upstream.duration",
		metric.WithDescription("Request duration for bounded upstream classes."),
		metric.WithUnit("s"),
		metric.WithExplicitBucketBoundaries(durationBuckets...),
	); err != nil {
		return nil, fmt.Errorf("create upstream duration histogram: %w", err)
	}
	if recorder.upstreamActive, err = meter.Int64UpDownCounter(
		"ziggy.control.upstream.active_requests",
		metric.WithDescription("Requests currently in flight to bounded upstream classes."),
		metric.WithUnit("{request}"),
	); err != nil {
		return nil, fmt.Errorf("create active upstream request counter: %w", err)
	}
	if recorder.uptime, err = meter.Float64ObservableGauge(
		"ziggy.control.service.uptime",
		metric.WithDescription("Seconds since this ziggy-control process initialized telemetry."),
		metric.WithUnit("s"),
	); err != nil {
		return nil, fmt.Errorf("create service uptime gauge: %w", err)
	}
	recorder.uptimeRegistration, err = meter.RegisterCallback(func(_ context.Context, observer metric.Observer) error {
		observer.ObserveFloat64(recorder.uptime, time.Since(started).Seconds())
		return nil
	}, recorder.uptime)
	if err != nil {
		return nil, fmt.Errorf("register service uptime callback: %w", err)
	}

	return recorder, nil
}

func (recorder *Recorder) Enabled() bool {
	return recorder != nil && recorder.enabled
}

// Shutdown flushes exporters within the caller's deadline. Export failures are
// returned for logging but must not change the serving outcome.
func (recorder *Recorder) Shutdown(ctx context.Context) error {
	if recorder == nil {
		return nil
	}
	var unregisterErr error
	if recorder.uptimeRegistration != nil {
		unregisterErr = recorder.uptimeRegistration.Unregister()
	}
	return errors.Join(unregisterErr, recorder.shutdown(ctx))
}

// StartRequest starts a content-free server span and returns a completion
// function. Route and method are reduced to bounded enums before recording.
func (recorder *Recorder) StartRequest(
	ctx context.Context,
	headers http.Header,
	route string,
	method string,
) (context.Context, func(int, int64)) {
	if recorder == nil {
		recorder = Noop()
	}
	route = safeRoute(route)
	method = safeMethod(method)
	ctx = recorder.propagator.Extract(ctx, propagation.HeaderCarrier(headers))
	ctx, span := recorder.tracer.Start(
		ctx,
		"HTTP "+route,
		trace.WithSpanKind(trace.SpanKindServer),
		trace.WithAttributes(
			attribute.String("ziggy.route", route),
			attribute.String("http.request.method", method),
		),
	)
	ctx = context.WithValue(ctx, routeContextKey{}, route)
	activeAttributes := metric.WithAttributes(attribute.String("ziggy.route", route))
	recorder.httpActive.Add(ctx, 1, activeAttributes)
	started := time.Now()

	return ctx, func(status int, responseBytes int64) {
		finishContext := context.WithoutCancel(ctx)
		statusClass := safeStatusClass(status)
		attributes := metric.WithAttributes(
			attribute.String("ziggy.route", route),
			attribute.String("http.request.method", method),
			attribute.String("http.response.status_class", statusClass),
		)
		recorder.httpActive.Add(finishContext, -1, activeAttributes)
		recorder.httpRequests.Add(finishContext, 1, attributes)
		recorder.httpDuration.Record(finishContext, time.Since(started).Seconds(), attributes)
		recorder.httpResponseSize.Record(finishContext, max(responseBytes, 0), attributes)
		if route == "bootstrap" {
			recorder.bootstrapAttempts.Add(finishContext, 1, metric.WithAttributes(
				attribute.String("ziggy.outcome", bootstrapOutcome(status)),
			))
		}

		span.SetAttributes(attribute.Int("http.response.status_code", status))
		if status >= http.StatusInternalServerError {
			span.SetStatus(codes.Error, "server_error")
		}
		span.End()
	}
}

// WrapTransport records a client span and RED metrics without inspecting or
// recording the request URL, headers, body, or response content.
func (recorder *Recorder) WrapTransport(base http.RoundTripper, fixedOperation string) http.RoundTripper {
	if recorder == nil {
		recorder = Noop()
	}
	if base == nil {
		base = http.DefaultTransport
	}
	return roundTripper{base: base, recorder: recorder, fixedOperation: safeOperation(fixedOperation)}
}

type roundTripper struct {
	base           http.RoundTripper
	recorder       *Recorder
	fixedOperation string
}

func (transport roundTripper) RoundTrip(request *http.Request) (*http.Response, error) {
	operation := transport.fixedOperation
	if operation == "dynamic" {
		operation = operationFromContext(request.Context())
	}
	method := safeMethod(request.Method)
	ctx, span := transport.recorder.tracer.Start(
		request.Context(),
		"upstream "+operation,
		trace.WithSpanKind(trace.SpanKindClient),
		trace.WithAttributes(
			attribute.String("ziggy.operation", operation),
			attribute.String("http.request.method", method),
		),
	)
	activeAttributes := metric.WithAttributes(attribute.String("ziggy.operation", operation))
	transport.recorder.upstreamActive.Add(ctx, 1, activeAttributes)
	started := time.Now()

	upstreamRequest := request.Clone(ctx)
	transport.recorder.propagator.Inject(ctx, propagation.HeaderCarrier(upstreamRequest.Header))
	response, err := transport.base.RoundTrip(upstreamRequest)

	finishContext := context.WithoutCancel(ctx)
	status := 0
	if response != nil {
		status = response.StatusCode
	}
	outcome := upstreamOutcome(status, err)
	attributes := metric.WithAttributes(
		attribute.String("ziggy.operation", operation),
		attribute.String("http.response.status_class", safeStatusClass(status)),
		attribute.String("ziggy.outcome", outcome),
	)
	transport.recorder.upstreamActive.Add(finishContext, -1, activeAttributes)
	transport.recorder.upstreamRequests.Add(finishContext, 1, attributes)
	transport.recorder.upstreamDuration.Record(finishContext, time.Since(started).Seconds(), attributes)
	if status != 0 {
		span.SetAttributes(attribute.Int("http.response.status_code", status))
	}
	if err != nil || status >= http.StatusInternalServerError {
		span.SetStatus(codes.Error, outcome)
	}
	span.End()
	return response, err
}

func safeRoute(route string) string {
	switch route {
	case "health", "readiness", "bootstrap", "proxy":
		return route
	default:
		return "other"
	}
}

func safeMethod(method string) string {
	switch strings.ToUpper(strings.TrimSpace(method)) {
	case http.MethodGet:
		return http.MethodGet
	case http.MethodHead:
		return http.MethodHead
	case http.MethodPost:
		return http.MethodPost
	case http.MethodPut:
		return http.MethodPut
	case http.MethodPatch:
		return http.MethodPatch
	case http.MethodDelete:
		return http.MethodDelete
	case http.MethodOptions:
		return http.MethodOptions
	case http.MethodConnect:
		return http.MethodConnect
	case http.MethodTrace:
		return http.MethodTrace
	default:
		return "OTHER"
	}
}

func safeStatusClass(status int) string {
	switch {
	case status >= 100 && status < 200:
		return "1xx"
	case status >= 200 && status < 300:
		return "2xx"
	case status >= 300 && status < 400:
		return "3xx"
	case status >= 400 && status < 500:
		return "4xx"
	case status >= 500 && status < 600:
		return "5xx"
	default:
		return "unset"
	}
}

func bootstrapOutcome(status int) string {
	switch {
	case status >= 200 && status < 400:
		return "authorized"
	case status == http.StatusUnauthorized:
		return "unauthenticated"
	case status == http.StatusForbidden:
		return "denied"
	case status == http.StatusMethodNotAllowed:
		return "invalid_method"
	case status >= 500:
		return "unavailable"
	default:
		return "rejected"
	}
}

func safeOperation(operation string) string {
	switch operation {
	case "identity", "readiness":
		return operation
	case "":
		return "dynamic"
	default:
		return "other"
	}
}

func operationFromContext(ctx context.Context) string {
	route, _ := ctx.Value(routeContextKey{}).(string)
	if route == "bootstrap" {
		return "nanobot_bootstrap"
	}
	return "nanobot_proxy"
}

func upstreamOutcome(status int, err error) string {
	if err != nil {
		switch {
		case errors.Is(err, context.Canceled):
			return "canceled"
		case errors.Is(err, context.DeadlineExceeded):
			return "timeout"
		}
		var networkError net.Error
		if errors.As(err, &networkError) && networkError.Timeout() {
			return "timeout"
		}
		return "network_error"
	}
	switch {
	case status >= 200 && status < 400:
		return "success"
	case status >= 400 && status < 500:
		return "http_4xx"
	case status >= 500:
		return "http_5xx"
	default:
		return "invalid_response"
	}
}
