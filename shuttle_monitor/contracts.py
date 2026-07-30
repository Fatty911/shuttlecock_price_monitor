"""Versioned public-data contracts shared by build, audit, and Pages."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any


SCHEMA_VERSION = 4
OUTCOMES = ("success", "blocked", "rejected", "error", "out_of_stock")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
REQUIRED_STATUS_FIELDS = frozenset(
    {
        "task_id",
        "outcome",
        "attempts",
        "started_at",
        "finished_at",
        "source_url",
        "final_url",
        "rejection_reason",
        "evidence_hash",
        "parser_version",
    }
)


class ContractError(ValueError):
    """A batch cannot be safely published."""


def _summary(statuses: Iterable[Mapping[str, Any]], expected_tasks: int) -> dict[str, int]:
    counts = Counter(str(row.get("outcome") or "") for row in statuses)
    return {
        "expected_tasks": expected_tasks,
        "attempted": sum(counts.values()),
        **{outcome: counts[outcome] for outcome in OUTCOMES},
    }


def build_envelope(
    *,
    repo: str,
    run_id: str,
    run_attempt: str,
    source_sha: str,
    config_sha256: str,
    started_at: str,
    finished_at: str,
    mode: str,
    baseline_batch_id: str | None,
    expected_tasks: int,
    statuses: list[dict[str, Any]],
    prices: list[dict[str, Any]],
    evidence_sha256: str,
    audit_status: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "batch_id": f"{repo}:{run_id}:{run_attempt}",
        "run_id": str(run_id),
        "run_attempt": str(run_attempt),
        "source_sha": source_sha,
        "config_sha256": config_sha256,
        "started_at": started_at,
        "finished_at": finished_at,
        "mode": mode,
        "baseline_batch_id": baseline_batch_id,
        "expected_tasks": expected_tasks,
        "statuses": statuses,
        "prices": prices,
        "summary": _summary(statuses, expected_tasks),
        "evidence_sha256": evidence_sha256,
        "audit_status": audit_status,
    }


def validate_envelope(envelope: Mapping[str, Any], expected_task_ids: Iterable[str]) -> None:
    expected = list(expected_task_ids)
    if envelope.get("schema_version") != SCHEMA_VERSION:
        raise ContractError("schema_version must be 4")
    if not COMMIT_RE.fullmatch(str(envelope.get("source_sha") or "")):
        raise ContractError("source_sha must be a 40-character commit")
    for field in ("config_sha256", "evidence_sha256"):
        if not SHA256_RE.fullmatch(str(envelope.get(field) or "")):
            raise ContractError(f"{field} must be sha256")
    if envelope.get("mode") not in {"live", "fixture"}:
        raise ContractError("mode must be live or fixture")
    statuses = envelope.get("statuses")
    prices = envelope.get("prices")
    if not isinstance(statuses, list) or not isinstance(prices, list):
        raise ContractError("statuses and prices must be arrays")
    actual = [str(row.get("task_id") or "") for row in statuses if isinstance(row, Mapping)]
    if (
        len(statuses) != len(expected)
        or len(actual) != len(set(actual))
        or set(actual) != set(expected)
        or int(envelope.get("expected_tasks") or -1) != len(expected)
    ):
        raise ContractError("task set must be unique and exactly conserved")
    success_ids: set[str] = set()
    for row in statuses:
        if not isinstance(row, Mapping) or not REQUIRED_STATUS_FIELDS.issubset(row):
            raise ContractError("status is missing required fields")
        outcome = str(row["outcome"])
        if outcome not in OUTCOMES:
            raise ContractError(f"invalid outcome: {outcome}")
        if outcome != "success" and any(
            row.get(field) is not None for field in ("price", "offer", "product_url")
        ):
            raise ContractError("non-success status must not contain price, offer, or product_url")
        if outcome == "success":
            success_ids.add(str(row["task_id"]))
    price_ids = [str(row.get("task_id") or "") for row in prices if isinstance(row, Mapping)]
    if len(price_ids) != len(set(price_ids)) or set(price_ids) != success_ids:
        raise ContractError("prices must correspond one-to-one with successful tasks")
    expected_summary = _summary(statuses, len(expected))
    if envelope.get("summary") != expected_summary:
        raise ContractError("summary does not conserve outcomes")
    if expected_summary["attempted"] != sum(expected_summary[name] for name in OUTCOMES):
        raise ContractError("outcome conservation failed")


def validate_live_envelope(envelope: Mapping[str, Any], expected_task_ids: Iterable[str]) -> None:
    validate_envelope(envelope, expected_task_ids)
    if envelope.get("mode") != "live":
        raise ContractError("current Pages payload requires mode=live")
