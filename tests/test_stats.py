import json
from typing import TYPE_CHECKING

import pytest
from router import stats

if TYPE_CHECKING:
    from pathlib import Path


def write_records(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def test_overrides_counted_including_removed_effort(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    write_records(
        path,
        [
            {
                "source": "jev",
                "requested_model": "opus",
                "requested_effort": "high",
                "alias": "haiku",
                "effort": "low",
            },
            {
                "source": "rules",
                "requested_model": None,
                "requested_effort": None,
                "alias": "haiku",
                "effort": "low",
            },
            {
                "source": "escalate",
                "requested_model": None,
                "requested_effort": "high",
                "alias": "haiku",
                "effort": None,
            },
            {
                "source": "jev",
                "requested_model": "haiku",
                "requested_effort": "low",
                "alias": "haiku",
                "effort": "low",
            },
        ],
    )

    assert stats(path)["overrides"] == {"count": 2, "n": 4}


def test_pinned_records_never_count_as_overrides(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    write_records(
        path,
        [
            {
                "source": "pinned",
                "requested_model": "opus",
                "requested_effort": "high",
                "alias": "haiku",
                "effort": "low",
            }
        ],
    )

    assert stats(path)["overrides"] == {"count": 0, "n": 0}


def test_confidence_means_over_jev_records(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    write_records(
        path,
        [
            {"source": "jev", "tier_confidence": 0.8, "effort_confidence": 0.4},
            {"source": "jev", "tier_confidence": 0.6, "effort_confidence": 0.2},
            {"source": "jev", "tier_confidence": 0.9},
            {"source": "rules", "tier_confidence": 0.9, "effort_confidence": 0.9},
            {"source": "jev", "tier_confidence": "0.9", "effort_confidence": 0.9},
            {"source": "jev", "tier_confidence": True, "effort_confidence": 0.9},
        ],
    )

    assert stats(path)["confidence"] == {
        "tier_mean": pytest.approx(0.7),
        "effort_mean": pytest.approx(0.3),
        "n": 2,
    }


def test_legacy_records_are_unknown(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    write_records(
        path,
        [
            {"source": "jev", "tier": "fast", "effort": "low"},
            {"source": "rules", "tier": "balanced", "effort": "medium"},
        ],
    )

    summary = stats(path)

    assert summary["total"] == 2
    assert summary["overrides"] == {"count": 0, "n": 0}
    assert summary["confidence"] == {
        "tier_mean": None,
        "effort_mean": None,
        "n": 0,
    }


def test_truncated_last_line_is_malformed(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    write_records(
        path,
        [
            {"source": "jev", "tier": "fast", "effort": "low"},
            {"source": "escalate", "tier": "strong", "effort": "xhigh"},
            {"source": "error", "tier": None, "effort": None},
            {"source": "pinned", "tier": None, "effort": None},
        ],
    )
    with path.open("a", encoding="utf-8") as log:
        log.write('{"ts": "2026')

    summary = stats(path)

    assert (summary["total"], summary["malformed"]) == (4, 1)
    assert summary["source"] == {"jev": 1, "escalate": 1, "error": 1, "pinned": 1}
    assert summary["tier"] == {"fast": 1, "strong": 1, "none": 2}
    assert (summary["escalation_rate"], summary["error_rate"]) == (0.25, 0.25)


def test_missing_file_reports_zero(tmp_path: Path) -> None:
    assert stats(tmp_path / "none.jsonl")["total"] == 0
