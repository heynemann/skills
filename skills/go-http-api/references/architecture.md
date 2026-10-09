# Architecture

Snippets across this skill use module `example.com/acme/taskmanager`, binary
`taskmanager`, and the ACME Inc. task-manager domain (`projects`, `tasks`).
Substitute your module path, binary and features.

## Layout

```text
api/
  openapi.yaml            # the contract: every operation, tagged with its feature
  common.yaml             # shared components (Problem, FieldError, Limit, Cursor, error responses)
  codegen/<x>.yaml        # one oapi-codegen config per generated package
  bruno/environments/     # Bruno environments (local.yml), copied into gen/bruno
  embed.go                # embeds *.yaml for the request validator
cmd/taskmanager/main.go   # signal context + cobra root, nothing else
gen/                      # ALL generated code, committed, never edited
  go/httpapi/             #   shared types from common.yaml
  go/<feature>api/        #   DTOs + strict server interface for one tag
  go/<feature>mocks/      #   mockery testify mocks of that feature's interfaces
  bruno/                  #   Bruno collection generated from the spec (bruno.md)
internal/
  app/app.go              # Modules(): platform + every feature module
  cli/                    # cobra: serve, migrate up|down|status|create
  platform/               # infrastructure modules, one package each
    platform.go           # Module bundle (order = lifecycle order)
    config/ logger/ otel/ metrics/ db/ redisx/ cache/ ratelimit/ admin/ httpserver/
    problem/ pagination/  # helpers, no fx module
    auth/                 # optional, see auth.md
  <feature>/              # one package per feature, files by role:
    model.go errors.go service.go repository.go [cache.go] handler.go module.go
  testkit/                # containers + fx test app
migrations/               # goose SQL files + embed.go
```

A feature is one flat package; roles are files, not sub-packages. That keeps the
consumer-declared `Repository` interface next to the service that uses it and
avoids import cycles.

## Dependency rules

- `cmd` → `internal/cli` → `internal/app` → features + `platform`. Features
  never import each other; cross-feature integrity lives in the database
  (foreign keys) or goes through an interface the consumer declares.
- `platform/*` never imports a feature.
- Only `repository.go` (and `cache.go`) touch GORM/Redis. Services depend on the
  `Repository` interface they declare and on `db.Transactor`.
- Only `handler.go` touches `gen/go/<feature>api` DTOs. The service speaks GORM
  models (`model.go`) and plain input structs (`CreateInput`, `UpdateInput`).
- Interfaces are declared by the consumer (`projects.Repository` lives in
  `service.go`), never in the implementing package.

## Request flow

`requestid → otel → metrics → request logger → recover → [cors, helmet] → /v1:
[authn] → [limiter] → OpenAPI validator → generated strict wrapper → Handler →
Service → Repository (→ cache decorator) → Postgres`

Errors flow back as values: repository translates driver errors into the
feature's sentinel errors, the handler turns expected ones into typed spec
responses, and anything else becomes a 500 in `httpserver.ErrorHandler`. See
http.md.

## Wiring

`internal/app/app.go`

```go
// Package app lists the modules that make up the service. serve and tests both use it.
package app

import (
	"go.uber.org/fx"

	"example.com/acme/taskmanager/internal/platform"
	"example.com/acme/taskmanager/internal/projects"
)

// Modules is the whole service. Adding a feature = adding its Module here.
func Modules() fx.Option {
	return fx.Options(
		platform.Module,
		projects.Module,
	)
}
```

`internal/platform/platform.go`

```go
// Package platform bundles every infrastructure module a service needs.
package platform

import (
	"go.uber.org/fx"

	"example.com/acme/taskmanager/internal/platform/admin"
	"example.com/acme/taskmanager/internal/platform/cache"
	"example.com/acme/taskmanager/internal/platform/config"
	"example.com/acme/taskmanager/internal/platform/db"
	"example.com/acme/taskmanager/internal/platform/httpserver"
	"example.com/acme/taskmanager/internal/platform/logger"
	"example.com/acme/taskmanager/internal/platform/metrics"
	"example.com/acme/taskmanager/internal/platform/otel"
	"example.com/acme/taskmanager/internal/platform/ratelimit"
	"example.com/acme/taskmanager/internal/platform/redisx"
)

// Module order is lifecycle order: admin is invoked before httpserver, so on shutdown the
// public server drains first and /metrics + /readyz stay up until it is done.
var Module = fx.Options(
	fx.Provide(config.FromEnviron),
	logger.Module,
	otel.Module,
	metrics.Module,
	db.Module,
	redisx.Module,
	cache.Module,
	ratelimit.Module,
	admin.Module,
	httpserver.Module,
)
```

`internal/projects/module.go`

```go
// Package projects is the projects feature: model, repository (+cache), service, HTTP handler.
package projects

import (
	"go.uber.org/fx"

	"example.com/acme/taskmanager/internal/platform/httpserver"
)

// Module wires the whole feature; adding it to fx.New is all a service needs.
var Module = fx.Module("projects",
	fx.Provide(
		NewRepository,
		NewService,
		NewHandler,
		httpserver.AsRoutes(NewRoutes),
	),
	fx.Decorate(NewCachedRepository),
)
```

Adding a feature touches `internal/app/app.go` (one line), `.mockery.yml`, the
spec and a codegen config; nothing in `platform/`.

## Types per layer

| Layer | Types | Rule |
| --- | --- | --- |
| HTTP | `gen/go/<feature>api` DTOs, request/response objects | built only in `handler.go` via `toDTO` |
| Service | GORM model + input structs | no Fiber, no `*gorm.DB`; transactions via `db.Transactor` |
| Repository | GORM model | the only place that sees `*gorm.DB`, `pgconn` |

`toDTO` converts timestamps with `.UTC()`: pgx scans `timestamptz` into
`time.Local`.

## Entrypoint

`cmd/taskmanager/main.go`

```go
package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"example.com/acme/taskmanager/internal/cli"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	err := cli.NewRoot().ExecuteContext(ctx)
	stop()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
```

`internal/cli/root.go`

```go
// Package cli is the cobra command tree: serve and migrate.
package cli

import "github.com/spf13/cobra"

// NewRoot builds the root command.
func NewRoot() *cobra.Command {
	root := &cobra.Command{
		Use:           "taskmanager",
		Short:         "ACME Task Manager HTTP API",
		SilenceUsage:  true,
		SilenceErrors: true,
	}
	root.AddCommand(newServeCmd(), newMigrateCmd())
	return root
}
```

`internal/cli/serve.go`

```go
package cli

import (
	"context"
	"fmt"
	"time"

	"github.com/spf13/cobra"
	"go.uber.org/dig"
	"go.uber.org/fx"

	"example.com/acme/taskmanager/internal/app"
)

const (
	startTimeout = 15 * time.Second
	stopTimeout  = 30 * time.Second
)

func newServeCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "serve",
		Short: "Run the HTTP API (public + admin listeners)",
		RunE: func(cmd *cobra.Command, _ []string) error {
			fxApp := fx.New(
				app.Modules(),
				fx.StartTimeout(startTimeout),
				fx.StopTimeout(stopTimeout),
			)
			if err := fxApp.Err(); err != nil { // a constructor failed, e.g. missing DB_DSN
				return startupError(err)
			}
			startCtx, cancel := context.WithTimeout(cmd.Context(), startTimeout)
			defer cancel()
			if err := fxApp.Start(
				startCtx,
			); err != nil { // an OnStart hook failed, e.g. port in use
				return startupError(err)
			}

			sig := <-fxApp.Wait() // SIGINT/SIGTERM, or fx.Shutdowner when a server dies

			stopCtx, cancelStop := context.WithTimeout(
				context.WithoutCancel(cmd.Context()),
				stopTimeout,
			)
			defer cancelStop()
			if err := fxApp.Stop(stopCtx); err != nil {
				return fmt.Errorf("shutdown: %w", err)
			}
			if sig.ExitCode != 0 {
				return fmt.Errorf(
					"server stopped unexpectedly (exit code %d); see the log above",
					sig.ExitCode,
				)
			}
			return nil
		},
	}
}

// startupError reports the error the failing constructor or hook returned, not fx's chain
// of "could not build arguments for ..." wrappers. LOG_LEVEL=debug shows the full chain.
func startupError(err error) error {
	return fmt.Errorf("startup failed: %w", dig.RootCause(err))
}
```
