# fx (go.uber.org/fx)

Every package with runtime dependencies exports
`var Module = fx.Module("name", ...)`. `internal/app.Modules()` composes them;
`serve` and the tests run that same graph.

## Patterns used

| Need | Pattern | Example |
| --- | --- | --- |
| Construct a dependency | `fx.Provide(NewX)`; constructors take deps as params, return `(T, error)` | `db.New` |
| Bind concrete to interface | provide a func returning the interface | `func(m *TxManager) Transactor { return m }` |
| Many providers, one consumer | value group via `fx.Annotate(f, fx.ResultTags(`group:"routes"`))`, consumed with `fx.In` + `group:"routes"` | `httpserver.AsRoutes`, `admin.AsCheck` |
| Wrap a dependency | `fx.Decorate(NewCachedRepository)` inside the feature module (scoped to it) | projects cache |
| Optional dependency | `fx.In` field with `optional:"true"` | `ratelimit.Params.Key`, `httpserver.Params.Authn` |
| Side effects at startup | `fx.Invoke(Run)` registering lifecycle hooks | `admin.Run`, `httpserver.Run` |
| Override in tests | `fx.Replace(value)` at the root | `fx.Replace(config.Source{...})` |

Helpers hide the annotation noise so feature modules stay one screen:

```go
func AsRoutes(f any) any { return fx.Annotate(f, fx.ResultTags(`group:"routes"`)) }
func AsCheck(f any) any  { return fx.Annotate(f, fx.ResultTags(`group:"readiness"`)) }
```

## Lifecycle

- `OnStart` must not block: bind the listener synchronously (so a busy port
  fails startup), serve in a goroutine, and call
  `fx.Shutdowner.Shutdown(fx.ExitCode(1))` if serving dies.
- Hooks stop in reverse registration order. A constructor's hooks register when
  it runs; an `fx.Invoke` registers after its params are built.
  `platform.Module` lists `admin.Module` before `httpserver.Module`, so on
  shutdown the public server drains first while `/metrics` and `/readyz` stay
  up, then admin stops, then Redis/DB/tracer close.
- `serve` (architecture.md) runs `fx.New` → `Err()` → `Start` → `<-Wait()` →
  `Stop` itself instead of `fx.App.Run()`, which exits the process on startup
  failure after logging fx's whole error chain. fx's signal handling starts with
  `Start`, so SIGINT/SIGTERM still end `Wait`. Keep `stopTimeout` >
  `HTTP_SHUTDOWN_DRAIN_DELAY` + longest request.
- Startup failures print one line: `startup failed: <root cause>` via
  `dig.RootCause`, e.g. `env: required environment variable "DB_DSN" is not set`
  or `listen tcp :9090: bind: address already in use`. The fx event logger sends
  both events and errors to debug (`UseErrorLevel`), so nothing is printed
  twice; `LOG_LEVEL=debug` shows the full chain.
- Close resources with `lc.Append(fx.StopHook(x.Close))`.

## fx event logging

`fxevent.ZapLogger` logs every provide/invoke at info with stack traces. Route
it to debug so failures (still logged at error) stand out:

`internal/platform/logger/logger.go`

```go
// Package logger provides the zap logger and request-scoped logger helpers.
package logger

import (
	"context"
	"fmt"

	"go.uber.org/fx"
	"go.uber.org/fx/fxevent"
	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"

	"example.com/acme/taskmanager/internal/platform/config"
)

// Config is parsed from LOG_* variables.
type Config struct {
	Level  string `env:"LEVEL"  envDefault:"info"`
	Format string `env:"FORMAT" envDefault:"json"` // json | console
}

// NewConfig parses LOG_* variables.
func NewConfig(src config.Source) (Config, error) { return config.Parse[Config](src, "LOG_") }

// New builds the process logger and installs it as zap's global fallback.
func New(lc fx.Lifecycle, cfg Config) (*zap.Logger, error) {
	level, err := zapcore.ParseLevel(cfg.Level)
	if err != nil {
		return nil, fmt.Errorf("LOG_LEVEL: %w", err)
	}
	zc := zap.NewProductionConfig()
	if cfg.Format == "console" {
		zc = zap.NewDevelopmentConfig()
	}
	zc.Level = zap.NewAtomicLevelAt(level)
	zc.DisableStacktrace = true // Go errors carry no stack; the logger's own stack is noise
	l, err := zc.Build()
	if err != nil {
		return nil, err
	}
	zap.ReplaceGlobals(l)
	lc.Append(fx.StopHook(func() { _ = l.Sync() }))
	return l, nil
}

// Module provides *zap.Logger and routes fx's own events through it.
var Module = fx.Options(
	fx.Module("logger", fx.Provide(NewConfig, New)),
	fx.WithLogger(func(l *zap.Logger) fxevent.Logger {
		fl := &fxevent.ZapLogger{Logger: l.Named("fx")}
		// Graph events and failures at debug: every fx error is also returned by fx.New/
		// Start/Stop, and serve prints its root cause once. LOG_LEVEL=debug shows the detail.
		fl.UseLogLevel(zapcore.DebugLevel)
		fl.UseErrorLevel(zapcore.DebugLevel)
		return fl
	}),
)

type ctxKey struct{}

// With returns ctx carrying l.
func With(ctx context.Context, l *zap.Logger) context.Context {
	return context.WithValue(ctx, ctxKey{}, l)
}

// From returns the request-scoped logger (request_id, trace_id, span_id) or the global logger.
func From(ctx context.Context) *zap.Logger {
	if l, ok := ctx.Value(ctxKey{}).(*zap.Logger); ok {
		return l
	}
	return zap.L() //nolint:forbidigo // outside requests; New installed it via ReplaceGlobals
}
```

## Gotchas

- `fx.Decorate` in a child module only affects that module's view of the type;
  other modules still get the undecorated value.
- Value groups are unordered. When order matters (middleware), use explicit
  optional params instead of a group.
- `fx.Replace` replaces a type wherever it is provided, including inside
  modules, when given at the root.
- Unused providers never run. A module that must start (servers) needs an
  `fx.Invoke`.
- Diagnose wiring with `fx.New(...).Err()` in a test, or
  `fx.VisualizeError(err)`.
