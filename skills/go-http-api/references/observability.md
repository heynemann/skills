# Observability: Prometheus metrics, OpenTelemetry traces, log correlation

- Metrics: `prometheus/client_golang` on a private registry served by the admin
  port at `/metrics`.
- Traces: OpenTelemetry SDK, exporter chosen by standard `OTEL_*` variables
  (`autoexport`).
- Correlation: the request logger adds `trace_id`/`span_id`; responses carry
  `traceparent` and `X-Request-Id`; problems carry `request_id`.

## Metrics

`internal/platform/metrics/metrics.go`

```go
// Package metrics owns the Prometheus registry and HTTP RED metrics.
package metrics

import (
	"strconv"
	"time"

	"github.com/gofiber/fiber/v3"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"go.uber.org/fx"
)

// Module provides *prometheus.Registry, prometheus.Registerer and *HTTPMetrics.
var Module = fx.Module("metrics",
	fx.Provide(
		NewRegistry,
		func(r *prometheus.Registry) prometheus.Registerer { return r },
		NewHTTPMetrics,
	),
)

// NewRegistry returns a private registry (never the global default) with Go and process collectors.
func NewRegistry() *prometheus.Registry {
	r := prometheus.NewRegistry()
	r.MustRegister(
		collectors.NewGoCollector(),
		collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}),
	)
	return r
}

// HTTPMetrics records request rate, errors and duration.
type HTTPMetrics struct {
	duration *prometheus.HistogramVec
}

// NewHTTPMetrics registers http_server_request_duration_seconds.
func NewHTTPMetrics(reg prometheus.Registerer) (*HTTPMetrics, error) {
	d := prometheus.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "http_server_request_duration_seconds",
		Help:    "HTTP request latency by method, route template and status.",
		Buckets: prometheus.DefBuckets,
	}, []string{"method", "route", "status"})
	if err := reg.Register(d); err != nil {
		return nil, err
	}
	return &HTTPMetrics{duration: d}, nil
}

// Middleware must run outside the request logger, which renders errors, so the status is final.
func (m *HTTPMetrics) Middleware() fiber.Handler {
	return func(c fiber.Ctx) error {
		start := time.Now()
		err := c.Next()
		route := c.FullPath() // route template, e.g. /v1/projects/:projectId
		if !c.Matched() {
			route = "unmatched" // raw paths would explode cardinality
		}
		m.duration.WithLabelValues(c.Method(), route, strconv.Itoa(c.Response().StatusCode())).
			Observe(time.Since(start).Seconds())
		return err
	}
}
```

| Metric | Source | Use |
| --- | --- | --- |
| `http_server_request_duration_seconds{method,route,status}` | HTTP middleware | rate (`_count`), errors (`status=~"5.."`), latency (histogram quantiles) |
| `go_sql_*{db_name="main"}` | `collectors.NewDBStatsCollector` | pool saturation: `go_sql_wait_count_total`, `go_sql_in_use_connections` |
| `cache_requests_total{cache,result}` | cache package | hit ratio per feature, `result="error"` shows Redis trouble |
| `ratelimit_decisions_total{result}` | rate limiter | `limited` rate, `error` = failing open |
| `go_*`, `process_*` | default collectors | runtime |

Label rules: `route` is the template (`c.FullPath()`), never the raw path;
status as a string; no IDs, emails or user input as labels. Unrouted requests
are `route="unmatched"`.

Business metric example (register in the feature module's constructor, increment
in the service):

```go
type Metrics struct{ tasksCreated prometheus.Counter }

func NewMetrics(reg prometheus.Registerer) (*Metrics, error) {
	c := prometheus.NewCounter(
		prometheus.CounterOpts{Name: "tasks_created_total", Help: "Tasks created."},
	)
	return &Metrics{tasksCreated: c}, reg.Register(c)
}
```

Always `reg.Register` and return the error (duplicate names fail startup instead
of panicking); never use the global `prometheus.DefaultRegisterer`.

## Tracing

`internal/platform/otel/otel.go`

```go
// Package otel configures the OpenTelemetry tracer provider from OTEL_* variables.
package otel

import (
	"context"

	"go.opentelemetry.io/contrib/exporters/autoexport"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
	"go.uber.org/fx"

	"example.com/acme/taskmanager/internal/platform/config"
)

// Config holds what this module decides itself; every other OTEL_* variable
// (OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_TRACES_SAMPLER, ...) is read by the SDK from the process env.
type Config struct {
	ServiceName string `env:"SERVICE_NAME" envDefault:"taskmanager"`
	// TracesExporter mirrors OTEL_TRACES_EXPORTER so tests can turn exporting off via config.Source.
	TracesExporter string `env:"TRACES_EXPORTER" envDefault:"otlp"`
}

// NewConfig parses OTEL_SERVICE_NAME.
func NewConfig(src config.Source) (Config, error) { return config.Parse[Config](src, "OTEL_") }

// NewTracerProvider builds the SDK provider, installs it globally and flushes it on stop.
func NewTracerProvider(lc fx.Lifecycle, cfg Config) (trace.TracerProvider, error) {
	ctx := context.Background()
	res, err := resource.New(ctx,
		resource.WithAttributes(attribute.String("service.name", cfg.ServiceName)),
		resource.WithFromEnv(), // OTEL_RESOURCE_ATTRIBUTES / OTEL_SERVICE_NAME win
		resource.WithTelemetrySDK(),
	)
	if err != nil {
		return nil, err
	}
	opts := []sdktrace.TracerProviderOption{sdktrace.WithResource(res)}
	if cfg.TracesExporter != "none" {
		// otlp | console, endpoint from OTEL_EXPORTER_OTLP_*
		exp, err := autoexport.NewSpanExporter(ctx)
		if err != nil {
			return nil, err
		}
		opts = append(opts, sdktrace.WithBatcher(exp))
	}
	tp := sdktrace.NewTracerProvider(opts...) // spans still get trace IDs for log correlation
	otel.SetTracerProvider(tp)
	otel.SetTextMapPropagator(
		propagation.NewCompositeTextMapPropagator(
			propagation.TraceContext{},
			propagation.Baggage{},
		),
	)
	lc.Append(fx.StopHook(tp.Shutdown))
	return tp, nil
}

// Module provides trace.TracerProvider.
var Module = fx.Module("otel", fx.Provide(NewConfig, NewTracerProvider))
```

Instrumentation already wired:

| Layer | Package | Where |
| --- | --- | --- |
| HTTP server spans + W3C propagation | `github.com/gofiber/contrib/v3/otel` (`otel.New`) | httpserver |
| SQL spans | `gorm.io/plugin/opentelemetry/tracing` | db |
| Redis spans | `github.com/redis/go-redis/extra/redisotel/v9` | redisx |

Environment (read by the SDK from the process env):

| Variable | Example |
| --- | --- |
| `OTEL_TRACES_EXPORTER` | `otlp` (default), `console`, `none` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://otel-collector:4318` |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/protobuf` or `grpc` |
| `OTEL_TRACES_SAMPLER` / `OTEL_TRACES_SAMPLER_ARG` | `parentbased_traceidratio` / `0.1` |
| `OTEL_RESOURCE_ATTRIBUTES` | `deployment.environment=prod,service.version=1.4.2` |

With `OTEL_TRACES_EXPORTER=none` spans are still created (trace IDs keep
appearing in logs) but nothing is exported. Tests use that.

Manual span in a service:

```go
var tracer = otel.Tracer("example.com/acme/taskmanager/internal/tasks")

ctx, span := tracer.Start(ctx, "tasks.estimate")
defer span.End()
```

## Local stack

`docker compose up` (tooling.md) runs Jaeger; open `http://localhost:16686`,
service `taskmanager`. A `POST /v1/projects` shows `POST /v1/projects` with a
child `insert projects` span.
