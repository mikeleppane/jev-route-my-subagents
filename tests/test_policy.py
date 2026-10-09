import copy
import json
import math
from typing import TYPE_CHECKING, Any

import pytest
from router import (
    EFFORT_NAMES,
    MARKERS,
    POLICY_PATH,
    Policy,
    PolicyError,
    clamp_effort,
    level,
    load_policy,
    parse_header,
    validate_policy,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def test_header_markers_and_stripping() -> None:
    assert parse_header("[escalate]\nbody") == (frozenset({"escalate"}), "body")
    assert parse_header("  [model-pinned]  \nb") == (frozenset({"model-pinned"}), "b")
    assert parse_header("[escalate][model-pinned]\nb")[0] == frozenset(MARKERS)
    assert parse_header("[escalate] [model-pinned]\nb")[0] == frozenset(MARKERS)
    assert parse_header("[escalate] [escalate]\nb")[0] == frozenset({"escalate"})
    assert parse_header("[escalate]\r\nb") == (frozenset({"escalate"}), "b")
    assert parse_header("[escalate]") == (frozenset({"escalate"}), "")
    assert parse_header("\n[escalate]\nx") == (frozenset(), "\n[escalate]\nx")


@pytest.mark.parametrize(
    "line", ["[escalate] fix the bug", "[ESCALATE]", "[pinned]", "[escalate", ""]
)
def test_non_headers_are_text(line: str) -> None:
    assert parse_header(f"{line}\nbody") == (frozenset(), f"{line}\nbody")


@pytest.mark.parametrize("prompt", ["fix [model-pinned] here", "Note:\n[escalate]\nx"])
def test_markers_outside_header_are_text(prompt: str) -> None:
    assert parse_header(prompt) == (frozenset(), prompt)


def test_header_crlf_keeps_body() -> None:
    assert parse_header("[escalate]\r\nx\r\ny") == (frozenset({"escalate"}), "x\r\ny")


@pytest.mark.parametrize(
    ("score", "count", "want"),
    [
        (-1.0, 3, 0),
        (0.0, 3, 0),
        (0.49, 3, 0),
        (0.5, 3, 1),
        (1.49, 3, 1),
        (1.5, 3, 2),
        (9.0, 3, 2),
        (2.0, 4, 2),
    ],
)
def test_level_rounds_half_up_and_clamps(score: float, count: int, want: int) -> None:
    assert level(score, count) == want


@pytest.mark.parametrize("score", [math.nan, math.inf, -math.inf])
def test_level_rejects_non_finite(score: float) -> None:
    with pytest.raises(ValueError, match="non-finite"):
        level(score, 3)


def test_clamp_effort_nearest_ties_up() -> None:
    order = list(EFFORT_NAMES)
    assert clamp_effort("medium", ["low", "high"], order) == "high"
    assert clamp_effort("xhigh", ["low", "medium"], order) == "medium"
    assert clamp_effort("xhigh", ["low", "high"], order) == "high"
    assert clamp_effort("low", ["medium", "xhigh"], order) == "medium"
    assert clamp_effort("low", [], order) is None


def test_shipped_policy_loads_and_uses_no_fable(policy: Policy) -> None:
    assert [t["alias"] for t in policy["tiers"]] == ["haiku", "sonnet", "opus"]
    assert policy["rules"] == {"Explore": ["fast", "low"]}
    assert policy["escalation"] == ["strong", "xhigh"]


def test_fable_alias_accepted_full_model_id_rejected(policy: Policy) -> None:
    fable = copy.deepcopy(policy)
    fable["tiers"][2]["alias"] = "fable"
    assert validate_policy(fable)["tiers"][2]["alias"] == "fable"
    full_id = copy.deepcopy(policy)
    full_id["tiers"][2]["alias"] = "claude-opus-5-5"
    with pytest.raises(PolicyError, match="alias"):
        validate_policy(full_id)


# Each edit breaks a deep copy of the shipped policy; Any lets one-line edits reach nested values.
_INVALID: list[tuple[str, Callable[[Any], object], str]] = [
    ("duplicate tier", lambda p: p["tiers"].append(dict(p["tiers"][0])), "tiers.name"),
    ("max effort", lambda p: p["efforts"].append({"name": "max", "criteria": "x"}), "efforts.name"),
    ("unknown tier effort", lambda p: p["tiers"][0]["efforts"].append("ultra"), "fast.efforts"),
    ("long tier name", lambda p: p["tiers"][0].update(name="x" * 33), "tiers.name"),
    ("rule unknown tier", lambda p: p["rules"].update(Plan=["huge", "low"]), "rules.Plan"),
    ("escalation unknown effort", lambda p: p.update(escalation=["strong", "max"]), "escalation"),
    ("missing instructions", lambda p: p.pop("instructions"), "instructions.tier"),
    ("one tier", lambda p: p.update(tiers=p["tiers"][:1]), "tiers"),
    ("not an object", lambda p: p.clear(), "efforts"),
]


@pytest.mark.parametrize(("edit", "match"), [c[1:] for c in _INVALID], ids=[c[0] for c in _INVALID])
def test_invalid_policies_are_rejected(
    policy: Policy, edit: Callable[[Any], object], match: str
) -> None:
    raw: Any = copy.deepcopy(policy)
    edit(raw)
    with pytest.raises(PolicyError, match=match):
        validate_policy(raw)


def test_non_object_policy_is_rejected() -> None:
    with pytest.raises(PolicyError, match="must be an object"):
        validate_policy(["not", "a", "policy"])


def test_load_policy_errors_are_policy_errors(tmp_path: Path) -> None:
    bad_utf8 = tmp_path / "bad.json"
    bad_utf8.write_bytes(b"\xff\xfe{}")
    not_json = tmp_path / "text.json"
    not_json.write_text("not json", encoding="utf-8")
    for path in (bad_utf8, tmp_path, not_json, tmp_path / "missing.json"):
        with pytest.raises(PolicyError, match="unreadable"):
            load_policy(path)
    bad_alias = tmp_path / "alias.json"
    raw = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    raw["tiers"][0]["alias"] = "gpt"
    bad_alias.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(PolicyError, match="alias"):
        load_policy(bad_alias)
