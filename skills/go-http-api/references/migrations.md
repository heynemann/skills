# Migrations (goose)

Versioned SQL files embedded in the binary and applied by
`taskmanager migrate up`, run as a separate step (Kubernetes Job / init
container, compose `migrate` service) before new pods start. The server never
migrates on boot, so replicas never race and a failed migration never takes
serving pods down.

`migrations/embed.go`

```go
// Package migrations embeds goose SQL migrations into the binary.
package migrations

import "embed"

//go:embed *.sql
var FS embed.FS
```

`migrations/20261008120000_create_projects.sql`

```sql
-- +goose Up
CREATE TABLE projects (
    id         uuid PRIMARY KEY,
    slug       text        NOT NULL CONSTRAINT projects_slug_key UNIQUE,
    name       text        NOT NULL,
    team       text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- +goose Down
DROP TABLE projects;
```

`migrations/20261008120100_create_tasks.sql`

```sql
-- +goose Up
CREATE TABLE tasks (
    id               uuid PRIMARY KEY,
    project_id       uuid        NOT NULL CONSTRAINT tasks_project_id_fkey REFERENCES projects (id),
    assignee_name    text        NOT NULL,
    status           text        NOT NULL CONSTRAINT tasks_status_check
                                 CHECK (status IN ('todo', 'in_progress', 'in_review', 'done', 'cancelled')),
    estimate_minutes bigint      NOT NULL CHECK (estimate_minutes >= 0),
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX tasks_project_id_idx ON tasks (project_id);

CREATE TABLE subtasks (
    id                  uuid PRIMARY KEY,
    task_id             uuid        NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    name                text        NOT NULL,
    sessions            integer     NOT NULL CHECK (sessions > 0),
    minutes_per_session bigint      NOT NULL CHECK (minutes_per_session >= 0),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX subtasks_task_id_idx ON subtasks (task_id);

-- +goose Down
DROP TABLE subtasks;
DROP TABLE tasks;
```

## Command

`internal/cli/migrate.go`

```go
package cli

import (
	"database/sql"
	"errors"
	"fmt"

	_ "github.com/jackc/pgx/v5/stdlib" // database/sql driver "pgx"
	"github.com/pressly/goose/v3"
	"github.com/spf13/cobra"

	"example.com/acme/taskmanager/internal/platform/config"
	"example.com/acme/taskmanager/internal/platform/db"
	"example.com/acme/taskmanager/migrations"
)

func newMigrateCmd() *cobra.Command {
	cmd := &cobra.Command{Use: "migrate", Short: "Apply or inspect database migrations (DB_DSN)"}
	cmd.AddCommand(
		&cobra.Command{
			Use:   "up",
			Short: "Apply all pending migrations",
			RunE: withProvider(func(cmd *cobra.Command, p *goose.Provider) error {
				res, err := p.Up(cmd.Context())
				for _, r := range res {
					fmt.Fprintln(cmd.OutOrStdout(), r)
				}
				return err
			}),
		},
		&cobra.Command{
			Use:   "down",
			Short: "Roll back the latest migration",
			RunE: withProvider(func(cmd *cobra.Command, p *goose.Provider) error {
				res, err := p.Down(cmd.Context())
				if res != nil {
					fmt.Fprintln(cmd.OutOrStdout(), res)
				}
				return err
			}),
		},
		&cobra.Command{
			Use:   "status",
			Short: "Show applied and pending migrations",
			RunE: withProvider(func(cmd *cobra.Command, p *goose.Provider) error {
				sts, err := p.Status(cmd.Context())
				for _, s := range sts {
					fmt.Fprintf(cmd.OutOrStdout(), "%-8s %s\n", s.State, s.Source.Path)
				}
				return err
			}),
		},
		&cobra.Command{
			Use:   "create NAME",
			Short: "Create a new SQL migration in ./migrations",
			Args:  cobra.ExactArgs(1),
			RunE: func(_ *cobra.Command, args []string) error {
				return goose.Create(nil, "migrations", args[0], "sql")
			},
		},
	)
	return cmd
}

func withProvider(
	run func(*cobra.Command, *goose.Provider) error,
) func(*cobra.Command, []string) error {
	return func(cmd *cobra.Command, _ []string) (err error) {
		cfg, err := db.NewConfig(config.FromEnviron())
		if err != nil {
			return err
		}
		sqlDB, err := sql.Open("pgx", cfg.DSN)
		if err != nil {
			return err
		}
		defer func() { err = errors.Join(err, sqlDB.Close()) }()
		p, err := goose.NewProvider(goose.DialectPostgres, sqlDB, migrations.FS)
		if err != nil {
			return err
		}
		return run(cmd, p)
	}
}
```

- `migrate create add_task_labels` (or `make migration name=add_task_labels`)
  writes `migrations/<timestamp>_add_task_labels.sql`. Timestamps avoid version
  collisions between branches.
- `migrate status` lists applied/pending; `migrate down` rolls back one.

## Rules

- Never edit a migration that has been applied anywhere; add a new one.
- Every file has `-- +goose Up` and `-- +goose Down`.
- Name every constraint and index explicitly; repositories match on constraint
  names (database.md).
- Foreign keys without `ON DELETE RESTRICT` (default NO ACTION raises `23503`);
  use `ON DELETE CASCADE` only for owned children (`subtasks`).
- `id uuid PRIMARY KEY` without a default: the application generates UUIDv7.
- Index every foreign key column.
- Large tables: `CREATE INDEX CONCURRENTLY` needs `-- +goose NO TRANSACTION` at
  the top of the file and nothing else in it.
- Expand/contract for breaking changes: add nullable column → deploy code
  writing both → backfill → make `NOT NULL` / drop old column in a later
  migration.
- Tests run the real migrations once per package into a template database
  (testing.md), so a broken migration fails `make test`.
