"""Deterministic structural and product gates for public shuttlecock batches."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from shuttle_monitor.contracts import ContractError, validate_envelope

PLATFORMS = frozenset({"taobao", "jd", "pdd"})


def _official_detail(platform: str, raw_url: object) -> bool:
    parsed = urlparse(str(raw_url or ""))
    host = (parsed.hostname or "").lower()
    query = parse_qs(parsed.query)
    if parsed.scheme != "https":
        return False
    if platform == "taobao":
        return (
            host in {"item.taobao.com", "detail.tmall.com"}
            and parsed.path == "/item.htm"
            and bool(query.get("id"))
        )
    if platform == "jd":
        return host == "item.jd.com" and parsed.path.endswith(".html")
    if platform == "pdd":
        return (
            host in {"mobile.yangkeduo.com", "www.pinduoduo.com"}
            and parsed.path in {"/goods", "/goods.html"}
            and bool(query.get("goods_id"))
        )
    return False


def _native_id(platform: str, raw_url: object) -> str | None:
    parsed = urlparse(str(raw_url or ""))
    query = parse_qs(parsed.query)
    if platform in {"taobao", "pdd"}:
        field = "id" if platform == "taobao" else "goods_id"
        return query.get(field, [None])[0]
    if platform == "jd":
        match = re.fullmatch(r"/([1-9]\d*)\.html", parsed.path)
        return match.group(1) if match else None
    return None


def product_quality_gate(statuses: Iterable[Mapping[str, object]]) -> bool:
    """Require at least one independently verified live detail offer per platform."""
    successful_platforms: set[str] = set()
    seen_tasks: set[str] = set()
    for row in statuses:
        task_id = str(row.get("task_id") or "")
        platform = str(row.get("platform") or "")
        if not task_id or task_id in seen_tasks:
            continue
        seen_tasks.add(task_id)
        if (
            platform in PLATFORMS
            and row.get("outcome") == "success"
            and row.get("mode") == "live"
            and row.get("detail_verified") is True
            and bool(row.get("native_product_id"))
            and isinstance(row.get("price"), (int, float))
            and float(row["price"]) > 0
            and _official_detail(platform, row.get("product_url"))
        ):
            successful_platforms.add(platform)
    return successful_platforms == PLATFORMS


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_file_manifest(
    root: Path,
    relative_paths: Iterable[str],
    *,
    batch_id: str,
    source_sha: str,
) -> dict[str, object]:
    files = {
        path: {"sha256": _sha256(root / path), "size": (root / path).stat().st_size}
        for path in sorted(set(relative_paths))
    }
    return {
        "schema_version": 4,
        "batch_id": batch_id,
        "source_sha": source_sha,
        "files": files,
    }


def verify_file_manifest(root: Path, manifest: Mapping[str, object]) -> list[str]:
    mismatches: list[str] = []
    files = manifest.get("files")
    if not isinstance(files, Mapping):
        return ["manifest.files"]
    for name, metadata in files.items():
        path = root / str(name)
        expected = metadata.get("sha256") if isinstance(metadata, Mapping) else None
        if not path.is_file() or expected != _sha256(path):
            mismatches.append(str(name))
    return sorted(mismatches)


def audit_envelope(
    envelope: Mapping[str, object],
    expected_task_ids: Iterable[str],
) -> dict[str, object]:
    violations: list[dict[str, object]] = []
    expected = list(expected_task_ids)
    try:
        validate_envelope(envelope, expected)
        structure_status = "pass"
    except ContractError as exc:
        structure_status = "blocked"
        violations.append({"code": "structure_contract", "detail": str(exc)})
    statuses = envelope.get("statuses")
    rows = statuses if isinstance(statuses, list) else []
    for row in rows:
        if not isinstance(row, Mapping) or row.get("outcome") != "success":
            continue
        platform = str(row.get("platform") or "")
        if (
            row.get("detail_verified") is not True
            or not row.get("native_product_id")
            or not _official_detail(platform, row.get("product_url"))
            or _native_id(platform, row.get("product_url"))
            != str(row.get("native_product_id"))
        ):
            violations.append(
                {"code": "success_evidence_invalid", "task_id": row.get("task_id")}
            )
            structure_status = "blocked"
    for platform in sorted(PLATFORMS):
        if not any(
            isinstance(row, Mapping)
            and row.get("platform") == platform
            and row.get("outcome") == "success"
            for row in rows
        ):
            violations.append({"code": f"platform_success_zero:{platform}"})
    product_status = "pass" if structure_status == "pass" and product_quality_gate(rows) else "blocked"
    fingerprint_input = {
        "schema_version": 4,
        "source_sha": envelope.get("source_sha"),
        "codes": sorted(str(row["code"]) for row in violations),
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_input, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "schema_version": 4,
        "batch_id": envelope.get("batch_id"),
        "source_sha": envelope.get("source_sha"),
        "structure_status": structure_status,
        "product_status": product_status,
        "status": "pass" if structure_status == product_status == "pass" else "blocked",
        "fingerprint": fingerprint,
        "violations": violations,
    }
