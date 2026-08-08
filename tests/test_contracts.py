import hashlib

import pytest

from shuttle_monitor.contracts import (
    ContractError,
    build_envelope,
    validate_envelope,
    validate_live_envelope,
)


SOURCE_SHA = "a" * 40
CONFIG_SHA = hashlib.sha256(b"products").hexdigest()


def _status(task_id: str, outcome: str = "blocked") -> dict:
    return {
        "task_id": task_id,
        "platform": task_id.split(":", 1)[0],
        "outcome": outcome,
        "attempts": 1,
        "started_at": "2026-07-30T00:00:00Z",
        "finished_at": "2026-07-30T00:00:01Z",
        "source_url": "https://search.example.invalid/",
        "final_url": "https://search.example.invalid/",
        "rejection_reason": "challenge" if outcome != "success" else None,
        "evidence_hash": "b" * 64,
        "parser_version": "shuttle-v4",
    }


def _envelope(mode: str = "live") -> tuple[dict, list[str]]:
    task_ids = [
        f"{platform}:model-{index}"
        for platform in ("taobao", "jd", "pdd")
        for index in range(31)
    ]
    statuses = [_status(task_id) for task_id in task_ids]
    envelope = build_envelope(
        repo="shuttlecock_price_monitor",
        run_id="123",
        run_attempt="2",
        source_sha=SOURCE_SHA,
        config_sha256=CONFIG_SHA,
        started_at="2026-07-30T00:00:00Z",
        finished_at="2026-07-30T00:01:00Z",
        mode=mode,
        baseline_batch_id=None,
        expected_tasks=93,
        statuses=statuses,
        prices=[],
        evidence_sha256="c" * 64,
        audit_status="blocked",
    )
    return envelope, task_ids


def test_v4_envelope_has_batch_identity_and_exact_93_task_conservation():
    envelope, task_ids = _envelope()
    validate_envelope(envelope, task_ids)
    assert envelope["schema_version"] == 4
    assert envelope["batch_id"] == "shuttlecock_price_monitor:123:2"
    assert envelope["source_sha"] == SOURCE_SHA
    assert envelope["config_sha256"] == CONFIG_SHA
    assert envelope["summary"]["blocked"] == 93
    assert sum(envelope["summary"][key] for key in ("success", "blocked", "rejected", "error", "out_of_stock")) == 93


def test_non_success_status_with_price_fails_closed():
    envelope, task_ids = _envelope()
    envelope["statuses"][0]["price"] = 99.0
    with pytest.raises(ContractError, match="non-success"):
        validate_envelope(envelope, task_ids)


def test_fixture_envelope_cannot_pass_live_validation():
    envelope, task_ids = _envelope(mode="fixture")
    validate_envelope(envelope, task_ids)
    with pytest.raises(ContractError, match="mode=live"):
        validate_live_envelope(envelope, task_ids)
