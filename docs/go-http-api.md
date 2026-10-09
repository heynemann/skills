# go-http-api

An opinionated playbook for building Go HTTP API services. It steers the agent
toward one stack and one layout so every service looks the same: contract first
in OpenAPI, one self-wiring fx module per feature, and a verification gate
before work counts as done.

## Install

```bash
npx skills add heynemann/skills --skill go-http-api
```

## When it activates

The agent loads it when you scaffold a Go API service, add a resource,
endpoint or feature module to one, or change its middleware, caching,
migrations, observability or Bruno collection.

## Stack

Fiber v3, `go.uber.org/fx`, zap, GORM on PostgreSQL 18, goose migrations,
go-redis v9, Prometheus, OpenTelemetry, oapi-codegen, testify, mockery,
testcontainers, golangci-lint v2 and Bruno. Each concern has a reference file
under [`skills/go-http-api/references/`](../skills/go-http-api/references/).

## How to use it

Scaffold a new service. Give the module path, binary name and first feature
up front, or the agent asks for them:

```text
Scaffold a Go HTTP API: module example.com/acme/taskmanager, binary
taskmanager, first feature "projects" with create, get, list, update and
delete.
```

Add a feature or endpoint to an existing service:

```text
Add a "tasks" feature: tasks belong to a project, have an assignee, a status
(todo, in_progress, in_review, done, cancelled) and subtasks.
```

Change one concern. The agent reads that concern's reference and follows its
rules:

```text
Cache project reads in Redis.
```

## What you get

- `api/openapi.yaml` as the source of truth, with generated code in `gen/`.
- A feature package per resource: model, errors, service, repository, handler
  and fx module.
- Embedded SQL migrations behind a `migrate` subcommand.
- Unit, repository and API tests, a Makefile, Dockerfile and compose stack.

## Done means

The skill's verification gate: `make generate` produces only intended changes,
`make fmt-check`, `make lint` and `make test` pass, the README's run commands
work from a clean shell, and each new operation is smoke-tested with `curl`.
