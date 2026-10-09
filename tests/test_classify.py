import copy
import math
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from router import (
    BUILT_IN_AGENTS,
    AskJev,
    Dispatch,
    Policy,
    Scores,
    classify,
    pin_source,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def agent(directory: Path, filename: str, name: str, model: str) -> None:
    write(directory / filename, f"---\nname: {name}\nmodel: {model}\n---\nbody")


def jev(tier: float, effort: float, states: list[str] | None = None) -> AskJev:
    def ask(state: str, policy: Policy) -> Scores:
        if states is not None:
            states.append(state)
        return Scores(tier, effort, 1.0, 1.0)

    return ask


def never(state: str, policy: Policy) -> Scores:
    pytest.fail("Jev must not be called")


def dispatch(home: Path, subagent_type: str = "general-purpose", prompt: str = "x") -> Dispatch:
    return Dispatch(subagent_type, "", prompt, None, home)


def test_parent_directory_definition_pins(tmp_path: Path) -> None:
    agent(tmp_path / "repo/.claude/agents", "x.md", "reviewer", "opus")
    (tmp_path / "repo/sub/dir").mkdir(parents=True)
    assert pin_source("reviewer", tmp_path / "repo/sub/dir", tmp_path / "home") == "pinned"


def test_nearer_inherit_does_not_hide_farther_concrete(tmp_path: Path) -> None:
    agent(tmp_path / "a/.claude/agents", "r.md", "reviewer", "opus")
    agent(tmp_path / "a/b/.claude/agents", "r.md", "reviewer", "inherit")
    assert pin_source("reviewer", tmp_path / "a/b", tmp_path / "home") == "pinned"


def test_match_uses_frontmatter_name_not_filename(tmp_path: Path) -> None:
    agent(tmp_path / "home/.claude/agents", "file-name.md", "real-name", "haiku")
    assert pin_source("real-name", None, tmp_path / "home") == "pinned"
    assert pin_source("file-name", None, tmp_path / "home") == "unresolved"


def test_unknown_custom_agent_is_unresolved(tmp_path: Path) -> None:
    assert pin_source("from-agents-json", tmp_path, tmp_path) == "unresolved"


@pytest.mark.parametrize("name", sorted(BUILT_IN_AGENTS))
def test_built_ins_without_definitions_route(tmp_path: Path, name: str) -> None:
    assert pin_source(name, tmp_path, tmp_path) is None


def test_inherit_or_missing_model_routes(tmp_path: Path) -> None:
    agents = tmp_path / ".claude/agents"
    agent(agents, "a.md", "inh", "inherit")
    write(agents / "b.md", "---\nname: bare\n---\nbody")
    assert pin_source("inh", None, tmp_path) is None
    assert pin_source("bare", None, tmp_path) is None


def test_yaml_scalar_models(tmp_path: Path) -> None:
    agents = tmp_path / ".claude/agents"
    write(agents / "a.md", "---\nname: c1  # note\nmodel: opus # pinned on purpose\n---\n")
    write(agents / "b.md", "---\nname: \"c2\"\nmodel: 'inherit'  # routed\n---\n")
    write(agents / "c.md", "---\nname: c3\nmodel: 3\n---\n")
    assert pin_source("c1", None, tmp_path) == "pinned"
    assert pin_source("c2", None, tmp_path) is None
    assert pin_source("c3", None, tmp_path) is None  # a non-string model is not concrete


def test_fork_pinned_plugin_agent_unresolved(tmp_path: Path, policy: Policy) -> None:
    assert pin_source("fork", tmp_path, tmp_path) == "pinned"
    assert pin_source("plug:agent", tmp_path, tmp_path) == "unresolved"
    route = classify(dispatch(tmp_path, "plug:agent"), policy, never)
    assert (route.source, route.alias) == ("unresolved", None)


def test_escalate_on_pinned_sets_flag(tmp_path: Path, policy: Policy) -> None:
    agent(tmp_path / ".claude/agents", "f.md", "fixed", "opus")
    for subagent_type, prompt in (
        ("general-purpose", "[model-pinned][escalate]\nb"),
        ("fixed", "[escalate]\nb"),
    ):
        route = classify(dispatch(tmp_path, subagent_type, prompt), policy, never)
        assert (route.source, route.flags, route.prompt) == ("pinned", ("escalate_ignored",), "b")
        assert route.alias is None


def test_escalate_beats_rules(tmp_path: Path, policy: Policy) -> None:
    route = classify(dispatch(tmp_path, "Explore", "[escalate]\nfind it"), policy, never)
    assert (route.source, route.tier, route.alias, route.effort, route.prompt) == (
        "escalate",
        "strong",
        "opus",
        "xhigh",
        "find it",
    )


def test_rules(tmp_path: Path, policy: Policy) -> None:
    route = classify(dispatch(tmp_path, "Explore"), policy, never)
    assert (route.source, route.alias, route.effort) == ("rules", "haiku", "low")
    assert route.tier_score is None


def test_jev_scores_and_state(tmp_path: Path, policy: Policy) -> None:
    states: list[str] = []
    route = classify(
        Dispatch("general-purpose", "desc", "body", None, tmp_path), policy, jev(1.5, 0.49, states)
    )
    assert (route.source, route.tier, route.alias, route.effort) == ("jev", "strong", "opus", "low")
    assert (route.tier_score, route.effort_score) == (1.5, 0.49)
    assert states == ["subagent_type: general-purpose\ndescription: desc\n\nbody"]


def test_jev_route_carries_scores_and_confidences(tmp_path: Path, policy: Policy) -> None:
    def ask(state: str, p: Policy) -> Scores:
        return Scores(2.0, 1.0, 0.9, 0.6)

    r = classify(Dispatch("general-purpose", "d", "p", None, tmp_path), policy, ask)
    assert (r.source, r.tier, r.alias, r.effort) == ("jev", "strong", "opus", "medium")
    assert (r.tier_confidence, r.effort_confidence) == (0.9, 0.6)


def test_jev_effort_clamped_to_tier(tmp_path: Path, policy: Policy) -> None:
    narrowed = copy.deepcopy(policy)
    narrowed["tiers"][0]["efforts"] = ["low", "high"]
    assert classify(dispatch(tmp_path), narrowed, jev(0, 1)).effort == "high"


def test_jev_non_finite_score_raises(tmp_path: Path, policy: Policy) -> None:
    with pytest.raises(ValueError, match="non-finite"):
        classify(dispatch(tmp_path), policy, jev(math.nan, 1))


def test_malformed_agent_files_are_skipped(tmp_path: Path) -> None:
    agents = tmp_path / ".claude/agents"
    agents.mkdir(parents=True)
    (agents / "bin.md").write_bytes(b"\xff\xfe\x00garbage")
    write(agents / "nofm.md", "no frontmatter")
    write(agents / "open.md", "---\nname: open\nmodel: opus\n")  # never closed
    write(agents / "bad.md", "---\nname: [unclosed\n---\n")  # invalid YAML
    write(agents / "list.md", "---\n- a\n- b\n---\n")  # not a mapping
    agent(agents, "z.md", "good", "opus")
    assert pin_source("good", None, tmp_path) == "pinned"
    assert pin_source("open", None, tmp_path) == "unresolved"


def test_huge_agent_body_keeps_pin(tmp_path: Path) -> None:
    path = tmp_path / ".claude/agents/big.md"
    write(path, "---\nname: big\nmodel: opus\n---\n" + "x" * 1_100_000)
    assert pin_source("big", None, tmp_path) == "pinned"


def test_unreadable_agents_dir_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent(tmp_path / "home/.claude/agents", "r.md", "reviewer", "opus")
    blocked = tmp_path / "a/b/.claude/agents"
    blocked.mkdir(parents=True)
    rglob = Path.rglob

    def guarded(self: Path, pattern: str) -> Iterator[Path]:
        if self == blocked:
            raise PermissionError(self)
        return rglob(self, pattern)

    monkeypatch.setattr(Path, "rglob", guarded)
    assert pin_source("reviewer", tmp_path / "a/b", tmp_path / "home") == "pinned"


@pytest.mark.parametrize("cwd", [None, Path("relative/dir"), Path("gone/x/y")])
def test_missing_or_bogus_cwd_does_not_walk(tmp_path: Path, cwd: Path | None) -> None:
    if cwd is not None and cwd == Path("gone/x/y"):
        cwd = tmp_path / cwd  # absolute but never created
    agent(tmp_path / ".claude/agents", "r.md", "reviewer", "opus")
    home = tmp_path / "home"
    assert pin_source("general-purpose", cwd, home) is None
    assert pin_source("reviewer", cwd, home) == "unresolved"
    agent(home / ".claude/agents", "r.md", "reviewer", "opus")
    assert pin_source("reviewer", cwd, home) == "pinned"
