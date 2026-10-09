"""Pure routing logic for subagent dispatches."""

import json
import math
import re
from pathlib import Path
from typing import TypedDict

MARKERS = ("escalate", "model-pinned")
EFFORT_NAMES = ("low", "medium", "high", "xhigh")
ALIASES = ("haiku", "sonnet", "opus", "fable")
POLICY_PATH = Path(__file__).with_name("policy.json")
_HEADER = re.compile(r"\s*(?:\[(?:escalate|model-pinned)\]\s*)+")
_MARKER = re.compile(r"\[(escalate|model-pinned)\]")
_MIN_LEVELS = 2
_MAX_NAME = 32


class PolicyError(ValueError):
    pass


class Level(TypedDict):
    name: str
    criteria: str


class Tier(TypedDict):
    name: str
    alias: str
    efforts: list[str]
    criteria: str


class Instructions(TypedDict):
    tier: str
    effort: str


class Policy(TypedDict):
    tiers: list[Tier]
    efforts: list[Level]
    instructions: Instructions
    rules: dict[str, list[str]]
    escalation: list[str]


def parse_header(prompt: str) -> tuple[frozenset[str], str]:
    first, _, rest = prompt.partition("\n")
    line = first.removesuffix("\r")
    if not _HEADER.fullmatch(line):
        return frozenset(), prompt
    return frozenset(_MARKER.findall(line)), rest


def _text(value: object, key: str) -> str:
    if not isinstance(value, str) or not value:
        raise PolicyError(f"{key}: must be a non-empty string")
    return value


def _strings(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    strings = [v for v in value if isinstance(v, str)]
    return strings if len(strings) == len(value) else None


def _named_levels(
    policy: dict[object, object], key: str
) -> list[tuple[Level, dict[object, object]]]:
    levels = policy.get(key)
    if not isinstance(levels, list) or len(levels) < _MIN_LEVELS:
        raise PolicyError(f"{key}: need a list of at least 2 entries")
    names: set[str] = set()
    checked: list[tuple[Level, dict[object, object]]] = []
    for entry in levels:
        if not isinstance(entry, dict):
            raise PolicyError(f"{key}: entry is not an object")
        name = entry.get("name")
        if not isinstance(name, str) or not 1 <= len(name) <= _MAX_NAME or name in names:
            raise PolicyError(f"{key}.name: must be unique, 1-32 characters")
        names.add(name)
        criteria = _text(entry.get("criteria"), f"{key}.{name}.criteria")
        checked.append(({"name": name, "criteria": criteria}, entry))
    return checked


def _pair(value: object, key: str, tiers: set[str], efforts: set[str]) -> list[str]:
    match value:
        case [str() as tier, str() as effort] if tier in tiers and effort in efforts:
            return [tier, effort]
        case _:
            raise PolicyError(f"{key}: must be [known tier, known effort]")


def validate_policy(raw: object) -> Policy:
    if not isinstance(raw, dict):
        raise PolicyError("policy: must be an object")
    efforts = [lvl for lvl, _ in _named_levels(raw, "efforts")]
    effort_names = {e["name"] for e in efforts}
    if not effort_names <= set(EFFORT_NAMES):
        raise PolicyError("efforts.name: must be low, medium, high or xhigh")
    tiers: list[Tier] = []
    for lvl, entry in _named_levels(raw, "tiers"):
        name = lvl["name"]
        alias = entry.get("alias")
        if not isinstance(alias, str) or alias not in ALIASES:
            raise PolicyError(f"tiers.{name}.alias: must be haiku, sonnet, opus or fable")
        supported = _strings(entry.get("efforts"))
        if supported is None or not set(supported) <= effort_names:
            raise PolicyError(f"tiers.{name}.efforts: must list known efforts")
        tiers.append(
            {"name": name, "alias": alias, "efforts": supported, "criteria": lvl["criteria"]}
        )
    given = raw.get("instructions")
    source = given if isinstance(given, dict) else {}
    instructions: Instructions = {
        "tier": _text(source.get("tier"), "instructions.tier"),
        "effort": _text(source.get("effort"), "instructions.effort"),
    }
    tier_names = {t["name"] for t in tiers}
    rules = raw.get("rules", {})
    if not isinstance(rules, dict):
        raise PolicyError("rules: must be an object")
    return {
        "tiers": tiers,
        "efforts": efforts,
        "instructions": instructions,
        "rules": {
            str(name): _pair(value, f"rules.{name}"[:100], tier_names, effort_names)
            for name, value in rules.items()
        },
        "escalation": _pair(raw.get("escalation"), "escalation", tier_names, effort_names),
    }


def load_policy(path: Path) -> Policy:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise PolicyError("policy file unreadable or not JSON") from e
    return validate_policy(raw)


def level(score: float, count: int) -> int:
    if not math.isfinite(score):
        raise ValueError("non-finite score")
    return math.floor(min(max(score, 0), count - 1) + 0.5)


def clamp_effort(effort: str, supported: list[str], order: list[str]) -> str | None:
    if not supported:
        return None
    want = order.index(effort)
    return min(supported, key=lambda e: (abs(order.index(e) - want), -order.index(e)))
