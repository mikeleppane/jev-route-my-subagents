# A passed model does not pin a dispatch

The `Agent` tool's `model` field cannot tell the router who chose it: the user ("use opus for this")
or the main model's own guess, which is exactly what the router exists to replace. If any passed
`model` pinned a dispatch, routing would silently stop whenever the main model fills the field.
So a dispatch is pinned only by the `[model-pinned]` marker, by an agent definition with a concrete
model, or by being a fork; a passed `model` without the marker is overwritten on a routed dispatch.
The main model is told this rule at session start, and decision records keep the requested model
and effort so the override rate can be measured.

## Considered options

- **Any passed `model` pins.** Rejected: routing coverage would depend on how often the main model
  fills a field it was never asked to fill.
- **Agents whose definition the router cannot read are routed like other dispatches.** Rejected:
  the router cannot tell whether their author pinned a model. Plugin agents (`plugin:agent`) and
  custom agents with no definition the router can find (for example from `--agents` JSON or
  managed settings) pass through as unresolved instead.
