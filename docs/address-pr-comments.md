# address-pr-comments

Takes a GitHub pull request from "has review feedback and red checks" to "every
comment answered and resolved, every check green". The agent works through the
PR with the `gh` CLI, without pausing for approval.

## Install

```bash
npx skills add heynemann/skills --skill address-pr-comments
```

Needs the [GitHub CLI](https://cli.github.com/) logged in (`gh auth login`)
with push access to the PR's branch.

## When it activates

When you ask the agent to address, handle, answer or resolve PR comments,
review feedback or failing PR checks.

## How to use it

From a checkout of the repository:

```text
Address the comments on PR 42.
```

```text
Address the PR comments on this branch.
```

Without a number, the agent uses the PR of the current branch, and asks for a
number when the branch has none.

## What it does

1. Checks out the PR's head branch. It stops if you have uncommitted changes.
2. Collects failing checks, unresolved review threads, top-level comments and
   review summaries.
3. Fixes each failing check at its source, reproducing it locally first.
   Skipping tests or loosening lint does not count as a fix.
4. Decides, per comment, to **fix** it or **rebut** it with evidence.
5. Commits each fix separately, then pushes (never force-pushes).
6. Replies to each comment where it lives:
   - review threads get a reply in the thread (`Fixed in <SHA>: ...` or the
     rebuttal) and are resolved;
   - top-level comments and review summaries get a quote-reply, and the
     original is minimized as resolved, since GitHub cannot resolve them.
7. Watches the checks after the push and fixes new failures, up to three
   rounds.
8. Reports in chat: the outcome of each comment and check, and anything left
   open.

It never edits the PR description or posts a single summary comment covering
everything. Comments that ask for nothing (approvals, bot status reports) get
no reply and are listed in the report.
