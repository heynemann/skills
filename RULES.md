# Rules

Rules for changing this repository.

## Adding a skill

A skill is added only when all of these land in the same change:

1. `skills/<name>/SKILL.md` exists, with frontmatter `name` equal to `<name>`
   and a `description` that says what the skill does and when to use it.
   Supporting files live next to it inside `skills/<name>/`.
2. `README.md` lists the skill in *The skills* table (linking its doc, with
   the prompt that triggers it) and in the per-skill `--skill` install block.
3. `docs/<name>.md` sells and explains the skill, in this order:
   - **Hook**: the title, then one bold sentence on the outcome the user gets,
     and a short paragraph on the problem it solves.
   - **Proof**: a screenshot or example of real output when the skill
     produces something visible. Store images in `docs/assets/`.
   - **Try it**: the install command and two or three example prompts.
   - **What it does / what you get**: concrete behaviour, plus a Mermaid
     diagram when the flow has more than three steps.
   - **Rules it keeps**: the guardrails a user needs in order to trust it.
   - **Requirements** and a short **FAQ**.

   Write for a user deciding whether to install: second person, outcomes
   before mechanics, and nothing the skill doesn't actually do.

Renaming or removing a skill updates the README and the doc too.

## Markdown

Every Markdown file passes `markdownlint` with the repository's
`.markdownlint.json`. The pre-commit hook enforces this; enable it once per
clone with `git config core.hooksPath .githooks`.
