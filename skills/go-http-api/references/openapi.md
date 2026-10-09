# OpenAPI-first: spec, codegen, strict handlers, validation

`api/openapi.yaml` is the source of truth. oapi-codegen turns each tag into a Go
package of DTOs plus a strict server interface; a kin-openapi middleware
validates every request against the same document before any handler runs.

## Spec conventions

- `openapi: 3.0.3`. kin-openapi and oapi-codegen target 3.0; 3.1 support is
  partial. Re-check on tool upgrades before moving.
- `servers: [{url: /v1}]`; paths are written without the prefix. A breaking
  change means `/v2` (new spec, new group).
- Every operation has exactly one tag = the feature package name, and a
  PascalCase `operationId` (it becomes the Go method name).
- Shared components live in `api/common.yaml` and are referenced as
  `./common.yaml#/components/...`: `Problem`, `FieldError`, the `Limit`/`Cursor`
  parameters and the `BadRequest`/`NotFound`/`Conflict`/`UnprocessableEntity`
  responses.
- Every operation lists every error status it can return; the handler can only
  return what the spec declares. Any operation with a path/query parameter or a
  request body declares `400` (the validator returns it for malformed input).
- Request bodies: `additionalProperties: false`, explicit `required`, string
  `minLength`/`maxLength`/`pattern`, numeric bounds. These are enforced by the
  validator, so handlers and services never re-check shape.
- Every request-body property and optional query parameter (cursors excepted)
  has a valid `example` or `default`: the generated Bruno requests use them
  (bruno.md). kin-openapi rejects an example that violates its schema when the
  spec loads.
- IDs: `type: string, format: uuid`. Times: `format: date-time` (UTC). Money:
  integer cents with `format: int64`.
- Optional-and-nullable response fields: `nullable: true` (3.0 style).
- Enums: inline `enum` or a named schema. oapi-codegen emits a named string type
  (`tasksapi.TaskStatus`) with constants. The GORM model keeps a plain `string`
  with its own constants in `model.go`; `handler.go` converts with
  `string(req.Body.Status)` / `tasksapi.TaskStatus(t.Status)`; the migration
  mirrors the values in a named `CHECK` constraint.
- JSON field names are `snake_case`.

`api/common.yaml`

```yaml
openapi: 3.0.3
info:
  title: Shared components
  version: 1.0.0
paths: {}
components:
  schemas:
    Problem:
      type: object
      description: RFC 9457 problem details.
      required: [type, title, status]
      properties:
        type:
          type: string
          example: about:blank
        title:
          type: string
        status:
          type: integer
        detail:
          type: string
        instance:
          type: string
        request_id:
          type: string
        errors:
          type: array
          items:
            $ref: '#/components/schemas/FieldError'
    FieldError:
      type: object
      required: [location, message]
      properties:
        location:
          type: string
          description: Where the error is, e.g. body.name or query.limit.
        message:
          type: string
  parameters:
    Limit:
      name: limit
      in: query
      schema:
        type: integer
        minimum: 1
        maximum: 100
        default: 20
    Cursor:
      name: cursor
      in: query
      schema:
        type: string
        maxLength: 128
  responses:
    BadRequest:
      description: Request failed validation.
      content:
        application/problem+json:
          schema:
            $ref: '#/components/schemas/Problem'
    NotFound:
      description: Resource not found.
      content:
        application/problem+json:
          schema:
            $ref: '#/components/schemas/Problem'
    Conflict:
      description: Request conflicts with current state.
      content:
        application/problem+json:
          schema:
            $ref: '#/components/schemas/Problem'
    UnprocessableEntity:
      description: Request is well-formed but semantically invalid.
      content:
        application/problem+json:
          schema:
            $ref: '#/components/schemas/Problem'
```

`api/openapi.yaml` (projects feature; the `tasks` paths and schemas follow the
same pattern):

```yaml
openapi: 3.0.3
info:
  title: ACME Task Manager API
  version: 1.0.0
servers:
  - url: /v1
tags:
  - name: projects
  - name: tasks
paths:
  /projects:
    post:
      tags: [projects]
      operationId: CreateProject
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/CreateProjectRequest'
      responses:
        '201':
          description: Created.
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Project'
        '400':
          $ref: './common.yaml#/components/responses/BadRequest'
        '409':
          $ref: './common.yaml#/components/responses/Conflict'
    get:
      tags: [projects]
      operationId: ListProjects
      parameters:
        - $ref: './common.yaml#/components/parameters/Limit'
        - $ref: './common.yaml#/components/parameters/Cursor'
      responses:
        '200':
          description: A page of projects, newest first.
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ProjectPage'
        '400':
          $ref: './common.yaml#/components/responses/BadRequest'
  /projects/{projectId}:
    parameters:
      - name: projectId
        in: path
        required: true
        schema:
          type: string
          format: uuid
    get:
      tags: [projects]
      operationId: GetProject
      responses:
        '200':
          description: The project.
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Project'
        '400':
          $ref: './common.yaml#/components/responses/BadRequest'
        '404':
          $ref: './common.yaml#/components/responses/NotFound'
    patch:
      tags: [projects]
      operationId: UpdateProject
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/UpdateProjectRequest'
      responses:
        '200':
          description: Updated.
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/Project'
        '400':
          $ref: './common.yaml#/components/responses/BadRequest'
        '404':
          $ref: './common.yaml#/components/responses/NotFound'
        '409':
          $ref: './common.yaml#/components/responses/Conflict'
    delete:
      tags: [projects]
      operationId: DeleteProject
      responses:
        '204':
          description: Deleted.
        '400':
          $ref: './common.yaml#/components/responses/BadRequest'
        '404':
          $ref: './common.yaml#/components/responses/NotFound'
        '409':
          $ref: './common.yaml#/components/responses/Conflict'
components:
  schemas:
    Project:
      type: object
      required: [id, slug, name, team, created_at, updated_at]
      properties:
        id:
          type: string
          format: uuid
        slug:
          type: string
        name:
          type: string
        team:
          type: string
        created_at:
          type: string
          format: date-time
        updated_at:
          type: string
          format: date-time
    CreateProjectRequest:
      type: object
      additionalProperties: false
      required: [slug, name, team]
      properties:
        slug:
          type: string
          example: website-redesign
          pattern: '^[a-z0-9]+(-[a-z0-9]+)*$'
          minLength: 2
          maxLength: 64
        name:
          type: string
          example: Website Redesign
          minLength: 1
          maxLength: 200
        team:
          type: string
          example: platform
          minLength: 1
          maxLength: 64
    UpdateProjectRequest:
      type: object
      additionalProperties: false
      minProperties: 1
      properties:
        slug:
          type: string
          example: website-relaunch
          pattern: '^[a-z0-9]+(-[a-z0-9]+)*$'
          minLength: 2
          maxLength: 64
        name:
          type: string
          example: Website Relaunch
          minLength: 1
          maxLength: 200
        team:
          type: string
          example: platform
          minLength: 1
          maxLength: 64
    ProjectPage:
      type: object
      required: [items]
      properties:
        items:
          type: array
          items:
            $ref: '#/components/schemas/Project'
        next_cursor:
          type: string
          nullable: true
```

## Code generation

One config per generated package. Shared components first (with `strict-server`
so the shared `...ApplicationProblemPlusJSONResponse` types exist), then one per
tag:

`api/codegen/httpapi.yaml`

```yaml
# Shared components (api/common.yaml) -> gen/go/httpapi
package: httpapi
output: gen/go/httpapi/httpapi.gen.go
generate:
  models: true
  fiber-v3-server: true
  strict-server: true
output-options:
  skip-prune: true
  name-normalizer: ToCamelCaseWithInitialisms
```

`api/codegen/projects.yaml`

```yaml
package: projectsapi
output: gen/go/projectsapi/projectsapi.gen.go
generate:
  models: true
  fiber-v3-server: true
  strict-server: true
output-options:
  name-normalizer: ToCamelCaseWithInitialisms
  include-tags: [projects]
import-mapping:
  ./common.yaml: example.com/acme/taskmanager/gen/go/httpapi
```

- `include-tags` selects the feature's operations; `import-mapping` makes
  `./common.yaml` refs resolve to `gen/go/httpapi` instead of duplicating
  `Problem` in every package.
- `name-normalizer: ToCamelCaseWithInitialisms` gives `ID`, `ProjectID`,
  `RequestID`.
- Generate with `make generate-api` (tooling.md). Output is committed; CI fails
  if it is stale.

`api/embed.go`

```go
// Package api embeds the OpenAPI documents so the binary can validate requests against them.
package api

import "embed"

// FS holds openapi.yaml and every file it references.
//
//go:embed *.yaml
var FS embed.FS
```

## Strict handler

The generated wrapper parses path/query params, binds the JSON body, calls your
method with `c.Context()` and a typed request object, and writes whichever typed
response you return. Expected errors become typed responses; unexpected errors
are returned and end up as a logged 500.

`internal/projects/handler.go`

```go
package projects

import (
	"context"
	"errors"
	"net/http"

	"github.com/gofiber/fiber/v3"

	"example.com/acme/taskmanager/gen/go/httpapi"
	"example.com/acme/taskmanager/gen/go/projectsapi"
	"example.com/acme/taskmanager/internal/platform/httpserver"
	"example.com/acme/taskmanager/internal/platform/pagination"
	"example.com/acme/taskmanager/internal/platform/problem"
)

// Handler implements the generated strict interface: typed requests in, typed responses out.
type Handler struct {
	svc *Service
}

var _ projectsapi.StrictServerInterface = (*Handler)(nil)

// NewHandler is the fx constructor.
func NewHandler(svc *Service) *Handler { return &Handler{svc: svc} }

// NewRoutes registers this feature on the /v1 router.
func NewRoutes(h *Handler) httpserver.RegisterFunc {
	return func(r fiber.Router) {
		projectsapi.RegisterHandlers(r, projectsapi.NewStrictHandler(h, nil))
	}
}

func (h *Handler) CreateProject(
	ctx context.Context,
	req projectsapi.CreateProjectRequestObject,
) (projectsapi.CreateProjectResponseObject, error) {
	r, err := h.svc.Create(
		ctx,
		CreateInput{Slug: req.Body.Slug, Name: req.Body.Name, Team: req.Body.Team},
	)
	switch {
	case errors.Is(err, ErrSlugTaken):
		return projectsapi.CreateProject409ApplicationProblemPlusJSONResponse{
			ConflictApplicationProblemPlusJSONResponse: httpapi.ConflictApplicationProblemPlusJSONResponse(
				problem.New(ctx, http.StatusConflict, err.Error()),
			),
		}, nil
	case err != nil:
		return nil, err // unmapped: ErrorHandler logs it and answers 500
	}
	return projectsapi.CreateProject201JSONResponse(toDTO(r)), nil
}

func (h *Handler) ListProjects(
	ctx context.Context,
	req projectsapi.ListProjectsRequestObject,
) (projectsapi.ListProjectsResponseObject, error) {
	page, err := h.svc.List(ctx, req.Params.Cursor, req.Params.Limit)
	switch {
	case errors.Is(err, pagination.ErrInvalidCursor):
		return projectsapi.ListProjects400ApplicationProblemPlusJSONResponse{
			BadRequestApplicationProblemPlusJSONResponse: httpapi.BadRequestApplicationProblemPlusJSONResponse(
				problem.New(ctx, http.StatusBadRequest, err.Error()),
			),
		}, nil
	case err != nil:
		return nil, err
	}
	out := projectsapi.ListProjects200JSONResponse{
		Items: make([]projectsapi.Project, 0, len(page.Items)),
	}
	for i := range page.Items {
		out.Items = append(out.Items, toDTO(&page.Items[i]))
	}
	if page.NextCursor != "" {
		out.NextCursor = &page.NextCursor
	}
	return out, nil
}

func (h *Handler) GetProject(
	ctx context.Context,
	req projectsapi.GetProjectRequestObject,
) (projectsapi.GetProjectResponseObject, error) {
	r, err := h.svc.Get(ctx, req.ProjectID)
	switch {
	case errors.Is(err, ErrNotFound):
		return projectsapi.GetProject404ApplicationProblemPlusJSONResponse{
			NotFoundApplicationProblemPlusJSONResponse: httpapi.NotFoundApplicationProblemPlusJSONResponse(
				problem.New(ctx, http.StatusNotFound, err.Error()),
			),
		}, nil
	case err != nil:
		return nil, err
	}
	return projectsapi.GetProject200JSONResponse(toDTO(r)), nil
}

func (h *Handler) UpdateProject(
	ctx context.Context,
	req projectsapi.UpdateProjectRequestObject,
) (projectsapi.UpdateProjectResponseObject, error) {
	r, err := h.svc.Update(
		ctx,
		req.ProjectID,
		UpdateInput{Slug: req.Body.Slug, Name: req.Body.Name, Team: req.Body.Team},
	)
	switch {
	case errors.Is(err, ErrNotFound):
		return projectsapi.UpdateProject404ApplicationProblemPlusJSONResponse{
			NotFoundApplicationProblemPlusJSONResponse: httpapi.NotFoundApplicationProblemPlusJSONResponse(
				problem.New(ctx, http.StatusNotFound, err.Error()),
			),
		}, nil
	case errors.Is(err, ErrSlugTaken):
		return projectsapi.UpdateProject409ApplicationProblemPlusJSONResponse{
			ConflictApplicationProblemPlusJSONResponse: httpapi.ConflictApplicationProblemPlusJSONResponse(
				problem.New(ctx, http.StatusConflict, err.Error()),
			),
		}, nil
	case err != nil:
		return nil, err
	}
	return projectsapi.UpdateProject200JSONResponse(toDTO(r)), nil
}

func (h *Handler) DeleteProject(
	ctx context.Context,
	req projectsapi.DeleteProjectRequestObject,
) (projectsapi.DeleteProjectResponseObject, error) {
	err := h.svc.Delete(ctx, req.ProjectID)
	switch {
	case errors.Is(err, ErrNotFound):
		return projectsapi.DeleteProject404ApplicationProblemPlusJSONResponse{
			NotFoundApplicationProblemPlusJSONResponse: httpapi.NotFoundApplicationProblemPlusJSONResponse(
				problem.New(ctx, http.StatusNotFound, err.Error()),
			),
		}, nil
	case errors.Is(err, ErrHasTasks):
		return projectsapi.DeleteProject409ApplicationProblemPlusJSONResponse{
			ConflictApplicationProblemPlusJSONResponse: httpapi.ConflictApplicationProblemPlusJSONResponse(
				problem.New(ctx, http.StatusConflict, err.Error()),
			),
		}, nil
	case err != nil:
		return nil, err
	}
	return projectsapi.DeleteProject204Response{}, nil
}

func toDTO(r *Project) projectsapi.Project {
	return projectsapi.Project{
		ID: r.ID, Slug: r.Slug, Name: r.Name, Team: r.Team,
		CreatedAt: r.CreatedAt.UTC(), UpdatedAt: r.UpdatedAt.UTC(), // pgx scans in time.Local
	}
}
```

- `var _ projectsapi.StrictServerInterface = (*Handler)(nil)` makes a spec
  change that adds an operation fail compilation until it is implemented.
- `NewRoutes` registers the generated routes on the `/v1` router that httpserver
  passes to every `RegisterFunc` (fx.md: value group `routes`).
- Shared response types embed:
  `projectsapi.GetProject404ApplicationProblemPlusJSONResponse{NotFoundApplicationProblemPlusJSONResponse:
  httpapi.NotFoundApplicationProblemPlusJSONResponse(problem.New(ctx, 404,
  msg))}`.

## Request validation (kin-openapi on Fiber v3)

`internal/platform/httpserver/validator.go`

```go
package httpserver

import (
	"errors"
	"io/fs"
	"net/url"
	"path"
	"slices"
	"strings"

	"github.com/getkin/kin-openapi/openapi3"
	"github.com/getkin/kin-openapi/openapi3filter"
	"github.com/getkin/kin-openapi/routers"
	"github.com/getkin/kin-openapi/routers/gorillamux"
	"github.com/gofiber/fiber/v3"
	"github.com/gofiber/fiber/v3/middleware/adaptor"
	"github.com/google/uuid"

	"example.com/acme/taskmanager/api"
	"example.com/acme/taskmanager/gen/go/httpapi"
	"example.com/acme/taskmanager/internal/platform/problem"
)

// kin-openapi's built-in uuid regexp only accepts v1-v5; UUIDv7 ids need this. The format
// registry is a package-level map, so register once: LoadSpec runs concurrently in parallel tests.
func init() {
	openapi3.DefineStringFormatValidator(
		"uuid",
		openapi3.NewCallbackValidator(func(s string) error {
			_, err := uuid.Parse(s)
			return err
		}),
	)
}

// ErrForbidden is wrapped by an Authn.Check error when the caller is authenticated but lacks
// the operation's scopes; the validator then answers 403 instead of 401.
var ErrForbidden = errors.New("forbidden")

// LoadSpec loads api/openapi.yaml (and its relative $refs) from the embedded FS.
func LoadSpec() (*openapi3.T, error) {
	loader := openapi3.NewLoader()
	loader.IsExternalRefsAllowed = true
	loader.ReadFromURIFunc = func(_ *openapi3.Loader, u *url.URL) ([]byte, error) {
		return fs.ReadFile(api.FS, path.Clean(u.Path))
	}
	doc, err := loader.LoadFromFile("openapi.yaml")
	if err != nil {
		return nil, err
	}
	return doc, doc.Validate(loader.Context)
}

// NewValidator rejects requests that violate the spec with a 400 problem listing every error,
// and requests failing the operation's security requirements (checked by authn) with 401.
// oapi-codegen/fiber-middleware only supports Fiber v2, hence this adapter.
func NewValidator(doc *openapi3.T, authn openapi3filter.AuthenticationFunc) (fiber.Handler, error) {
	router, err := gorillamux.NewRouter(doc)
	if err != nil {
		return nil, err
	}
	opts := &openapi3filter.Options{
		MultiError:         true,
		AuthenticationFunc: authn,
	}
	return func(c fiber.Ctx) error {
		req, err := adaptor.ConvertRequest(c, false)
		if err != nil {
			return err
		}
		route, pathParams, err := router.FindRoute(req)
		switch {
		case errors.Is(err, routers.ErrMethodNotAllowed):
			return fiber.ErrMethodNotAllowed
		case err != nil:
			return c.Next() // not in the spec: let Fiber answer 404
		}
		in := &openapi3filter.RequestValidationInput{
			Request:    req,
			PathParams: pathParams,
			Route:      route,
			Options:    opts,
		}
		if err := openapi3filter.ValidateRequest(c.Context(), in); err != nil {
			if secErr := (*openapi3filter.SecurityRequirementsError)(nil); errors.As(err, &secErr) {
				if slices.ContainsFunc(
					secErr.Errors,
					func(e error) bool { return errors.Is(e, ErrForbidden) },
				) {
					return &problem.Error{
						Problem: problem.New(c, fiber.StatusForbidden, "insufficient scope"),
					}
				}
				c.Set(fiber.HeaderWWWAuthenticate, "Bearer")
				return &problem.Error{
					Problem: problem.New(
						c,
						fiber.StatusUnauthorized,
						"valid bearer token required",
					),
				}
			}
			p := problem.New(c, fiber.StatusBadRequest, "request does not match the API contract")
			fieldErrs := fieldErrors("", err)
			p.Errors = &fieldErrs
			return &problem.Error{Problem: p}
		}
		return c.Next()
	}, nil
}

// fieldErrors flattens kin-openapi's nested errors into location/message pairs.
// It switches on concrete types: errors.As would unwrap a RequestError into its inner
// MultiError and lose the parameter/body location.
func fieldErrors(loc string, err error) []httpapi.FieldError {
	switch e := err.(type) { //nolint:errorlint // concrete types on purpose, see above
	case openapi3.MultiError:
		var out []httpapi.FieldError
		for _, inner := range e {
			out = append(out, fieldErrors(loc, inner)...)
		}
		return out
	case *openapi3filter.RequestError:
		switch {
		case e.Parameter != nil:
			loc = e.Parameter.In + "." + e.Parameter.Name
		case e.RequestBody != nil:
			loc = "body"
		}
		if e.Err != nil {
			return fieldErrors(loc, e.Err)
		}
		return []httpapi.FieldError{{Location: loc, Message: e.Reason}}
	case *openapi3.SchemaError:
		if ptr := e.JSONPointer(); len(ptr) > 0 {
			loc += "." + strings.Join(ptr, ".")
		}
		return []httpapi.FieldError{{Location: loc, Message: e.Reason}}
	default:
		return []httpapi.FieldError{{Location: loc, Message: err.Error()}}
	}
}
```

- The spec is loaded once from the embedded FS with external refs allowed.
- kin-openapi's built-in `uuid` format only accepts versions 1-5; the `init`
  callback accepts UUIDv7. It runs once because the format registry is a global
  map and `LoadSpec` runs concurrently in parallel tests (a race under `-race`
  otherwise).
- `fieldErrors` switches on concrete types: `errors.As` unwraps a `RequestError`
  into its inner `MultiError` and loses `body.`/`query.` locations.
- Paths absent from the spec fall through to Fiber's 404; wrong methods get 405.
- The `authn` argument enforces `security` requirements (auth.md); without auth
  it is `openapi3filter.NoopAuthenticationFunc`.

Example response:

```json
{"type":"about:blank","title":"Bad Request","status":400,"detail":"request does not match the API contract",
 "request_id":"V8CF...","errors":[{"location":"body.slug","message":"string doesn't match the regular expression ..."},
 {"location":"body.name","message":"property \"name\" is missing"},{"location":"query.limit","message":"number must be at most 100"}]}
```

## Pagination: keyset cursors

Lists return `{items: [...], next_cursor: string|null}` newest first. The cursor
is the base64url of the last item's UUIDv7, opaque to clients. The service asks
the repository for `limit+1` rows to decide whether a next page exists
(database.md has the query).

`internal/platform/pagination/cursor.go`

```go
// Package pagination implements opaque keyset cursors over UUIDv7 primary keys.
package pagination

import (
	"encoding/base64"
	"errors"

	"github.com/google/uuid"
)

const (
	DefaultLimit = 20
	MaxLimit     = 100
)

// ErrInvalidCursor is returned for cursors this service did not issue.
var ErrInvalidCursor = errors.New("invalid cursor")

// Limit applies the default and the ceiling.
func Limit(requested *int) int {
	switch {
	case requested == nil || *requested <= 0:
		return DefaultLimit
	case *requested > MaxLimit:
		return MaxLimit
	default:
		return *requested
	}
}

// Encode turns the last returned ID into an opaque cursor.
func Encode(id uuid.UUID) string { return base64.RawURLEncoding.EncodeToString(id[:]) }

// Decode returns uuid.Nil for an empty cursor (first page).
func Decode(cursor *string) (uuid.UUID, error) {
	if cursor == nil || *cursor == "" {
		return uuid.Nil, nil
	}
	b, err := base64.RawURLEncoding.DecodeString(*cursor)
	if err != nil {
		return uuid.Nil, ErrInvalidCursor
	}
	id, err := uuid.FromBytes(b)
	if err != nil {
		return uuid.Nil, ErrInvalidCursor
	}
	return id, nil
}
```

A bad cursor is a 400 the handler maps (`pagination.ErrInvalidCursor`), so the
operation must declare `400`.
