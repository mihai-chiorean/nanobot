package telemetry

import (
	"context"
	"log/slog"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/metric"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	oteltrace "go.opentelemetry.io/otel/trace"
)

type Telemetry interface {
	Start(context.Context, string) (context.Context, oteltrace.Span)
	HTTP(context.Context, string, string, int, time.Duration)
	Queue(context.Context, string, time.Duration, error)
	Execution(context.Context, string, time.Duration, error)
	Transition(context.Context, string, string)
	Retry(context.Context, string)
	Cancel(context.Context, string)
	Dependency(context.Context, string, bool)
	Tracer() oteltrace.Tracer
	Shutdown(context.Context) error
}

type noop struct{}

func Noop() Telemetry { return noop{} }

func (noop) Start(ctx context.Context, name string) (context.Context, oteltrace.Span) {
	return ctx, oteltrace.SpanFromContext(ctx)
}
func (noop) HTTP(context.Context, string, string, int, time.Duration) {}
func (noop) Queue(context.Context, string, time.Duration, error)      {}
func (noop) Execution(context.Context, string, time.Duration, error)  {}
func (noop) Transition(context.Context, string, string)               {}
func (noop) Retry(context.Context, string)                            {}
func (noop) Cancel(context.Context, string)                           {}
func (noop) Dependency(context.Context, string, bool)                 {}
func (noop) Tracer() oteltrace.Tracer                                 { return otel.Tracer("ziggy-work") }
func (noop) Shutdown(context.Context) error                           { return nil }

type real struct {
	tracer      oteltrace.Tracer
	http        metric.Float64Histogram
	queue       metric.Float64Histogram
	execution   metric.Float64Histogram
	outcomes    metric.Int64Counter
	transitions metric.Int64Counter
	retries     metric.Int64Counter
	cancels     metric.Int64Counter
	deps        metric.Int64Gauge
	tp          *sdktrace.TracerProvider
	mp          *sdkmetric.MeterProvider
}

func New(ctx context.Context, endpoint, version string, logger *slog.Logger) (Telemetry, error) {
	if endpoint == "" {
		return noop{}, nil
	}
	to, err := otlptracehttp.New(ctx, otlptracehttp.WithEndpoint(endpoint), otlptracehttp.WithInsecure())
	if err != nil {
		return nil, err
	}
	mo, err := otlpmetrichttp.New(ctx, otlpmetrichttp.WithEndpoint(endpoint), otlpmetrichttp.WithInsecure())
	if err != nil {
		return nil, err
	}
	tp := sdktrace.NewTracerProvider(sdktrace.WithBatcher(to), sdktrace.WithResource(telemetryResource(version)))
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(sdkmetric.NewPeriodicReader(mo, sdkmetric.WithInterval(15*time.Second))))
	tr := tp.Tracer("github.com/mihai-chiorean/nanobot/services/ziggy-work")
	m := mp.Meter("github.com/mihai-chiorean/nanobot/services/ziggy-work")
	newHist := func(name string) metric.Float64Histogram { h, _ := m.Float64Histogram(name); return h }
	newCounter := func(name string) metric.Int64Counter { c, _ := m.Int64Counter(name); return c }
	g, _ := m.Int64Gauge("ziggy.work.dependency.healthy")
	if logger != nil {
		logger.Info("telemetry enabled", "collector", "configured")
	}
	return &real{tracer: tr, http: newHist("ziggy.work.http.duration"), queue: newHist("ziggy.work.queue.duration"), execution: newHist("ziggy.work.execution.duration"), outcomes: newCounter("ziggy.work.outcome"), transitions: newCounter("ziggy.work.status_transition"), retries: newCounter("ziggy.work.retry"), cancels: newCounter("ziggy.work.cancel"), deps: g, tp: tp, mp: mp}, nil
}

func telemetryResource(version string) *resource.Resource {
	return resource.NewSchemaless(attribute.String("service.name", "ziggy-work"), attribute.String("service.version", safe(version)))
}

func (t *real) Start(ctx context.Context, name string) (context.Context, oteltrace.Span) {
	return t.tracer.Start(ctx, safe(name))
}
func safe(s string) string {
	if len(s) > 64 {
		return s[:64]
	}
	return s
}
func attrs(kind, outcome string) []attribute.KeyValue {
	return []attribute.KeyValue{attribute.String("operation", safe(kind)), attribute.String("outcome", safe(outcome))}
}
func outcome(err error) string {
	if err == nil {
		return "success"
	}
	return "error"
}
func (t *real) HTTP(ctx context.Context, method, route string, status int, d time.Duration) {
	t.http.Record(ctx, d.Seconds(), metric.WithAttributes(attribute.String("method", safe(method)), attribute.Int("status", status), attribute.String("route", safe(route))))
}
func (t *real) Queue(ctx context.Context, operation string, d time.Duration, err error) {
	t.queue.Record(ctx, d.Seconds(), metric.WithAttributes(attrs(operation, outcome(err))...))
	t.outcomes.Add(ctx, 1, metric.WithAttributes(attrs("queue", outcome(err))...))
}
func (t *real) Execution(ctx context.Context, operation string, d time.Duration, err error) {
	t.execution.Record(ctx, d.Seconds(), metric.WithAttributes(attrs(operation, outcome(err))...))
	t.outcomes.Add(ctx, 1, metric.WithAttributes(attrs("execution", outcome(err))...))
}
func (t *real) Transition(ctx context.Context, from, to string) {
	t.transitions.Add(ctx, 1, metric.WithAttributes(attribute.String("from", safe(from)), attribute.String("to", safe(to))))
}
func (t *real) Retry(ctx context.Context, operation string) {
	t.retries.Add(ctx, 1, metric.WithAttributes(attribute.String("operation", safe(operation))))
}
func (t *real) Cancel(ctx context.Context, operation string) {
	t.cancels.Add(ctx, 1, metric.WithAttributes(attribute.String("operation", safe(operation))))
}
func (t *real) Dependency(ctx context.Context, dependency string, healthy bool) {
	n := int64(0)
	if healthy {
		n = 1
	}
	t.deps.Record(ctx, n, metric.WithAttributes(attribute.String("dependency", safe(dependency))))
}
func (t *real) Tracer() oteltrace.Tracer { return t.tracer }
func (t *real) Shutdown(ctx context.Context) error {
	if err := t.mp.Shutdown(ctx); err != nil {
		return err
	}
	return t.tp.Shutdown(ctx)
}
