# jev-route-my-subagents

A public Claude Code plugin: one fail-open `PreToolUse` hook on `Agent` that routes each subagent
dispatch it may change to a model alias and an effort. Vocabulary: `GLOSSARY.md`. Decisions:
`docs/adr/`. User documentation: `README.md`.

## Layout

The repository root is both the marketplace root and the plugin root.

- `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`: plugin and marketplace manifests.
- `hooks/hooks.json`: the `PreToolUse` hook on `Agent` and two `SessionStart` hooks (rules text,
  async uv warm-up). `hooks/session-context.json`: the static rules text for the main model.
- `scripts/route_hook.py`: the only entry point, with the PEP 723 header; modes: hook (no
  arguments), `--warm`, `--stats PATH`. `scripts/route_hook.py.lock`: its `uv lock --script` lockfile.
- `scripts/router.py`: pure routing logic. `scripts/policy.json`: the shipped default policy.
- `skills/stats/SKILL.md`: the `/jev-route-my-subagents:stats` skill.
- `tests/`: pytest unit tests; `tests/smoke.py` is the manual, paid live smoke.
- `pyproject.toml`, `uv.lock`: development only (ruff, pyrefly, pytest).
- `.pre-commit-config.yaml`: prek hooks. `.github/workflows/ci.yml`: CI, the real gate.

## Commands

```sh
uv sync
uv run pytest
uv run ruff check
uv run ruff format
uv run pyrefly check
prek install
prek install --hook-type commit-msg
uv run python tests/smoke.py --preflight   # always before the full smoke
uv run python tests/smoke.py
```

The smoke is manual and paid; run it after every Claude Code upgrade.

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
- Never send prompts to LangSmith.

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

## Upstream source of truth: GitHits

Use GitHits to verify dependency behavior. Use Code for source at the relevant version,
Documentation for package docs, and Package Intelligence for metadata, vulnerabilities,
dependencies, and release changes. Use get_example for implementation patterns, then check the
APIs against our dependency versions.

- Do this whenever a change relies on how an open source package, CLI or service behaves
  (`requests`, `pyyaml`, `uv`, `ruff`, `pyrefly`, `pytest`, `prek`, gitleaks, GitHub Actions,
  Claude Code plugin and hook formats). Do not answer from memory.
- Versions: runtime pins in `pyproject.toml` and the PEP 723 header of `scripts/route_hook.py`,
  resolved versions in `uv.lock` and `scripts/route_hook.py.lock`. Target that version, for example
  `pypi:requests@2.34.2`.
- Code and docs: `search` to discover, then `read`, `grep` or `list` on the hit. Use
  `resolve_target` only when the canonical target is unknown.
- Package Intelligence: before adding a dependency or changing a pin, check license,
  vulnerabilities, dependencies and release history (`pkg_info`, `pkg_vulns`, `pkg_deps`,
  `pkg_changelog`). For a version bump, compare current and target versions: advisories, release
  notes and dependency changes (`pkg_upgrade_review`, `pkg_changelog`, `code_diff`).
- Debugging: use GitHits Code to inspect the package at that version; search for the symbol or
  error, then read the relevant source lines.
- Patterns: `get_example` for prior art; adapt it to this repo and cite the source.
- Review: check the APIs and dependency behavior a change relies on; cite what you used, and flag
  any assumption GitHits could not confirm.
- Not covered: the TypeSafe HTTP API is not an OSS package. Verify it against
  <https://docs.typesafe.ai/llms.txt> and <https://docs.typesafe.ai/api.md>.
- Never send prompts, secrets, private paths or private code to GitHits; queries name public
  packages and APIs only.
- Guides: <https://docs.githits.com/guides/agentic-workflow>,
  <https://docs.githits.com/guides/trigger-githits>.
