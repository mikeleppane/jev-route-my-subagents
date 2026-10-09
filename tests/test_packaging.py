from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "hooks" / "hooks.json"
SCRIPT_ARGS = [
    "run",
    "--no-config",
    "--locked",
    "--script",
    "${CLAUDE_PLUGIN_ROOT}/scripts/route_hook.py",
]


def plugin_root_paths(value: object) -> Iterator[Path]:
    if isinstance(value, str):
        for match in re.finditer(r"\$\{CLAUDE_PLUGIN_ROOT\}(/[\w./-]+)", value):
            yield ROOT / match.group(1).removeprefix("/")
    elif isinstance(value, dict):
        for nested in value.values():
            yield from plugin_root_paths(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from plugin_root_paths(nested)


def test_hook_paths_exist() -> None:
    hooks = json.loads(HOOKS.read_text(encoding="utf-8"))
    paths = list(plugin_root_paths(hooks))

    assert paths
    assert all(path.resolve().is_relative_to(ROOT.resolve()) for path in paths)
    assert all(path.is_file() for path in paths)


def test_hooks_shape() -> None:
    hooks = json.loads(HOOKS.read_text(encoding="utf-8"))

    assert hooks == {
        "description": "Route subagent dispatches to a model tier and effort",
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Agent",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "uv",
                            "args": SCRIPT_ARGS,
                            "timeout": 5,
                        }
                    ],
                }
            ],
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "cat",
                            "args": ["${CLAUDE_PLUGIN_ROOT}/hooks/session-context.json"],
                        }
                    ]
                },
                {
                    "matcher": "startup",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "uv",
                            "args": [*SCRIPT_ARGS, "--warm"],
                            "async": True,
                        }
                    ],
                },
            ],
        },
    }


def test_session_context_is_valid_sessionstart_output() -> None:
    context = json.loads((ROOT / "hooks" / "session-context.json").read_text(encoding="utf-8"))
    output = context["hookSpecificOutput"]

    assert output["hookEventName"] == "SessionStart"
    additional_context = output["additionalContext"]
    assert isinstance(additional_context, str)
    assert "[model-pinned]" in additional_context
    assert "[escalate]" in additional_context
    assert "Router <id>: <tier> (<alias>, effort <effort>)." in additional_context


def test_plugin_metadata() -> None:
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    marketplace = json.loads(
        (ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )

    assert plugin["name"] == "jev-route-my-subagents"
    assert plugin["version"] == "0.1.0"
    assert plugin["description"]
    assert plugin["license"] == "MIT"
    assert plugin["repository"] == "https://github.com/mikeleppane/jev-route-my-subagents"
    assert set(plugin) <= {
        "name",
        "version",
        "description",
        "author",
        "homepage",
        "repository",
        "license",
        "keywords",
        "userConfig",
    }

    author = plugin["author"]
    assert author["name"] == "mikeleppane"
    assert set(author) == {"name"}

    options = plugin["userConfig"]
    assert set(options) == {"typesafe_api_key"}
    api_key = options["typesafe_api_key"]
    assert api_key["type"] == "string"
    assert api_key["sensitive"] is True
    assert api_key["title"]
    assert api_key["description"]
    assert "sent only to TypeSafe" in api_key["description"]
    assert set(api_key) <= {
        "type",
        "title",
        "description",
        "required",
        "default",
        "options",
        "multiple",
        "sensitive",
        "min",
        "max",
    }

    assert marketplace["name"] == "jev-route-my-subagents"
    assert isinstance(marketplace["description"], str)
    assert marketplace["description"].strip()
    assert marketplace["owner"]["name"] == "mikeleppane"
    assert set(marketplace["owner"]) == {"name"}
    assert marketplace["plugins"][0]["name"] == "jev-route-my-subagents"
    assert marketplace["plugins"][0]["source"] == "./"


def test_stats_skill() -> None:
    skill = (ROOT / "skills" / "stats" / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = re.match(
        r"\A---\n(?P<metadata>.*?)\n---\n(?P<body>.*)\Z",
        skill,
        re.DOTALL,
    )

    assert frontmatter is not None
    assert re.search(r"(?m)^name: stats$", frontmatter["metadata"])
    assert re.search(r"(?m)^disable-model-invocation: true$", frontmatter["metadata"])
    assert "--no-config --locked --script" in frontmatter["body"]
