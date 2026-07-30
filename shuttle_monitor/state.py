"""Safe cross-run state restoration and append-only history handling."""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any


MAX_ARCHIVE_BYTES = 10 * 1024 * 1024
MAX_MEMBER_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_MEMBERS = 20
ALLOWED_FILES = {
    "state/metadata.json": "metadata.json",
    "state/checkpoint.json": "checkpoint.json",
    "state/history.json": "history.json",
}
ARCHIVE_ALIASES = {
    **{name: name for name in ALLOWED_FILES},
    **{output_name: archive_name for archive_name, output_name in ALLOWED_FILES.items()},
}


class InvalidStateArtifact(ValueError):
    """An artifact is unsafe, stale, or belongs to another batch lineage."""


def _parse_archive(archive_bytes: bytes) -> dict[str, bytes]:
    if len(archive_bytes) > MAX_ARCHIVE_BYTES:
        raise InvalidStateArtifact("archive compressed size limit exceeded")
    output: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as bundle:
            members = bundle.infolist()
            if len(members) > MAX_MEMBERS:
                raise InvalidStateArtifact("archive member count limit exceeded")
            if sum(member.file_size for member in members) > MAX_TOTAL_BYTES:
                raise InvalidStateArtifact("archive expanded size limit exceeded")
            for member in members:
                normalized = member.filename.replace("\\", "/")
                path = PurePosixPath(normalized)
                if path.is_absolute() or ".." in path.parts:
                    raise InvalidStateArtifact(f"unsafe archive path: {member.filename}")
                if member.is_dir():
                    continue
                if normalized not in ARCHIVE_ALIASES:
                    raise InvalidStateArtifact(f"unsafe archive member: {member.filename}")
                if member.file_size > MAX_MEMBER_BYTES:
                    raise InvalidStateArtifact(f"member size limit exceeded: {member.filename}")
                canonical = ARCHIVE_ALIASES[normalized]
                if canonical in output:
                    raise InvalidStateArtifact(f"duplicate archive member: {member.filename}")
                output[canonical] = bundle.read(member)
    except InvalidStateArtifact:
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise InvalidStateArtifact("state archive cannot be parsed") from exc
    if set(output) != set(ALLOWED_FILES):
        raise InvalidStateArtifact("state archive is incomplete")
    return output


def restore_state_archive(
    archive_bytes: bytes,
    destination: Path,
    *,
    repo: str,
    branch: str,
    current_run_id: str,
    config_sha256: str,
) -> dict[str, Any]:
    files = _parse_archive(archive_bytes)
    try:
        metadata = json.loads(files["state/metadata.json"])
        checkpoint = json.loads(files["state/checkpoint.json"])
        history = json.loads(files["state/history.json"])
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidStateArtifact("state JSON cannot be parsed") from exc
    if (
        not isinstance(metadata, dict)
        or metadata.get("schema_version") != 4
        or metadata.get("repo") != repo
        or metadata.get("branch") != branch
        or str(metadata.get("run_id")) == str(current_run_id)
        or metadata.get("config_sha256") != config_sha256
        or not isinstance(checkpoint, dict)
        or not isinstance(history, list)
    ):
        raise InvalidStateArtifact("state identity or schema mismatch")

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.restore-", dir=destination.parent))
    backup = destination.parent / f".{destination.name}.backup-{uuid.uuid4().hex}"
    moved = False
    try:
        for archive_name, output_name in ALLOWED_FILES.items():
            staging.joinpath(output_name).write_bytes(files[archive_name])
        if destination.exists():
            destination.replace(backup)
            moved = True
        try:
            staging.replace(destination)
        except Exception:
            if moved and backup.exists() and not destination.exists():
                backup.replace(destination)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if backup.exists() and destination.exists():
            shutil.rmtree(backup)
    return metadata


def _write_json(path: Path, value: Any) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def write_state_directory(
    destination: Path,
    *,
    repo: str,
    branch: str,
    run_id: str,
    config_sha256: str,
    batch_id: str,
    completed_task_ids: list[str],
    history: list[dict[str, Any]],
) -> None:
    """Atomically persist the bounded files consumed by the restore path."""
    if len(set(completed_task_ids)) != len(completed_task_ids):
        raise ValueError("completed task IDs must be unique")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.write-", dir=destination.parent))
    backup = destination.parent / f".{destination.name}.backup-{uuid.uuid4().hex}"
    try:
        _write_json(
            staging / "metadata.json",
            {
                "schema_version": 4,
                "repo": repo,
                "branch": branch,
                "run_id": str(run_id),
                "config_sha256": config_sha256,
            },
        )
        _write_json(
            staging / "checkpoint.json",
            {
                "schema_version": 4,
                "batch_id": batch_id,
                "completed_task_ids": sorted(completed_task_ids),
            },
        )
        _write_json(staging / "history.json", history)
        if destination.exists():
            destination.replace(backup)
        try:
            staging.replace(destination)
        except Exception:
            if backup.exists() and not destination.exists():
                backup.replace(destination)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if backup.exists() and destination.exists():
            shutil.rmtree(backup)


def _timestamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc)


def merge_history_events(
    existing: list[dict[str, Any]],
    current: list[dict[str, Any]],
    *,
    now: str,
    retention_days: int = 180,
) -> list[dict[str, Any]]:
    current_time = _timestamp(now)
    if current_time is None:
        raise ValueError("now must be an ISO timestamp")
    cutoff = current_time - timedelta(days=retention_days)
    merged: dict[str, dict[str, Any]] = {}
    for row in [*existing, *current]:
        event_id = str(row.get("event_id") or "")
        observed = _timestamp(row.get("observed_at"))
        if event_id and observed is not None and observed >= cutoff:
            merged[event_id] = dict(row)
    return sorted(
        merged.values(),
        key=lambda row: (_timestamp(row["observed_at"]) or cutoff, str(row["event_id"])),
    )
