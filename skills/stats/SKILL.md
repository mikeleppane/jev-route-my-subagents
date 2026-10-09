---
name: stats
description: Show a summary of subagent routing decisions recorded by this plugin.
disable-model-invocation: true
---

Run the stats command and show its JSON output:

```sh
uv run --no-config --locked --script "${CLAUDE_PLUGIN_ROOT}/scripts/route_hook.py" --stats "${CLAUDE_PLUGIN_DATA}/decisions.jsonl"
```

Then summarize totals by source, tier, and effort; escalation and error rates; mean tier and effort
confidence; and overrides. Include the sample size for each summary: `total` for source, tier, and
effort counts; `confidence.n` for confidence means; `overrides.n` for overrides; and `total` as
the denominator for escalation and error rates. A missing log reports `total: 0`.
