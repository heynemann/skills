# Logging (zap)

One process logger, JSON in production, plus a request-scoped child carrying
`request_id`, `trace_id`, `span_id`. Code inside a request always logs through
`logger.From(ctx)`; code outside requests uses the injected `*zap.Logger`.

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

- `zap.ReplaceGlobals` makes `logger.From` fall back to the real logger outside
  requests.
- Stack traces are disabled: Go errors carry none, and zap's own stack is noise
  in every error line.

## Request logger

Installed by httpserver (http.md). It puts the child logger into the request
context, renders errors through the app's `ErrorHandler` so the status it logs
(and that metrics/otel outside it record) is final, then writes one access line.

`internal/platform/httpserver/middleware.go`

```go
package httpserver

import (
	"errors"
	"strings"
	"time"

	"github.com/gofiber/fiber/v3"
	"github.com/gofiber/fiber/v3/middleware/requestid"
	"go.opentelemetry.io/otel/trace"
	"go.uber.org/zap"

	"example.com/acme/taskmanager/internal/platform/logger"
	"example.com/acme/taskmanager/internal/platform/problem"
)

// requestLogger puts a request-scoped logger in the context, renders errors through
// ErrorHandler (so outer middleware see the final status) and writes one access log line.
func requestLogger(base *zap.Logger) fiber.Handler {
	return func(c fiber.Ctx) error {
		start := time.Now()
		fields := []zap.Field{zap.String("request_id", requestid.FromContext(c))}
		if sc := trace.SpanContextFromContext(c.Context()); sc.IsValid() {
			fields = append(
				fields,
				zap.Stringer("trace_id", sc.TraceID()),
				zap.Stringer("span_id", sc.SpanID()),
			)
		}
		l := base.With(fields...)
		c.SetContext(logger.With(c.Context(), l))

		if err := c.Next(); err != nil {
			if herr := c.App().ErrorHandler(c, err); herr != nil {
				_ = c.SendStatus(fiber.StatusInternalServerError)
			}
		}

		status := c.Response().StatusCode()
		level := zap.InfoLevel
		if status >= fiber.StatusInternalServerError {
			level = zap.ErrorLevel
		}
		l.Log(level, "request",
			zap.String("method", c.Method()),
			zap.String("route", c.FullPath()),
			zap.String("path", c.Path()),
			zap.Int("status", status),
			zap.Duration("latency", time.Since(start)),
			zap.String("ip", c.IP()),
		)
		return nil
	}
}

// problemContentType restores application/problem+json on typed error responses: the
// oapi-codegen fiber-v3 strict wrappers set that header and then call c.JSON(v), which
// overwrites it with application/json. Every 4xx/5xx body in the spec is a Problem.
func problemContentType(c fiber.Ctx) error {
	err := c.Next()
	if c.Response().StatusCode() >= fiber.StatusBadRequest &&
		strings.HasPrefix(string(c.Response().Header.ContentType()), fiber.MIMEApplicationJSON) {
		c.Set(fiber.HeaderContentType, problem.ContentType)
	}
	return err
}

// ErrorHandler is the single place errors become HTTP responses (RFC 9457).
// Feature handlers return typed spec responses for expected errors; anything that reaches
// here unmapped is a 500 whose details are logged, never sent.
func ErrorHandler(c fiber.Ctx, err error) error {
	var pe *problem.Error
	if errors.As(err, &pe) {
		return problem.Write(c, pe.Problem)
	}
	var fe *fiber.Error
	if errors.As(err, &fe) && fe.Code < fiber.StatusInternalServerError {
		return problem.Write(c, problem.New(c, fe.Code, fe.Message))
	}
	logger.From(c.Context()).Error("unhandled error", zap.Error(err))
	return problem.Write(c, problem.New(c, fiber.StatusInternalServerError, ""))
}
```

## GORM adapter

GORM output goes through the request logger. Failed queries log at debug because
repositories translate expected failures (unique/foreign key) into feature
errors and anything unexpected is logged once by `ErrorHandler`.

`internal/platform/db/gormlog.go`

```go
package db

import (
	"context"
	"errors"
	"fmt"
	"time"

	"go.uber.org/zap"
	"gorm.io/gorm"
	gormlogger "gorm.io/gorm/logger"

	"example.com/acme/taskmanager/internal/platform/logger"
)

// gormLogger sends GORM output to zap with request correlation from ctx.
type gormLogger struct {
	slow time.Duration
}

// NewGormLogger logs slow queries at warn; failed queries and all SQL at debug.
func NewGormLogger(slow time.Duration) gormlogger.Interface {
	return &gormLogger{slow: slow}
}

func (g *gormLogger) LogMode(
	gormlogger.LogLevel,
) gormlogger.Interface {
	return g
} // zap level decides

func (g *gormLogger) log(ctx context.Context) *zap.Logger { return logger.From(ctx).Named("gorm") }

func (g *gormLogger) Info(ctx context.Context, msg string, args ...any) {
	g.log(ctx).Info(fmt.Sprintf(msg, args...))
}

func (g *gormLogger) Warn(ctx context.Context, msg string, args ...any) {
	g.log(ctx).Warn(fmt.Sprintf(msg, args...))
}

func (g *gormLogger) Error(ctx context.Context, msg string, args ...any) {
	g.log(ctx).Error(fmt.Sprintf(msg, args...))
}

func (g *gormLogger) Trace(
	ctx context.Context,
	begin time.Time,
	fc func() (string, int64),
	err error,
) {
	elapsed := time.Since(begin)
	l := g.log(ctx)
	switch {
	case err != nil && !errors.Is(err, gorm.ErrRecordNotFound):
		// Debug, not error: repositories translate expected failures (unique, FK) into feature
		// errors, and anything unexpected is logged once by the HTTP ErrorHandler.
		if ce := l.Check(zap.DebugLevel, "query failed"); ce != nil {
			sql, rows := fc()
			ce.Write(
				zap.Error(err),
				zap.Duration("elapsed", elapsed),
				zap.String("sql", sql),
				zap.Int64("rows", rows),
			)
		}
	case g.slow > 0 && elapsed > g.slow:
		sql, rows := fc()
		l.Warn(
			"slow query",
			zap.Duration("elapsed", elapsed),
			zap.String("sql", sql),
			zap.Int64("rows", rows),
		)
	default:
		if ce := l.Check(zap.DebugLevel, "query"); ce != nil {
			sql, rows := fc()
			ce.Write(
				zap.Duration("elapsed", elapsed),
				zap.String("sql", sql),
				zap.Int64("rows", rows),
			)
		}
	}
}
```

## Rules

- Log an error or return it, never both. The HTTP edge logs unhandled errors
  once.
- Structured fields (`zap.String("project_id", id.String())`), never
  `fmt.Sprintf` into the message.
- Levels: `debug` SQL and internals; `info` lifecycle and access log; `warn`
  degraded but serving (cache down, slow query, limiter fail-open); `error`
  request failed with 5xx or a background job failed.
- Never log request bodies, tokens, DSNs or whole `Config` structs.
- `LOG_LEVEL=debug` shows every SQL statement with its request id.
