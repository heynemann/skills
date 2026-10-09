# Database: GORM on PostgreSQL 18

Schema is owned by goose migrations (migrations.md); GORM only maps rows.
`AutoMigrate` is never called.

## Module

`internal/platform/db/db.go`

```go
// Package db provides GORM over PostgreSQL, the transaction manager and error predicates.
package db

import (
	"context"
	"fmt"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"go.opentelemetry.io/otel/trace"
	"go.uber.org/fx"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	gormlogger "gorm.io/gorm/logger"
	"gorm.io/plugin/opentelemetry/tracing"

	"example.com/acme/taskmanager/internal/platform/admin"
	"example.com/acme/taskmanager/internal/platform/config"
)

// Config is parsed from DB_* variables.
type Config struct {
	DSN                string        `env:"DSN,required"`
	MaxOpenConns       int           `env:"MAX_OPEN_CONNS"       envDefault:"20"`
	MaxIdleConns       int           `env:"MAX_IDLE_CONNS"       envDefault:"10"`
	ConnMaxLifetime    time.Duration `env:"CONN_MAX_LIFETIME"    envDefault:"30m"`
	ConnMaxIdleTime    time.Duration `env:"CONN_MAX_IDLE_TIME"   envDefault:"5m"`
	SlowQueryThreshold time.Duration `env:"SLOW_QUERY_THRESHOLD" envDefault:"200ms"`
}

// NewConfig parses DB_* variables.
func NewConfig(src config.Source) (Config, error) { return config.Parse[Config](src, "DB_") }

// New opens the pool (pings it), instruments it and closes it on stop.
func New(
	lc fx.Lifecycle,
	cfg Config,
	reg prometheus.Registerer,
	tp trace.TracerProvider,
) (*gorm.DB, error) {
	g, err := gorm.Open(postgres.Open(cfg.DSN), &gorm.Config{
		// Silent while connecting: a failed Open would otherwise log the error that serve
		// already reports as "startup failed: ...". The real logger is attached below.
		Logger: gormlogger.Discard,
		// Writes that need atomicity use TxManager.WithinTx.
		SkipDefaultTransaction: true,
		// timestamptz stores microseconds; truncating keeps POST and GET responses identical.
		NowFunc: func() time.Time { return time.Now().UTC().Truncate(time.Microsecond) },
	})
	if err != nil {
		return nil, fmt.Errorf("open postgres: %w", err)
	}
	g.Logger = NewGormLogger(cfg.SlowQueryThreshold)
	if err := g.Use(
		tracing.NewPlugin(tracing.WithTracerProvider(tp), tracing.WithoutMetrics()),
	); err != nil {
		return nil, err
	}
	sqlDB, err := g.DB()
	if err != nil {
		return nil, err
	}
	sqlDB.SetMaxOpenConns(cfg.MaxOpenConns)
	sqlDB.SetMaxIdleConns(cfg.MaxIdleConns)
	sqlDB.SetConnMaxLifetime(cfg.ConnMaxLifetime)
	sqlDB.SetConnMaxIdleTime(cfg.ConnMaxIdleTime)
	if err := reg.Register(collectors.NewDBStatsCollector(sqlDB, "main")); err != nil {
		return nil, err
	}
	lc.Append(fx.StopHook(sqlDB.Close))
	return g, nil
}

// NewReadinessCheck makes /readyz fail while Postgres is unreachable.
func NewReadinessCheck(g *gorm.DB) admin.Check {
	return admin.Check{Name: "postgres", Fn: func(ctx context.Context) error {
		sqlDB, err := g.DB()
		if err != nil {
			return err
		}
		return sqlDB.PingContext(ctx)
	}}
}

// Module provides *gorm.DB, *TxManager and Transactor.
var Module = fx.Module("db",
	fx.Provide(
		NewConfig,
		New,
		NewTxManager,
		func(m *TxManager) Transactor { return m },
		admin.AsCheck(NewReadinessCheck),
	),
)
```

- `SkipDefaultTransaction: true`: single statements run without an implicit
  transaction. Anything that must be atomic (multi-row writes,
  read-modify-write, GORM association inserts) goes through `WithinTx`.
- `gorm.Open` pings: an unreachable database fails startup, and `/readyz`
  reports it afterwards.
- `tracing.WithoutMetrics()`: SQL spans only; pool metrics come from
  `collectors.NewDBStatsCollector` (observability.md).

## Models and IDs

`internal/platform/db/model.go`

```go
package db

import (
	"time"

	"github.com/google/uuid"
	"gorm.io/gorm"
)

// Base is embedded by every GORM model: UUIDv7 primary key plus timestamps.
type Base struct {
	ID        uuid.UUID `gorm:"type:uuid;primaryKey"`
	CreatedAt time.Time
	UpdatedAt time.Time
}

// BeforeCreate assigns a time-ordered UUIDv7 so the ID is known before INSERT.
func (b *Base) BeforeCreate(*gorm.DB) error {
	if b.ID != uuid.Nil {
		return nil
	}
	id, err := uuid.NewV7()
	if err != nil {
		return err
	}
	b.ID = id
	return nil
}
```

`internal/projects/model.go`

```go
package projects

import "example.com/acme/taskmanager/internal/platform/db"

// Project is the GORM model; the service works on it directly.
type Project struct {
	db.Base
	Slug string
	Name string
	Team string
}
```

`internal/tasks/model.go`

```go
package tasks

import (
	"github.com/google/uuid"

	"example.com/acme/taskmanager/internal/platform/db"
)

// Status values mirror the spec enum and the tasks_status_check constraint.
const StatusTodo = "todo"

type Task struct {
	db.Base
	ProjectID       uuid.UUID `gorm:"type:uuid"`
	AssigneeName    string
	Status          string
	EstimateMinutes int64
	Subtasks        []Subtask
}

type Subtask struct {
	db.Base
	TaskID            uuid.UUID `gorm:"type:uuid"`
	Name              string
	Sessions          int
	MinutesPerSession int64
}
```

- UUIDv7 is generated in Go so the ID exists before INSERT (usable in logs,
  events, cache keys) and works on any Postgres version. IDs are time-ordered,
  so they double as the keyset pagination key.
- Columns are `NOT NULL` unless the domain says otherwise; Go fields are plain
  values, pointers only for nullable columns.
- Soft delete is off. If one entity truly needs it, embed `gorm.DeletedAt` in
  that model only and make its unique indexes partial
  (`WHERE deleted_at IS NULL`).
- pgx scans `timestamptz` into `time.Local`. Convert with `.UTC()` at the DTO
  boundary.

## Transactions

`internal/platform/db/tx.go`

```go
package db

import (
	"context"

	"gorm.io/gorm"
)

// Transactor runs fn atomically. Services depend on this, never on *gorm.DB.
type Transactor interface {
	WithinTx(ctx context.Context, fn func(ctx context.Context) error) error
}

type txKey struct{}

type txState struct {
	tx          *gorm.DB
	afterCommit []func(context.Context)
}

// TxManager stores the open transaction in ctx so repositories join it transparently.
type TxManager struct {
	db *gorm.DB
}

// NewTxManager wraps the pool.
func NewTxManager(db *gorm.DB) *TxManager { return &TxManager{db: db} }

// WithinTx runs fn in a transaction. Nested calls join the outer transaction.
// AfterCommit hooks run only after the outermost commit succeeds.
func (m *TxManager) WithinTx(ctx context.Context, fn func(ctx context.Context) error) error {
	if _, ok := ctx.Value(txKey{}).(*txState); ok {
		return fn(ctx)
	}
	st := &txState{}
	err := m.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		st.tx = tx
		return fn(context.WithValue(ctx, txKey{}, st))
	})
	if err != nil {
		return err
	}
	for _, hook := range st.afterCommit {
		hook(ctx)
	}
	return nil
}

// DB returns the transaction bound to ctx, or the pool. Every repository query starts here.
func (m *TxManager) DB(ctx context.Context) *gorm.DB {
	if st, ok := ctx.Value(txKey{}).(*txState); ok {
		return st.tx.WithContext(ctx)
	}
	return m.db.WithContext(ctx)
}

// InTx reports whether ctx carries an open transaction.
func InTx(ctx context.Context) bool {
	_, ok := ctx.Value(txKey{}).(*txState)
	return ok
}

// AfterCommit defers fn until the surrounding transaction commits; without one it runs now.
// Use it for side effects that must not observe uncommitted state (cache invalidation, events).
func AfterCommit(ctx context.Context, fn func(context.Context)) {
	if st, ok := ctx.Value(txKey{}).(*txState); ok {
		st.afterCommit = append(st.afterCommit, fn)
		return
	}
	fn(ctx)
}
```

- Services depend on `db.Transactor`; unit tests pass a fake that just calls
  `fn(ctx)`.
- Repositories always start queries with `tm.DB(ctx)`, which joins the open
  transaction if there is one.
- `AfterCommit` defers side effects that must not observe uncommitted state:
  cache invalidation, publishing events. Without a transaction it runs
  immediately.
- Nested `WithinTx` joins the outer transaction (no savepoints).

## Repository

`internal/projects/repository.go`

```go
package projects

import (
	"context"
	"errors"
	"fmt"

	"github.com/google/uuid"
	"gorm.io/gorm"

	"example.com/acme/taskmanager/internal/platform/db"
)

type gormRepository struct {
	tm *db.TxManager
}

// NewRepository returns the Postgres implementation.
func NewRepository(tm *db.TxManager) Repository { return &gormRepository{tm: tm} }

func (r *gormRepository) Create(ctx context.Context, m *Project) error {
	return translate(r.tm.DB(ctx).Create(m).Error)
}

func (r *gormRepository) Get(ctx context.Context, id uuid.UUID) (*Project, error) {
	var m Project
	if err := r.tm.DB(ctx).First(&m, "id = ?", id).Error; err != nil {
		return nil, translate(err)
	}
	return &m, nil
}

// List is keyset pagination: UUIDv7 ids are time-ordered, so "id < cursor" pages newest first.
func (r *gormRepository) List(
	ctx context.Context,
	after uuid.UUID,
	limit int,
) ([]Project, error) {
	q := r.tm.DB(ctx).Order("id DESC").Limit(limit)
	if after != uuid.Nil {
		q = q.Where("id < ?", after)
	}
	var out []Project
	if err := q.Find(&out).Error; err != nil {
		return nil, translate(err)
	}
	return out, nil
}

func (r *gormRepository) Update(ctx context.Context, m *Project) error {
	res := r.tm.DB(ctx).Model(m).Select("slug", "name", "team", "updated_at").Updates(m)
	if res.Error != nil {
		return translate(res.Error)
	}
	if res.RowsAffected == 0 {
		return ErrNotFound
	}
	return nil
}

func (r *gormRepository) Delete(ctx context.Context, id uuid.UUID) error {
	res := r.tm.DB(ctx).Delete(&Project{}, "id = ?", id)
	if res.Error != nil {
		return translate(res.Error)
	}
	if res.RowsAffected == 0 {
		return ErrNotFound
	}
	return nil
}

// translate maps driver errors to this feature's errors at the repository boundary.
func translate(err error) error {
	switch {
	case err == nil:
		return nil
	case errors.Is(err, gorm.ErrRecordNotFound):
		return ErrNotFound
	case db.IsUniqueViolation(err, "projects_slug_key"):
		return ErrSlugTaken
	case db.IsForeignKeyViolation(err, "tasks_project_id_fkey"):
		return ErrHasTasks
	default:
		return fmt.Errorf("projects repository: %w", err)
	}
}
```

Rules:

- Declare the interface in the consumer (`service.go`); return it from
  `NewRepository` so `fx.Decorate` can wrap it.
- `translate` maps errors at the boundary: `gorm.ErrRecordNotFound` →
  `ErrNotFound`, named constraint violations → feature errors, everything else
  wrapped with the repository name. `*gorm.DB`, `pgconn` and GORM errors never
  escape.
- Updates use `Model(m).Select(cols...).Updates(m)` and check `RowsAffected`;
  `Save` would INSERT a row that was deleted concurrently.
- Deletes check `RowsAffected` for 404.
- Keyset list: `ORDER BY id DESC LIMIT n` plus `WHERE id < cursor`. Never
  `OFFSET`.
- Associations: `Create(&task)` inserts `task.Subtasks` too; wrap it in
  `WithinTx` (tasks/service.go). Read with
  `Preload("Subtasks", func(q *gorm.DB) *gorm.DB { return q.Order("id") })`.

## Service

Business rules over the consumer-declared `Repository` and `db.Transactor`.
Inputs are plain structs (not DTOs); lists return a `Page`.

`internal/projects/errors.go`

```go
package projects

import "errors"

var (
	ErrNotFound  = errors.New("project not found")
	ErrSlugTaken = errors.New("project slug already taken")
	ErrHasTasks  = errors.New("project has tasks")
)
```

Sentinel error messages are client-visible (`detail`), so write them for
clients.

`internal/projects/service.go`

```go
package projects

import (
	"context"

	"github.com/google/uuid"

	"example.com/acme/taskmanager/internal/platform/db"
	"example.com/acme/taskmanager/internal/platform/pagination"
)

// Repository is declared by its consumer. Implementations translate driver errors to
// this package's errors; *gorm.DB and pgconn types never cross this boundary.
type Repository interface {
	Create(ctx context.Context, r *Project) error
	Get(ctx context.Context, id uuid.UUID) (*Project, error)
	List(ctx context.Context, after uuid.UUID, limit int) ([]Project, error)
	Update(ctx context.Context, r *Project) error
	Delete(ctx context.Context, id uuid.UUID) error
}

// Service holds the business rules.
type Service struct {
	repo Repository
	tx   db.Transactor
}

// NewService is the fx constructor.
func NewService(repo Repository, tx db.Transactor) *Service { return &Service{repo: repo, tx: tx} }

// CreateInput is what callers may set on creation.
type CreateInput struct {
	Slug, Name, Team string
}

// UpdateInput is a partial update; nil fields are left unchanged.
type UpdateInput struct {
	Slug, Name, Team *string
}

// Page is one keyset page; NextCursor is empty on the last page.
type Page struct {
	Items      []Project
	NextCursor string
}

func (s *Service) Create(ctx context.Context, in CreateInput) (*Project, error) {
	r := &Project{Slug: in.Slug, Name: in.Name, Team: in.Team}
	if err := s.repo.Create(ctx, r); err != nil {
		return nil, err
	}
	return r, nil
}

func (s *Service) Get(ctx context.Context, id uuid.UUID) (*Project, error) {
	return s.repo.Get(ctx, id)
}

// List returns projects newest first. It fetches limit+1 rows to know whether a next page exists.
func (s *Service) List(ctx context.Context, cursor *string, limit *int) (Page, error) {
	after, err := pagination.Decode(cursor)
	if err != nil {
		return Page{}, err
	}
	n := pagination.Limit(limit)
	items, err := s.repo.List(ctx, after, n+1)
	if err != nil {
		return Page{}, err
	}
	page := Page{Items: items}
	if len(items) > n {
		page.Items = items[:n]
		page.NextCursor = pagination.Encode(items[n-1].ID)
	}
	return page, nil
}

// Update is read-modify-write, so it runs in one transaction.
func (s *Service) Update(ctx context.Context, id uuid.UUID, in UpdateInput) (*Project, error) {
	var out *Project
	err := s.tx.WithinTx(ctx, func(ctx context.Context) error {
		r, err := s.repo.Get(ctx, id)
		if err != nil {
			return err
		}
		if in.Slug != nil {
			r.Slug = *in.Slug
		}
		if in.Name != nil {
			r.Name = *in.Name
		}
		if in.Team != nil {
			r.Team = *in.Team
		}
		if err := s.repo.Update(ctx, r); err != nil {
			return err
		}
		out = r
		return nil
	})
	return out, err
}

func (s *Service) Delete(ctx context.Context, id uuid.UUID) error {
	return s.repo.Delete(ctx, id)
}
```

`internal/tasks/service.go`

```go
package tasks

import (
	"context"

	"github.com/google/uuid"

	"example.com/acme/taskmanager/internal/platform/db"
)

// Repository is declared by its consumer.
type Repository interface {
	Create(ctx context.Context, o *Task) error
	Get(ctx context.Context, id uuid.UUID) (*Task, error)
}

type Service struct {
	repo Repository
	tx   db.Transactor
}

func NewService(repo Repository, tx db.Transactor) *Service { return &Service{repo: repo, tx: tx} }

type SubtaskInput struct {
	Name              string
	Sessions          int
	MinutesPerSession int64
}

type CreateInput struct {
	ProjectID    uuid.UUID
	AssigneeName string
	Subtasks     []SubtaskInput
}

// Create inserts the task and its subtasks atomically.
func (s *Service) Create(ctx context.Context, in CreateInput) (*Task, error) {
	t := &Task{ProjectID: in.ProjectID, AssigneeName: in.AssigneeName, Status: StatusTodo}
	for _, st := range in.Subtasks {
		t.Subtasks = append(
			t.Subtasks,
			Subtask{Name: st.Name, Sessions: st.Sessions, MinutesPerSession: st.MinutesPerSession},
		)
		t.EstimateMinutes += int64(st.Sessions) * st.MinutesPerSession
	}
	if err := s.tx.WithinTx(
		ctx,
		func(ctx context.Context) error { return s.repo.Create(ctx, t) },
	); err != nil {
		return nil, err
	}
	return t, nil
}

func (s *Service) Get(
	ctx context.Context,
	id uuid.UUID,
) (*Task, error) {
	return s.repo.Get(ctx, id)
}
```

## Postgres error predicates

`internal/platform/db/errors.go`

```go
package db

import (
	"errors"

	"github.com/jackc/pgx/v5/pgconn"
)

const (
	codeUniqueViolation     = "23505"
	codeForeignKeyViolation = "23503"
)

// IsUniqueViolation reports a unique constraint violation, optionally on a named constraint.
func IsUniqueViolation(err error, constraint ...string) bool {
	return isPgError(err, codeUniqueViolation, constraint)
}

// IsForeignKeyViolation reports a foreign key violation, optionally on a named constraint.
func IsForeignKeyViolation(err error, constraint ...string) bool {
	return isPgError(err, codeForeignKeyViolation, constraint)
}

func isPgError(err error, code string, constraint []string) bool {
	var pg *pgconn.PgError
	if !errors.As(err, &pg) || pg.Code != code {
		return false
	}
	return len(constraint) == 0 || pg.ConstraintName == constraint[0]
}
```

| SQLSTATE | Meaning | Typical mapping |
| --- | --- | --- |
| `23505` | unique_violation | `ErrSlugTaken` → 409 |
| `23503` | foreign_key_violation (FK declared without an action, i.e. NO ACTION) | insert with unknown parent → 422; delete parent with children → 409 |
| `23001` | restrict_violation (FK declared `ON DELETE RESTRICT`) | same as above; declare FKs without `RESTRICT` so one predicate covers both cases |
| `23514` | check_violation | usually a bug: the spec should have rejected it |

Always name constraints in migrations (`CONSTRAINT projects_slug_key UNIQUE`)
and match on the name, so two unique columns map to two different errors.

## Pool sizing

`DB_MAX_OPEN_CONNS` × replicas must stay below Postgres `max_connections` minus
headroom for migrations and admin. Watch `go_sql_wait_count_total`
(observability.md); a rising rate means the pool is too small or queries too
slow.
