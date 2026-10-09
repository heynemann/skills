# go-http-api

**Ask for a Go API twice and get the same architecture twice: contract first,
wired by fx, tested against real Postgres, and not done until it passes the
gate.**

Agents are fast at writing Go services and inconsistent at it: a different
router, layout and error format every time. `go-http-api` is a playbook that
pins one stack and one way of building with it, so every service, and every
feature you add to one, looks like the last.

## Try it

```bash
npx skills add heynemann/skills --skill go-http-api
```

Start a service. Give the module path, binary name and first feature up front,
or the agent asks for them:

```text
Scaffold a Go HTTP API: module example.com/acme/taskmanager, binary
taskmanager, first feature "projects" with create, get, list, update and
delete.
```

Grow it:

```text
Add a "tasks" feature: tasks belong to a project, have an assignee, a status
(todo, in_progress, in_review, done, cancelled) and subtasks.
```

Change one concern, and the agent follows that concern's rules:

```text
Cache project reads in Redis.
```

## The stack

| Concern | Choice |
| --- | --- |
| HTTP | Fiber v3, sonic JSON, RFC 9457 problem responses, separate admin port, graceful drain |
| Contract | OpenAPI 3.0.3 first; oapi-codegen strict servers; requests validated before handlers run |
| Wiring | `go.uber.org/fx`, one self-wiring module per feature |
| Data | GORM + PostgreSQL 18, UUIDv7 IDs, transactions carried in the context |
| Schema | goose SQL migrations embedded in the binary, run as their own step |
| Cache, rate limits | go-redis v9: a cache decorator around the repository that keeps serving if Redis fails, plus GCRA rate limiting |
| Observability | zap logs with request and trace IDs, Prometheus, OpenTelemetry |
| Tests | testify + mockery, testcontainers, full-app API tests |
| Tooling | Makefile, golangci-lint v2, distroless Docker image, compose stack, Bruno collection |
| Auth (opt-in) | JWT bearer via JWKS, enforced from the spec's `security` |

## What a service looks like

```text
api/openapi.yaml          # the contract: every operation lives here first
cmd/taskmanager/main.go   # signals + CLI, nothing else
gen/                      # generated code, committed, never edited by hand
internal/
  platform/               # config, logger, db, redis, metrics, http server...
  projects/               # one package per feature:
    model.go errors.go service.go repository.go handler.go module.go
migrations/               # SQL, embedded in the binary
```

A feature switches on with one line in `internal/app/app.go`. Its handler can
only return responses the spec declares, and its repository is the only code
that touches the database.

## Done means done

No task counts as finished until all of these pass:

- `make generate` leaves only the changes you intended in `gen/`.
- `make fmt-check`, `make lint` (zero issues) and `make test` pass.
- The README's run commands work verbatim from a clean shell.
- Every new endpoint is called with `curl`, once on its happy path and once on
  an error path.
- After lifecycle changes, `SIGTERM` drains in-flight requests and exits
  cleanly.

## Requirements

- Go, Docker (for Postgres, Redis and testcontainers) and Node (the Bruno
  collection is generated through `npx`).

## FAQ

**Can I swap Fiber for chi, or GORM for sqlc?** Not within the skill: its value
is that the choices are made once. Fork it and change the references; each
concern lives in its own file under `skills/go-http-api/references/`.

**Does it work on an existing service?** Yes, if the service follows the same
layout. For one concern, like caching, migrations or metrics, the agent reads
that concern's reference and follows its rules.

**Why the task manager examples?** Every snippet uses one consistent domain
(projects, tasks, subtasks) so the pieces fit together. Swap in your own
module path and features.
