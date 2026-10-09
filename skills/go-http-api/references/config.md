# Configuration (caarlos0/env v11)

Each module owns a `Config` struct and parses its own prefix from an injected
`config.Source`. Nothing calls `os.Getenv`. Adding a module never edits a
central config file, and tests override any value without touching the process
environment (so `t.Parallel()` stays safe).

`internal/platform/config/config.go`

```go
// Package config parses per-module configuration from an injectable environment.
package config

import (
	"os"
	"strings"

	"github.com/caarlos0/env/v11"
)

// Source is the environment every module parses its config from.
// Production uses the process environment; tests fx.Replace it with a map.
type Source map[string]string

// FromEnviron snapshots the process environment.
func FromEnviron() Source {
	src := Source{}
	for _, kv := range os.Environ() { //nolint:forbidigo // the one place that reads the environment
		if k, v, ok := strings.Cut(kv, "="); ok {
			src[k] = v
		}
	}
	return src
}

// Parse fills T from src using caarlos0/env tags, with every key prefixed by prefix.
func Parse[T any](src Source, prefix string) (T, error) {
	return env.ParseAsWithOptions[T](env.Options{Prefix: prefix, Environment: src})
}
```

## Module pattern

```go
// Config is parsed from DB_* variables.
type Config struct {
	DSN          string        `env:"DSN,required"`
	MaxOpenConns int           `env:"MAX_OPEN_CONNS" envDefault:"20"`
	ConnMaxLifetime time.Duration `env:"CONN_MAX_LIFETIME" envDefault:"30m"`
}

// NewConfig parses DB_* variables.
func NewConfig(src config.Source) (Config, error) { return config.Parse[Config](src, "DB_") }

var Module = fx.Module("db", fx.Provide(NewConfig, New /* takes Config */))
```

- `platform.Module` provides `config.FromEnviron` once; every `NewConfig`
  receives it.
- Tag options: `required` fails startup when unset; `envDefault` for defaults;
  slices split on commas (`[]string`); `time.Duration` parses `250ms`, `5s`.
- Secrets come from the environment like everything else (mounted by the
  platform); never log a `Config`.
- Outside fx (the `migrate` command) call `db.NewConfig(config.FromEnviron())`
  directly.
- Tests: `fx.Replace(config.Source{"DB_DSN": dsn, ...})`; see testing.md.

## Variables

| Prefix | Variable | Default | Notes |
| --- | --- | --- | --- |
| `LOG_` | `LEVEL`, `FORMAT` | `info`, `json` | `FORMAT=console` for local dev |
| `OTEL_` | `SERVICE_NAME`, `TRACES_EXPORTER` | `taskmanager`, `otlp` | other `OTEL_*` read by the SDK (observability.md) |
| `DB_` | `DSN` | required | `postgres://u:p@host:5432/db?sslmode=disable` |
| `DB_` | `MAX_OPEN_CONNS`, `MAX_IDLE_CONNS`, `CONN_MAX_LIFETIME`, `CONN_MAX_IDLE_TIME`, `SLOW_QUERY_THRESHOLD` | `20`, `10`, `30m`, `5m`, `200ms` | |
| `REDIS_` | `URL`, `TIMEOUT` | `redis://localhost:6379/0`, `250ms` | short timeout keeps fail-open fast |
| `RATE_LIMIT_` | `ENABLED`, `RATE`, `BURST`, `PERIOD` | `false`, `100`, `100`, `1s` | |
| `HTTP_` | `ADDR`, `READ_TIMEOUT`, `WRITE_TIMEOUT`, `IDLE_TIMEOUT`, `BODY_LIMIT` | `:8080`, `10s`, `10s`, `60s`, `1048576` | |
| `HTTP_` | `TRUST_PROXY`, `TRUSTED_PROXIES`, `PROXY_HEADER` | `false`, empty, `X-Forwarded-For` | set behind a load balancer so `c.IP()` is the client |
| `HTTP_` | `SHUTDOWN_DRAIN_DELAY` | `5s` | `0s` in tests |
| `HTTP_` | `CORS_ALLOW_ORIGINS`, `HELMET` | empty (off), `false` | opt-in |
| `ADMIN_` | `ADDR`, `READINESS_TIMEOUT` | `:9090`, `2s` | |
| `AUTH_` | `JWKS_URL`, `ISSUER`, `AUDIENCE` | required when auth.Module is used | auth.md |

Keep this table in the service README when you scaffold; update it in the same
change that adds a variable.
