---
name: go-http-api
description: Go HTTP API services on Fiber v3, fx, zap, GORM/PostgreSQL, Redis, Prometheus and OpenTelemetry. Use when scaffolding a Go API service, adding a resource/endpoint/feature module to one, or changing its middleware, caching, migrations, observability or Bruno collection.
---

# Go HTTP API services

An opinionated stack and layout for Go HTTP APIs. The contract is written first
in OpenAPI, and each feature is a self-wiring fx module. Every reference below
was verified end-to-end against a running service (Go 1.27, Fiber v3.5, Postgres
18, Redis 8) in October 2026. For general Go topics (concurrency, generics,
interfaces, idioms), use `skill://golang-pro`.

## Stack

| Concern | Choice | Reference |
| --- | --- | --- |
| Layout, layers, wiring | feature packages under `internal/`, generated code under `gen/` | [architecture.md](references/architecture.md) |
| Dependency injection | `go.uber.org/fx`: one `Module` per package | [fx.md](references/fx.md) |
| Configuration | `caarlos0/env/v11`, a prefix per module, injectable `config.Source` | [config.md](references/config.md) |
| Logging | `zap`, request-scoped `logger.From(ctx)`, GORM adapter | [logging.md](references/logging.md) |
| HTTP | Fiber v3, sonic JSON, RFC 9457 problems, admin port, graceful drain | [http.md](references/http.md) |
| Contract | OpenAPI 3.0.3, oapi-codegen strict server per tag, kin-openapi validator, keyset cursors, `/v1` | [openapi.md](references/openapi.md) |
| Data | GORM + PostgreSQL 18, UUIDv7, TxManager in ctx, per-feature errors | [database.md](references/database.md) |
| Schema | goose SQL migrations embedded in the binary, `migrate` subcommand (cobra) | [migrations.md](references/migrations.md) |
| Cache, rate limit | go-redis v9, repository decorator, singleflight, fail-open; `redis_rate` GCRA | [redis.md](references/redis.md) |
| Metrics, traces | Prometheus `client_golang`; OpenTelemetry with `autoexport` | [observability.md](references/observability.md) |
| Tests | testify + mockery, testcontainers with a template DB, fxtest + `app.Test` | [testing.md](references/testing.md) |
| Build, lint, format, run | `go tool`, Makefile, golangci-lint v2 (lint, forbidigo stack rules, gofumpt/goimports/golines), distroless Docker, compose | [tooling.md](references/tooling.md) |
| Auth (opt-in) | JWT bearer via JWKS, enforced through spec `security` | [auth.md](references/auth.md) |
| Manual API testing | Bruno collection generated from the spec into `gen/bruno` | [bruno.md](references/bruno.md) |

When you change one concern in an existing service, read that row's reference
and follow its rules. Check the invariants below before you finish.

## Invariants

These hold in every service built with this skill. A change that breaks one is
wrong even if it compiles. `make lint` enforces the library choices behind them
with forbidigo (tooling.md): no mutexes (xsync/atomic), no logger but zap, no
JSON but sonic, no `os.Getenv`, no global Prometheus registry, no
`AutoMigrate`/`Save`, no UUIDv4, no `time.Sleep`.

1. **Contract first.** `api/openapi.yaml` defines every operation. The validator
   rejects requests that don't match it before any handler runs, so handlers
   never re-check shape. Handlers return only the responses the spec declares.
2. **Generated code goes in `gen/` only** (`gen/go/<feature>api`,
   `gen/go/httpapi`, `gen/go/<feature>mocks`, `gen/bruno`). It is committed and
   never edited by hand. Run `make generate` after every change to the spec or
   to an interface.
3. **Features wire themselves.** `internal/<feature>/module.go` provides the
   repository, service and handler, and registers routes through
   `httpserver.AsRoutes`. Turning a feature on means adding `<feature>.Module`
   to `internal/app/app.go`.
4. **Config is per module.** Each module parses its own env prefix from
   `config.Source`. Nothing calls `os.Getenv`.
5. **Repositories are the only GORM users.** They start every query with
   `tm.DB(ctx)`. They translate `gorm`/`pgconn` errors into the feature's
   sentinel errors, matching on named constraints. Services depend on the
   `Repository` interface they declare themselves, and on `db.Transactor`.
6. **Errors become HTTP responses in exactly two places.** Expected feature
   errors become typed spec responses in `handler.go`. Everything else becomes a
   logged 500 in `httpserver.ErrorHandler`. Response bodies never contain
   internal error text.
7. **Log or return an error, not both.** Inside a request, log through
   `logger.From(ctx)` so every line carries `request_id` and `trace_id`.
8. **Cache only through a repository decorator.** Invalidate with
   `db.AfterCommit`, bypass the cache inside transactions, and fail open.
9. **Metric labels stay bounded.** The route label is the template, never the
   raw path. No metric uses the global Prometheus registry.
10. **Migrations own the schema.** Never call `AutoMigrate`. IDs are UUIDv7,
    generated in Go by `db.Base`. Convert timestamps with `.UTC()` at the DTO
    boundary.
11. **Runnable as documented.** From a fresh clone and a clean shell, every
    command in the README works as written: `make deps-up migrate-up run` serves
    the API with no exported variables, and a missing tool, dependency or
    setting fails with one line naming the fix. A command that needs an
    undocumented `export` is a bug in the Makefile defaults, not in the docs.

## Workflow A: scaffold a new service

Ask for these if they're missing: the module path, the binary name, and the
first feature with its operations. Then work through the steps in order. Each
step has a done-condition; don't start the next step until it holds.

1. **Module, tools and Makefile** ([tooling.md](references/tooling.md)): run
   `go mod init`, then `go mod edit -go=1.27`, then add the `go get -tool` lines
   for oapi-codegen and mockery. Add the Makefile now; step 3 needs
   `make generate-api`. Node must be installed: `make generate-bruno` runs
   pinned tools through `npx`.
   *Done:* `go tool oapi-codegen -version` and `go tool mockery version` both
   print a version.
2. **Platform packages.** Create
   `internal/platform/{config,logger,otel,metrics,db,redisx,cache,ratelimit,admin,problem,pagination,httpserver}`
   and `platform.go`, taking the code from config.md, logging.md,
   observability.md, database.md, redis.md, http.md and openapi.md (validator
   and pagination). Leave `auth/` out unless the user asked for auth.
   *Done:* every package in architecture.md's layout exists. They compile only
   after step 3 generates `gen/go/httpapi` and adds `api/embed.go`.
3. **Contract** ([openapi.md](references/openapi.md)): write `api/common.yaml`,
   `api/openapi.yaml` (`servers: /v1`, the first feature's tag), `api/embed.go`,
   `api/codegen/httpapi.yaml`, one `api/codegen/<feature>.yaml` per tag, and
   `api/bruno/environments/local.yml` ([bruno.md](references/bruno.md)).
   *Done:* `make generate-api generate-bruno` writes `gen/go/httpapi`,
   `gen/go/<feature>api` and `gen/bruno` without errors.
4. **Entrypoint and schema** ([architecture.md](references/architecture.md),
   [migrations.md](references/migrations.md)): write `cmd/<binary>/main.go`,
   `internal/cli/{root,serve,migrate}.go`, `internal/app/app.go`,
   `migrations/embed.go` and the first migration. Write that migration by hand
   as `migrations/<YYYYMMDDhhmmss>_create_<things>.sql`: `//go:embed *.sql`
   doesn't compile with no files, so `make migration` only works once one
   exists. Then run `go mod tidy`.
   *Done:* `go build ./...` succeeds.
5. **First feature:** follow Workflow B.
6. **Test kit** ([testing.md](references/testing.md)): write
   `internal/testkit/{testkit,main}.go` and `.mockery.yml`, then run
   `go mod tidy`.
7. **Tooling** ([tooling.md](references/tooling.md)): add `.golangci.yml`, the
   Dockerfile, `.dockerignore`, `compose.yaml` and `.gitignore`. Write the
   README: a Run section that is exactly `make deps-up`, `make migrate-up`,
   `make run`, the endpoints, the dev commands (`make generate/fmt/lint/test`),
   and the env var table (config.md).
8. **Verify:** pass the gate below.

## Workflow B: add a feature, resource or endpoint

To add an endpoint to an existing feature, run steps 1, 2 and 4 to 8 and skip
whatever doesn't change.

1. **Spec:** add the paths, tagged `<feature>`, plus their schemas. List every
   error status each operation can return, using the shared responses from
   `common.yaml`. Give request properties `example`s so the Bruno requests are
   ready to send ([openapi.md](references/openapi.md)).
2. **Codegen:** for a new feature, copy `api/codegen/<other>.yaml` and change
   `package`, `output` and `include-tags`. Then run
   `make generate-api generate-bruno`.
3. **Migration:** run `make migration name=create_<things>` (a service's first
   feature already has one from Workflow A step 4). Name every constraint, and
   give foreign keys no `RESTRICT` ([migrations.md](references/migrations.md)).
4. **Feature package** `internal/<feature>/`
   ([architecture.md](references/architecture.md),
   [database.md](references/database.md), [openapi.md](references/openapi.md)):
   - `model.go`: GORM model embedding `db.Base`.
   - `errors.go`: sentinel errors whose messages are safe to show clients.
   - `service.go`: the `Repository` interface, input structs, and `Service`
     (`WithinTx` for anything that must be atomic).
   - `repository.go`: a GORM implementation with `translate`.
   - `handler.go`: the strict handler, with a
     `var _ <feature>api.StrictServerInterface = (*Handler)(nil)` check, a
     `switch` mapping errors to typed responses, a `toDTO` that calls `.UTC()`,
     and `NewRoutes`.
   - `module.go`: provides the above, plus `fx.Decorate(NewCachedRepository)` if
     reads are hot ([redis.md](references/redis.md)).
5. **Wire it:** add `<feature>.Module` to `internal/app/app.go`. Add the
   `Repository` interface to `.mockery.yml`, then run `make generate-mocks`.
6. **Tests** ([testing.md](references/testing.md)), all in
   `package <feature>_test`:
   - `main_test.go`
   - service unit tests for each rule
   - a repository test for each translated error
   - an API test per operation covering the happy path and its main error
7. **Metrics or spans** for business events, only if the feature needs them
   ([observability.md](references/observability.md)).
8. **Verify:** pass the gate below.

## Verification gate

Work is done only when all of these hold:

- `make generate` leaves `git diff -- gen/` limited to the changes you intended.
  In CI, `make check-generated` passes.
- `make fmt` has been run and `make fmt-check` exits 0 (no diff).
- `make lint` reports `0 issues`.
- `make test` passes. It needs Docker for testcontainers.
- Clean-shell run, after scaffolding or any change to the Makefile, config or
  startup: in a shell with nothing exported
  (`env -i HOME="$HOME" PATH="$PATH" bash`), run the README's Run commands
  verbatim (`make deps-up migrate-up run`). The API must come up. Also start it
  once with Postgres stopped and confirm the output is a single
  `startup failed: ...` line. If the default ports are taken, set only
  `PG_PORT`/`REDIS_PORT`/`HTTP_ADDR`/`ADMIN_ADDR`, plus `COMPOSE_PROJECT_NAME`
  so you don't touch someone's running stack.
- Smoke test against that running service: `curl` each new or changed operation
  once on its happy path and once on a 4xx path. Confirm that:
  - 4xx responses are `application/problem+json` with a `request_id`;
  - `curl <ADMIN_ADDR>/metrics` shows the operation's `route` template.
- Drain check, after changes to the server or lifecycle: with `make run` up,
  send `kill -TERM` to the server process (the pid listening on `HTTP_ADDR`,
  e.g. `lsof -ti :8080`). For `HTTP_SHUTDOWN_DRAIN_DELAY`, `/readyz` should
  return 503 `{"status":"shutting_down"}` while the API still answers. After
  that `make run` should exit 0.
