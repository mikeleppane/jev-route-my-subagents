"""PreToolUse hook on Agent: route each dispatch it may change. Always fails open."""

import time

_STARTED = time.monotonic()

import hashlib  # noqa: E402 - latency counts from before the imports
import json  # noqa: E402 - latency counts from before the imports
import sys  # noqa: E402 - latency counts from before the imports
import threading  # noqa: E402 - latency counts from before the imports
from datetime import UTC, datetime  # noqa: E402 - latency counts from before the imports
from pathlib import Path  # noqa: E402 - latency counts from before the imports
from typing import TYPE_CHECKING  # noqa: E402 - latency counts from before the imports

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from router import Policy, Route, Scores

BUDGET_S = 3.0
MAX_LINE = 4096
CAP = 64
OFF_VAR = "JEV_ROUTE_MY_SUBAGENTS"
_MAX_ERROR = 200


class Deadline(Exception):  # noqa: N818 - the spec names it; it records as "Deadline: ..."
    """Routing outlived the budget; its worker thread is abandoned."""


class KeyUnavailable(Exception):  # noqa: N818 - the spec names it; it records as "KeyUnavailable: ..."
    pass


class PayloadError(ValueError):
    pass


def cap(text: str, limit: int = CAP) -> str:
    size = 0
    for i, char in enumerate(text):
        size += len(json.dumps(char)) - 2  # escaped length; ASCII, so bytes
        if size > limit:
            return text[:i]
    return text


def load_key(env: Mapping[str, str]) -> str:
    for name in ("CLAUDE_PLUGIN_OPTION_TYPESAFE_API_KEY", "TYPESAFE_API_KEY"):
        if key := env.get(name):
            return key
    raise KeyUnavailable("key missing")


def error_text(exc: BaseException) -> str:
    local: tuple[type[BaseException], ...] = (KeyUnavailable, PayloadError, Deadline)
    router = sys.modules.get("router")  # absent when its import failed
    if router is not None:
        local += (router.PolicyError, router.JevError)
    if isinstance(exc, local):
        return f"{type(exc).__name__}: {exc}"[:_MAX_ERROR]
    return type(exc).__name__  # external messages can carry prompt text or response bodies


def policy_path(env: Mapping[str, str]) -> Path:
    import router  # noqa: PLC0415 - decide imports it first, inside its guarded block

    data = env.get("CLAUDE_PLUGIN_DATA")
    path = Path(data, "policy.json") if data else None
    return path if path is not None and path.is_file() else router.POLICY_PATH


def _load(raw: bytes) -> dict[str, object]:
    try:
        payload: object = json.loads(raw.decode("utf-8"))
    except ValueError:  # UnicodeDecodeError and JSONDecodeError; their messages hold the input
        raise PayloadError("payload is not UTF-8 JSON") from None
    if not isinstance(payload, dict):
        raise PayloadError("payload is not an object")
    return payload


def _prompt(tool_input: dict[str, object]) -> str:
    prompt = tool_input.get("prompt")
    if not isinstance(prompt, str):
        raise PayloadError("tool_input.prompt is not a string")
    return prompt


def _within[T](work: Callable[[], T], deadline: float) -> T:
    result: list[T] = []
    failure: list[BaseException] = []

    def target() -> None:
        try:
            result.append(work())
        except BaseException as exc:  # SystemExit and KeyboardInterrupt too: report, never die
            failure.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(max(0.0, deadline - time.monotonic()))
    if thread.is_alive():
        raise Deadline(f"routing exceeded {BUDGET_S} s")
    if failure:
        raise failure[0]
    if not result:
        raise RuntimeError("routing ended without an outcome")
    return result[0]


def _output(route: Route, tool_input: dict[str, object], tool_use_id: object) -> str:
    updated = dict(tool_input, model=route.alias, prompt=route.prompt)
    if route.effort is None:
        updated.pop("effort", None)
    else:
        updated["effort"] = route.effort
    note = (
        f"Router {tool_use_id}: {route.tier} ({route.alias}, effort {route.effort or 'default'})."
    )
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "updatedInput": updated,
                "additionalContext": note,
            }
        },
        allow_nan=False,  # NaN or Infinity in the input (1e309 parses as inf) is invalid JSON
    )


def _capped(value: object) -> str | None:
    return cap(value) if isinstance(value, str) else None


def decide(
    raw: bytes, env: Mapping[str, str], deadline: float
) -> tuple[str | None, dict[str, object] | None]:
    if env.get(OFF_VAR) == "off" or sys.platform == "win32":
        return None, None
    payload: dict[str, object] = {}
    tool_input: dict[str, object] = {}
    route: Route | None = None
    sha: str | None = None
    out: str | None = None
    error: BaseException | None = None
    try:
        payload = _load(raw)
        given = payload.get("tool_input")
        tool_input = given if isinstance(given, dict) else {}
        prompt = _prompt(tool_input)
        import router  # noqa: PLC0415 - a broken install must still fail open

        body = router.parse_header(prompt)[1]
        sha = hashlib.sha256(body.encode("utf-8", "surrogatepass")).hexdigest()

        def ask(state: str, policy: Policy) -> Scores:
            return router.jev_asker(load_key(env))(state, policy)

        def classify() -> Route:
            policy = router.load_policy(policy_path(env))
            subagent_type = tool_input.get("subagent_type")
            description = tool_input.get("description")
            cwd = payload.get("cwd")
            dispatch = router.Dispatch(
                subagent_type
                if isinstance(subagent_type, str) and subagent_type
                else "general-purpose",
                description if isinstance(description, str) else "",
                prompt,
                Path(cwd) if isinstance(cwd, str) else None,
                Path(env.get("HOME") or Path.home()),
            )
            return router.classify(dispatch, policy, ask)

        route = _within(classify, deadline)
        if route.source in router.ROUTED:
            out = _output(route, tool_input, payload.get("tool_use_id"))
    except BaseException as exc:  # fail open: any error leaves the dispatch untouched
        error = exc
    record: dict[str, object] = {
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "session_id": _capped(payload.get("session_id")),
        "tool_use_id": _capped(payload.get("tool_use_id")),
        "subagent_type": _capped(tool_input.get("subagent_type")),
        "requested_model": _capped(tool_input.get("model")),
        "requested_effort": _capped(tool_input.get("effort")),
        "source": "error" if error is not None or route is None else route.source,
        "tier": None if route is None else _capped(route.tier),
        "alias": None if route is None else route.alias,
        "effort": None if route is None else route.effort,
        "tier_score": None if route is None else route.tier_score,
        "effort_score": None if route is None else route.effort_score,
        "tier_confidence": None if route is None else route.tier_confidence,
        "effort_confidence": None if route is None else route.effort_confidence,
        "flags": [] if route is None else list(route.flags),
        "latency_ms": int((time.monotonic() - _STARTED) * 1000),
        "prompt_sha": sha,
        "error": None if error is None else error_text(error),
    }
    return (out if error is None else None), record
