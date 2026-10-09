import math
import os
import subprocess
import sys

import pytest
import requests
from router import JEV_URL, JevError, Policy, Scores, jev_asker

KEY = "k-test"


class Reply:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self.body = body

    def json(self) -> object:
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


def answers(
    tier_score: object = 2,
    tier_confidence: object = 0.9,
    effort_score: object = 1,
    effort_confidence: object = 0.6,
) -> dict[str, object]:
    return {
        "answers": {
            "tier": {"score": tier_score, "confidence": tier_confidence},
            "effort": {"score": effort_score, "confidence": effort_confidence},
        }
    }


def stub(monkeypatch: pytest.MonkeyPatch, reply: Reply) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def post(url: str, **kwargs: object) -> Reply:
        calls.append({"url": url, **kwargs})
        return reply

    monkeypatch.setattr(requests, "post", post)
    return calls


def test_request_shape(monkeypatch: pytest.MonkeyPatch, policy: Policy) -> None:
    calls = stub(monkeypatch, Reply(200, answers()))
    assert jev_asker(KEY)("s", policy) == Scores(2.0, 1.0, 0.9, 0.6)
    assert calls == [
        {
            "url": JEV_URL,
            "json": {
                "state": "s",
                "model": "jev-latest",
                "questions": {
                    "tier": {
                        "type": "score",
                        "instructions": policy["instructions"]["tier"],
                        "criteria": [f"{t['name']}: {t['criteria']}" for t in policy["tiers"]],
                    },
                    "effort": {
                        "type": "score",
                        "instructions": policy["instructions"]["effort"],
                        "criteria": [f"{e['name']}: {e['criteria']}" for e in policy["efforts"]],
                    },
                },
            },
            "headers": {"Authorization": f"Bearer {KEY}"},
            "timeout": (1.0, 2.0),
            "allow_redirects": False,
        }
    ]


@pytest.mark.parametrize("status", [307, 401, 422, 429, 529])
def test_non_200_raises_once(monkeypatch: pytest.MonkeyPatch, policy: Policy, status: int) -> None:
    calls = stub(monkeypatch, Reply(status, answers()))
    with pytest.raises(JevError, match=f"^status {status}$"):
        jev_asker(KEY)("s", policy)
    assert len(calls) == 1


def test_undecodable_body_raises(monkeypatch: pytest.MonkeyPatch, policy: Policy) -> None:
    error = requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0)
    stub(monkeypatch, Reply(200, error))
    with pytest.raises(JevError, match="body invalid") as caught:
        jev_asker(KEY)("s", policy)
    assert "<html>" not in str(caught.value)


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ([], "body invalid"),
        ({"answers": {"tier": {"score": 1, "confidence": 0.5}}}, "answers.effort invalid"),
        (answers(tier_score=True), "answers.tier.score invalid"),
        (answers(tier_score=math.inf), "answers.tier.score invalid"),
        (answers(tier_score=math.nan), "answers.tier.score invalid"),
        (answers(tier_score=10**400), "answers.tier.score invalid"),
        (answers(effort_score=math.nan), "answers.effort.score invalid"),
        (answers(tier_confidence=-0.1), "answers.tier.confidence invalid"),
        (answers(effort_confidence=1.5), "answers.effort.confidence invalid"),
        (answers(effort_confidence=None), "answers.effort.confidence invalid"),
    ],
)
def test_bad_bodies_raise_without_content(
    monkeypatch: pytest.MonkeyPatch, policy: Policy, body: object, match: str
) -> None:
    stub(monkeypatch, Reply(200, body))
    with pytest.raises(JevError, match=f"^{match}$"):
        jev_asker(KEY)("s", policy)


def test_requests_imported_lazily() -> None:
    result = subprocess.run(
        [sys.executable, "-c", 'import router, sys; assert "requests" not in sys.modules'],
        env={**os.environ, "PYTHONPATH": "scripts"},
        check=False,
    )
    assert result.returncode == 0
