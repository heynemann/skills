---
name: review-pr
description: Review a GitHub pull request along two axes, Standards (the repo's documented coding standards plus a code-smell baseline) and Spec (the PR description and linked issues), and post the findings with the gh CLI as inline review comments on the PR's changed lines, CodeRabbit-style. Use when asked to review a PR or pull request, or to leave review comments on one.
---

# Review PR

Two-axis review of a pull request, published on GitHub:

- **Standards**: does the code conform to this repo's documented coding
  standards?
- **Spec**: does the code faithfully implement the PR's description and the
  issues it links?

Both axes run as **parallel sub-agents** so they don't pollute each other's
context. Their findings are posted as **one GitHub review** with event
`COMMENT`: every finding is an inline comment on the changed line it concerns,
and the review body carries only a short summary.

## Rules

- **Inline, anchored.** A finding goes on the line it concerns, using a line
  label from the annotated diff (step 2). A finding with no changed line to
  anchor to goes in the review body under *Outside the diff*.
- **Axes stay separate.** Every comment names its axis. Findings are never
  merged or reranked across axes (see *Why two axes*).
- **Event is always `COMMENT`.** The review advises; it never approves or
  requests changes.
- **One review per run.** Comments are posted together through the reviews
  API, never one by one, and nothing is posted outside that review.
- Skip anything tooling already enforces (formatters, linters, type checkers,
  tests in CI).

## 1. Find the PR

With a PR number or URL from the user, use it. Otherwise ask `gh` for the PR of
the current branch:

```bash
gh pr view [<number>] --json number,url,state,title,body,headRefOid,closingIssuesReferences
```

`no pull requests found for branch` means there is none: ask the user for the
PR number. Owner and repo come from the URL
(`https://github.com/<owner>/<repo>/pull/<number>`). A PR that is not `OPEN`
is not reviewed: tell the user.

Done when number, owner, repo and `headRefOid` are known.

## 2. Pin the diff and the code

Work in a scratch directory outside the repository (`tmp=$(mktemp -d)`).
`<skill-dir>` is the directory this `SKILL.md` lives in.

```bash
gh pr diff <number> > "$tmp/pr.diff"
python3 <skill-dir>/scripts/pr_diff.py annotate < "$tmp/pr.diff" > "$tmp/pr.annotated.diff"
gh pr view <number> --json commits --jq '.commits[] | "\(.oid[0:7]) \(.messageHeadline)"'
```

The annotated diff labels every commentable line: `[NEW:m]` added (side
`RIGHT`, line m), `[OLD:n]` deleted (side `LEFT`, line n), `[OLD:n,NEW:m]`
unchanged context (side `RIGHT`, line m). These labels are the only source of
truth for comment locations.

The reviewers also read the surrounding code at the PR head. If
`git rev-parse HEAD` equals `headRefOid`, use the working tree. Otherwise check
the head out in a worktree, leaving the user's checkout untouched:

```bash
git worktree add --detach "$tmp/head"
(cd "$tmp/head" && gh pr checkout <number> --detach)
```

Done when the diff is non-empty, the annotated diff is written and the code at
`headRefOid` is readable.

## 3. Identify the spec source

Look for the originating spec, in this order, and gather everything found:

1. The PR title and description.
2. Issues the PR closes (`closingIssuesReferences`) and issue references in the
   commit messages (`#123`, `Closes #45`):
   `gh issue view <number> -R <owner>/<repo> --json title,body,comments`.
3. A spec file under `docs/`, `specs/` or `.scratch/` matching the branch name
   or feature.

If none of these states what the change should do, the **Spec** sub-agent is
skipped and the summary says "Spec: skipped, no spec found".

## 4. Identify the standards sources

Search the repo for every file that documents how code should be written. When
`CODING_STANDARDS.md`, `CONTRIBUTING.md`, `AGENTS.md` or `CLAUDE.md` exists, it
must be on the list. Linter and formatter configs tell you what tooling already
enforces.

On top of whatever the repo documents, the Standards axis always carries the
**smell baseline** below: a fixed set of Fowler code smells (*Refactoring*,
ch.3) that applies even when a repo documents nothing. Two rules bind it:

- **The repo overrides.** A documented repo standard always wins; where it
  endorses something the baseline would flag, suppress the smell.
- **Always a judgement call.** Each smell is a labelled heuristic ("possible
  Feature Envy"), never a hard violation. Like any standard here, skip anything
  tooling already enforces.

Each smell reads *what it is* → *how to fix*; match it against the diff:

- **Mysterious Name**: a function, variable, or type whose name doesn't reveal
  what it does or holds. → rename it; if no honest name comes, the design's
  murky.
- **Duplicated Code**: the same logic shape appears in more than one hunk or
  file in the change. → extract the shared shape, call it from both.
- **Feature Envy**: a method that reaches into another object's data more than
  its own. → move the method onto the data it envies.
- **Data Clumps**: the same few fields or params keep travelling together (a
  type wanting to be born). → bundle them into one type, pass that.
- **Primitive Obsession**: a primitive or string standing in for a domain
  concept that deserves its own type. → give the concept its own small type.
- **Repeated Switches**: the same `switch`/`if`-cascade on the same type recurs
  across the change. → replace with polymorphism, or one map both sites share.
- **Shotgun Surgery**: one logical change forces scattered edits across many
  files in the diff. → gather what changes together into one module.
- **Divergent Change**: one file or module is edited for several unrelated
  reasons. → split so each module changes for one reason.
- **Speculative Generality**: abstraction, parameters, or hooks added for needs
  the spec doesn't have. → delete it; inline back until a real need shows.
- **Message Chains**: long `a.b().c().d()` navigation the caller shouldn't
  depend on. → hide the walk behind one method on the first object.
- **Middle Man**: a class or function that mostly just delegates onward. → cut
  it, call the real target direct.
- **Refused Bequest**: a subclass or implementer that ignores or overrides most
  of what it inherits. → drop the inheritance, use composition.

## 5. Spawn both sub-agents in parallel

Issue both sub-agent calls together, in the foreground. Without sub-agents, run
the two reviews as separate passes and keep their findings apart.

Both prompts include the path of the annotated diff, the commit list, where to
read the code at the PR head, and this **finding format**, one entry per
finding:

- `axis`: `Standards` or `Spec`.
- `kind`: Standards uses `violation` (documented standard) or
  `possible <Smell>` (baseline); Spec uses `missing`, `scope creep` or
  `wrong`.
- `path`, `side`, `line`, optional `start_line`: copied from a label in the
  annotated diff (`[OLD:n]` is `LEFT` n; `[NEW:m]` and `[OLD:n,NEW:m]` are
  `RIGHT` m). A range spans at most 10 lines inside one hunk. With no changed
  line to point at, `line` is `null`.
- `message`: what is wrong and why it matters, at most three sentences.
- `evidence`: the standard (file and rule, as a link), the quoted spec line, or
  the smell's name with the quoted hunk.
- `suggestion` (optional): replacement code for exactly lines
  `start_line`–`line` (or `line`) on the `RIGHT` side, in the file's
  indentation, opening and closing the same brackets the replaced lines do.
  GitHub commits a suggestion on its own, so it must leave the file working
  when applied alone; a change that also needs other lines gets no suggestion.

**Standards sub-agent prompt** also includes the list of standards-source files
from step 4, **plus the smell baseline from step 4** pasted in full (the
sub-agent has no other access to it), and the brief: "Report every place the
diff violates a documented standard (cite the file and rule) and any baseline
smell you spot (name it and quote the hunk). Documented-standard breaches can
be hard violations; baseline smells are always judgement calls, and a
documented repo standard overrides the baseline. Skip anything tooling
enforces."

**Spec sub-agent prompt** also includes the spec contents from step 3, and the
brief: "Report: (a) requirements the spec asked for that are missing or
partial; (b) behaviour in the diff that wasn't asked for (scope creep); (c)
requirements that look implemented but where the implementation looks wrong.
Quote the spec line for each finding."

Done when both sub-agents have returned (or the Spec one is skipped).

## 6. Build the review

Drop findings that repeat an existing comment on the same lines:

```bash
gh api repos/<owner>/<repo>/pulls/<number>/comments --paginate --jq '.[] | {path, line, body}'
```

Each anchored finding becomes one comment; a suggestion that would break the
file when committed alone is dropped and its message kept. The body starts with
the axis and kind, so authors can tell hard findings from judgement calls:

````markdown
**Standards · possible Feature Envy**: <message>

<evidence>

```suggestion
<replacement lines>
```
````

The review body is the summary, one line per axis with its finding count and
worst finding, then the findings that have no line:

```markdown
**Standards:** 4 findings (1 violation, 3 possible smells). Worst: <one line>.
**Spec:** 1 finding. Worst: <one line>.

**Outside the diff**

- `path/to/file.go`: <finding>
```

With no findings at all, the body is the two count lines alone.

Write `$tmp/review.json`:

```json
{
  "commit_id": "<headRefOid>",
  "event": "COMMENT",
  "body": "<summary>",
  "comments": [
    { "path": "a/b.go", "line": 42, "side": "RIGHT", "start_line": 40, "start_side": "RIGHT", "body": "..." }
  ]
}
```

GitHub rejects the whole review if a single comment misses the diff, so
validate first:

```bash
python3 <skill-dir>/scripts/pr_diff.py check "$tmp/review.json" < "$tmp/pr.diff"
```

Fix each reported comment from the annotated diff, or move it to *Outside the
diff*, and rerun.

Done when the check prints `N/N comments valid`.

## 7. Post and report

```bash
gh api repos/<owner>/<repo>/pulls/<number>/reviews --input "$tmp/review.json" --jq .html_url
```

A `422` naming a line or `commit_id` means the PR moved since step 2: start
again from step 2.

Remove the worktree if step 2 made one (`git worktree remove "$tmp/head"`), then
tell the user: the review URL, the finding count and worst finding per axis,
and whether the Spec axis was skipped.

## Why two axes

A change can pass one axis and fail the other:

- Code that follows every standard but implements the wrong thing →
  **Standards pass, Spec fail.**
- Code that does exactly what the issue asked but breaks the project's
  conventions → **Spec pass, Standards fail.**

Reporting them separately stops one axis from masking the other.

---

Adapted from [mattpocock/skills `code-review`](https://github.com/mattpocock/skills/tree/main/skills/engineering/code-review)
under the MIT License; see [NOTICE](NOTICE).
