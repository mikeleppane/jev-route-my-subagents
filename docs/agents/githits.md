# GitHits: upstream source of truth

Use GitHits to verify dependency behavior. Use Code for source at the relevant version,
Documentation for package docs, and Package Intelligence for metadata, vulnerabilities,
dependencies, and release changes. Use get_example for implementation patterns, then check the
APIs against our dependency versions.

- Do this whenever a change relies on how an open source package, CLI or service behaves
  (`requests`, `pyyaml`, `uv`, `ruff`, `pyrefly`, `pytest`, `prek`, gitleaks, GitHub Actions).
  Do not answer from memory.
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
- Not covered: Claude Code plugin, hook and CLI formats are in
  `docs/agents/claude-code-formats.md`. The TypeSafe HTTP API is not an OSS package; verify it
  against <https://docs.typesafe.ai/llms.txt> and <https://docs.typesafe.ai/api.md>.
- Never send prompts, secrets, private paths or private code to GitHits; queries name public
  packages and APIs only.
- Guides: <https://docs.githits.com/guides/agentic-workflow>,
  <https://docs.githits.com/guides/trigger-githits>.
