import hashlib
import json
import math
import sys
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, NoReturn

import pytest
import requests
import router
from route_hook import CAP, MAX_LINE, OFF_VAR, KeyUnavailable, cap, decide, error_text, load_key
from router import AskJev, JevError, Policy, PolicyError, Scores

if TYPE_CHECKING:
    from pathlib import Path

FIELDS = {
    "ts", "session_id", "tool_use_id", "subagent_type", "requested_model", "requested_effort",
    "source", "tier", "alias", "effort", "tier_score", "effort_score", "tier_confidence",
    "effort_confidence", "flags", "latency_ms", "prompt_sha", "error",
}  # fmt: skip
SCORES = Scores(2.0, 1.0, 0.9, 0.6)
KEY = {"TYPESAFE_API_KEY": "k-test"}
FACE = "\U0001f600"

type Payload = Callable[..., bytes]


def far() -> float:
    return time.monotonic() + 60


def asker(ask: AskJev) -> Callable[..., AskJev]:
    def make(api_key: str, timeout: tuple[float, float] = (1.0, 2.0)) -> AskJev:
        return ask

    return make


def fail(exc: BaseException) -> Callable[..., NoReturn]:
    def raise_it(*_: object) -> NoReturn:
        raise exc

    return raise_it


def hook_output(out: str | None) -> dict[str, object]:
    assert out is not None
    return json.loads(out)["hookSpecificOutput"]


def updated_input(out: str | None) -> dict[str, object]:
    found = hook_output(out)["updatedInput"]
    assert isinstance(found, dict)
    return found


def override(data_dir: Path, policy: object) -> None:
    data_dir.mkdir()
    (data_dir / "policy.json").write_text(json.dumps(policy), encoding="utf-8")


@pytest.fixture(autouse=True)
def jev(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub Jev in every test: no request ever leaves; returns the keys it was given."""
    keys: list[str] = []

    def make(api_key: str, timeout: tuple[float, float] = (1.0, 2.0)) -> AskJev:
        keys.append(api_key)
        return lambda _state, _policy: SCORES

    monkeypatch.setattr(router, "jev_asker", make)
    return keys


def test_routes_jev_and_records(payload: Payload, env: dict[str, str]) -> None:
    out, rec = decide(payload(), env | KEY, far())
    o = hook_output(out)
    assert set(o) == {"hookEventName", "updatedInput", "additionalContext"}
    assert o["hookEventName"] == "PreToolUse"
    ui = updated_input(out)
    assert (ui["model"], ui["effort"]) == ("opus", "medium")
    assert o["additionalContext"] == "Router tu1: strong (opus, effort medium)."
    assert rec is not None
    assert set(rec) == FIELDS
    assert (rec["source"], rec["tier_confidence"], rec["effort_confidence"]) == ("jev", 0.9, 0.6)
    assert (rec["session_id"], rec["tool_use_id"], rec["error"]) == ("s1", "tu1", None)
    assert (rec["tier"], rec["alias"], rec["effort"]) == ("strong", "opus", "medium")
    assert str(rec["ts"]).endswith("Z")


def test_passed_model_overwritten_and_recorded(payload: Payload, env: dict[str, str]) -> None:
    out, rec = decide(payload(subagent_type="Explore", model="opus", effort="high"), env, far())
    ui = updated_input(out)
    assert (ui["model"], ui["effort"]) == ("haiku", "low")
    assert rec is not None
    assert (rec["source"], rec["requested_model"], rec["requested_effort"]) == (
        "rules",
        "opus",
        "high",
    )


def test_pinned_keeps_requested_and_emits_nothing(payload: Payload, env: dict[str, str]) -> None:
    out, rec = decide(payload(prompt="[model-pinned]\nx", model="sonnet"), env, far())
    assert out is None
    assert rec is not None
    assert (rec["source"], rec["requested_model"], rec["alias"]) == ("pinned", "sonnet", None)
    assert rec["error"] is None


def test_unresolved_emits_nothing(payload: Payload, env: dict[str, str]) -> None:
    out, rec = decide(payload(subagent_type="unknown-agent"), env | KEY, far())
    assert out is None
    assert rec is not None
    assert (rec["source"], rec["error"]) == ("unresolved", None)


def test_effort_removed_when_tier_has_none(
    payload: Payload, env: dict[str, str], data_dir: Path
) -> None:
    raw = json.loads(router.POLICY_PATH.read_text(encoding="utf-8"))
    next(t for t in raw["tiers"] if t["name"] == "fast")["efforts"] = []
    override(data_dir, raw)
    out, rec = decide(payload(subagent_type="Explore", effort="high"), env, far())
    assert "effort" not in updated_input(out)
    assert hook_output(out)["additionalContext"] == "Router tu1: fast (haiku, effort default)."
    assert rec is not None
    assert (rec["effort"], rec["requested_effort"]) == (None, "high")


def test_missing_subagent_type_routes_as_general_purpose(
    payload: Payload, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    states: list[str] = []

    def ask(state: str, policy: Policy) -> Scores:
        states.append(state)
        return SCORES

    monkeypatch.setattr(router, "jev_asker", asker(ask))
    for given in (None, ""):
        raw = json.loads(payload())
        if given is None:
            del raw["tool_input"]["subagent_type"]
        else:
            raw["tool_input"]["subagent_type"] = given
        out, rec = decide(json.dumps(raw).encode(), env | KEY, far())
        assert updated_input(out)["model"] == "opus"
        assert rec is not None
        assert (rec["source"], rec["subagent_type"]) == ("jev", given)
    assert all(s.startswith("subagent_type: general-purpose\n") for s in states)
    assert len(states) == 2


def test_unrelated_input_fields_preserved(payload: Payload, env: dict[str, str]) -> None:
    out, _ = decide(payload(run_in_background=True, x=[1]), env | KEY, far())
    ui = updated_input(out)
    assert (ui["run_in_background"], ui["x"], ui["description"]) == (True, [1], "d")
    assert ui["subagent_type"] == "general-purpose"


def test_header_stripped_and_hashed(payload: Payload, env: dict[str, str], data_dir: Path) -> None:
    sha = hashlib.sha256(b"body").hexdigest()
    out, rec = decide(payload(prompt="[escalate]\nbody"), env, far())
    ui = updated_input(out)
    assert (ui["prompt"], ui["model"], ui["effort"]) == ("body", "opus", "xhigh")
    assert rec is not None
    assert (rec["source"], rec["prompt_sha"]) == ("escalate", sha)
    override(data_dir, {})
    _, err = decide(payload(prompt="[escalate]\nbody"), env, far())
    assert err is not None
    assert (err["source"], err["prompt_sha"]) == ("error", sha)


def test_key_option_beats_env_and_is_read_only_on_jev_path(
    payload: Payload, env: dict[str, str], jev: list[str]
) -> None:
    assert load_key({"CLAUDE_PLUGIN_OPTION_TYPESAFE_API_KEY": "a", "TYPESAFE_API_KEY": "b"}) == "a"
    assert load_key({"CLAUDE_PLUGIN_OPTION_TYPESAFE_API_KEY": "", "TYPESAFE_API_KEY": "b"}) == "b"
    with pytest.raises(KeyUnavailable, match="key missing"):
        load_key({})
    with pytest.raises(KeyUnavailable, match="key missing"):
        load_key({"CLAUDE_PLUGIN_OPTION_TYPESAFE_API_KEY": "", "TYPESAFE_API_KEY": ""})
    for raw in (
        payload(prompt="[escalate]\np"),
        payload(subagent_type="Explore"),
        payload(prompt="[model-pinned]\np"),
    ):
        _, rec = decide(raw, env, far())
        assert rec is not None
        assert rec["error"] is None
    assert jev == []
    _, rec = decide(payload(), env, far())
    assert rec is not None
    assert (rec["source"], rec["error"]) == ("error", "KeyUnavailable: key missing")
    decide(payload(), env | {"CLAUDE_PLUGIN_OPTION_TYPESAFE_API_KEY": "opt"} | KEY, far())
    assert jev == ["opt"]


def test_deadline(payload: Payload, env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(*_: object) -> None:
        time.sleep(1)

    monkeypatch.setattr(router, "classify", slow)
    start = time.monotonic()
    out, rec = decide(payload(), env, start + 0.1)
    assert time.monotonic() - start < 0.5
    assert out is None
    assert rec is not None
    assert rec["source"] == "error"
    assert str(rec["error"]).startswith("Deadline")


def test_worker_base_exception_fails_open(
    payload: Payload, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for exc, name in ((SystemExit(3), "SystemExit"), (KeyboardInterrupt(), "KeyboardInterrupt")):
        monkeypatch.setattr(router, "classify", fail(exc))
        out, rec = decide(payload(), env, far())
        assert out is None
        assert rec is not None
        assert (rec["source"], rec["error"]) == ("error", name)


def test_off_and_win32_do_nothing(
    payload: Payload, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert decide(payload(), env | KEY | {OFF_VAR: "off"}, far()) == (None, None)
    monkeypatch.setattr(sys, "platform", "win32")
    assert decide(payload(), env | KEY, far()) == (None, None)


def test_invalid_override_fails_open(payload: Payload, env: dict[str, str], data_dir: Path) -> None:
    override(data_dir, {"tiers": []})
    out, rec = decide(payload(subagent_type="Explore"), env, far())
    assert out is None
    assert rec is not None
    assert rec["source"] == "error"
    assert str(rec["error"]).startswith("PolicyError")


@pytest.mark.parametrize("kind", ["directory", "dangling-symlink"])
def test_present_non_file_override_fails_open(
    payload: Payload, env: dict[str, str], data_dir: Path, kind: str
) -> None:
    data_dir.mkdir()
    path = data_dir / "policy.json"
    if kind == "directory":
        path.mkdir()
    else:
        path.symlink_to(data_dir / "absent.json")
    out, rec = decide(payload(subagent_type="Explore"), env, far())
    assert out is None
    assert rec is not None
    assert (rec["source"], rec["error"]) == (
        "error",
        "PolicyError: policy file unreadable or not JSON",
    )


def test_fail_open_cases(
    payload: Payload, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow_ask(state: str, policy: Policy) -> Scores:
        time.sleep(1)
        return SCORES

    def slow_scan(*_: object) -> list[object]:
        time.sleep(1)
        return ["opus"]  # pinned: the abandoned worker must never reach the real Jev client

    keyed = env | KEY
    nan = Scores(math.nan, 0.0, 0.5, 0.5)
    # name: (stdin, env, router attribute to patch, its replacement, budget s, error prefix)
    cases: dict[str, tuple[bytes, dict[str, str], str | None, object, float, str]] = {
        "malformed stdin": (b"{not json", keyed, None, None, 60, "PayloadError"),
        "payload not an object": (b"[]", keyed, None, None, 60, "PayloadError"),
        "prompt not a string": (payload(prompt=None), keyed, None, None, 60, "PayloadError"),
        "tool_input not an object": (b'{"tool_input": []}', keyed, None, None, 60, "PayloadError"),
        "jev raises": (
            payload(),
            keyed,
            "jev_asker",
            asker(fail(RuntimeError("boom"))),
            60,
            "RuntimeError",
        ),
        "non-finite score": (
            payload(),
            keyed,
            "jev_asker",
            asker(lambda _state, _policy: nan),
            60,
            "ValueError",
        ),
        "key missing": (payload(), env, None, None, 60, "KeyUnavailable"),
        "deadline in jev": (payload(), keyed, "jev_asker", asker(slow_ask), 0.2, "Deadline"),
        "deadline in agent scan": (payload(), keyed, "agent_models", slow_scan, 0.2, "Deadline"),
        "imported SystemExit(2)": (
            payload(),
            keyed,
            "classify",
            fail(SystemExit(2)),
            60,
            "SystemExit",
        ),
    }
    for name, (raw, case_env, target, value, budget, prefix) in cases.items():
        with monkeypatch.context() as m:
            if target is not None:
                m.setattr(router, target, value)
            start = time.monotonic()
            out, rec = decide(raw, case_env, start + budget)
            assert time.monotonic() - start < 1.0, name
            assert out is None, name
            assert rec is not None, name
            assert rec["source"] == "error", name
            assert str(rec["error"]).startswith(prefix), name


def test_invalid_utf8_stdin(env: dict[str, str]) -> None:
    out, rec = decide(b"\xff\xfe", env | KEY, far())
    assert out is None
    assert rec is not None
    assert set(rec) == FIELDS
    assert (rec["source"], rec["prompt_sha"]) == ("error", None)
    assert str(rec["error"]).startswith("PayloadError")


def test_cap_counts_escaped_bytes() -> None:
    assert cap("a" * 100) == "a" * 64
    assert cap("short") == "short"
    assert len(json.dumps(cap(FACE * 100))) - 2 <= CAP
    assert cap(FACE * 100) == FACE * 5  # 12 escaped bytes each
    assert cap('"' * 100) == '"' * 32  # 2 escaped bytes each


def test_huge_prompt_and_ids(payload: Payload, env: dict[str, str]) -> None:
    big = FACE * 10_000
    for subagent_type in (big, "general-purpose"):
        raw = payload(
            prompt="[escalate]\n" + "x" * 3_000_000,
            session_id=big,
            tool_use_id=big,
            subagent_type=subagent_type,
            model=big,
            effort=big,
        )
        out, rec = decide(raw, env, far())
        assert (out is not None) == (subagent_type == "general-purpose")
        assert rec is not None
        assert len((json.dumps(rec) + "\n").encode()) < MAX_LINE
        assert rec["requested_model"] == FACE * 5
        assert rec["error"] is None


def test_secrets_never_in_record(
    payload: Payload, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    exc = requests.ConnectionError("MARKER-123 sk-SECRET")
    monkeypatch.setattr(router, "jev_asker", asker(fail(exc)))
    out, rec = decide(payload(prompt="MARKER-123"), env | {"TYPESAFE_API_KEY": "sk-SECRET"}, far())
    assert out is None
    assert rec is not None
    assert rec["error"] == "ConnectionError"
    text = json.dumps(rec)
    assert "MARKER-123" not in text
    assert "sk-SECRET" not in text


def test_error_text_allowlist() -> None:
    assert error_text(PolicyError("x" * 500)) == ("PolicyError: " + "x" * 500)[:200]
    assert error_text(JevError("status 429")) == "JevError: status 429"
    assert error_text(KeyUnavailable("key missing")) == "KeyUnavailable: key missing"
    assert error_text(ValueError("prompt text")) == "ValueError"
    assert error_text(requests.HTTPError("body text")) == "HTTPError"


def test_non_finite_json_numbers_fail_open(payload: Payload, env: dict[str, str]) -> None:
    for number in ("NaN", "Infinity", "-Infinity", "1e309"):
        text = payload(subagent_type="Explore").decode()
        raw = text.replace('"prompt": "p"', f'"prompt": "p", "future": {number}').encode()
        assert raw != text.encode()
        out, rec = decide(raw, env, far())
        assert out is None, number
        assert rec is not None
        assert (rec["source"], rec["error"]) == ("error", "ValueError"), number


def test_lone_surrogate_prompt_routes(payload: Payload, env: dict[str, str]) -> None:
    out, rec = decide(payload(prompt="a\ud800b"), env | KEY, far())
    assert updated_input(out)["prompt"] == "a\ud800b"
    assert rec is not None
    assert rec["source"] == "jev"
    assert (
        rec["prompt_sha"] == hashlib.sha256("a\ud800b".encode("utf-8", "surrogatepass")).hexdigest()
    )
