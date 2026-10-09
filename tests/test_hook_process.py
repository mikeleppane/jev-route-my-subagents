import io
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import pytest
import route_hook
from route_hook import OFF_VAR, append_log, run_hook

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "route_hook.py"
ESCALATE = "[escalate]\nb"

type Payload = Callable[..., bytes]


def records(env: dict[str, str]) -> list[dict[str, object]]:
    data = env.get("CLAUDE_PLUGIN_DATA")
    path = Path(data, "decisions.jsonl") if data else None
    if path is None or path.is_symlink() or not path.is_file():
        return []
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def run(
    args: list[str], stdin: bytes, env: dict[str, str], script: Path = SCRIPT
) -> tuple[int, str, list[dict[str, object]]]:
    done = subprocess.run(
        [sys.executable, str(script), *args],
        input=stdin,
        capture_output=True,
        env={"PATH": os.environ["PATH"], **env},
        timeout=20,
        check=False,
    )
    return done.returncode, done.stdout.decode(), records(env)


def routed(stdout: str) -> dict[str, object]:
    found = json.loads(stdout)["hookSpecificOutput"]["updatedInput"]
    assert isinstance(found, dict)
    return found


def test_routed_dispatch_writes_output_and_one_record(
    payload: Payload, env: dict[str, str]
) -> None:
    code, out, log = run([], payload(prompt=ESCALATE), env)
    assert code == 0
    assert len(out.splitlines()) == 1
    updated = routed(out)
    assert (updated["model"], updated["effort"], updated["prompt"]) == ("opus", "xhigh", "b")
    assert [(r["source"], r["alias"], r["effort"]) for r in log] == [("escalate", "opus", "xhigh")]


def test_garbage_stdin_exits_zero(env: dict[str, str]) -> None:
    code, out, log = run([], b"not json", env)
    assert (code, out) == (0, "")
    assert [(r["source"], r["error"]) for r in log] == [
        ("error", "PayloadError: payload is not UTF-8 JSON")
    ]


def test_missing_data_dir_is_created(payload: Payload, env: dict[str, str], data_dir: Path) -> None:
    assert not data_dir.exists()
    code, _, log = run([], payload(prompt=ESCALATE), env)
    assert code == 0
    assert data_dir.stat().st_mode & 0o777 == 0o700
    assert len(log) == 1


def test_existing_permissive_log_becomes_600(
    payload: Payload, env: dict[str, str], data_dir: Path
) -> None:
    data_dir.mkdir()
    log_path = data_dir / "decisions.jsonl"
    log_path.touch()
    log_path.chmod(0o644)
    code, _, log = run([], payload(prompt=ESCALATE), env)
    assert code == 0
    assert log_path.stat().st_mode & 0o777 == 0o600
    assert len(log) == 1


def test_symlinked_log_refused_route_still_emitted(
    payload: Payload, env: dict[str, str], data_dir: Path, tmp_path: Path
) -> None:
    data_dir.mkdir()
    target = tmp_path / "other.txt"
    target.write_text("keep\n", encoding="utf-8")
    (data_dir / "decisions.jsonl").symlink_to(target)
    code, out, _ = run([], payload(prompt=ESCALATE), env)
    assert code == 0
    assert routed(out)["model"] == "opus"
    assert target.read_text(encoding="utf-8") == "keep\n"


def test_log_failure_still_routes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: Payload,
    env: dict[str, str],
) -> None:
    calls: list[Path] = []

    def refuse(path: Path, record: dict[str, object]) -> NoReturn:
        calls.append(path)
        raise PermissionError(path)

    def exit_(code: int) -> NoReturn:
        raise SystemExit(code)

    for name, value in env.items():
        monkeypatch.setenv(name, value)
    for name in (OFF_VAR, "TYPESAFE_API_KEY", "CLAUDE_PLUGIN_OPTION_TYPESAFE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(route_hook, "append_log", refuse)
    monkeypatch.setattr(os, "_exit", exit_)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(payload(prompt=ESCALATE))))
    with pytest.raises(SystemExit, match=r"^0$"):
        run_hook()
    assert calls == [Path(env["CLAUDE_PLUGIN_DATA"], "decisions.jsonl")]
    assert routed(capsys.readouterr().out)["model"] == "opus"


def test_no_data_dir_env_means_no_log(
    payload: Payload, env: dict[str, str], data_dir: Path
) -> None:
    code, out, _ = run([], payload(prompt=ESCALATE), {"HOME": env["HOME"]})
    assert code == 0
    assert routed(out)["model"] == "opus"
    assert not data_dir.exists()


def test_off_switch_writes_nothing(payload: Payload, env: dict[str, str], data_dir: Path) -> None:
    code, out, _ = run([], payload(prompt=ESCALATE), env | {OFF_VAR: "off"})
    assert (code, out) == (0, "")
    assert not data_dir.exists()


def test_import_failure_fails_open(tmp_path: Path, payload: Payload, env: dict[str, str]) -> None:
    lone = tmp_path / "lone"
    lone.mkdir()
    shutil.copy(SCRIPT, lone)
    (lone / "router.py").write_text('raise RuntimeError("broken install")\n', encoding="utf-8")
    code, out, log = run([], payload(prompt=ESCALATE), env, lone / "route_hook.py")
    assert (code, out) == (0, "")
    assert [(r["source"], r["error"], r["prompt_sha"]) for r in log] == [
        ("error", "RuntimeError", None)
    ]


def test_concurrent_appends(payload: Payload, env: dict[str, str]) -> None:
    stdin = payload(prompt=ESCALATE)
    procs = [
        subprocess.Popen(
            [sys.executable, str(SCRIPT)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env={"PATH": os.environ["PATH"], **env},
        )
        for _ in range(20)
    ]
    # Every child blocks reading stdin, so all 20 are alive before any of them can append.
    assert [proc.poll() for proc in procs] == [None] * 20
    for proc in procs:
        assert proc.stdin is not None
        proc.stdin.write(stdin)
        proc.stdin.close()
    outs = [proc.communicate(timeout=20)[0] for proc in procs]
    assert [proc.returncode for proc in procs] == [0] * 20
    assert all(routed(out.decode())["model"] == "opus" for out in outs)
    raw = Path(env["CLAUDE_PLUGIN_DATA"], "decisions.jsonl").read_bytes()
    assert raw.count(b"\n") == 20
    assert [json.loads(line)["source"] for line in raw.splitlines()] == ["escalate"] * 20


def test_cli_modes(env: dict[str, str], tmp_path: Path) -> None:
    assert run(["--warm"], b"", env)[:2] == (0, "")
    log_path = tmp_path / "decisions.jsonl"
    log_path.write_text('{"source": "jev"}\n', encoding="utf-8")
    code, out, _ = run(["--stats", str(log_path)], b"", env)
    assert code == 0
    assert "overrides" in json.loads(out)
    bogus = subprocess.run(
        [sys.executable, str(SCRIPT), "--bogus"],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], **env},
        timeout=20,
        check=False,
    )
    assert (bogus.returncode, bogus.stdout) == (1, "")
    assert "usage" in bogus.stderr
    assert run([], b"", env)[:2] == (0, "")


def test_append_log_rejects_long_line(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    with pytest.raises(ValueError, match="too long"):
        append_log(path, {"error": "x" * route_hook.MAX_LINE})
    assert not path.exists()


def test_pep723_pins_equal_pyproject() -> None:
    block = re.search(
        r"^# /// script$\s(?P<content>(^#(| .*)$\s)+)^# ///$",
        SCRIPT.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert block is not None
    header = tomllib.loads("".join(line[2:] for line in block["content"].splitlines(keepends=True)))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert header["dependencies"] == project["dependencies"]
    assert header["requires-python"] == project["requires-python"]
