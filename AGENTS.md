# jev-route-my-subagents

A public Claude Code plugin: one fail-open `PreToolUse` hook on `Agent` that routes each subagent
dispatch it may change to a model alias and an effort. Vocabulary: `GLOSSARY.md`. Decisions:
`docs/adr/`. User documentation: `README.md`.

## Layout

The repository root is both the marketplace root and the plugin root. `scripts/route_hook.py` is
the only entry point (PEP 723 header; modes: hook with no arguments, `--warm`, `--stats PATH`);
`scripts/router.py` holds the pure routing logic and `scripts/policy.json` the shipped policy.
Runtime dependencies live in the PEP 723 header; `pyproject.toml` and `uv.lock` are development
only. `tests/smoke.py` is the manual, paid live smoke.

## Commands

Setup, the gate and the smoke: `README.md`, section Development. After any change to
`tests/smoke.py`, run `uv run python tests/smoke.py --preflight` on a machine with Claude Code and
expect every line PASS. It costs one small paid run; CI cannot run it, because it inspects this
machine's hooks and plugins.

## Invariants

These must survive every change:

- Hook output is exactly `hookSpecificOutput {hookEventName, updatedInput, additionalContext}`.
  `updatedInput.model` takes aliases only (`haiku`, `sonnet`, `opus`, `fable`). JSON is written with
  `allow_nan=False`.
- The hook path always exits 0; any error fails open (no output) and is logged through
  `error_text`. Usage errors exit 1. Exit 2 is never used: it blocks the dispatch.
- Pinned dispatches pass through untouched with source `pinned`: the `[model-pinned]` marker, an
  agent with any matching definition whose frontmatter `model` is concrete (not `inherit`), and a
  fork (`subagent_type == "fork"`). Unresolved dispatches pass through untouched with source
  `unresolved`.
- A passed model does not pin: a `model` or `effort` passed without `[model-pinned]` is overwritten
  on a routed dispatch (`docs/adr/0001-a-passed-model-does-not-pin.md`).
- A missing or empty `subagent_type` routes as `general-purpose`.
- When the route has no effort, `effort` is removed from the input and the router note says
  `effort default`.
- Control header: line 1 (a trailing `\r` ignored) is a header only when it fully matches
  `\s*(?:\[(?:escalate|model-pinned)\]\s*)+`. `[model-pinned]` wins over `[escalate]`. Routed
  prompts have the header stripped; pinned and unresolved prompts keep it. `[escalate]` on a pinned
  dispatch is ignored and the record gets the flag `escalate_ignored`.
- Decision record: one `O_APPEND` write under 4 KiB; the prompt is stored only as SHA-256. The log
  is opened with `O_WRONLY | O_APPEND | O_CREAT | O_NOFOLLOW` and mode 600, then `fchmod` to 600.
  Free-text fields are cut to at most 64 bytes of their JSON-escaped form. A failed write is
  swallowed and never changes the route or the exit code.
- `JEV_ROUTE_MY_SUBAGENTS=off` and `win32` write no decision record.
- Effort names are `low`, `medium`, `high`, `xhigh`; `max` is not supported.
- Router note: `Router <tool_use_id>: <tier> (<alias>, effort <effort>).`

## Quality rules

- Never loosen the ruff or pyrefly configuration.
- Every `# noqa` and `# pyrefly: ignore` names its reason.
- Never skip the commit hooks; CI runs the same checks.
- Tests are plain pytest functions, fully annotated, returning `-> None`.
- Runtime pins in the PEP 723 header of `scripts/route_hook.py` and in `pyproject.toml` stay equal
  (a test checks this).

## Public-repo rules

- This repository is public. Scan each diff for keys, private paths and emails before committing.
- Never commit `docs/design/` or `.scratch/`; both are in `.gitignore`. Never force-add them.
- Stage explicit paths only.
- Never print, log or commit the TypeSafe key.
- Never send prompts to LangSmith. Never send prompts, secrets, private paths or private code to
  GitHits.

## Commit conventions

Conventional Commits: `type(scope): subject`, header at most 72 characters. Types: `feat`, `fix`,
`docs`, `test`, `refactor`, `perf`, `build`, `ci`, `chore`, `revert`. Scopes: `hook`, `router`,
`policy`, `plugin`, `skill`, `tests`, `docs`, `ci`, `deps`, `repo`. No AI attribution. The
`commit-msg` hooks enforce both.

## Agent skills

### Issue tracker

Issues are tracked as local markdown files under `.scratch/<feature-slug>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.

## Upstream behaviour

- Before relying on how an OSS package, CLI or GitHub Action behaves: `docs/agents/githits.md`.
- Before relying on a Claude Code plugin, hook or CLI format: `docs/agents/claude-code-formats.md`.
