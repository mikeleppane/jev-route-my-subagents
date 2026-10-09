# Claude Code formats this repo relies on

Checked 2026-10-09 against the docs below and Claude Code 2.1.295. Re-check a fact here before a
change depends on it, and update this file when a doc or a live smoke disagrees with it.

Sources: [plugin manifest reference](https://code.claude.com/docs/en/plugins-reference),
[hooks reference](https://code.claude.com/docs/en/hooks). Both pages are long (40 KB and over
250 KB); fetch them with a narrow prompt.

## Plugin manifest (`plugin.json`)

- `hooks` takes a path, an inline object, or an array of either. A path is relative to the plugin
  root, starts with `./`, and must resolve inside the root and exist.
- A hooks file wraps the event map in a top-level `"hooks"` key. An inline object is the event map
  itself, with no wrapper.
- Declared hooks merge with `hooks/hooks.json`, which loads first when it exists.
- `userConfig` with `sensitive: true` masks input and stores the value in the platform's secure
  credential store, not `settings.json`.
- Every option is exported to hook processes as `CLAUDE_PLUGIN_OPTION_<KEY>`, `<KEY>` uppercased.
  `${user_config.KEY}` is substituted in exec-form hook `args`; shell-form commands reject it.

## Plugin variables

- `CLAUDE_PLUGIN_ROOT`: the installed version's directory; it moves on update, so no state there.
- `CLAUDE_PLUGIN_DATA`: `~/.claude/plugins/data/<id>/`, where `<id>` is the plugin identifier
  (`name@marketplace`) with every character other than a letter, digit, `_` or `-` replaced by
  `-`. Created on first reference, kept across updates, deleted on the last uninstall unless
  `--keep-data`.
- Hook commands get `${...}` substituted anywhere in `command` and `args`, and receive
  `CLAUDE_PLUGIN_ROOT`, `CLAUDE_PLUGIN_DATA`, `CLAUDE_PROJECT_DIR` and `CLAUDE_PLUGIN_OPTION_<KEY>`
  in their environment.
- The path variables are not in the environment of Bash tool commands. The docs do not say whether
  `CLAUDE_PLUGIN_OPTION_<KEY>` is; the plan's marketplace-install step checks it.

## Hooks

- `PreToolUse`: `hookSpecificOutput.updatedInput` replaces the tool's arguments before it runs.
- `updatedInput.effort` on the `Agent` tool is not documented. Only the live smoke
  (`tests/smoke.py`, subagent `*.meta.json`) shows that it takes effect.
- What happens when several `PreToolUse` hooks return `updatedInput` is not documented. Matching
  hooks run in parallel.
- Exit 0 is success. Exit 2 blocks the tool call even with a JSON allow. Other codes do not block.
- A command hook that reaches its `timeout` is cancelled and its output discarded; on `PreToolUse`
  the call continues. The command-hook default timeout is 600 s; `hooks/hooks.json` sets its own.
- `SessionStart`: `hookSpecificOutput.additionalContext` is added to Claude's context before the
  first prompt.

## CLI output (observed, not documented)

- `claude plugin list --json` prints a list of objects with `id` (`name@marketplace`), `scope`,
  `enabled`, `projectEnabled` and `installPath`. `tests/test_smoke_helpers.py` fakes this shape.
