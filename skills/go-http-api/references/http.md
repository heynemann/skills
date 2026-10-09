# HTTP: Fiber v3, errors, admin port, shutdown

Two listeners: the public Fiber app (`HTTP_ADDR`, API under `/v1`) and a private
admin app (`ADMIN_ADDR`: `/metrics`, `/livez`, `/readyz`). Probes and metrics
never pass through API middleware and are never exposed publicly.

## Public app

`internal/platform/httpserver/server.go`

```go
// Package httpserver builds the public Fiber app and mounts every feature's routes under /v1.
package httpserver

import (
	"context"
	"net"
	"time"

	"github.com/bytedance/sonic"
	"github.com/getkin/kin-openapi/openapi3"
	"github.com/getkin/kin-openapi/openapi3filter"
	"github.com/gofiber/contrib/v3/otel"
	"github.com/gofiber/fiber/v3"
	"github.com/gofiber/fiber/v3/middleware/cors"
	"github.com/gofiber/fiber/v3/middleware/helmet"
	"github.com/gofiber/fiber/v3/middleware/recover"
	"github.com/gofiber/fiber/v3/middleware/requestid"
	"go.opentelemetry.io/otel/trace"
	"go.uber.org/fx"
	"go.uber.org/zap"

	"example.com/acme/taskmanager/internal/platform/admin"
	"example.com/acme/taskmanager/internal/platform/config"
	"example.com/acme/taskmanager/internal/platform/metrics"
	"example.com/acme/taskmanager/internal/platform/ratelimit"
)

// Config is parsed from HTTP_* variables.
type Config struct {
	Addr               string        `env:"ADDR"                 envDefault:":8080"`
	ReadTimeout        time.Duration `env:"READ_TIMEOUT"         envDefault:"10s"`
	WriteTimeout       time.Duration `env:"WRITE_TIMEOUT"        envDefault:"10s"`
	IdleTimeout        time.Duration `env:"IDLE_TIMEOUT"         envDefault:"60s"`
	BodyLimit          int           `env:"BODY_LIMIT"           envDefault:"1048576"`
	TrustProxy         bool          `env:"TRUST_PROXY"          envDefault:"false"`
	TrustedProxies     []string      `env:"TRUSTED_PROXIES"` // IPs/CIDRs allowed to set ProxyHeader
	ProxyHeader        string        `env:"PROXY_HEADER"         envDefault:"X-Forwarded-For"`
	ShutdownDrainDelay time.Duration `env:"SHUTDOWN_DRAIN_DELAY" envDefault:"5s"`
	CORSAllowOrigins   []string      `env:"CORS_ALLOW_ORIGINS"` // empty = CORS middleware off
	Helmet             bool          `env:"HELMET"               envDefault:"false"`
}

// NewConfig parses HTTP_* variables.
func NewConfig(src config.Source) (Config, error) { return config.Parse[Config](src, "HTTP_") }

// RegisterFunc mounts one feature's routes on the /v1 router.
type RegisterFunc func(r fiber.Router)

// AsRoutes annotates a constructor returning RegisterFunc into the "routes" group.
func AsRoutes(f any) any { return fx.Annotate(f, fx.ResultTags(`group:"routes"`)) }

// Authn is provided by an optional auth module. Middleware authenticates a bearer token when
// present; Check enforces each operation's OpenAPI security requirements inside the validator.
type Authn struct {
	Middleware fiber.Handler
	Check      openapi3filter.AuthenticationFunc
}

// Params are the app's dependencies.
type Params struct {
	fx.In
	Config  Config
	Log     *zap.Logger
	Tracer  trace.TracerProvider
	Metrics *metrics.HTTPMetrics
	Limiter *ratelimit.Limiter
	Spec    *openapi3.T
	Authn   *Authn         `optional:"true"`
	Routes  []RegisterFunc `                group:"routes"`
}

// NewApp wires the middleware chain. Order matters:
//
//	requestid → otel → metrics → request logger → recover → [cors, helmet] → /v1: [authn] → [limiter] → problemContentType → validator → handlers
//
// otel, metrics and the logger read the final status, so the logger (innermost of the three)
// renders errors via the ErrorHandler; recover sits inside it so panics are logged and counted.
func NewApp(p Params) (*fiber.App, error) {
	app := fiber.New(fiber.Config{
		AppName:             "taskmanager",
		ReadTimeout:         p.Config.ReadTimeout,
		WriteTimeout:        p.Config.WriteTimeout,
		IdleTimeout:         p.Config.IdleTimeout,
		BodyLimit:           p.Config.BodyLimit,
		TrustProxy:          p.Config.TrustProxy,
		TrustProxyConfig:    fiber.TrustProxyConfig{Proxies: p.Config.TrustedProxies},
		ProxyHeader:         p.Config.ProxyHeader,
		JSONEncoder:         sonic.Marshal,
		JSONDecoder:         sonic.Unmarshal,
		ErrorHandler:        ErrorHandler,
		PassLocalsToContext: true, // requestid.FromContext works on the handler's context.Context
	})

	app.Use(requestid.New())
	app.Use(otel.New(otel.WithTracerProvider(p.Tracer)))
	app.Use(p.Metrics.Middleware())
	app.Use(requestLogger(p.Log))
	app.Use(recover.New(recover.Config{EnableStackTrace: true}))
	if len(p.Config.CORSAllowOrigins) > 0 {
		app.Use(cors.New(cors.Config{AllowOrigins: p.Config.CORSAllowOrigins}))
	}
	if p.Config.Helmet {
		app.Use(helmet.New())
	}

	v1 := app.Group("/v1")
	check := openapi3filter.NoopAuthenticationFunc
	if p.Authn != nil {
		v1.Use(p.Authn.Middleware)
		check = p.Authn.Check
	}
	if h := p.Limiter.Handler(); h != nil {
		v1.Use(h)
	}
	validator, err := NewValidator(p.Spec, check)
	if err != nil {
		return nil, err
	}
	v1.Use(problemContentType, validator)
	for _, register := range p.Routes {
		register(v1)
	}
	return app, nil
}

// Run listens on OnStart and drains on OnStop: readiness flips to 503, the drain delay lets
// the load balancer stop routing, then in-flight requests finish.
func Run(
	lc fx.Lifecycle,
	cfg Config,
	app *fiber.App,
	health *admin.Health,
	sd fx.Shutdowner,
	log *zap.Logger,
) {
	lc.Append(fx.Hook{
		OnStart: func(ctx context.Context) error {
			ln, err := (&net.ListenConfig{}).Listen(ctx, "tcp", cfg.Addr)
			if err != nil {
				return err
			}
			log.Info("http listening", zap.Stringer("addr", ln.Addr()))
			go func() {
				if err := app.Listener(
					ln,
					fiber.ListenConfig{DisableStartupMessage: true},
				); err != nil {
					log.Error("http server stopped", zap.Error(err))
					_ = sd.Shutdown(fx.ExitCode(1))
				}
			}()
			return nil
		},
		OnStop: func(ctx context.Context) error {
			health.MarkShuttingDown()
			select {
			case <-time.After(cfg.ShutdownDrainDelay):
			case <-ctx.Done():
			}
			return app.ShutdownWithContext(ctx)
		},
	})
}

// Module provides the public *fiber.App and serves it.
var Module = fx.Module("httpserver",
	fx.Provide(NewConfig, NewApp, LoadSpec),
	fx.Invoke(Run),
)
```

### Why this middleware order

| Position | Middleware | Reason |
| --- | --- | --- |
| 1 | `requestid` | every later log line, problem and trace can carry the id |
| 2 | otel `New` | span wraps everything; it renders errors itself and reads the final status |
| 3 | metrics | outside the logger, so it observes the status after errors were rendered |
| 4 | request logger | sets `logger.From(ctx)`, calls `ErrorHandler` on errors, writes the access log |
| 5 | `recover` | inside the logger so a panic becomes a logged, counted 500 |
| 6 | CORS / helmet | opt-in via `HTTP_CORS_ALLOW_ORIGINS`, `HTTP_HELMET` |
| /v1 | authn → limiter → problemContentType → validator | identify caller, limit (by principal or IP), fix typed error content types, reject contract violations before handlers |

Requests rejected before routing (validator, limiter) have no matched route:
metrics label them `route="unmatched"`, the access log shows the group path.

### Fiber v3 facts this code relies on

- `fiber.Ctx` is an interface; handlers are `func(c fiber.Ctx) error`.
- `c.Context()` returns the request's `context.Context`; `c.SetContext(ctx)`
  replaces it. Strict handlers receive `c.Context()`, so values set there
  (logger, span, principal) reach services.
- `PassLocalsToContext: true` makes `requestid.FromContext(ctx)` work on that
  `context.Context` (otherwise only on `fiber.Ctx`).
- `app.Listener(ln, fiber.ListenConfig{DisableStartupMessage: true})` serves a
  pre-bound listener; `app.ShutdownWithContext(ctx)` drains.
- `c.FullPath()` is the matched route template (`/v1/projects/:projectId`);
  `c.Matched()` is false for unrouted requests.
- `c.JSON(v, "application/problem+json")` sets a custom content type;
  `c.JSON(v)` without it resets the header to `application/json`. oapi-codegen's
  fiber-v3 strict wrappers set `Content-Type: application/problem+json` and then
  call `c.JSON(v)`, so `problemContentType` (logging.md, middleware.go) restores
  it on 4xx/5xx JSON responses. `TestAPIDuplicateSlugIsProblemConflict` guards
  it.
- `JSONEncoder`/`JSONDecoder` = `sonic.Marshal`/`sonic.Unmarshal` (generated
  strict wrappers use `c.Bind().Body` and `c.JSON`, so they go through sonic).
  sonic falls back to `encoding/json` on non-amd64/arm64.
- `github.com/gofiber/contrib/v3/otel`: use `otel.New(...)`; `otel.Middleware`
  is deprecated.
- `oapi-codegen/fiber-middleware` supports Fiber v2 only; the validator in
  openapi.md is the v3 replacement.

## Errors: RFC 9457 problem details

`internal/platform/problem/problem.go`

```go
// Package problem builds RFC 9457 problem details (application/problem+json).
package problem

import (
	"context"
	"net/http"

	"github.com/gofiber/fiber/v3"
	"github.com/gofiber/fiber/v3/middleware/requestid"

	"example.com/acme/taskmanager/gen/go/httpapi"
)

// ContentType is the RFC 9457 media type.
const ContentType = "application/problem+json"

// New returns a problem for status; detail is shown to clients, so never pass internal errors.
func New(ctx context.Context, status int, detail string) httpapi.Problem {
	p := httpapi.Problem{Type: "about:blank", Title: http.StatusText(status), Status: status}
	if detail != "" {
		p.Detail = &detail
	}
	if rid := requestid.FromContext(ctx); rid != "" {
		p.RequestID = &rid
	}
	return p
}

// Error carries a ready-made problem through Fiber's error path (validator, limiter).
type Error struct {
	Problem httpapi.Problem
}

func (e *Error) Error() string { return e.Problem.Title }

// Write renders p as the response.
func Write(c fiber.Ctx, p httpapi.Problem) error {
	if p.RequestID == nil {
		if rid := requestid.FromContext(c); rid != "" {
			p.RequestID = &rid
		}
	}
	return c.Status(p.Status).JSON(p, ContentType)
}
```

- Expected errors are part of the contract: each operation lists its 4xx
  responses in the spec and the handler returns the typed response (openapi.md).
- `ErrorHandler` (in middleware.go, logging.md) is the single exit for
  everything else: `*problem.Error` as-is, `*fiber.Error` < 500 with its
  message, anything else logged and answered as a bare 500.
- `detail` is client-visible. Feature sentinel errors are written to be safe to
  show (`"project slug already taken"`); wrapped infrastructure errors never
  reach `detail`.

## Admin app, probes, shutdown

`internal/platform/admin/admin.go`

```go
// Package admin serves /metrics, /livez and /readyz on a private port.
package admin

import (
	"context"
	"net"
	"sync/atomic"
	"time"

	"github.com/gofiber/fiber/v3"
	"github.com/gofiber/fiber/v3/middleware/adaptor"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"go.uber.org/fx"
	"go.uber.org/zap"

	"example.com/acme/taskmanager/internal/platform/config"
)

// Config is parsed from ADMIN_* variables.
type Config struct {
	Addr             string        `env:"ADDR"              envDefault:":9090"`
	ReadinessTimeout time.Duration `env:"READINESS_TIMEOUT" envDefault:"2s"`
}

// NewConfig parses ADMIN_* variables.
func NewConfig(src config.Source) (Config, error) { return config.Parse[Config](src, "ADMIN_") }

// Check is one readiness dependency. Provide it with AsCheck.
type Check struct {
	Name string
	Fn   func(context.Context) error
}

// AsCheck annotates a constructor returning Check into the "readiness" group.
func AsCheck(f any) any { return fx.Annotate(f, fx.ResultTags(`group:"readiness"`)) }

// Health aggregates readiness checks and the shutdown flag.
type Health struct {
	checks       []Check
	shuttingDown atomic.Bool
}

// HealthParams collects every Check in the graph.
type HealthParams struct {
	fx.In
	Checks []Check `group:"readiness"`
}

// NewHealth builds the readiness aggregator.
func NewHealth(p HealthParams) *Health { return &Health{checks: p.Checks} }

// MarkShuttingDown makes /readyz return 503 so the load balancer drains this instance.
func (h *Health) MarkShuttingDown() { h.shuttingDown.Store(true) }

func (h *Health) ready(c fiber.Ctx, timeout time.Duration) error {
	if h.shuttingDown.Load() {
		return c.Status(fiber.StatusServiceUnavailable).JSON(fiber.Map{"status": "shutting_down"})
	}
	ctx, cancel := context.WithTimeout(c.Context(), timeout)
	defer cancel()
	status, results := fiber.StatusOK, fiber.Map{}
	for _, chk := range h.checks {
		if err := chk.Fn(ctx); err != nil {
			status, results[chk.Name] = fiber.StatusServiceUnavailable, err.Error()
			continue
		}
		results[chk.Name] = "ok"
	}
	return c.Status(status).JSON(fiber.Map{"checks": results})
}

// Run starts the admin listener. It is invoked before the public server so it stops after it.
func Run(lc fx.Lifecycle, cfg Config, h *Health, reg *prometheus.Registry, log *zap.Logger) {
	app := fiber.New()
	app.Get(
		"/metrics",
		adaptor.HTTPHandler(promhttp.HandlerFor(reg, promhttp.HandlerOpts{Registry: reg})),
	)
	app.Get("/livez", func(c fiber.Ctx) error { return c.SendString("ok") })
	app.Get("/readyz", func(c fiber.Ctx) error { return h.ready(c, cfg.ReadinessTimeout) })
	lc.Append(fx.Hook{
		OnStart: func(ctx context.Context) error {
			ln, err := (&net.ListenConfig{}).Listen(ctx, "tcp", cfg.Addr)
			if err != nil {
				return err
			}
			log.Info("admin listening", zap.Stringer("addr", ln.Addr()))
			go func() {
				if err := app.Listener(
					ln,
					fiber.ListenConfig{DisableStartupMessage: true},
				); err != nil {
					log.Error("admin server stopped", zap.Error(err))
				}
			}()
			return nil
		},
		OnStop: app.ShutdownWithContext,
	})
}

// Module provides *Health and runs the admin server.
var Module = fx.Module("admin", fx.Provide(NewConfig, NewHealth), fx.Invoke(Run))
```

- `/livez`: process is up. Never checks dependencies (a DB outage must not
  restart every pod).
- `/readyz`: every `admin.Check` in the `readiness` group plus the shutdown
  flag. Postgres registers one (`db.NewReadinessCheck`); Redis deliberately does
  not (fail-open cache).
- Shutdown on SIGTERM: `/readyz` → 503, wait `HTTP_SHUTDOWN_DRAIN_DELAY` so the
  load balancer stops routing, `ShutdownWithContext` finishes in-flight
  requests, then Redis, admin, DB and tracer close in reverse start order
  (fx.md).

Add a readiness check for a new hard dependency:

```go
fx.Provide(admin.AsCheck(func(c *kafka.Client) admin.Check {
	return admin.Check{Name: "kafka", Fn: c.Ping}
}))
```

## Opt-ins

- CORS: `HTTP_CORS_ALLOW_ORIGINS=https://app.example.com` (comma-separated).
  Empty disables the middleware.
- Security headers: `HTTP_HELMET=true`.
- Rate limiting: `RATE_LIMIT_ENABLED=true` (redis.md).
- Authentication: add `auth.Module` (auth.md).
- Behind a proxy: `HTTP_TRUST_PROXY=true`, `HTTP_TRUSTED_PROXIES=10.0.0.0/8`, so
  `c.IP()` reads `X-Forwarded-For` only from trusted hops.
