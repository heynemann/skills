# Testing

| Level | What | Dependencies | Runs under `-short` |
| --- | --- | --- | --- |
| Service unit | business rules, pagination math, partial updates | mockery testify mocks + fake `Transactor` | yes |
| Handler unit | feature error → typed spec response | real service over mocked repository | yes |
| Repository integration | SQL, constraint → error translation, keyset paging | real Postgres (template clone) | no |
| API end-to-end | contract (validator), wiring, cache, limiter, auth | full fx graph, Postgres + Redis containers, `app.Test` | no |

Every feature gets at least: one unit test per service rule, one repository test
per translated error, one API test per operation's happy path plus its most
important error. `make test-unit` (`-short`) needs no Docker; `make test` runs
everything.

## Mocks: mockery v3, testify template, generated into gen/

`.mockery.yml`

```yaml
# mockery v3: testify mocks for consumer-declared interfaces -> gen/go/<pkg>mocks
template: testify
formatter: goimports
dir: "gen/go/{{.SrcPackageName}}mocks"
pkgname: "{{.SrcPackageName}}mocks"
filename: "{{.InterfaceName | snakecase}}.go"
structname: "{{.InterfaceName}}"
force-file-write: true
packages:
  example.com/acme/taskmanager/internal/projects:
    interfaces:
      Repository: {}
  example.com/acme/taskmanager/internal/tasks:
    interfaces:
      Repository: {}
```

- Mocks are generated code, so they live in `gen/go/<pkg>mocks` (exported
  package `projectsmocks`, type `Repository`, constructor `NewRepository(t)`
  which asserts expectations on cleanup).
- `projectsmocks` imports `projects` (for the model types), so tests importing
  the mocks must be external test packages: `package projects_test`. All tests
  in this skill are black-box tests.
- Add each new consumer-declared interface to `.mockery.yml`, then
  `make generate-mocks`.

## Unit tests

`internal/projects/service_test.go`

```go
package projects_test

import (
	"context"
	"testing"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/mock"
	"github.com/stretchr/testify/require"

	"example.com/acme/taskmanager/gen/go/projectsmocks"
	"example.com/acme/taskmanager/internal/platform/pagination"
	"example.com/acme/taskmanager/internal/projects"
)

// inlineTx runs fn directly; the transaction boundary itself is covered by integration tests.
type inlineTx struct{}

func (inlineTx) WithinTx(
	ctx context.Context,
	fn func(context.Context) error,
) error {
	return fn(ctx)
}

func TestServiceListSetsNextCursorOnlyWhenMoreRowsExist(t *testing.T) {
	repo := projectsmocks.NewRepository(t)
	svc := projects.NewService(repo, inlineTx{})
	rows := make([]projects.Project, 3)
	for i := range rows {
		rows[i].ID = uuid.Must(uuid.NewV7())
	}
	limit := 2
	repo.EXPECT().List(mock.Anything, uuid.Nil, 3).Return(rows, nil) // asks for limit+1

	page, err := svc.List(context.Background(), nil, &limit)

	require.NoError(t, err)
	assert.Len(t, page.Items, 2)
	assert.Equal(t, pagination.Encode(rows[1].ID), page.NextCursor)
}

func TestServiceListRejectsForeignCursor(t *testing.T) {
	svc := projects.NewService(projectsmocks.NewRepository(t), inlineTx{})
	bad := "not-a-cursor!"

	_, err := svc.List(context.Background(), &bad, nil)

	assert.ErrorIs(t, err, pagination.ErrInvalidCursor)
}

func TestServiceUpdateChangesOnlyProvidedFields(t *testing.T) {
	repo := projectsmocks.NewRepository(t)
	svc := projects.NewService(repo, inlineTx{})
	id := uuid.Must(uuid.NewV7())
	repo.EXPECT().
		Get(mock.Anything, id).
		Return(&projects.Project{Slug: "old", Name: "Old", Team: "design"}, nil)
	repo.EXPECT().Update(mock.Anything, mock.MatchedBy(func(r *projects.Project) bool {
		return r.Slug == "old" && r.Name == "New" && r.Team == "design"
	})).Return(nil)
	name := "New"

	_, err := svc.Update(context.Background(), id, projects.UpdateInput{Name: &name})

	require.NoError(t, err)
}
```

`internal/projects/handler_test.go`

```go
package projects_test

import (
	"context"
	"testing"

	"github.com/google/uuid"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/mock"
	"github.com/stretchr/testify/require"

	"example.com/acme/taskmanager/gen/go/projectsapi"
	"example.com/acme/taskmanager/gen/go/projectsmocks"
	"example.com/acme/taskmanager/internal/projects"
)

func TestHandlerMapsFeatureErrorsToSpecResponses(t *testing.T) {
	repo := projectsmocks.NewRepository(t)
	h := projects.NewHandler(projects.NewService(repo, inlineTx{}))
	id := uuid.Must(uuid.NewV7())
	repo.EXPECT().Delete(mock.Anything, id).Return(projects.ErrHasTasks)

	resp, err := h.DeleteProject(
		context.Background(),
		projectsapi.DeleteProjectRequestObject{ProjectID: id},
	)

	require.NoError(t, err)
	conflict, ok := resp.(projectsapi.DeleteProject409ApplicationProblemPlusJSONResponse)
	require.True(t, ok, "got %T", resp)
	assert.Equal(t, 409, conflict.Status)
}
```

## Containers: one per package, one database per test

`TestMain` starts Postgres 18 and Redis 8 once per package, migrates a template
database with the real goose migrations, and each test clones it with
`CREATE DATABASE ... TEMPLATE` (a file copy: milliseconds, fully isolated,
`t.Parallel()`-safe). Redis is shared: keys contain UUIDv7 ids, and limiter
tests use their own app with the limiter enabled.

`internal/testkit/testkit.go`

```go
// Package testkit starts throwaway Postgres and Redis containers and boots the real fx app.
package testkit

import (
	"context"
	"database/sql"
	"fmt"
	"net/url"
	"sync/atomic"
	"testing"

	"github.com/gofiber/fiber/v3"
	_ "github.com/jackc/pgx/v5/stdlib"
	"github.com/pressly/goose/v3"
	"github.com/testcontainers/testcontainers-go"
	tcpostgres "github.com/testcontainers/testcontainers-go/modules/postgres"
	tcredis "github.com/testcontainers/testcontainers-go/modules/redis"
	"go.uber.org/fx"
	"go.uber.org/fx/fxtest"

	"example.com/acme/taskmanager/internal/app"
	"example.com/acme/taskmanager/internal/platform/config"
	"example.com/acme/taskmanager/migrations"
)

const templateDB = "app_template"

// Env is shared by every test in one package (started in TestMain).
type Env struct {
	pg       *tcpostgres.PostgresContainer
	redis    *tcredis.RedisContainer
	baseDSN  *url.URL
	RedisURL string
	seq      atomic.Int64
}

// Start runs one Postgres (migrated template database) and one Redis container.
func Start(ctx context.Context) (*Env, error) {
	pg, err := tcpostgres.Run(ctx, "postgres:18-alpine",
		tcpostgres.WithDatabase(templateDB),
		tcpostgres.WithUsername("test"),
		tcpostgres.WithPassword("test"),
		tcpostgres.BasicWaitStrategies(),
	)
	if err != nil {
		return nil, err
	}
	e := &Env{pg: pg}
	dsn, err := pg.ConnectionString(ctx, "sslmode=disable")
	if err != nil {
		return nil, e.stop(ctx, err)
	}
	if e.baseDSN, err = url.Parse(dsn); err != nil {
		return nil, e.stop(ctx, err)
	}
	if err := migrate(ctx, dsn); err != nil {
		return nil, e.stop(ctx, err)
	}
	if e.redis, err = tcredis.Run(ctx, "redis:8-alpine"); err != nil {
		return nil, e.stop(ctx, err)
	}
	if e.RedisURL, err = e.redis.ConnectionString(ctx); err != nil {
		return nil, e.stop(ctx, err)
	}
	return e, nil
}

// Stop terminates the containers.
func (e *Env) Stop(ctx context.Context) error { return e.stop(ctx, nil) }

func (e *Env) stop(ctx context.Context, cause error) error {
	if e.redis != nil {
		_ = testcontainers.TerminateContainer(e.redis)
	}
	_ = testcontainers.TerminateContainer(e.pg)
	return cause
}

func migrate(ctx context.Context, dsn string) error {
	db, err := sql.Open("pgx", dsn)
	if err != nil {
		return err
	}
	defer func() { _ = db.Close() }() // the template must have no open connections to be cloned
	p, err := goose.NewProvider(goose.DialectPostgres, db, migrations.FS)
	if err != nil {
		return err
	}
	_, err = p.Up(ctx)
	return err
}

// NewDatabase clones the migrated template into a fresh database for t and drops it after.
// CREATE DATABASE ... TEMPLATE is a file copy: fast, isolated and safe under t.Parallel.
func (e *Env) NewDatabase(t testing.TB) string {
	t.Helper()
	name := fmt.Sprintf("test_%d", e.seq.Add(1))
	admin := e.dsnFor("postgres")
	exec(t, admin, fmt.Sprintf("CREATE DATABASE %s TEMPLATE %s", name, templateDB))
	t.Cleanup(func() { exec(t, admin, fmt.Sprintf("DROP DATABASE %s WITH (FORCE)", name)) })
	return e.dsnFor(name)
}

func (e *Env) dsnFor(db string) string {
	u := *e.baseDSN
	u.Path = "/" + db
	return u.String()
}

func exec(t testing.TB, dsn, stmt string) {
	t.Helper()
	db, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = db.Close() }()
	if _, err := db.ExecContext(context.Background(), stmt); err != nil {
		t.Fatalf("%s: %v", stmt, err)
	}
}

// NewApp boots the full service graph against a fresh database and returns the public
// Fiber app for app.Test. Listeners bind to random ports; overrides win over defaults.
func (e *Env) NewApp(t *testing.T, overrides config.Source, opts ...fx.Option) *fiber.App {
	t.Helper()
	src := config.Source{
		"DB_DSN":                    e.NewDatabase(t),
		"REDIS_URL":                 e.RedisURL,
		"HTTP_ADDR":                 "127.0.0.1:0",
		"ADMIN_ADDR":                "127.0.0.1:0",
		"HTTP_SHUTDOWN_DRAIN_DELAY": "0s",
		"LOG_LEVEL":                 "warn",
		"OTEL_TRACES_EXPORTER":      "none",
	}
	for k, v := range overrides {
		src[k] = v
	}
	var api *fiber.App
	fxApp := fxtest.New(t, append([]fx.Option{
		app.Modules(),
		fx.Replace(src),
		fx.Populate(&api),
	}, opts...)...)
	fxApp.RequireStart()
	t.Cleanup(fxApp.RequireStop)
	return api
}
```

`internal/testkit/main.go`

```go
package testkit

import (
	"context"
	"flag"
	"fmt"
	"os"
	"testing"
)

// Main is called from TestMain. With -short it skips containers so unit tests stay fast.
// Usage:
//
//	var env *testkit.Env
//	func TestMain(m *testing.M) { testkit.Main(m, &env) }
func Main(m *testing.M, env **Env) {
	flag.Parse()
	if testing.Short() {
		os.Exit(m.Run())
	}
	ctx := context.Background()
	e, err := Start(ctx)
	if err != nil {
		fmt.Fprintln(os.Stderr, "testkit:", err)
		os.Exit(1)
	}
	*env = e
	code := m.Run()
	_ = e.Stop(ctx)
	os.Exit(code)
}

// Require skips integration tests under -short.
func Require(t *testing.T, env *Env) *Env {
	t.Helper()
	if env == nil {
		t.Skip("integration test: needs Docker (run without -short)")
	}
	return env
}
```

`internal/projects/main_test.go`

```go
package projects_test

import (
	"testing"

	"example.com/acme/taskmanager/internal/testkit"
)

var env *testkit.Env

func TestMain(m *testing.M) { testkit.Main(m, &env) }
```

## Repository integration

`internal/projects/repository_test.go`

```go
package projects_test

import (
	"context"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"

	"example.com/acme/taskmanager/internal/platform/db"
	"example.com/acme/taskmanager/internal/projects"
	"example.com/acme/taskmanager/internal/testkit"
)

func newRepo(t *testing.T) projects.Repository {
	e := testkit.Require(t, env)
	g, err := gorm.Open(postgres.Open(e.NewDatabase(t)), &gorm.Config{Logger: logger.Discard})
	require.NoError(t, err)
	sqlDB, err := g.DB()
	require.NoError(t, err)
	t.Cleanup(func() { _ = sqlDB.Close() })
	return projects.NewRepository(db.NewTxManager(g))
}

func TestRepositoryTranslatesDuplicateSlug(t *testing.T) {
	t.Parallel()
	repo, ctx := newRepo(t), context.Background()
	require.NoError(
		t,
		repo.Create(ctx, &projects.Project{Slug: "mobile-app", Name: "A", Team: "x"}),
	)

	err := repo.Create(ctx, &projects.Project{Slug: "mobile-app", Name: "B", Team: "x"})

	assert.ErrorIs(t, err, projects.ErrSlugTaken)
}

func TestRepositoryListPagesNewestFirstWithoutGapsOrRepeats(t *testing.T) {
	t.Parallel()
	repo, ctx := newRepo(t), context.Background()
	var created []string
	for _, slug := range []string{"a1", "b2", "c3", "d4", "e5"} {
		r := &projects.Project{Slug: slug, Name: slug, Team: "x"}
		require.NoError(t, repo.Create(ctx, r))
		created = append([]string{slug}, created...) // newest first
	}

	var seen []string
	page, err := repo.List(ctx, [16]byte{}, 2)
	for err == nil && len(page) > 0 {
		for _, r := range page {
			seen = append(seen, r.Slug)
		}
		page, err = repo.List(ctx, page[len(page)-1].ID, 2)
	}

	require.NoError(t, err)
	assert.Equal(t, created, seen)
}
```

## API end-to-end (fxtest + app.Test)

The real module graph boots with `fx.Replace(config.Source{...})`; listeners
bind to `127.0.0.1:0`, and requests go through `app.Test` on the populated
`*fiber.App`, so the whole middleware chain and validator run.

`internal/projects/api_test.go`

```go
package projects_test

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/bytedance/sonic"
	"github.com/gofiber/fiber/v3"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"example.com/acme/taskmanager/internal/platform/config"
	"example.com/acme/taskmanager/internal/testkit"
)

func do(t *testing.T, app *fiber.App, method, path, body string) (*http.Response, map[string]any) {
	t.Helper()
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	if body != "" {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := app.Test(req, fiber.TestConfig{Timeout: 5 * time.Second})
	require.NoError(t, err)
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	require.NoError(t, err)
	var out map[string]any
	if len(raw) > 0 {
		require.NoError(t, sonic.Unmarshal(raw, &out), string(raw))
	}
	return resp, out
}

func TestAPIRejectsContractViolationsWithFieldErrors(t *testing.T) {
	t.Parallel()
	app := testkit.Require(t, env).NewApp(t, nil)

	resp, body := do(t, app, http.MethodPost, "/v1/projects", `{"slug":"Not A Slug","name":"x"}`)

	assert.Equal(t, http.StatusBadRequest, resp.StatusCode)
	assert.Equal(t, "application/problem+json", resp.Header.Get("Content-Type"))
	locations := map[string]bool{}
	for _, e := range body["errors"].([]any) {
		locations[e.(map[string]any)["location"].(string)] = true
	}
	assert.True(t, locations["body.slug"], body)
	assert.True(t, locations["body.team"], body)
}

func TestAPIDuplicateSlugIsProblemConflict(t *testing.T) {
	t.Parallel()
	app := testkit.Require(t, env).NewApp(t, nil)
	do(
		t,
		app,
		http.MethodPost,
		"/v1/projects",
		`{"slug":"q4-roadmap","name":"A","team":"marketing"}`,
	)

	resp, body := do(
		t,
		app,
		http.MethodPost,
		"/v1/projects",
		`{"slug":"q4-roadmap","name":"B","team":"marketing"}`,
	)

	assert.Equal(t, http.StatusConflict, resp.StatusCode)
	assert.Equal(t, "application/problem+json", resp.Header.Get("Content-Type"))
	assert.NotEmpty(t, body["request_id"])
}

func TestAPIUpdateIsVisibleThroughCache(t *testing.T) {
	t.Parallel()
	app := testkit.Require(t, env).NewApp(t, nil)
	_, created := do(
		t,
		app,
		http.MethodPost,
		"/v1/projects",
		`{"slug":"onboarding-flow","name":"Onboarding Flow","team":"growth"}`,
	)
	path := "/v1/projects/" + created["id"].(string)
	do(t, app, http.MethodGet, path, "") // populate cache

	resp, _ := do(t, app, http.MethodPatch, path, `{"name":"Curry Palace"}`)
	require.Equal(t, http.StatusOK, resp.StatusCode)
	_, got := do(t, app, http.MethodGet, path, "")

	assert.Equal(t, "Curry Palace", got["name"])
}

func TestAPIRateLimitReturns429Problem(t *testing.T) {
	t.Parallel()
	app := testkit.Require(t, env).NewApp(t, config.Source{
		"RATE_LIMIT_ENABLED": "true",
		"RATE_LIMIT_RATE":    "2",
		"RATE_LIMIT_BURST":   "2",
		"RATE_LIMIT_PERIOD":  "1m",
	})
	// app.Test uses 0.0.0.0 as client IP; other tests run with the limiter off, so the key is ours.
	for range 2 {
		resp, _ := do(t, app, http.MethodGet, "/v1/projects", "")
		require.Equal(t, http.StatusOK, resp.StatusCode)
	}

	resp, body := do(t, app, http.MethodGet, "/v1/projects", "")

	assert.Equal(t, http.StatusTooManyRequests, resp.StatusCode)
	assert.NotEmpty(t, resp.Header.Get("Retry-After"))
	assert.EqualValues(t, 429, body["status"])
}
```

Extra modules or replacements go in the variadic `opts`, e.g.
`env.NewApp(t, authEnv, auth.Module)`.

## Rules

- Assert behaviour the client can see (status, body, headers) or the invariant
  the code exists for; never assert that a mock was called with what the code
  obviously passes.
- A test for a bug must fail before the fix. When unsure a test detects
  anything, break the code once and watch it fail.
- No `time.Sleep` for synchronisation; no shared mutable fixtures across tests;
  every test creates its own data.
- Run with `-race` (the Makefile does).
