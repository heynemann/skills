# Rules

Rules for changing this repository.

## Adding a skill

A skill is added only when all of these land in the same change:

1. `skills/<name>/SKILL.md` exists, with frontmatter `name` equal to `<name>`
   and a `description` that says what the skill does and when to use it.
   Supporting files live next to it inside `skills/<name>/`.
2. `README.md` lists the skill in the Skills table, linking its `SKILL.md`
   and its doc.
3. `docs/<name>.md` explains the skill: what it is for, how to install it,
   when it activates, how to use it (with example prompts) and what it
   produces.

Renaming or removing a skill updates the README row and the doc too.

## Markdown

Every Markdown file passes `markdownlint` with the repository's
`.markdownlint.json`. The pre-commit hook enforces this; enable it once per
clone with `git config core.hooksPath .githooks`.
