import json
from typing import TYPE_CHECKING

import pytest
from router import POLICY_PATH, Policy, load_policy

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_TOP = ("session_id", "tool_use_id", "cwd")


@pytest.fixture
def policy() -> Policy:
    return load_policy(POLICY_PATH)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"  # not created: Claude Code creates it only on first reference


@pytest.fixture
def env(home: Path, data_dir: Path) -> dict[str, str]:
    return {"HOME": str(home), "CLAUDE_PLUGIN_DATA": str(data_dir)}


@pytest.fixture
def payload(home: Path) -> Callable[..., bytes]:
    def make(**fields: object) -> bytes:
        top = {name: fields.pop(name) for name in _TOP if name in fields}
        return json.dumps(
            {
                "session_id": "s1",
                "tool_use_id": "tu1",
                "cwd": str(home),
                "hook_event_name": "PreToolUse",
                "tool_name": "Agent",
                **top,
                "tool_input": {
                    "subagent_type": "general-purpose",
                    "description": "d",
                    "prompt": "p",
                    **fields,
                },
            }
        ).encode()

    return make
