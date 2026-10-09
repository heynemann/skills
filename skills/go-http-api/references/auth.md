# Authentication (opt-in): JWT bearer via JWKS

Not in the default scaffold. Apply when the service validates tokens itself
rather than trusting a gateway. Requires an issuer that publishes a JWKS
(`AUTH_JWKS_URL`, `AUTH_ISSUER`, `AUTH_AUDIENCE`).

Design: the auth middleware authenticates a bearer token when one is present and
stores the `Principal` in the request context. Whether an operation *requires* a
token, and which scopes, is declared in the spec and enforced by the OpenAPI
validator through `httpserver.Authn.Check`. Public operations simply declare no
`security`.

## httpserver hook (already in the core scaffold)

`httpserver.Params` has an optional `Authn *httpserver.Authn`; when present it
is mounted first on `/v1` and its `Check` replaces `NoopAuthenticationFunc` in
the validator. The validator answers 401 (`WWW-Authenticate: Bearer`) for
missing/invalid tokens and 403 when `Check` returns an error wrapping
`httpserver.ErrForbidden`. See `server.go` and `validator.go` in
http.md/openapi.md.

## Module

```sh
go get github.com/golang-jwt/jwt/v5 github.com/MicahParks/keyfunc/v3
```

`internal/platform/auth/auth.go`

```go
// Package auth verifies JWT bearer tokens against a JWKS and enforces OpenAPI security requirements.
// It is opt-in: add auth.Module to app.Modules and declare `security` in the spec.
package auth

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"strings"

	"github.com/MicahParks/keyfunc/v3"
	"github.com/getkin/kin-openapi/openapi3filter"
	"github.com/gofiber/fiber/v3"
	"github.com/golang-jwt/jwt/v5"
	"go.uber.org/fx"

	"example.com/acme/taskmanager/internal/platform/config"
	"example.com/acme/taskmanager/internal/platform/httpserver"
	"example.com/acme/taskmanager/internal/platform/ratelimit"
)

// Config is parsed from AUTH_* variables.
type Config struct {
	JWKSURL  string `env:"JWKS_URL,required"`
	Issuer   string `env:"ISSUER,required"`
	Audience string `env:"AUDIENCE,required"`
}

// NewConfig parses AUTH_* variables.
func NewConfig(src config.Source) (Config, error) { return config.Parse[Config](src, "AUTH_") }

// Principal is the authenticated caller.
type Principal struct {
	Subject string
	Scopes  []string
}

type ctxKey struct{}

// From returns the principal stored by the middleware.
func From(ctx context.Context) (Principal, bool) {
	p, ok := ctx.Value(ctxKey{}).(Principal)
	return p, ok
}

type claims struct {
	jwt.RegisteredClaims
	Scope string `json:"scope"`
}

// Verifier validates tokens; keys are fetched from the JWKS and refreshed in the background.
type Verifier struct {
	keys   keyfunc.Keyfunc
	parser *jwt.Parser
}

// NewVerifier fetches the JWKS; the refresh goroutine stops with the app.
func NewVerifier(lc fx.Lifecycle, cfg Config) (*Verifier, error) {
	ctx, cancel := context.WithCancel(context.Background())
	keys, err := keyfunc.NewDefaultCtx(ctx, []string{cfg.JWKSURL})
	if err != nil {
		cancel()
		return nil, fmt.Errorf("jwks: %w", err)
	}
	lc.Append(fx.StopHook(cancel))
	return &Verifier{keys: keys, parser: jwt.NewParser(
		jwt.WithIssuer(cfg.Issuer),
		jwt.WithAudience(cfg.Audience),
		jwt.WithValidMethods([]string{"RS256", "ES256", "EdDSA"}),
		jwt.WithExpirationRequired(),
	)}, nil
}

// Verify parses and validates a raw token.
func (v *Verifier) Verify(raw string) (Principal, error) {
	var c claims
	if _, err := v.parser.ParseWithClaims(raw, &c, v.keys.Keyfunc); err != nil {
		return Principal{}, err
	}
	return Principal{Subject: c.Subject, Scopes: strings.Fields(c.Scope)}, nil
}

// Middleware authenticates a bearer token when one is sent. Whether an operation *requires*
// one is decided by the spec's security requirements, enforced by Check in the validator.
func (v *Verifier) Middleware(c fiber.Ctx) error {
	header := c.Get(fiber.HeaderAuthorization)
	if header == "" {
		return c.Next()
	}
	raw, ok := strings.CutPrefix(header, "Bearer ")
	if !ok {
		return fiber.NewError(fiber.StatusUnauthorized, "authorization must be a bearer token")
	}
	p, err := v.Verify(raw)
	if err != nil {
		c.Set(fiber.HeaderWWWAuthenticate, `Bearer error="invalid_token"`)
		return fiber.NewError(fiber.StatusUnauthorized, "invalid bearer token")
	}
	c.SetContext(context.WithValue(c.Context(), ctxKey{}, p))
	return c.Next()
}

// Check satisfies an operation's `security: [{bearerAuth: [scopes...]}]` requirement.
func Check(ctx context.Context, in *openapi3filter.AuthenticationInput) error {
	p, ok := From(ctx)
	if !ok {
		return errors.New("no bearer token")
	}
	for _, scope := range in.Scopes {
		if !slices.Contains(p.Scopes, scope) {
			return fmt.Errorf("%w: missing scope %q", httpserver.ErrForbidden, scope)
		}
	}
	return nil
}

// RateLimitKey limits authenticated callers per subject and anonymous ones per IP.
func RateLimitKey(c fiber.Ctx) string {
	if p, ok := From(c.Context()); ok {
		return "sub:" + p.Subject
	}
	return "ip:" + c.IP()
}

// Module plugs authentication into httpserver and the rate limiter.
var Module = fx.Module("auth",
	fx.Provide(
		NewConfig,
		NewVerifier,
		func(v *Verifier) *httpserver.Authn { return &httpserver.Authn{Middleware: v.Middleware, Check: Check} },
		func() ratelimit.KeyFunc { return RateLimitKey },
	),
)
```

Enable it by adding `auth.Module` to `internal/app/app.go`. It also replaces the
rate-limit key with the token subject.

## Spec

```yaml
components:
  securitySchemes:
    bearerAuth:
      type: http
      scheme: bearer
      bearerFormat: JWT

paths:
  /tasks:
    post:
      operationId: CreateTask
      security:
        - bearerAuth: [tasks:write]   # scopes from the token's space-separated `scope` claim
      responses:
        '401': {$ref: './common.yaml#/components/responses/Unauthorized'}
        '403': {$ref: './common.yaml#/components/responses/Forbidden'}
```

Add the two responses to `api/common.yaml` under `components.responses`, then
`make generate`:

```yaml
    Unauthorized:
      description: Missing or invalid bearer token.
      content:
        application/problem+json:
          schema:
            $ref: '#/components/schemas/Problem'
    Forbidden:
      description: Authenticated but lacking the required scope.
      content:
        application/problem+json:
          schema:
            $ref: '#/components/schemas/Problem'
```

A top-level `security:` protects every operation; override with `security: []`
on public ones.

Services read the caller with `auth.From(ctx)`.

## Testing

Serve a JWKS from `httptest.NewServer`, sign tokens with the matching RSA key,
and boot the app with the module:

```go
key, _ := rsa.GenerateKey(rand.Reader, 2048)
b64 := base64.RawURLEncoding.EncodeToString
jwks, _ := sonic.Marshal(map[string]any{"keys": []map[string]string{{
	"kty": "RSA", "kid": "k1", "alg": "RS256", "use": "sig",
	"n": b64(key.N.Bytes()), "e": b64(big.NewInt(int64(key.E)).Bytes()),
}}})
srv := httptest.NewServer(
	http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write(jwks) }),
)
t.Cleanup(srv.Close)

sign := func(scope string) string {
	tok := jwt.NewWithClaims(jwt.SigningMethodRS256, jwt.MapClaims{
		"sub":   "u1",
		"iss":   "iss",
		"aud":   "aud",
		"exp":   time.Now().Add(time.Hour).Unix(),
		"scope": scope,
	})
	tok.Header["kid"] = "k1"
	s, _ := tok.SignedString(key)
	return s
}

app := env.NewApp(
	t,
	config.Source{"AUTH_JWKS_URL": srv.URL, "AUTH_ISSUER": "iss", "AUTH_AUDIENCE": "aud"},
	auth.Module,
)
```

Verified outcomes: no token → 401, malformed → 401, missing scope → 403, correct
scope → handler runs, operations without `security` stay public.
