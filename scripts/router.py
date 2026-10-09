"""Pure routing logic for subagent dispatches."""

import json
import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast

import yaml

MARKERS = ("escalate", "model-pinned")
EFFORT_NAMES = ("low", "medium", "high", "xhigh")
ALIASES = ("haiku", "sonnet", "opus", "fable")
BUILT_IN_AGENTS = frozenset(
    {"general-purpose", "Explore", "Plan", "statusline-setup", "claude-code-guide"}
)
ROUTED = frozenset({"escalate", "rules", "jev"})
MAX_FRONTMATTER = 1_000_000  # bytes read per agent file; the body is never needed
POLICY_PATH = Path(__file__).with_name("policy.json")
JEV_URL = "https://api.typesafe.ai/v1/systemone"
_HEADER = re.compile(r"\s*(?:\[(?:escalate|model-pinned)\]\s*)+")
_MARKER = re.compile(r"\[(escalate|model-pinned)\]")
_MIN_LEVELS = 2
_MAX_NAME = 32


class PolicyError(ValueError):
    pass


class JevError(Exception):
    """A failed Jev call; the message never holds request or response content."""


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


@dataclass(frozen=True)
class Dispatch:
    subagent_type: str
    description: str
    prompt: str
    cwd: Path | None
    home: Path


@dataclass(frozen=True)
class Scores:
    tier: float
    effort: float
    tier_confidence: float
    effort_confidence: float


@dataclass(frozen=True)
class Route:
    source: str  # pinned | unresolved | escalate | rules | jev
    prompt: str  # without the control header
    tier: str | None = None
    alias: str | None = None
    effort: str | None = None
    tier_score: float | None = None
    effort_score: float | None = None
    tier_confidence: float | None = None
    effort_confidence: float | None = None
    flags: tuple[str, ...] = ()


type AskJev = Callable[[str, Policy], Scores]


def read_frontmatter(path: Path) -> dict[str, object] | None:
    try:
        with path.open("rb") as f:
            head = f.read(MAX_FRONTMATTER)
        if len(head) == MAX_FRONTMATTER:
            head = head[: head.rfind(b"\n") + 1]  # never split a UTF-8 character
        lines = head.decode("utf-8").splitlines()
        if not lines or lines[0].strip() != "---":
            return None
        end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
        if end is None:
            return None
        data = yaml.safe_load("\n".join(lines[1:end]))
    except OSError, UnicodeDecodeError, yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def agent_models(name: str, cwd: Path | None, home: Path) -> list[object]:
    walk: list[Path] = (
        [cwd, *cwd.parents] if cwd is not None and cwd.is_absolute() and cwd.is_dir() else []
    )
    roots = dict.fromkeys([*(d / ".claude/agents" for d in walk), home / ".claude/agents"])
    models: list[object] = []
    for root in roots:
        try:
            paths = sorted(root.rglob("*.md"))
        except OSError:
            continue  # one unreadable directory must not hide the others
        for path in paths:
            meta = read_frontmatter(path)
            if meta is not None and meta.get("name") == name:
                models.append(meta.get("model"))
    return models


def pin_source(subagent_type: str, cwd: Path | None, home: Path) -> str | None:
    if ":" in subagent_type:
        return "unresolved"
    if subagent_type == "fork":
        return "pinned"
    models = agent_models(subagent_type, cwd, home)
    if any(isinstance(m, str) and m and m != "inherit" for m in models):
        return "pinned"
    if not models and subagent_type not in BUILT_IN_AGENTS:
        return "unresolved"
    return None


def classify(dispatch: Dispatch, policy: Policy, ask_jev: AskJev) -> Route:
    markers, body = parse_header(dispatch.prompt)
    if "model-pinned" in markers:
        pin = "pinned"
    else:
        pin = pin_source(dispatch.subagent_type, dispatch.cwd, dispatch.home)
    if pin:
        flags = ("escalate_ignored",) if pin == "pinned" and "escalate" in markers else ()
        return Route(pin, body, flags=flags)
    tiers, efforts = policy["tiers"], [e["name"] for e in policy["efforts"]]
    scores = None
    if "escalate" in markers:
        source, (tier_name, effort) = "escalate", policy["escalation"]
    elif dispatch.subagent_type in policy["rules"]:
        source, (tier_name, effort) = "rules", policy["rules"][dispatch.subagent_type]
    else:
        state = (
            f"subagent_type: {dispatch.subagent_type}\n"
            f"description: {dispatch.description}\n\n{body}"
        )
        scores = ask_jev(state, policy)
        source = "jev"
        tier_name = tiers[level(scores.tier, len(tiers))]["name"]
        effort = efforts[level(scores.effort, len(efforts))]
    tier = next(t for t in tiers if t["name"] == tier_name)
    return Route(
        source,
        body,
        tier=tier_name,
        alias=tier["alias"],
        effort=clamp_effort(effort, tier["efforts"], efforts),
        tier_score=None if scores is None else scores.tier,
        effort_score=None if scores is None else scores.effort,
        tier_confidence=None if scores is None else scores.tier_confidence,
        effort_confidence=None if scores is None else scores.effort_confidence,
    )


def _number(answer: dict[object, object], key: str, low: float, high: float) -> float:
    value = answer.get(key.rpartition(".")[2])
    try:
        number = (
            float(value)
            if isinstance(value, int | float) and not isinstance(value, bool)
            else math.nan
        )
    except OverflowError:  # a JSON integer beyond float range
        number = math.nan
    if not (math.isfinite(number) and low <= number <= high):
        raise JevError(f"{key} invalid")
    return number


def jev_asker(api_key: str, timeout: tuple[float, float] = (1.0, 2.0)) -> AskJev:
    import requests  # noqa: PLC0415 - only the Jev path pays the import

    def ask(state: str, policy: Policy) -> Scores:
        questions = {
            key: {
                "type": "score",
                "instructions": policy["instructions"][key],
                "criteria": [f"{x['name']}: {x['criteria']}" for x in levels],
            }
            for key, levels in (("tier", policy["tiers"]), ("effort", policy["efforts"]))
        }
        # One attempt: a retry after 429 or 529 would spend the hook's deadline.
        response = requests.post(
            JEV_URL,
            json={"state": state, "model": "jev-latest", "questions": questions},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            allow_redirects=False,  # a redirect would resend the prompt to another host
        )
        if response.status_code != 200:  # noqa: PLR2004 - HTTP OK
            raise JevError(f"status {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise JevError("body invalid") from None  # the decode error holds the body
        if not isinstance(body, dict):
            raise JevError("body invalid")
        given = body.get("answers")
        found: dict[object, object] = given if isinstance(given, dict) else {}
        scores: list[float] = []
        for key in ("tier", "effort"):
            answer = found.get(key)
            if not isinstance(answer, dict):
                raise JevError(f"answers.{key} invalid")
            scores += [
                _number(answer, f"answers.{key}.score", -math.inf, math.inf),
                _number(answer, f"answers.{key}.confidence", 0, 1),
            ]
        tier, tier_confidence, effort, effort_confidence = scores
        return Scores(tier, effort, tier_confidence, effort_confidence)

    return ask


def _confidence(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def stats(path: Path) -> dict[str, object]:
    total = malformed = 0
    counts: dict[str, Counter[str]] = {
        "tier": Counter(),
        "effort": Counter(),
        "source": Counter(),
    }
    confidence_tier_total = 0.0
    confidence_effort_total = 0.0
    confidence_n = 0
    override_count = 0
    override_n = 0
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    for line in lines:
        try:
            raw: object = cast("object", json.loads(line))
        except ValueError:
            raw = None
        if not isinstance(raw, dict):
            malformed += 1
            continue
        record = cast("dict[str, object]", raw)
        total += 1
        for key, counter in counts.items():
            value = record.get(key)
            counter["none" if value is None else str(value)] += 1

        source = record.get("source")
        if source == "jev":
            tier_confidence = _confidence(record.get("tier_confidence"))
            effort_confidence = _confidence(record.get("effort_confidence"))
            if tier_confidence is not None and effort_confidence is not None:
                confidence_tier_total += tier_confidence
                confidence_effort_total += effort_confidence
                confidence_n += 1

        if (
            isinstance(source, str)
            and source in ROUTED
            and "requested_model" in record
            and "requested_effort" in record
        ):
            override_n += 1
            requested_model = record["requested_model"]
            requested_effort = record["requested_effort"]
            if (requested_model is not None and requested_model != record.get("alias")) or (
                requested_effort is not None and requested_effort != record.get("effort")
            ):
                override_count += 1

    sources = counts["source"]
    confidence: dict[str, float | int | None] = {
        "tier_mean": confidence_tier_total / confidence_n if confidence_n else None,
        "effort_mean": confidence_effort_total / confidence_n if confidence_n else None,
        "n": confidence_n,
    }
    return {
        "total": total,
        "malformed": malformed,
        **{key: dict(counter) for key, counter in counts.items()},
        "escalation_rate": sources["escalate"] / total if total else 0.0,
        "error_rate": sources["error"] / total if total else 0.0,
        "confidence": confidence,
        "overrides": {"count": override_count, "n": override_n},
    }
