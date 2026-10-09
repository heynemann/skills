# Tooling: tools, Makefile, lint, Docker, compose, CI

## go.mod

```sh
go mod init example.com/acme/taskmanager
go mod edit -go=1.27
go get -tool github.com/oapi-codegen/oapi-codegen/v2/cmd/oapi-codegen@latest
go get -tool github.com/vektra/mockery/v3@latest
```

`go tool oapi-codegen` / `go tool mockery` then run the versions pinned in
`go.mod`. goose is used as a library through `taskmanager migrate` (its CLI
module pulls every database driver). golangci-lint is installed as a binary (its
maintainers advise against `go tool`); pin the version in CI.

Dependencies by major version. Don't `go get` them one by one: write the code,
then `go mod tidy` (after scaffold steps 4 and 6):

```text
github.com/gofiber/fiber/v3            github.com/gofiber/contrib/v3/otel
go.uber.org/fx                         go.uber.org/zap
gorm.io/gorm  gorm.io/driver/postgres  gorm.io/plugin/opentelemetry
github.com/jackc/pgx/v5                github.com/pressly/goose/v3
github.com/redis/go-redis/v9           github.com/redis/go-redis/extra/redisotel/v9
github.com/go-redis/redis_rate/v10     github.com/bytedance/sonic
github.com/prometheus/client_golang    go.opentelemetry.io/otel (+ /sdk, contrib/exporters/autoexport)
github.com/getkin/kin-openapi          github.com/oapi-codegen/runtime
github.com/caarlos0/env/v11            github.com/spf13/cobra
github.com/google/uuid                 golang.org/x/sync
github.com/stretchr/testify            github.com/testcontainers/testcontainers-go (+ modules/postgres, modules/redis)
github.com/puzpuzpuz/xsync/v4          (only when shared state needs a concurrent map/counter; see forbidigo)
```

## Makefile

`Makefile`

```makefile
BINARY := taskmanager
PKG    := ./cmd/$(BINARY)
FEATURES := $(basename $(notdir $(filter-out api/codegen/httpapi.yaml,$(wildcard api/codegen/*.yaml))))

# Node tools for the Bruno collection, pinned so check-generated is reproducible. Bump deliberately.
BRUNO   := npx -y @usebruno/cli@4.2.1
REDOCLY := REDOCLY_TELEMETRY=off REDOCLY_SUPPRESS_UPDATE_NOTICE=true npx -y @redocly/cli@2.60.0

# Local defaults for run/migrate, matching compose.yaml (`make deps-up`). Anything already set
# in the environment or on the command line wins; tests ignore these (testkit injects its own).
PG_PORT    ?= 5432
REDIS_PORT ?= 6379
export DB_DSN               ?= postgres://taskmanager:taskmanager@localhost:$(PG_PORT)/taskmanager?sslmode=disable
export REDIS_URL            ?= redis://localhost:$(REDIS_PORT)/0
export OTEL_TRACES_EXPORTER ?= none
export LOG_FORMAT           ?= console
export PG_PORT REDIS_PORT

.PHONY: generate generate-api generate-mocks generate-bruno check-generated build run test test-unit lint fmt fmt-check migrate-up migrate-down migration deps-up compose-up compose-down need-docker need-golangci-lint need-node

# Prerequisite checks: fail with an install hint instead of "command not found" halfway through.
need-docker:
	@docker info >/dev/null 2>&1 || { echo "Docker is not installed or not running: https://docs.docker.com/get-docker/" >&2; exit 1; }

need-golangci-lint:
	@golangci-lint version 2>/dev/null | grep -q 'version 2\.' || { echo "golangci-lint v2 is required: https://golangci-lint.run/welcome/install/" >&2; exit 1; }

need-node:
	@command -v npx >/dev/null 2>&1 || { echo "Node.js (npx) is required to generate the Bruno collection: https://nodejs.org/" >&2; exit 1; }

generate: generate-api generate-mocks generate-bruno ## regenerate everything under gen/

generate-api:
	go tool oapi-codegen -config api/codegen/httpapi.yaml api/common.yaml
	$(foreach f,$(FEATURES),go tool oapi-codegen -config api/codegen/$(f).yaml api/openapi.yaml &&) true

generate-mocks:
	go tool mockery

generate-bruno: need-node ## gen/bruno from the spec + api/bruno/environments (see bruno.md)
	@tmp=$$(mktemp -d) && trap 'rm -rf "$$tmp"' EXIT && \
	  $(REDOCLY) bundle api/openapi.yaml -o "$$tmp/openapi.yaml" && \
	  rm -rf gen/bruno && mkdir -p gen && \
	  $(BRUNO) import openapi -s "$$tmp/openapi.yaml" -o gen/bruno -n "$(BINARY) API" && \
	  rm -rf gen/bruno/environments && cp -r api/bruno/environments gen/bruno/environments

check-generated: generate ## CI: fail if committed gen/ is stale (changed or new files)
	@test -z "$$(git status --porcelain -- gen/)" || { git status --short -- gen/; exit 1; }

build:
	CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o bin/$(BINARY) $(PKG)

run:
	go run $(PKG) serve

test-unit:
	go test -short -race ./...

test: need-docker ## unit + integration (testcontainers)
	go test -race ./...

lint: need-golangci-lint
	golangci-lint run ./...

fmt: need-golangci-lint ## gofumpt + goimports + golines, configured in .golangci.yml
	golangci-lint fmt ./...

fmt-check: need-golangci-lint ## CI: fail if formatting would change anything (prints the diff)
	golangci-lint fmt --diff ./...

migrate-up:
	go run $(PKG) migrate up

migrate-down:
	go run $(PKG) migrate down

migration: ## make migration name=add_task_labels
	@test -n "$(name)" || { echo "usage: make migration name=<snake_case_name>" >&2; exit 1; }
	go run $(PKG) migrate create $(name)

deps-up: need-docker ## Postgres + Redis for `make migrate-up run` (PG_PORT/REDIS_PORT to move them)
	docker compose up -d --wait postgres redis

compose-up: need-docker
	docker compose up -d --build

compose-down: need-docker
	docker compose down
```

`make deps-up migrate-up run` works with no setup: the exported `?=` defaults
point `run` and `migrate-*` at the compose Postgres/Redis, skip trace export and
use console logs. Values already in the environment win, so a real `DB_DSN` or
`HTTP_ADDR=:18080` overrides them. Keep the DSN in sync with the `postgres`
service credentials in `compose.yaml`.

Every target fails fast with a one-line, actionable message instead of a stack
of tool output:

- `need-docker`, `need-golangci-lint`, `need-node` are prerequisites of the
  targets that use those tools and print an install link when the tool is
  missing (or Docker isn't running).
- `make migration` without `name=` prints its usage.
- `serve` reports startup failures as `startup failed: <root cause>` (fx.md).

When a new target needs a new tool, give it a `need-<tool>` prerequisite in the
same change.

`FEATURES` is derived from `api/codegen/*.yaml`, so a new feature only needs its
config file. `generate-bruno` needs Node (for `npx`) locally and in CI.
`check-generated` uses `git status` rather than `git diff`, so it also catches
new generated files nobody committed (a new operation adds a Bruno request
file).

## golangci-lint v2

`.golangci.yml`

```yaml
version: "2"
run:
  timeout: 5m
linters:
  default: standard # errcheck, govet, ineffassign, staticcheck, unused
  enable:
    - bodyclose
    - contextcheck
    - forbidigo
    - errorlint
    - gocritic
    - gosec
    - noctx
    - revive
    - sqlclosecheck
  settings:
    forbidigo:
      analyze-types: true # match on the real package path, not the import alias
      forbid:
        # Concurrency: no hand-rolled locking.
        - pattern: ^sync\.(Mutex|RWMutex|Map)$
          msg: use xsync.Map/xsync.Counter (github.com/puzpuzpuz/xsync/v4) or sync/atomic; xsync.RBMutex if a lock is unavoidable
        # Logging: zap only, through logger.From(ctx) or the injected *zap.Logger.
        - pattern: ^(log|slog)\.
          pkg: ^log(/slog)?$
          msg: log with zap via logger.From(ctx) or the injected *zap.Logger
        - pattern: ^(fmt\.Print(f|ln)?|print(ln)?)$
          msg: log with zap; CLI output goes to cmd.OutOrStdout()
        - pattern: ^zap\.(L|S)$
          msg: use logger.From(ctx) or the injected *zap.Logger
        - pattern: ^zap\.Logger\.Sugar$
          msg: use structured zap fields, not the sugared logger
        - pattern: .*
          pkg: ^github\.com/(sirupsen/logrus|rs/zerolog|go-kit/log|apex/log)
          msg: zap is the only logger
        # JSON: sonic.
        - pattern: .*
          pkg: ^(encoding/json|github\.com/json-iterator/go|github\.com/goccy/go-json)$
          msg: use github.com/bytedance/sonic (sonic.Marshal/Unmarshal)
        # Config: config.Source parsed per module.
        - pattern: ^os\.(Getenv|LookupEnv|Environ|Setenv|Unsetenv)$
          msg: read configuration from config.Source via config.Parse (config.md)
        - pattern: .*
          pkg: ^github\.com/(spf13/viper|knadh/koanf|kelseyhightower/envconfig|joho/godotenv)
          msg: configuration uses caarlos0/env through config.Parse
        # HTTP: Fiber v3 only.
        - pattern: ^http\.(ListenAndServe|ListenAndServeTLS|Serve|ServeTLS|NewServeMux|Handle|HandleFunc|Server)$
          msg: serve HTTP with Fiber (httpserver / admin modules)
        - pattern: .*
          pkg: ^github\.com/(gin-gonic/gin|labstack/echo|go-chi/chi|gorilla/mux|gofiber/fiber/v2)
          msg: the HTTP framework is github.com/gofiber/fiber/v3
        # Metrics: private registry injected as prometheus.Registerer.
        - pattern: ^prometheus\.(MustRegister|Register|Unregister|DefaultRegisterer|DefaultGatherer)$
          msg: register on the injected prometheus.Registerer (no global registry)
        - pattern: ^promauto\.New
          msg: use promauto.With(reg) or reg.Register with the injected prometheus.Registerer
        - pattern: ^promhttp\.Handler$
          msg: use promhttp.HandlerFor(reg, ...) with the private registry
        # Database: goose owns the schema; updates name their columns.
        - pattern: ^gorm\.(DB|Migrator)\.AutoMigrate$
          msg: schema changes are goose migrations (migrations.md)
        - pattern: ^gorm\.DB\.Save$
          msg: use Model(m).Select(cols...).Updates(m) and check RowsAffected (database.md)
        - pattern: .*
          pkg: ^github\.com/golang-migrate/migrate
          msg: migrations use goose through the migrate subcommand
        # IDs: UUIDv7.
        - pattern: ^uuid\.(New|NewRandom|NewString)$
          msg: IDs are UUIDv7 (db.Base assigns them; uuid.NewV7 elsewhere)
        # Redis: go-redis v9.
        - pattern: .*
          pkg: ^github\.com/(gomodule/redigo|redis/rueidis|valkey-io/valkey-go)
          msg: the Redis client is github.com/redis/go-redis/v9
        # Tests: mockery testify mocks; no sleeping.
        - pattern: .*
          pkg: ^(go\.uber\.org/mock|github\.com/golang/mock|github\.com/ovechkin-dm/mockio)
          msg: mocks are mockery testify mocks in gen/go/<pkg>mocks (testing.md)
        - pattern: ^time\.Sleep$
          msg: wait on a channel, context or timer; tests poll a condition instead of sleeping
    govet:
      enable-all: true
      disable: [fieldalignment, shadow]
    errcheck:
      exclude-functions: [fmt.Fprint, fmt.Fprintf, fmt.Fprintln]
    revive:
      rules:
        - name: exported
          disabled: true # features export many self-explanatory types
  exclusions:
    generated: strict # skips gen/ (files carry "Code generated ... DO NOT EDIT")
    rules:
      - path: _test\.go
        linters: [gosec, noctx, bodyclose, errcheck]
formatters:
  enable:
    - gofumpt
    - goimports
    - golines
  settings:
    goimports:
      local-prefixes: [example.com/acme/taskmanager]
    golines:
      max-len: 100 # golines default; splits long signatures, calls and literals
  exclusions:
    generated: strict # never reformat gen/: make generate owns it
```

`exclusions.generated: strict` (for linters and formatters) skips everything
under `gen/`. Fix findings rather than adding `//nolint`; when a nolint is right
(jitter uses `math/rand`), name the linter and the reason.

### Formatting

Formatting is golangci-lint v2's `fmt` command running three formatters, so the
editor, `make fmt` and CI share one config and one tool version:

| Formatter | Does |
| --- | --- |
| `gofumpt` | stricter `gofmt`: no empty lines at block edges, `0o` octals, `// comment` spacing, ... |
| `goimports` | adds/removes imports; groups stdlib, third-party, then `example.com/acme/taskmanager` |
| `golines` | splits lines over 100 columns at calls, signatures and composite literals; aligns struct tags |

- `make fmt` rewrites files. Run it before committing; point the editor's
  format-on-save at `golangci-lint fmt` (or gopls with `gofumpt: true` and run
  `make fmt` before pushing).
- `make fmt-check` prints the diff and exits non-zero if anything would change.
  CI runs it.
- `make lint` also fails on unformatted files, but names one formatter per file
  without a diff; `make fmt-check` shows the full diff, so CI runs both.
- golines can't split long strings, identifiers or comments: lines over 100
  columns are allowed when nothing can be split.
- golines moves a trailing comment to the end of the split statement. Keep
  `//nolint:<linter>` directives on short statements (assign to a variable
  first) so they stay on the line the linter reports.

### Forbidden APIs (forbidigo)

forbidigo turns the stack choices into lint errors, so a stray `log.Printf` or
`sync.Mutex` fails `make lint` with a message naming the replacement.
`analyze-types: true` matches the real package path (aliases don't dodge it) and
methods (`(*gorm.DB).Save`).

| Banned | Use instead | Why |
| --- | --- | --- |
| `sync.Mutex`, `sync.RWMutex`, `sync.Map` | `xsync.Map`, `xsync.Counter` ([`github.com/puzpuzpuz/xsync/v4`](https://github.com/puzpuzpuz/xsync)), `sync/atomic` types; `xsync.RBMutex` if a lock is unavoidable | no hand-rolled locking |
| `log`, `log/slog`, `fmt.Print*`, `print`/`println`, logrus, zerolog, go-kit/log, apex/log | `logger.From(ctx)` / injected `*zap.Logger`; CLI output via `cmd.OutOrStdout()` | one structured, correlated log stream |
| `zap.L`, `zap.S`, `(*zap.Logger).Sugar` | `logger.From(ctx)`, structured fields | request correlation, no printf-style logs |
| `encoding/json`, json-iterator, goccy/go-json | `sonic.Marshal` / `sonic.Unmarshal` | one JSON engine (Fiber and cache already use sonic) |
| `os.Getenv`, `LookupEnv`, `Environ`, `Setenv`, `Unsetenv`; viper, koanf, envconfig, godotenv | `config.Parse` on the injected `config.Source` | per-module config, parallel-safe tests |
| `http.ListenAndServe*`, `http.Serve*`, `http.NewServeMux`, `http.Handle*`, `http.Server`; gin, echo, chi, gorilla/mux, Fiber v2 | Fiber v3 via httpserver/admin | one framework, one middleware chain |
| `prometheus.MustRegister/Register/Unregister/DefaultRegisterer/DefaultGatherer`, `promauto.New*`, `promhttp.Handler` | injected `prometheus.Registerer`, `promauto.With(reg)`, `promhttp.HandlerFor(reg, ...)` | private registry |
| `(*gorm.DB).AutoMigrate`, `Migrator().AutoMigrate`; golang-migrate | goose migrations | migrations own the schema |
| `(*gorm.DB).Save` | `Model(m).Select(cols...).Updates(m)` + `RowsAffected` | `Save` silently re-inserts deleted rows |
| `uuid.New`, `uuid.NewRandom`, `uuid.NewString` | `db.Base` / `uuid.NewV7` | time-ordered IDs |
| redigo, rueidis, valkey-go | `github.com/redis/go-redis/v9` | one Redis client |
| `go.uber.org/mock`, `github.com/golang/mock`, mockio | mockery testify mocks in `gen/go/<pkg>mocks` | one mock style |
| `time.Sleep` | channels, `context`, `time.After`/`time.NewTimer`; tests wait on a condition | sleeps hide races and slow tests |

Rules:

- The rules apply to tests too. Generated code (`gen/`) is excluded.
- The two legitimate exceptions in the scaffold carry a reasoned
  `//nolint:forbidigo`: `config.FromEnviron` (the one reader of the process
  environment) and the `zap.L()` fallback in `logger.From`. Any new exception
  needs the same justification.
- Libraries used internally (singleflight's mutex, kin-openapi's
  `encoding/json`) are fine: forbidigo only checks this module's code.
- golangci-lint prints one issue per line by default, so a forbidigo hit can
  hide behind another linter's issue on the same line. Fix what is shown and run
  again, or use `golangci-lint run --uniq-by-line=false`.
- When the stack changes (another JSON engine, a second logger), update this
  table and the `forbidigo` block together.

## Docker

`Dockerfile`

```dockerfile
# syntax=docker/dockerfile:1
FROM golang:1.27 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod go mod download
COPY . .
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/taskmanager ./cmd/taskmanager

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/taskmanager /taskmanager
EXPOSE 8080 9090
ENTRYPOINT ["/taskmanager"]
CMD ["serve"]
```

`CGO_ENABLED=0` works with every dependency here (sonic included), so the image
is distroless/static, nonroot, ~50 MB. Same image runs `serve` (default) and
`migrate up`.

`.dockerignore`

```gitignore
bin/
.git/
*.log
gen/bruno/
api/bruno/
```

`.gitignore`

```gitignore
/bin/
*.log
.env
```

Never ignore `gen/`: it is committed.

## Local stack

`compose.yaml`

```yaml
services:
  postgres:
    image: postgres:18-alpine
    environment:
      POSTGRES_USER: taskmanager
      POSTGRES_PASSWORD: taskmanager
      POSTGRES_DB: taskmanager
    ports: ["${PG_PORT:-5432}:5432"]
    volumes: [pgdata:/var/lib/postgresql]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U taskmanager"]
      interval: 2s
      retries: 15

  redis:
    image: redis:8-alpine
    ports: ["${REDIS_PORT:-6379}:6379"]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 2s
      retries: 15

  jaeger:
    image: jaegertracing/jaeger:2.10.0
    ports:
      - "16686:16686" # UI
      - "4318:4318"   # OTLP/HTTP

  migrate:
    build: .
    command: ["migrate", "up"]
    environment:
      DB_DSN: postgres://taskmanager:taskmanager@postgres:5432/taskmanager?sslmode=disable
    depends_on:
      postgres: { condition: service_healthy }

  api:
    build: .
    environment:
      DB_DSN: postgres://taskmanager:taskmanager@postgres:5432/taskmanager?sslmode=disable
      REDIS_URL: redis://redis:6379/0
      OTEL_SERVICE_NAME: taskmanager
      OTEL_TRACES_EXPORTER: otlp
      OTEL_EXPORTER_OTLP_PROTOCOL: http/protobuf
      OTEL_EXPORTER_OTLP_ENDPOINT: http://jaeger:4318
      LOG_FORMAT: json
    ports: ["${API_PORT:-8080}:8080", "${ADMIN_PORT:-9090}:9090"]
    depends_on:
      migrate: { condition: service_completed_successfully }
      redis: { condition: service_healthy }

volumes:
  pgdata:
```

`API_PORT=18080 ADMIN_PORT=19090 PG_PORT=15432 REDIS_PORT=16379 docker compose
up -d --build` when default ports are taken. The `migrate` service runs before
`api`, which is the same ordering production uses.

## CI

1. `make check-generated` (gen/ is current; needs Go and Node)
2. `make fmt-check` and `make lint`
3. `make test` (needs Docker for testcontainers)
4. `docker build .`
