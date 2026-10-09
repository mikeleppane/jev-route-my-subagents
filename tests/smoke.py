"""Live smoke for the plugin, loaded from this working tree. Manual and paid.

Rerun after every Claude Code upgrade. `smoke.py --preflight` runs the checks that guard the
acceptance run: no other hook routes the smoke's dispatches, a hook killed by its timeout still
lets the dispatch run, and the plugin validates. `smoke.py` runs those and every routing check.
Prints one `PASS|FAIL|SKIP|INCONCLUSIVE <check>: <detail>` line per check and exits 0 only when
every check is PASS.

The TypeSafe key is read from `--key-file` (mode 600) and given only to the `claude` child as
`TYPESAFE_API_KEY`. It is never printed.
"""

import argparse
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import NoReturn, override

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "scripts" / "route_hook.py"
POLICY = REPO / "scripts" / "policy.json"
CONTEXT = REPO / "hooks" / "session-context.json"
CLAUDE_HOME = Path.home() / ".claude"
PROJECTS = CLAUDE_HOME / "projects"
PLUGIN_DATA = CLAUDE_HOME / "plugins" / "data"
KEY_FILE = Path.home() / ".config" / "jev-route-my-subagents" / "typesafe.key"
SETTINGS = (
    CLAUDE_HOME / "settings.json",
    Path(".claude") / "settings.json",
    Path(".claude") / "settings.local.json",
)
MANAGED_FILE = (
    Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    if sys.platform == "darwin"
    else Path("/etc/claude-code/managed-settings.json")
)
MANAGED_DIR = None if sys.platform == "darwin" else Path("/etc/claude-code/managed-settings.d")
PLUGIN = "jev-route-my-subagents"
CONTEXT_MARK = "Subagent routing (jev-route-my-subagents)"
OFF_VAR = "JEV_ROUTE_MY_SUBAGENTS"
KEY_VARS = ("TYPESAFE_API_KEY", "CLAUDE_PLUGIN_OPTION_TYPESAFE_API_KEY")
PLUGIN_FLAGS = ("--plugin-dir", str(REPO))
MAX_OVERHEAD_S = 1.5
OVERHEAD_RUNS = 5
HOOK_VARS = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TMPDIR", "LANG")
_TRANSIENT = re.compile(
    r"JevError: status (?:429|5\d\d)|ConnectTimeout|ReadTimeout|ConnectionError|Deadline: .*"
)
FAMILY = {"haiku": "claude-haiku", "sonnet": "claude-sonnet", "opus": "claude-opus"}
PRIMES = "Count the primes between 1000 and 1200. Reply with the count only."
OK = "Reply with the single word OK. Use no tools."

FAST = {"subagent_type": "Explore", "description": "smoke", "model": "opus", "prompt": OK}
ESCALATE = {
    "subagent_type": "general-purpose",
    "description": "smoke escalate",
    "prompt": "[escalate]\n" + PRIMES,
}
PINNED = {
    "subagent_type": "general-purpose",
    "description": "smoke",
    "model": "opus",
    "effort": "low",
    "prompt": "[model-pinned]\n" + PRIMES,
}
ALIAS_SONNET = {
    "subagent_type": "general-purpose",
    "description": "smoke",
    "model": "sonnet",
    "prompt": "[model-pinned]\n" + OK,
}
EFFORT_REMOVED = {
    "subagent_type": "Explore",
    "description": "smoke",
    "effort": "high",
    "prompt": OK,
}
JEV = {
    "subagent_type": "general-purpose",
    "description": "smoke",
    "prompt": "Fix the off-by-one in: for i in range(1, len(xs)): total += xs[i]. "
    "Reply with the fixed code only.",
}
DISPATCHES = (FAST, ESCALATE, PINNED, ALIAS_SONNET, EFFORT_REMOVED, JEV)
PREFLIGHT_CHECKS = ("timeout", "validate")
FULL_CHECKS = (
    "fast",
    "escalate",
    "effort",
    "effort-removed",
    "alias-sonnet",
    "jev",
    "context",
    "overhead",
)

type Json = dict[str, object]
results: list[str] = []
SECRETS: list[str] = []  # every key read; never printed


def report(status: str, check: str, detail: str) -> None:
    results.append(status)
    line = f"{status} {check}: {detail}"
    for secret in SECRETS:
        line = line.replace(secret, "<redacted>")
    print(line, flush=True)


def read_key_file(path: Path) -> str:
    """The key in `path`; errors name the path only, never the key."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode != 0o600:
            raise SystemExit(f"{path}: mode {mode:o}, need 600")
        key = path.read_text(encoding="utf-8").strip()
    except OSError, ValueError:  # a decode error's message holds file bytes
        raise SystemExit(f"{path}: key file missing or unreadable") from None
    if not key:
        raise SystemExit(f"{path}: key file is empty")
    return key


def jev_outage(error: str | None) -> bool:
    """True only for a transient Jev failure; auth, request and schema errors are FAILs."""
    return error is not None and bool(_TRANSIENT.fullmatch(error))


def subagent_meta(session_id: str | None, projects: Path) -> dict[str, Json]:
    """Claude Code's own record of each subagent's model and effort, keyed by tool_use_id."""
    found: dict[str, Json] = {}
    for path in projects.glob(f"*/{session_id}/subagents/*.meta.json") if session_id else []:
        try:
            meta: object = json.loads(path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            continue
        if isinstance(meta, dict) and isinstance(tool_use_id := meta.get("toolUseId"), str):
            found[tool_use_id] = meta
    return found


def find_records(session_id: str, data_root: Path) -> list[tuple[Path, Json]]:
    """Decision records of one session, each with the `decisions.jsonl` it came from."""
    found: list[tuple[Path, Json]] = []
    for path in sorted(data_root.glob("*/decisions.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError, ValueError:
            continue
        for line in lines:
            try:
                record: object = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("session_id") == session_id:
                found.append((path, record))
    return found


def child_env(**extra: str) -> dict[str, str]:
    """This environment without any key or off switch; a key enters only through `extra`."""
    drop = {*KEY_VARS, OFF_VAR}
    return {**{k: v for k, v in os.environ.items() if k not in drop}, **extra}


def hook_env(**extra: str) -> dict[str, str]:
    """Only `HOOK_VARS`, `LC_*` and `extra`: no hook the preflight runs gets a key or a secret."""
    kept = {k: v for k, v in os.environ.items() if k in HOOK_VARS or k.startswith("LC_")}
    return {**kept, **extra}


def instruction(calls: list[dict[str, str]]) -> str:
    count = "one Agent tool call" if len(calls) == 1 else f"{len(calls)} Agent tool calls"
    return (
        f"This is a routing test. Make exactly {count} in a single message, with exactly these "
        f"parameters and no others:\n{json.dumps(calls, indent=1)}\n"
        "Then reply with the agents' answers."
    )


def run(
    main: str,
    calls: list[dict[str, str]],
    env: dict[str, str],
    flags: tuple[str, ...] = PLUGIN_FLAGS,
    timeout: int = 600,
) -> tuple[Json | None, str]:
    cmd = ["claude", "-p", "--model", main, "--effort", "low", "--output-format", "json", *flags]
    try:
        r = subprocess.run(
            [*cmd, instruction(calls)],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, f"claude -p exceeded {timeout} s"
    if r.returncode != 0:  # the child holds the key: never forward its diagnostics
        return None, f"claude -p exit {r.returncode}"
    try:
        out: object = json.loads(r.stdout)
    except ValueError:
        return None, "stdout is not JSON"
    return (out, "") if isinstance(out, dict) else (None, "stdout is not a JSON object")


def text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def usage(out: Json) -> Json:
    found = out.get("modelUsage")
    return found if isinstance(found, dict) else {}


def has(out: Json, alias: str) -> bool:
    return any(k.startswith(FAMILY[alias]) for k in usage(out))


def models(out: Json) -> str:
    return ",".join(sorted(usage(out))) or "none"


def records(out: Json) -> list[tuple[Path, Json]]:
    session_id = text(out.get("session_id"))
    return find_records(session_id, PLUGIN_DATA) if session_id else []


def meta_of(out: Json, record: Json) -> Json | None:
    tool_use_id = text(record.get("tool_use_id"))
    return subagent_meta(text(out.get("session_id")), PROJECTS).get(tool_use_id or "")


def seen(out: Json, record: Json) -> tuple[object, object] | None:
    """The (model, effort) the subagent metadata shows for `record`; None without metadata."""
    meta = meta_of(out, record)
    return None if meta is None else (meta.get("model"), meta.get("effort"))


def applied(out: Json, record: Json) -> bool:
    return seen(out, record) == (record.get("alias"), record.get("effort"))


def covers_agent(matcher: object) -> bool:
    if matcher in (None, "", "*"):
        return True
    if not isinstance(matcher, str):
        return False
    try:
        return any(re.fullmatch(matcher, tool) for tool in ("Agent", "Task"))
    except re.error:
        return matcher in ("Agent", "Task")


def agent_hooks(config: object) -> list[Json]:
    """The `PreToolUse` hooks covering `Agent` in settings or a plugin's `hooks.json`."""
    hooks = config.get("hooks") if isinstance(config, dict) else None
    entries = hooks.get("PreToolUse") if isinstance(hooks, dict) else None
    found: list[Json] = []
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and covers_agent(entry.get("matcher")):
            listed = entry.get("hooks")
            found += [h for h in listed if isinstance(h, dict)] if isinstance(listed, list) else []
    return found


def with_plugin_vars(hook: Json, values: dict[str, str]) -> Json:
    """`hook` with each `${NAME}` of `values` replaced by its value in `command` and `args`."""

    def sub(value: object) -> object:
        if not isinstance(value, str):
            return value
        for name, replacement in values.items():
            value = value.replace(f"${{{name}}}", replacement)
        return value

    expanded: Json = {**hook, "command": sub(hook.get("command"))}
    if isinstance(args := hook.get("args"), list):
        expanded["args"] = [sub(a) for a in args]
    return expanded


def plugin_hook_sources(root: Path, manifest: object) -> tuple[list[Path | Json], bool]:
    """A plugin's `hooks/hooks.json` and the hook configs its `plugin.json` adds, each file once.

    A `hooks` value is a path relative to `root`, a list of them, an inline event map or an inline
    `{"hooks": ...}` config. The flag is False for any other shape and for a path that is not a
    file in `root`.
    """
    sources: list[Path | Json] = [(root / "hooks" / "hooks.json").resolve()]
    declared: object = manifest.get("hooks", []) if isinstance(manifest, dict) else []
    if isinstance(declared, dict):
        config: Json = declared if "hooks" in declared else {"hooks": declared}
        events = config["hooks"]
        valid = isinstance(events, dict) and all(isinstance(e, list) for e in events.values())
        return [*sources, config], valid
    for value in declared if isinstance(declared, list) else [declared]:
        path = (root / value).resolve() if isinstance(value, str) else None
        if path is None or not path.is_relative_to(root.resolve()) or not path.is_file():
            return sources, False
        if path not in sources:
            sources.append(path)
    return sources, True


def hook_emits(hook: Json, env: dict[str, str]) -> tuple[bool | None, str]:
    """Run one hook on every smoke dispatch: whether any stdout held `updatedInput`, and a note.

    None means the hook could not be checked: not a command hook, not runnable, timed out, or
    exited non-zero without `updatedInput`.
    """
    command, args = hook.get("command"), hook.get("args")
    if hook.get("type") != "command" or not isinstance(command, str):
        return None, "unverified (not a command hook)"
    # exec form (`args` present) runs without a shell
    argv = [command, *map(str, args)] if isinstance(args, list) else ["sh", "-c", command]
    for n, dispatch in enumerate(DISPATCHES):
        payload = {
            "session_id": "smoke",
            "tool_use_id": f"smoke-{n}",
            "cwd": str(Path.cwd()),
            "hook_event_name": "PreToolUse",
            "tool_name": "Agent",
            "tool_input": dispatch,
        }
        try:
            r = subprocess.run(
                argv,
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                timeout=10,
                env=env,
                check=False,
            )
            out, failure = r.stdout, f"exit {r.returncode}" if r.returncode else None
        except subprocess.TimeoutExpired as e:
            # the partial output is bytes even with text=True
            out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else ""
            failure = "timeout"
        except OSError as e:
            return None, f"unverified (not run: {type(e).__name__})"
        if "updatedInput" in out:  # routes whatever the exit
            return True, f"updatedInput before its {failure}" if failure else "updatedInput"
        if failure:
            return None, f"unverified ({failure})"
    return False, "no updatedInput"


def plugin_enabled(listing: object) -> bool | None:
    """Whether a `claude plugin list --json` listing holds this plugin, enabled at any scope.

    The listing is a list of entries such as `{"id": "<name>@<marketplace>", "enabled": false,
    "projectEnabled": false, ...}`; any other shape gives None. An entry without `enabled` counts
    as enabled (the safe direction).
    """
    if not isinstance(listing, list):
        return None
    found = False
    for entry in listing:
        if not isinstance(entry, dict):
            return None
        found |= PLUGIN in plugin_names(entry) and is_on(entry)
    return found


def plugin_names(entry: Json) -> set[str]:
    return {(text(entry.get(key)) or "").split("@")[0] for key in ("id", "name")}


def is_on(entry: Json) -> bool:
    """Enabled at any scope; an entry without `enabled` counts as enabled (the safe direction)."""
    return entry.get("enabled") is not False or entry.get("projectEnabled") is True


def plugin_listing(env: dict[str, str]) -> tuple[object, str | None]:
    """The `claude plugin list --json` listing and None, or None and why it is unknown."""
    try:
        r = subprocess.run(
            ["claude", "plugin", "list", "--json"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=60,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"unverified ({type(e).__name__})"
    if r.returncode != 0:
        return None, f"unverified (exit {r.returncode})"
    try:
        return json.loads(r.stdout), None
    except ValueError:
        return None, "unverified (listing is not JSON)"


def settings_paths() -> tuple[list[Path], str | None]:
    """Every settings file to inspect, and a note when the managed drop-in directory is unreadable.

    Missing files are listed too; reading them later skips them.
    """
    paths = [*SETTINGS, MANAGED_FILE]
    if MANAGED_DIR is None:
        return paths, None
    try:
        drop_ins = sorted(p for p in MANAGED_DIR.iterdir() if p.suffix == ".json")
    except FileNotFoundError:
        return paths, None
    except OSError as e:
        return paths, f"{MANAGED_DIR}: unverified (unreadable: {type(e).__name__})"
    return [*paths, *drop_ins], None


def check_double_route() -> None:
    """FAIL on any route found; INCONCLUSIVE when anything could not be inspected; else PASS.

    Inspects user, project, local and managed settings and, for every other enabled plugin,
    `hooks/hooks.json` and the hooks its `plugin.json` declares. Hooks get only basic variables,
    the project dir and, for a plugin, its root and data dir; one that routes only with a key or
    another secret passes.
    """
    project = str(Path.cwd())
    found: list[str] = []
    routes = unverified = False

    def unknown(note: str) -> None:
        nonlocal unverified
        unverified = True
        found.append(note)

    def load(path: Path) -> object:
        """The parsed file; None when it does not exist or cannot be read."""
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            unknown(f"{path}: unverified (unreadable: {type(e).__name__})")
            return None

    def inspect(where: object, config: object, plugin_vars: dict[str, str]) -> None:
        nonlocal routes, unverified
        run_env = hook_env(CLAUDE_PROJECT_DIR=project, **plugin_vars)
        for hook in agent_hooks(config):
            label = text(hook.get("command")) or "?"
            emitted, note = hook_emits(with_plugin_vars(hook, plugin_vars), run_env)
            routes |= emitted is True
            unverified |= emitted is None
            found.append(f"{where}: [{label[:80]}] {note}")

    paths, failure = settings_paths()
    if failure:
        unknown(failure)
    for path in paths:
        inspect(path, load(path), {})
    listing, failure = plugin_listing(child_env())
    installed = plugin_enabled(listing)
    shape = "unverified (unknown listing shape)" if installed is None else str(installed)
    found.append(f"installed {PLUGIN} enabled={failure or shape}")
    for entry in listing if installed is not None and isinstance(listing, list) else []:
        if PLUGIN in plugin_names(entry) or not is_on(entry):
            continue
        name, root = text(entry.get("id")), text(entry.get("installPath"))
        if not name or not root:
            unknown(f"plugin {name or '?'}: unverified (no id or installPath)")
            continue
        # the documented data dir: the id with each character outside [A-Za-z0-9_-] as `-`
        plugin_vars = {
            "CLAUDE_PLUGIN_ROOT": root,
            "CLAUDE_PLUGIN_DATA": str(PLUGIN_DATA / re.sub(r"[^A-Za-z0-9_-]", "-", name)),
        }
        sources, supported = plugin_hook_sources(
            Path(root), load(Path(root, ".claude-plugin", "plugin.json"))
        )
        if not supported:
            unknown(f"plugin {name}: unverified (unsupported plugin.json hooks)")
        for source in sources:
            if isinstance(source, Path):
                inspect(source, load(source), plugin_vars)
            else:
                inspect(f"plugin {name} plugin.json", source, plugin_vars)
    if routes or installed:
        status = "FAIL"
    else:
        status = "INCONCLUSIVE" if unverified or installed is None else "PASS"
    report(status, "double-route", "; ".join(found))


def check_timeout() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "hook-started"
        settings = Path(tmp) / "settings.json"
        hook = {
            "type": "command",
            "command": f"sh -c {shlex.quote(f'touch {shlex.quote(str(marker))}; sleep 30')}",
            "timeout": 2,
        }
        settings.write_text(
            json.dumps({"hooks": {"PreToolUse": [{"matcher": "Agent", "hooks": [hook]}]}}),
            encoding="utf-8",
        )
        start = time.monotonic()
        out, err = run(
            "sonnet",
            [
                {
                    "subagent_type": "general-purpose",
                    "description": "smoke",
                    "model": "opus",
                    "prompt": OK,
                }
            ],
            child_env(**{OFF_VAR: "off"}),  # test only the killed hook
            flags=("--settings", str(settings)),
            timeout=60,
        )
        took = round(time.monotonic() - start)
        started = marker.exists()
    if out is None:
        return report("FAIL", "timeout", err)
    # under 30 s proves the 30 s hook was killed rather than waited for
    ok = started and took < 30 and has(out, "opus")
    return report(
        "PASS" if ok else "FAIL", "timeout", f"marker={started} {took}s models={models(out)}"
    )


def check_validate() -> None:
    try:
        r = subprocess.run(
            ["claude", "plugin", "validate", "--strict", "."],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=120,
            cwd=REPO,
            env=child_env(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return report("FAIL", "validate", type(e).__name__)
    tail = (r.stdout + r.stderr).strip()[-200:]
    return report(
        "PASS" if r.returncode == 0 else "FAIL", "validate", f"exit {r.returncode}: {tail}"
    )


def check_fast(env: dict[str, str]) -> tuple[str | None, Path | None]:
    """Return the session id and the plugin data directory the run's record was written to."""
    out, err = run("sonnet", [FAST], env)
    if out is None:
        report("FAIL", "fast", err)
        return None, None
    recs = records(out)
    got = [(r.get("source"), r.get("alias"), r.get("effort")) for _, r in recs]
    ok = (
        got == [("rules", "haiku", "low")]
        and applied(out, recs[0][1])
        and has(out, "haiku")
        and not has(out, "opus")
    )
    report("PASS" if ok else "FAIL", "fast", f"records={got} models={models(out)}")
    return text(out.get("session_id")), recs[0][0].parent if recs else None


def check_escalate(env: dict[str, str]) -> bool | None:
    out, err = run("sonnet", [FAST, ESCALATE], env)
    if out is None:
        report("FAIL", "escalate", err)
        return None
    recs = [r for _, r in records(out)]
    ids = {r.get("tool_use_id") for r in recs}
    sources = sorted(str(r.get("source")) for r in recs)
    escalated = [r for r in recs if r.get("source") == "escalate"]
    got = [((r.get("alias"), r.get("effort")), seen(out, r)) for r in escalated]
    # the escalation must be recorded and applied as exactly opus, xhigh
    xhigh = got == [(("opus", "xhigh"), ("opus", "xhigh"))]
    ok = (
        len(recs) == 2
        and len(ids) == 2
        and sources == ["escalate", "rules"]
        and all(applied(out, r) for r in recs)
        and xhigh
        and has(out, "haiku")
        and has(out, "opus")
    )
    detail = f"sources={sources} escalate (record, meta)={got} models={models(out)}"
    report("PASS" if ok else "FAIL", "escalate", detail)
    return xhigh


def check_effort(env: dict[str, str], xhigh_applied: bool | None) -> None:
    out, err = run("sonnet", [PINNED], env)
    if out is None:
        return report("FAIL", "effort", err)
    recs = [r for _, r in records(out)]
    if [r.get("source") for r in recs] != ["pinned"]:
        return report("FAIL", "effort", f"records={[r.get('source') for r in recs]}")
    meta = meta_of(out, recs[0])
    if meta is None or xhigh_applied is None:
        return report("INCONCLUSIVE", "effort", "no subagent record or no escalate run")
    pinned = (meta.get("model"), meta.get("effort"))
    ok = pinned == ("opus", "low") and xhigh_applied
    detail = f"pinned kept {pinned}; routed xhigh applied={xhigh_applied}"
    return report("PASS" if ok else "FAIL", "effort", detail)


def check_effort_removed(env: dict[str, str], data: Path | None) -> None:
    if data is None:
        return report("INCONCLUSIVE", "effort-removed", "no fast record to locate the data dir")
    override = data / "policy.json"
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    for tier in policy["tiers"]:
        if tier["name"] == "fast":
            tier["efforts"] = []
    try:
        handle = override.open("x", encoding="utf-8")  # never replace an existing override
    except FileExistsError:
        return report("FAIL", "effort-removed", f"{override} exists; left untouched, not run")
    try:
        with handle:
            handle.write(json.dumps(policy))
        out, err = run("sonnet", [EFFORT_REMOVED], env)
    finally:
        override.unlink(missing_ok=True)
    if out is None:
        return report("FAIL", "effort-removed", err)
    recs = [r for _, r in records(out)]
    got = [(r.get("source"), r.get("alias"), r.get("effort")) for r in recs]
    meta = seen(out, recs[0]) if recs else None
    ok = got == [("rules", "haiku", None)] and meta == ("haiku", None)
    return report("PASS" if ok else "FAIL", "effort-removed", f"records={got} meta={meta}")


def check_alias_sonnet(env: dict[str, str]) -> None:
    out, err = run("haiku", [ALIAS_SONNET], env)
    if out is None:
        return report("FAIL", "alias-sonnet", err)
    recs = [r for _, r in records(out)]
    got = [(r.get("source"), r.get("requested_model"), r.get("requested_effort")) for r in recs]
    meta = seen(out, recs[0]) if len(recs) == 1 else None
    # pinned passes through untouched: sonnet as passed, and no effort since none was passed
    ok = got == [("pinned", "sonnet", None)] and meta == ("sonnet", None) and has(out, "sonnet")
    detail = f"records={got} meta={meta} models={models(out)}"
    return report("PASS" if ok else "FAIL", "alias-sonnet", detail)


def check_jev(env: dict[str, str]) -> None:
    out, err = run("opus", [JEV], env)
    if out is None:
        return report("FAIL", "jev", err)
    recs = [r for _, r in records(out)]
    got = [(r.get("source"), r.get("error")) for r in recs]
    if (
        len(recs) == 1
        and recs[0].get("source") == "error"
        and jev_outage(text(recs[0].get("error")))
    ):
        return report("INCONCLUSIVE", "jev", f"Jev unavailable, failed open: {got[0][1]}")
    if [source for source, _ in got] != ["jev"]:
        return report("FAIL", "jev", f"records={got}")
    record = recs[0]
    alias = text(record.get("alias"))
    confidences = [record.get(k) for k in ("tier_confidence", "effort_confidence")]
    detail = (
        f"selected={alias} effort={record.get('effort')} confidences={confidences} "
        f"models={models(out)}"
    )
    # the main session is opus, so an opus selection rests on the subagent record alone
    ok = (
        all(isinstance(c, int | float) and not isinstance(c, bool) for c in confidences)
        and alias in FAMILY
        and applied(out, record)
        and (alias == "opus" or has(out, alias or ""))
    )
    return report("PASS" if ok else "FAIL", "jev", detail)


def check_context(session_id: str | None) -> None:
    paths = list(PROJECTS.glob(f"*/{session_id}.jsonl")) if session_id else []
    seen = False
    for path in paths:
        try:
            seen |= CONTEXT_MARK in path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    detail = f"rules text in the fast session transcript={seen} ({len(paths)} file)"
    report("PASS" if seen else "FAIL", "context", detail)


def check_overhead() -> None:
    payload = json.dumps(
        {
            "session_id": "smoke-overhead",
            "tool_use_id": "smoke-overhead",
            "cwd": str(Path.cwd()),
            "hook_event_name": "PreToolUse",
            "tool_name": "Agent",
            "tool_input": {
                "subagent_type": "smoke:unresolved",
                "description": "smoke",
                "prompt": OK,
            },
        }
    )
    cmd = ["uv", "run", "--no-config", "--locked", "--script", str(HOOK)]
    times: list[float] = []
    with tempfile.TemporaryDirectory() as tmp:
        env = child_env(CLAUDE_PLUGIN_DATA=str(Path(tmp, "data")))
        for _ in range(OVERHEAD_RUNS):
            start = time.monotonic()
            r = subprocess.run(
                cmd, input=payload, capture_output=True, text=True, timeout=60, env=env, check=False
            )
            times.append(time.monotonic() - start)
            if r.returncode != 0 or r.stdout:
                return report("FAIL", "overhead", f"exit {r.returncode}, stdout={bool(r.stdout)}")
        sources = [r.get("source") for _, r in find_records("smoke-overhead", Path(tmp))]
    worst = max(times)
    ok = worst <= MAX_OVERHEAD_S and sources == ["unresolved"] * OVERHEAD_RUNS
    detail = f"max {worst:.2f} s of {[round(t, 2) for t in times]}; sources={set(sources)}"
    return report("PASS" if ok else "FAIL", "overhead", detail)


class Parser(argparse.ArgumentParser):
    @override
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")  # exit 2 is banned project-wide


def main(argv: list[str]) -> int:
    parser = Parser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--preflight", action="store_true", help="run only the preflight checks")
    parser.add_argument("--key-file", type=Path, default=KEY_FILE, help="TypeSafe key, mode 600")
    parser.add_argument("--skip-jev", action="store_true", help="no key; report jev as SKIP")
    args = parser.parse_args(argv)
    env: dict[str, str] = {}
    if not args.preflight and args.skip_jev:
        env = child_env()
    elif not args.preflight:  # read the key before any paid run; a bad key file ends the run
        SECRETS.append(key := read_key_file(args.key_file))
        env = child_env(TYPESAFE_API_KEY=key)
    check_double_route()
    if results[-1] != "PASS":  # every later check runs claude; a double route would spoil them
        for check in PREFLIGHT_CHECKS if args.preflight else PREFLIGHT_CHECKS + FULL_CHECKS:
            report("SKIP", check, "double-route did not pass")
        return 1
    check_timeout()
    check_validate()
    if not args.preflight:
        session_id, data = check_fast(env)
        check_effort(env, check_escalate(env))
        check_effort_removed(env, data)
        check_alias_sonnet(env)
        if args.skip_jev:
            report("SKIP", "jev", "--skip-jev given")
        else:
            check_jev(env)
        check_context(session_id)
        check_overhead()
    return 0 if results and all(status == "PASS" for status in results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
