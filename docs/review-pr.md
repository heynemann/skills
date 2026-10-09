# review-pr

Reviews a GitHub pull request and posts the findings as inline comments on the
lines the PR changes, the way CodeRabbit does. Adapted from
[mattpocock/skills `code-review`](https://github.com/mattpocock/skills/tree/main/skills/engineering/code-review),
which reports in chat instead.

## Install

```bash
npx skills add heynemann/skills --skill review-pr
```

Needs the [GitHub CLI](https://cli.github.com/) logged in (`gh auth login`)
and `python3` (for the bundled diff helper).

## When it activates

When you ask the agent to review a PR or pull request, or to leave review
comments on one.

## How to use it

From a checkout of the repository:

```text
Review PR 42.
```

```text
Review the PR for this branch.
```

Without a number, the agent uses the PR of the current branch, and asks for a
number when the branch has none.

## What it does

The review runs along two axes, each in its own sub-agent so neither colours
the other:

- **Standards**: the repo's documented coding standards (`CONTRIBUTING.md`,
  `AGENTS.md`, `CLAUDE.md`, style guides and similar), plus a baseline of
  Fowler code smells that the repo's own standards override.
- **Spec**: the PR description, the issues it closes or references, and any
  matching spec file. The axis is skipped when none of these describe the
  change.

The findings are posted as one GitHub review with event `COMMENT` (it never
approves or requests changes):

- each finding is an inline comment on its line, labelled with its axis and
  kind, e.g. `Standards · violation`, `Standards · possible Feature Envy`
  (a judgement call) or `Spec · missing`, often with a suggestion block you
  can commit from GitHub;
- the review body is a short summary: the finding count and worst finding per
  axis, plus any finding that has no changed line to attach to.

Before posting, the bundled `scripts/pr_diff.py` checks every comment against
the PR's diff, since GitHub rejects the whole review if one comment misses it.
Findings already raised on the same lines are not repeated.

To answer and resolve the comments it leaves, use
[address-pr-comments](address-pr-comments.md).
