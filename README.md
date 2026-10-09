# jev-route-my-subagents

Opus 5.5 orchestrates, Jev routes your subagents.

A Claude Code plugin with one `PreToolUse` hook on the `Agent` tool. The hook examines every
dispatch (one `Agent` call that starts one subagent) and routes each one it may change to a model
alias (`haiku`, `sonnet`, `opus`) and an effort (`low`, `medium`, `high`, `xhigh`). The main
session model is never touched, so the main session's prompt cache stays intact. Cheap work runs
on cheap models, which stretches your Claude Max quota.

## Status

Alpha. There are no savings claims yet. Effort routing relies on Claude Code applying an `effort`
field that a hook writes into the `Agent` input; that field is not in the hooks documentation and a
Claude Code release may change or drop it.

## Prior art

- Main-turn routers choose the model for the main conversation: `gargpratyush/jev-router`,
  `dirien/jev-router` and `alexei-led/claude-router`, which leaves subagents unchanged.
- Subagent routing that depends on the main model following instructions:
  `DefensiveSniper/jev-subagent-router` and `kerpopule/hermes-jev-skills`.

This plugin is a hook: it sees every dispatch, needs no cooperation from the main model, and fails
open.

## Requirements

- Linux (tested) or macOS (expected to work, untested). On Windows the hook passes every dispatch
  through unchanged.
- [`uv`](https://docs.astral.sh/uv/) on the `PATH` that Claude Code hooks see.
- Claude Code <!-- filled after the smoke (Task 10) -->
- A TypeSafe API key for Jev.

Tested with the Anthropic API alias mapping (Claude Max). Other providers map the aliases to older
models, some without effort support.

## Install

1. Install Python 3.14 for uv:

   ```sh
   uv python install 3.14
   ```

   Otherwise the first dispatch may fail open while uv downloads the interpreter. The interpreter
   and the uv cache persist across sessions, and a session-start hook warms the cache in the
   background.

2. Add the marketplace and install the plugin:

   ```sh
   claude plugin marketplace add mikeleppane/jev-route-my-subagents
   claude plugin install jev-route-my-subagents@jev-route-my-subagents
   ```

3. Enter your TypeSafe API key when Claude Code prompts for it. The plugin option is marked
   sensitive. <!-- filled after the smoke (Task 10) -->

## Privacy

- When a dispatch is routed by Jev, the full dispatch prompt, its `subagent_type` and its
  `description` are sent to TypeSafe (`https://api.typesafe.ai`). TypeSafe's retention policy for
  that data is unknown. Do not install this plugin if your prompts must not leave your machine.
- Nothing is sent to LangSmith or any other tracer.
- The local decision record stores only a SHA-256 hash of the prompt, never the prompt itself.
- Do not export `TYPESAFE_API_KEY` in your shell: every Bash command the model runs can read it.
  Use the plugin prompt instead. The hook reads `TYPESAFE_API_KEY` only as a fallback when the
  plugin option is empty. Whether the plugin option is visible to Bash tool commands is not yet
  verified.

## Routing

The hook decides each dispatch in this order:

1. **Control header.** Line 1 of the prompt is a control header only when it holds nothing but
   markers, `[escalate]` and `[model-pinned]` (for example `[escalate]` or
   `[escalate] [model-pinned]`). `[escalate] fix the bug` is not a header, and markers are
   case-sensitive. Routed prompts have the header line removed; pinned and unresolved prompts keep
   it.
2. **Pinned.** The dispatch passes through unchanged when it carries `[model-pinned]`, when it is a
   fork, or when any agent definition matching its `subagent_type` sets a concrete `model` (not
   `inherit`). Definitions are read from `.claude/agents` in the working directory and every parent
   directory, then from `~/.claude/agents`, matching the frontmatter `name`. `[escalate]` on a
   pinned dispatch is ignored.
3. **Unresolved.** The dispatch also passes through unchanged when the router cannot read the
   agent's definition: a plugin agent (`plugin:agent`), or a custom agent with no definition the
   router can find, which includes agents from `--agents` JSON and from managed settings.
4. **`[escalate]`** routes to the policy's escalation tier and effort.
5. **Rules** map a `subagent_type` straight to a tier and effort.
6. **Jev** scores everything else for tier and effort.

A passed `model` does not pin. A `model` or `effort` that the main model passes without
`[model-pinned]` is overwritten on a routed dispatch, because the router cannot tell whether the
user or the main model chose it. See
[ADR 0001](docs/adr/0001-a-passed-model-does-not-pin.md).

A session-start hook tells the main model these rules. After each routed dispatch, the main model
sees a router note: `Router <tool_use_id>: <tier> (<alias>, effort <effort>).`

The default policy (`scripts/policy.json`):

| Tier | Alias | Efforts |
| --- | --- | --- |
| `fast` | `haiku` | `low`, `medium`, `high`, `xhigh` |
| `balanced` | `sonnet` | `low`, `medium`, `high`, `xhigh` |
| `strong` | `opus` | `low`, `medium`, `high`, `xhigh` |

Rules:

| `subagent_type` | Tier | Effort |
| --- | --- | --- |
| `Explore` | `fast` | `low` |

Escalation: `strong`, `xhigh`. A missing `subagent_type` routes as `general-purpose`.

## Configuration

**Policy override.** Put a policy at `~/.claude/plugins/data/<plugin id>/policy.json` (the
plugin's data directory). It replaces the shipped policy whole, so start from a copy of
`scripts/policy.json`. Tier aliases may be `haiku`, `sonnet`, `opus` or `fable`. An invalid
override is logged as an error and every dispatch passes through.

Uninstalling the plugin deletes its data directory, including your override and the decision
records, unless you pass `--keep-data`.

**Fable.** An override may map a tier to `fable`. Depending on your plan and seat tier, Fable can
bill to usage credits rather than your plan's included limits, and in a non-interactive session it
may wait on a consent prompt.

**Off switch.** For one session, start Claude Code with `JEV_ROUTE_MY_SUBAGENTS=off`. To turn it
off persistently, run `claude plugin disable jev-route-my-subagents@jev-route-my-subagents`. The
session-start rules text is still injected when routing is off or no key is set.

## Stats

Run the user-invocable skill:

```text
/jev-route-my-subagents:stats
```

Or run the command yourself from a clone of this repository:

```sh
uv run --no-config --locked --script scripts/route_hook.py \
  --stats "$HOME/.claude/plugins/data/jev-route-my-subagents-jev-route-my-subagents/decisions.jsonl"
```

The data directory name is the plugin ID `jev-route-my-subagents@jev-route-my-subagents` with
every character other than a letter, digit, `_` or `-` replaced by `-`.

Both print totals by source, tier and effort; escalation and error rates; mean Jev confidences; and
how often a model or effort requested by the main model was overridden. Stats cover only the
dispatches the router saw: not sessions with routing off, and not Windows.

After installing, check the error rate. Fail-open also hides misconfiguration such as a missing
key or an invalid override: routing silently stops, and errors in the stats are the signal.

## Failure behaviour

The hook fails open in every case: a missing key, a Jev error, the 3 second routing budget running
out, an invalid policy, `uv` missing from the `PATH`, or any other error lets the dispatch run
exactly as the main model requested. The hook never blocks a dispatch.

Each dispatch the router sees appends one decision record to
`~/.claude/plugins/data/<plugin id>/decisions.jsonl`: time, session and tool-use IDs, subagent
type, requested model and effort, source, tier, alias, effort, Jev scores and confidences, flags,
latency, the prompt's SHA-256 hash and an error summary.

A hook that Claude Code kills at its 5 second timeout writes no decision record, so `--stats`
cannot see it.

Another plugin or hook that also rewrites the `Agent` tool input combines with this one in a way
Claude Code does not document. Disable one of them.

The live smoke test reads each spawned subagent's `*.meta.json`. That file shows the model and
effort Claude Code recorded for the subagent, not the model's own report of what it is, and is the
evidence that routing took effect.

## Development

See [`AGENTS.md`](AGENTS.md).

## License

MIT. See [`LICENSE`](LICENSE).
