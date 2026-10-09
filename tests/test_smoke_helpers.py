import json
import re
import subprocess
from typing import TYPE_CHECKING

import pytest
import smoke
from smoke import CONTEXT, CONTEXT_MARK, find_records, jev_outage, read_key_file, subagent_meta

if TYPE_CHECKING:
    from pathlib import Path

KEY = "sk-test-7f3a9c"
LISTED_OFF = json.dumps(
    [{"id": f"{smoke.PLUGIN}@market", "scope": "user", "enabled": False, "projectEnabled": False}]
)
LISTED_PROJECT = json.dumps(
    [{"id": f"{smoke.PLUGIN}@market", "scope": "user", "enabled": False, "projectEnabled": True}]
)
ROUTES = '{"hookSpecificOutput": {"updatedInput": {"model": "haiku"}}}'
type Hook = subprocess.CompletedProcess[str] | Exception | str | None


def completed(
    stdout: str = "", returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def no_subprocess(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
    raise AssertionError("no child process expected")


@pytest.fixture(autouse=True)
def no_managed_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the managed settings at absent paths: tests never read this machine's settings."""
    monkeypatch.setattr(smoke, "MANAGED_FILE", tmp_path / "absent-managed.json")
    monkeypatch.setattr(smoke, "MANAGED_DIR", tmp_path / "absent-managed.d")


def agent_hook_config(command: str) -> str:
    entry = {"matcher": "Agent", "hooks": [{"type": "command", "command": command}]}
    return json.dumps({"hooks": {"PreToolUse": [entry]}})


def test_read_key_file_mode(tmp_path: Path) -> None:
    path = tmp_path / "typesafe.key"
    path.write_text(KEY + "\n", encoding="utf-8")
    path.chmod(0o600)
    assert read_key_file(path) == KEY

    path.chmod(0o644)
    with pytest.raises(SystemExit, match=re.escape(str(path))) as caught:
        read_key_file(path)
    assert KEY not in str(caught.value)

    path.write_text("", encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(SystemExit, match="empty"):
        read_key_file(path)

    with pytest.raises(SystemExit, match=re.escape(str(tmp_path / "absent.key"))):
        read_key_file(tmp_path / "absent.key")


def test_jev_outage_only_transient() -> None:
    for error in (
        "JevError: status 429",
        "JevError: status 503",
        "ConnectTimeout",
        "ReadTimeout",
        "ConnectionError",
        "Deadline: routing exceeded 3.0 s",
    ):
        assert jev_outage(error), error
    for error in (
        "JevError: status 401",
        "JevError: answers.tier.score invalid",
        None,
        "KeyUnavailable: key missing",
    ):
        assert not jev_outage(error), error


def test_find_records_globs_plugin_data(tmp_path: Path) -> None:
    first = tmp_path / "inline-a" / "decisions.jsonl"
    second = tmp_path / "inline-b" / "decisions.jsonl"
    for path in (first, second):
        path.parent.mkdir()
    first.write_text(
        json.dumps({"session_id": "s1", "source": "rules"})
        + "\n{broken\n"
        + json.dumps({"session_id": "s2", "source": "jev"})
        + "\n",
        encoding="utf-8",
    )
    second.write_text(json.dumps({"session_id": "s1", "source": "escalate"}) + "\n")

    assert find_records("s1", tmp_path) == [
        (first, {"session_id": "s1", "source": "rules"}),
        (second, {"session_id": "s1", "source": "escalate"}),
    ]
    assert find_records("s3", tmp_path) == []


def test_subagent_meta_by_tool_use_id(tmp_path: Path) -> None:
    sub = tmp_path / "-some-project/sid-1/subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a.meta.json").write_text(
        json.dumps({"toolUseId": "tu1", "model": "opus", "effort": "xhigh"})
    )
    (sub / "agent-b.meta.json").write_text("{broken")
    other = tmp_path / "-other/sid-2/subagents"
    other.mkdir(parents=True)
    (other / "agent-c.meta.json").write_text(json.dumps({"toolUseId": "tu9"}))

    meta = subagent_meta("sid-1", tmp_path)

    assert meta == {"tu1": {"toolUseId": "tu1", "model": "opus", "effort": "xhigh"}}
    assert subagent_meta(None, tmp_path) == {}


def test_context_mark_is_in_session_context() -> None:
    assert CONTEXT_MARK in CONTEXT.read_text(encoding="utf-8")


def double_route(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    hook: Hook,
    listing: str,
) -> str:
    """Run `check_double_route` on one settings file whose one Agent hook gives `hook`.

    `hook` None writes no settings file; "unreadable" makes the settings path a directory.
    """
    settings = tmp_path / "settings.json"
    if hook == "unreadable":
        settings.mkdir()
    elif hook is not None:
        entry = {"matcher": "Agent", "hooks": [{"type": "command", "command": "route-elsewhere"}]}
        settings.write_text(json.dumps({"hooks": {"PreToolUse": [entry]}}), encoding="utf-8")

    def fake(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if argv[0] == "claude":
            return completed(listing)
        if isinstance(hook, Exception):
            raise hook
        assert isinstance(hook, subprocess.CompletedProcess)
        return hook

    monkeypatch.setattr(smoke, "SETTINGS", (settings,))
    monkeypatch.setattr(smoke, "results", [])
    monkeypatch.setattr(smoke.subprocess, "run", fake)
    smoke.check_double_route()
    return smoke.results[-1]


@pytest.mark.parametrize(
    ("hook", "listing", "expected"),
    [
        (completed("{}"), LISTED_OFF, "PASS"),
        (completed(ROUTES), LISTED_OFF, "FAIL"),
        (completed(returncode=1), LISTED_OFF, "INCONCLUSIVE"),
        (completed(ROUTES, returncode=1), LISTED_OFF, "FAIL"),
        ("unreadable", "[]", "INCONCLUSIVE"),
        (FileNotFoundError("route-elsewhere"), "[]", "INCONCLUSIVE"),
        (subprocess.TimeoutExpired("sh", 10), LISTED_OFF, "INCONCLUSIVE"),
        (subprocess.TimeoutExpired("sh", 10, output=ROUTES.encode()), LISTED_OFF, "FAIL"),
        (None, "not json", "INCONCLUSIVE"),
        (None, LISTED_PROJECT, "FAIL"),
    ],
    ids=[
        "clean",
        "routes",
        "nonzero-exit",
        "nonzero-exit-routes",
        "unreadable-settings",
        "missing-executable",
        "timeout",
        "timeout-routes",
        "listing-not-json",
        "project-enabled",
    ],
)
def test_double_route_never_passes_unverified(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    hook: Hook,
    listing: str,
    expected: str,
) -> None:
    assert double_route(monkeypatch, tmp_path, hook, listing) == expected


@pytest.mark.parametrize("where", ["file", "drop-in"])
def test_double_route_managed_hook_routes_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, where: str
) -> None:
    drop_ins = tmp_path / "managed.d"
    drop_ins.mkdir()
    path = tmp_path / "managed.json" if where == "file" else drop_ins / "10-route.json"
    path.write_text(agent_hook_config("managed-router"), encoding="utf-8")
    ran: list[list[str]] = []

    def fake(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if argv[0] == "claude":
            return completed("[]")
        ran.append(argv)
        return completed(ROUTES)

    monkeypatch.setattr(smoke, "SETTINGS", ())
    monkeypatch.setattr(smoke, "MANAGED_FILE", tmp_path / "managed.json")
    monkeypatch.setattr(smoke, "MANAGED_DIR", drop_ins)
    monkeypatch.setattr(smoke, "results", [])
    monkeypatch.setattr(smoke.subprocess, "run", fake)

    smoke.check_double_route()

    assert smoke.results == ["FAIL"]
    assert ran == [["sh", "-c", "managed-router"]]


def other_plugin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    hook: subprocess.CompletedProcess[str],
    manifest: dict[str, object],
    enabled: bool = True,
) -> list[tuple[list[str], dict[str, str]]]:
    """Run `check_double_route` with one other plugin whose Agent hook gives `hook`.

    Returns each hook run's argv and env.
    """
    root = tmp_path / "other"
    (root / "hooks").mkdir(parents=True)
    (root / ".claude-plugin").mkdir()
    (root / "hooks" / "hooks.json").write_text(
        agent_hook_config("${CLAUDE_PLUGIN_ROOT}/route.sh"), encoding="utf-8"
    )
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    listing = json.dumps(
        [
            {"id": f"{smoke.PLUGIN}@market", "enabled": False, "projectEnabled": False},
            {
                "id": "other@market",
                "enabled": enabled,
                "projectEnabled": False,
                "installPath": str(root),
            },
        ]
    )
    ran: list[tuple[list[str], dict[str, str]]] = []

    def fake(argv: list[str], env: dict[str, str], **_: object) -> subprocess.CompletedProcess[str]:
        if argv[0] == "claude":
            return completed(listing)
        ran.append((argv, env))
        return hook

    for name in ("TYPESAFE_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_PLUGIN_OPTION_TOKEN"):
        monkeypatch.setenv(name, KEY)
    monkeypatch.setattr(smoke, "SETTINGS", ())
    monkeypatch.setattr(smoke, "results", [])
    monkeypatch.setattr(smoke.subprocess, "run", fake)
    smoke.check_double_route()
    return ran


@pytest.mark.parametrize(
    ("hook", "manifest", "expected"),
    [
        (completed(ROUTES), {"name": "other"}, "FAIL"),
        (completed("{}"), {"name": "other"}, "PASS"),
        (completed("{}"), {"name": "other", "hooks": "./more-hooks.json"}, "INCONCLUSIVE"),
        (completed(ROUTES), {"name": "other", "hooks": {"PreToolUse": []}}, "FAIL"),
    ],
    ids=["routes", "clean", "extra-hooks-path", "extra-hooks-and-routes"],
)
def test_double_route_runs_other_plugin_hooks_without_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    hook: subprocess.CompletedProcess[str],
    manifest: dict[str, object],
    expected: str,
) -> None:
    ran = other_plugin(monkeypatch, tmp_path, hook, manifest)

    assert smoke.results == [expected]
    root = str(tmp_path / "other")
    assert {tuple(argv) for argv, _ in ran} == {("sh", "-c", f"{root}/route.sh")}
    assert all(env["CLAUDE_PLUGIN_ROOT"] == root for _, env in ran)
    assert all(KEY not in env.values() for _, env in ran)


def test_double_route_skips_disabled_plugin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ran = other_plugin(monkeypatch, tmp_path, completed(ROUTES), {"name": "other"}, enabled=False)

    assert smoke.results == ["PASS"]
    assert ran == []


def test_full_run_stops_before_paid_checks_unless_double_route_passes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[list[str]] = []

    def fake(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[:3] == ["claude", "plugin", "list"]:
            return completed(LISTED_PROJECT)  # this plugin is enabled: double-route FAILs
        raise AssertionError(f"no other child process expected: {argv[:2]}")

    monkeypatch.setattr(smoke, "SETTINGS", ())
    monkeypatch.setattr(smoke, "results", [])
    monkeypatch.setattr(smoke.subprocess, "run", fake)

    assert smoke.main(["--skip-jev"]) == 1

    assert [argv[:3] for argv in calls] == [["claude", "plugin", "list"]]
    expected = ["FAIL", *["SKIP"] * len(smoke.PREFLIGHT_CHECKS + smoke.FULL_CHECKS)]
    assert smoke.results == expected
    assert "SKIP jev: double-route did not pass\n" in capsys.readouterr().out


def routed_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    records: list[dict[str, object]],
    metas: list[dict[str, object]],
    usage: list[str],
) -> None:
    """Fake one `claude -p` run of session `s` with these decision records and subagent metas."""
    data = tmp_path / "data" / "inline"
    data.mkdir(parents=True)
    lines = "".join(json.dumps({"session_id": "s", **r}) + "\n" for r in records)
    (data / "decisions.jsonl").write_text(lines, encoding="utf-8")
    sub = tmp_path / "projects" / "-repo" / "s" / "subagents"
    sub.mkdir(parents=True)
    for n, meta in enumerate(metas):
        (sub / f"agent-{n}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    out = json.dumps({"session_id": "s", "modelUsage": {model: {} for model in usage}})

    def fake(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        return completed(out)

    monkeypatch.setattr(smoke, "PLUGIN_DATA", tmp_path / "data")
    monkeypatch.setattr(smoke, "PROJECTS", tmp_path / "projects")
    monkeypatch.setattr(smoke, "results", [])
    monkeypatch.setattr(smoke.subprocess, "run", fake)


@pytest.mark.parametrize(("effort", "expected"), [("xhigh", "PASS"), ("low", "FAIL")])
def test_escalate_requires_opus_xhigh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, effort: str, expected: str
) -> None:
    records: list[dict[str, object]] = [
        {"tool_use_id": "tu1", "source": "rules", "alias": "haiku", "effort": "low"},
        {"tool_use_id": "tu2", "source": "escalate", "alias": "opus", "effort": effort},
    ]
    metas: list[dict[str, object]] = [
        {"toolUseId": "tu1", "model": "haiku", "effort": "low"},
        {"toolUseId": "tu2", "model": "opus", "effort": effort},
    ]
    routed_run(monkeypatch, tmp_path, records, metas, ["claude-haiku-4-5", "claude-opus-4-1"])

    xhigh_applied = smoke.check_escalate({})

    assert smoke.results == [expected]
    assert xhigh_applied is (expected == "PASS")


PINNED_SONNET: dict[str, object] = {
    "tool_use_id": "tu1",
    "source": "pinned",
    "requested_model": "sonnet",
    "requested_effort": None,
}


@pytest.mark.parametrize(
    ("records", "metas", "expected"),
    [
        ([PINNED_SONNET], [{"toolUseId": "tu1", "model": "sonnet"}], "PASS"),
        ([PINNED_SONNET], [], "FAIL"),
        ([PINNED_SONNET], [{"toolUseId": "tu1", "model": "sonnet", "effort": "low"}], "FAIL"),
        (
            [PINNED_SONNET, {**PINNED_SONNET, "tool_use_id": "tu2"}],
            [{"toolUseId": "tu1", "model": "sonnet"}],
            "FAIL",
        ),
        ([], [{"toolUseId": "tu1", "model": "sonnet"}], "FAIL"),
    ],
    ids=["applied", "missing-meta", "effort-set", "extra-dispatch", "no-record"],
)
def test_alias_sonnet_reads_record_and_meta(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    records: list[dict[str, object]],
    metas: list[dict[str, object]],
    expected: str,
) -> None:
    routed_run(monkeypatch, tmp_path, records, metas, ["claude-sonnet-4-5"])

    smoke.check_alias_sonnet({})

    assert smoke.results == [expected]


def test_child_failure_never_prints_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key_file = tmp_path / "typesafe.key"
    key_file.write_text(KEY + "\n", encoding="utf-8")
    key_file.chmod(0o600)

    def fake(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["claude", "plugin", "list"]:
            return completed("[]")  # double-route passes, so the paid checks run
        return completed(f"stdout {KEY}", 1, f"Error: bad key {KEY}")

    monkeypatch.setattr(smoke, "SETTINGS", ())
    monkeypatch.setattr(smoke, "PLUGIN_DATA", tmp_path / "data")
    monkeypatch.setattr(smoke, "PROJECTS", tmp_path / "projects")
    monkeypatch.setattr(smoke, "results", [])
    monkeypatch.setattr(smoke, "SECRETS", [])
    monkeypatch.setattr(smoke.subprocess, "run", fake)

    assert smoke.main(["--key-file", str(key_file)]) == 1
    smoke.report("FAIL", "probe", f"echo {KEY}")

    printed = capsys.readouterr()
    assert KEY not in printed.out + printed.err
    assert "FAIL fast: claude -p exit 1\n" in printed.out
    assert "FAIL probe: echo <redacted>\n" in printed.out


@pytest.mark.parametrize("argv", [["--bogus"], ["--key-file"]], ids=["unknown", "missing-value"])
def test_argument_errors_exit_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    monkeypatch.setattr(smoke.subprocess, "run", no_subprocess)

    with pytest.raises(SystemExit, match=r"^1$") as caught:
        smoke.main(argv)

    assert caught.value.code == 1
    assert "error:" in capsys.readouterr().err
