# Subagent routing

A Claude Code plugin chooses the model tier and effort for the subagents the main model starts,
so that cheap work runs on cheap models. The main model itself is never changed.

## Dispatches and routes

**Dispatch**:
One call by the main model to the `Agent` tool, which starts one subagent.
_Avoid_: subagent call, spawn

**Route**:
The outcome for one dispatch: its source, and for a routed dispatch its tier, alias and effort.
_Avoid_: decision, routing result

**Source**:
What determined a route: pinned, unresolved, escalate, rules, jev, or error.

**Routed dispatch**:
A dispatch whose source is escalate, rules or jev; its model and effort are replaced.

**Pass-through**:
A dispatch that runs exactly as the main model requested.
_Avoid_: skip, bypass

**Fail open**:
A pass-through caused by an error rather than by a pin or the off switch.

**Decision record**:
One log line describing the route of one dispatch.
_Avoid_: log entry, route record

## Pins

**Pinned dispatch**:
A dispatch whose model the router must not change: it carries the `[model-pinned]` marker, names
an agent whose definition sets a concrete model, or is a fork.
_Avoid_: locked, fixed

**Unresolved dispatch**:
A dispatch naming an agent whose definition the router cannot read: a plugin agent, or a custom
agent with no definition the router can find. It passes through.

## Control header

**Control header**:
Line 1 of a dispatch prompt when it holds nothing but markers.
_Avoid_: header line, prefix

**Marker**:
One of `[escalate]` or `[model-pinned]` inside a control header.
_Avoid_: tag, flag

## Policy

**Policy**:
The set of tiers, efforts, rules, escalation and Jev instructions the router routes by.
_Avoid_: config, roster

**Tier**:
A named capability level in the policy, such as `fast`, `balanced` or `strong`, mapped to one alias.
_Avoid_: model, class

**Alias**:
The Claude Code model name a tier maps to: `haiku`, `sonnet`, `opus` or `fable`.
_Avoid_: model ID

**Effort**:
How much the subagent's model thinks: `low`, `medium`, `high` or `xhigh`.
_Avoid_: reasoning level, thinking budget

**Rule**:
A policy entry that maps one subagent type straight to a tier and effort.

**Escalation**:
The policy's tier and effort selected by the `[escalate]` marker.
_Avoid_: upgrade, retry tier

## Jev

**Jev**:
TypeSafe's classifier model, asked to score a dispatch for tier and effort when no marker or rule
applies.

**Score**:
Jev's numeric answer to one question, rounded to a tier or effort level.

**Confidence**:
Jev's own certainty in one score, from 0 to 1.

**Router note**:
The line the main model sees after a routed dispatch, naming its tier, alias and effort.
