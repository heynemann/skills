# skills

Agent skills I use day-to-day. Each skill lives in `skills/<name>/SKILL.md`
and installs with the [skills.sh](https://skills.sh) CLI.

## Skills

| Skill | Description | Docs |
| --- | --- | --- |
| [address-pr-comments](skills/address-pr-comments/SKILL.md) | Fix a PR's failing checks and answer every review comment (fix or rebut, then resolve) with the gh CLI. | [docs/address-pr-comments.md](docs/address-pr-comments.md) |
| [go-http-api](skills/go-http-api/SKILL.md) | Go HTTP API services on Fiber v3, fx, zap, GORM/PostgreSQL, Redis, Prometheus and OpenTelemetry. | [docs/go-http-api.md](docs/go-http-api.md) |
| [review-pr](skills/review-pr/SKILL.md) | Review a PR against the repo's standards and its spec, posting findings as inline comments on the changed lines with the gh CLI. | [docs/review-pr.md](docs/review-pr.md) |

## Install

```bash
# List the skills in this repo
npx skills add heynemann/skills --list

# Install one skill
npx skills add heynemann/skills --skill go-http-api

# Install all of them
npx skills add heynemann/skills --skill '*'
```

Add `-g` to install for your user instead of the current project, and
`-a <agent>` to target specific agents.

## Contributing

Read [RULES.md](RULES.md) before adding or changing a skill. Enable the
Markdown lint hook once per clone (needs `markdownlint-cli`, e.g.
`brew install markdownlint-cli`):

```bash
git config core.hooksPath .githooks
```
