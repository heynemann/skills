# Redis: cache-aside and distributed rate limiting

Redis is a cache, never a source of truth. Every Redis failure degrades to "no
cache" / "no limit" (fail-open): logged at warn, counted, never surfaced to the
client, and `/readyz` ignores Redis.

## Client

`internal/platform/redisx/redis.go`

```go
// Package redisx provides the shared go-redis client.
package redisx

import (
	"context"
	"time"

	"github.com/redis/go-redis/extra/redisotel/v9"
	"github.com/redis/go-redis/v9"
	"go.opentelemetry.io/otel/trace"
	"go.uber.org/fx"
	"go.uber.org/zap"

	"example.com/acme/taskmanager/internal/platform/config"
)

// Config is parsed from REDIS_* variables.
type Config struct {
	URL     string        `env:"URL"     envDefault:"redis://localhost:6379/0"`
	Timeout time.Duration `env:"TIMEOUT" envDefault:"250ms"` // read/write; short so fail-open is fast
}

// NewConfig parses REDIS_* variables.
func NewConfig(src config.Source) (Config, error) { return config.Parse[Config](src, "REDIS_") }

// New builds the client. Redis is a cache: an unreachable server is logged, not fatal.
func New(
	lc fx.Lifecycle,
	cfg Config,
	log *zap.Logger,
	tp trace.TracerProvider,
) (redis.UniversalClient, error) {
	opts, err := redis.ParseURL(cfg.URL)
	if err != nil {
		return nil, err
	}
	opts.ReadTimeout, opts.WriteTimeout = cfg.Timeout, cfg.Timeout
	c := redis.NewClient(opts)
	if err := redisotel.InstrumentTracing(c, redisotel.WithTracerProvider(tp)); err != nil {
		return nil, err
	}
	lc.Append(fx.Hook{
		OnStart: func(ctx context.Context) error {
			if err := c.Ping(ctx).Err(); err != nil {
				log.Warn("redis unreachable at startup; continuing without cache", zap.Error(err))
			}
			return nil
		},
		OnStop: func(context.Context) error { return c.Close() },
	})
	return c, nil
}

// Module provides redis.UniversalClient.
var Module = fx.Module("redis", fx.Provide(NewConfig, New))
```

Consumers depend on `redis.UniversalClient`, so moving to Redis Cluster/Sentinel
only changes the constructor.

## Cache

`internal/platform/cache/cache.go`

```go
// Package cache implements fail-open cache-aside on Redis.
package cache

import (
	"context"
	"errors"
	"math/rand/v2"
	"time"

	"github.com/bytedance/sonic"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/redis/go-redis/v9"
	"go.uber.org/fx"
	"go.uber.org/zap"
	"golang.org/x/sync/singleflight"

	"example.com/acme/taskmanager/internal/platform/logger"
)

// Cache is shared by every feature's cached repository decorator.
type Cache struct {
	rdb      redis.UniversalClient
	group    singleflight.Group
	requests *prometheus.CounterVec
}

// New registers cache_requests_total{cache,result}.
func New(rdb redis.UniversalClient, reg prometheus.Registerer) (*Cache, error) {
	requests := prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "cache_requests_total",
		Help: "Cache lookups by cache name and result (hit, miss, error).",
	}, []string{"cache", "result"})
	if err := reg.Register(requests); err != nil {
		return nil, err
	}
	return &Cache{rdb: rdb, requests: requests}, nil
}

// Module provides *Cache.
var Module = fx.Module("cache", fx.Provide(New))

// GetOrLoad returns the cached value for key or calls load once per key across concurrent
// callers (singleflight) and caches the result with ttl ±10% jitter. Redis errors fall
// through to load (fail-open). Errors from load are returned and never cached.
func GetOrLoad[T any](
	ctx context.Context,
	c *Cache,
	name, key string,
	ttl time.Duration,
	load func(context.Context) (T, error),
) (T, error) {
	raw, err := c.rdb.Get(ctx, key).Bytes()
	switch {
	case err == nil:
		var v T
		uerr := sonic.Unmarshal(raw, &v)
		if uerr == nil {
			c.requests.WithLabelValues(name, "hit").Inc()
			return v, nil
		}
		c.fail(ctx, name, "decode", uerr) // corrupt or old-shape entry: reload and overwrite
	case errors.Is(err, redis.Nil):
		c.requests.WithLabelValues(name, "miss").Inc()
	default:
		c.fail(ctx, name, "get", err)
	}

	v, err, _ := c.group.Do(key, func() (any, error) {
		v, err := load(ctx)
		if err != nil {
			return nil, err
		}
		if raw, merr := sonic.Marshal(v); merr != nil {
			c.fail(ctx, name, "encode", merr)
		} else if serr := c.rdb.Set(ctx, key, raw, jitter(ttl)).Err(); serr != nil {
			c.fail(ctx, name, "set", serr)
		}
		return v, nil
	})
	if err != nil {
		var zero T
		return zero, err
	}
	return v.(T), nil
}

// Delete removes keys; failures are logged and counted, never returned.
func (c *Cache) Delete(ctx context.Context, name string, keys ...string) {
	if err := c.rdb.Del(ctx, keys...).Err(); err != nil {
		c.fail(ctx, name, "delete", err)
	}
}

func (c *Cache) fail(ctx context.Context, name, op string, err error) {
	c.requests.WithLabelValues(name, "error").Inc()
	logger.From(ctx).
		Warn("cache error", zap.String("cache", name), zap.String("op", op), zap.Error(err))
}

func jitter(ttl time.Duration) time.Duration {
	f := rand.Float64()*0.2 - 0.1 //nolint:gosec // jitter, not security
	return ttl + time.Duration(f*float64(ttl))
}
```

- Values are encoded with `sonic` (JSON, readable in `redis-cli`).
- `singleflight` collapses concurrent misses for one key into a single load.
- TTL ±10% jitter avoids synchronized expiry.
- Errors from `load` (including not-found) are not cached.

## Repository decorator

Caching lives in a decorator implementing the same consumer interface, applied
with `fx.Decorate` inside the feature module. The service never knows the cache
exists.

`internal/projects/cache.go`

```go
package projects

import (
	"context"
	"time"

	"github.com/google/uuid"

	"example.com/acme/taskmanager/internal/platform/cache"
	"example.com/acme/taskmanager/internal/platform/db"
)

const (
	cacheName = "projects"
	cacheTTL  = 5 * time.Minute
)

// cachedRepository decorates Repository with cache-aside reads and post-commit invalidation.
type cachedRepository struct {
	Repository // methods without caching pass straight through
	cache      *cache.Cache
}

// NewCachedRepository is applied with fx.Decorate, so the service never knows about the cache.
func NewCachedRepository(next Repository, c *cache.Cache) Repository {
	return &cachedRepository{Repository: next, cache: c}
}

// Bump the version segment when the cached shape changes; old keys simply expire.
func cacheKey(id uuid.UUID) string { return "projects:v1:" + id.String() }

func (r *cachedRepository) Get(ctx context.Context, id uuid.UUID) (*Project, error) {
	if db.InTx(ctx) { // read-modify-write must see the database, not the cache
		return r.Repository.Get(ctx, id)
	}
	return cache.GetOrLoad(
		ctx,
		r.cache,
		cacheName,
		cacheKey(id),
		cacheTTL,
		func(ctx context.Context) (*Project, error) {
			return r.Repository.Get(ctx, id)
		},
	)
}

func (r *cachedRepository) Update(ctx context.Context, m *Project) error {
	if err := r.Repository.Update(ctx, m); err != nil {
		return err
	}
	r.invalidate(ctx, m.ID)
	return nil
}

func (r *cachedRepository) Delete(ctx context.Context, id uuid.UUID) error {
	if err := r.Repository.Delete(ctx, id); err != nil {
		return err
	}
	r.invalidate(ctx, id)
	return nil
}

// invalidate after commit: deleting inside the transaction would let a concurrent reader
// re-cache the old row before the commit lands.
func (r *cachedRepository) invalidate(ctx context.Context, id uuid.UUID) {
	db.AfterCommit(ctx, func(ctx context.Context) { r.cache.Delete(ctx, cacheName, cacheKey(id)) })
}
```

Rules:

- Key = `<feature>:v<N>:<id>`. Bump `vN` when the cached struct changes shape;
  old keys expire on their own.
- Inside a transaction, reads bypass the cache: read-modify-write must see the
  database.
- Invalidate with `db.AfterCommit`, not inside the transaction. Deleting inside
  it lets a concurrent reader re-cache the old row before commit.
- Cache by primary key only. Lists and searches are not cached (invalidation by
  key cannot cover them).
- Name the cache (`cacheName`) so `cache_requests_total{cache="projects"}`
  separates hit ratios per feature.

## Rate limiting

GCRA via `go-redis/redis_rate` (one atomic Lua call per request), shared across
all replicas. Fiber's built-in limiter with a Redis storage does get-then-set
and is only approximate across pods.

`internal/platform/ratelimit/ratelimit.go`

```go
// Package ratelimit is a distributed GCRA rate limiter on Redis (opt-in).
package ratelimit

import (
	"math"
	"strconv"
	"time"

	"github.com/go-redis/redis_rate/v10"
	"github.com/gofiber/fiber/v3"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/redis/go-redis/v9"
	"go.uber.org/fx"
	"go.uber.org/zap"

	"example.com/acme/taskmanager/internal/platform/config"
	"example.com/acme/taskmanager/internal/platform/logger"
)

// Config is parsed from RATE_LIMIT_* variables.
type Config struct {
	Enabled bool          `env:"ENABLED" envDefault:"false"`
	Rate    int           `env:"RATE"    envDefault:"100"`
	Burst   int           `env:"BURST"   envDefault:"100"`
	Period  time.Duration `env:"PERIOD"  envDefault:"1s"`
}

// NewConfig parses RATE_LIMIT_* variables.
func NewConfig(
	src config.Source,
) (Config, error) {
	return config.Parse[Config](src, "RATE_LIMIT_")
}

// KeyFunc identifies the caller. Provide one (e.g. the authenticated principal) to override client IP.
type KeyFunc func(c fiber.Ctx) string

// Limiter is always provided; Handler is nil when disabled.
type Limiter struct {
	cfg       Config
	limiter   *redis_rate.Limiter
	key       KeyFunc
	decisions *prometheus.CounterVec
}

// Params lets an optional KeyFunc replace the IP default.
type Params struct {
	fx.In
	Config Config
	Redis  redis.UniversalClient
	Reg    prometheus.Registerer
	Key    KeyFunc `optional:"true"`
}

// New registers ratelimit_decisions_total{result}.
func New(p Params) (*Limiter, error) {
	decisions := prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "ratelimit_decisions_total",
		Help: "Rate limiter decisions (allowed, limited, error).",
	}, []string{"result"})
	if err := p.Reg.Register(decisions); err != nil {
		return nil, err
	}
	key := p.Key
	if key == nil {
		key = func(c fiber.Ctx) string { return c.IP() } // honours HTTP_TRUST_PROXY settings
	}
	return &Limiter{
		cfg:       p.Config,
		limiter:   redis_rate.NewLimiter(p.Redis),
		key:       key,
		decisions: decisions,
	}, nil
}

// Handler returns the middleware, or nil when RATE_LIMIT_ENABLED=false.
func (l *Limiter) Handler() fiber.Handler {
	if !l.cfg.Enabled {
		return nil
	}
	limit := redis_rate.Limit{Rate: l.cfg.Rate, Burst: l.cfg.Burst, Period: l.cfg.Period}
	return func(c fiber.Ctx) error {
		res, err := l.limiter.Allow(c.Context(), "ratelimit:"+l.key(c), limit)
		if err != nil { // fail-open: Redis trouble must not take the API down
			l.decisions.WithLabelValues("error").Inc()
			logger.From(c.Context()).Warn("rate limiter unavailable", zap.Error(err))
			return c.Next()
		}
		c.Set("RateLimit-Limit", strconv.Itoa(limit.Burst))
		c.Set("RateLimit-Remaining", strconv.Itoa(res.Remaining))
		c.Set("RateLimit-Reset", strconv.Itoa(ceilSeconds(res.ResetAfter)))
		if res.Allowed == 0 {
			l.decisions.WithLabelValues("limited").Inc()
			c.Set(fiber.HeaderRetryAfter, strconv.Itoa(ceilSeconds(res.RetryAfter)))
			return fiber.NewError(fiber.StatusTooManyRequests, "rate limit exceeded")
		}
		l.decisions.WithLabelValues("allowed").Inc()
		return c.Next()
	}
}

func ceilSeconds(d time.Duration) int { return int(math.Ceil(d.Seconds())) }

// Module provides *Limiter.
var Module = fx.Module("ratelimit", fx.Provide(NewConfig, New))
```

- Off by default; `RATE_LIMIT_ENABLED=true` mounts it on `/v1` after
  authentication.
- Key: client IP (`c.IP()`, so configure
  `HTTP_TRUST_PROXY`/`HTTP_TRUSTED_PROXIES` behind a load balancer). Providing a
  `ratelimit.KeyFunc` replaces it; `auth.Module` provides one keyed by token
  subject.
- 429 goes through `ErrorHandler` as a problem with `Retry-After` and
  `RateLimit-Limit/Remaining/Reset` headers.
- Per-route limits: build a second `redis_rate.Limit` and mount another handler
  on that route's group.
