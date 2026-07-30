import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from shuttle_monitor.state import (
    InvalidStateArtifact,
    merge_history_events,
    restore_state_archive,
    write_state_directory,
)


CONFIG_SHA = "1" * 64


def _archive(files: dict[str, object]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        for name, payload in files.items():
            raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            bundle.writestr(name, raw)
    return output.getvalue()


def _valid_archive() -> bytes:
    return _archive(
        {
            "state/metadata.json": {
                "schema_version": 4,
                "repo": "shuttlecock_price_monitor",
                "branch": "main",
                "run_id": "100",
                "config_sha256": CONFIG_SHA,
            },
            "state/checkpoint.json": {"batch_id": "batch-100", "completed_task_ids": ["a"]},
            "state/history.json": [],
        }
    )


def test_restore_accepts_only_prior_main_v4_matching_config(tmp_path):
    destination = tmp_path / "state"
    result = restore_state_archive(
        _valid_archive(),
        destination,
        repo="shuttlecock_price_monitor",
        branch="main",
        current_run_id="101",
        config_sha256=CONFIG_SHA,
    )
    assert result["run_id"] == "100"
    assert json.loads((destination / "checkpoint.json").read_text())["completed_task_ids"] == ["a"]


def test_unsafe_archive_rolls_back_without_overwriting_existing_state(tmp_path):
    destination = tmp_path / "state"
    destination.mkdir()
    (destination / "checkpoint.json").write_text('{"keep": true}', encoding="utf-8")
    poisoned = _archive(
        {
            "state/metadata.json": {
                "schema_version": 4,
                "repo": "shuttlecock_price_monitor",
                "branch": "main",
                "run_id": "100",
                "config_sha256": CONFIG_SHA,
            },
            "../escape.json": {},
        }
    )
    with pytest.raises(InvalidStateArtifact, match="unsafe"):
        restore_state_archive(
            poisoned,
            destination,
            repo="shuttlecock_price_monitor",
            branch="main",
            current_run_id="101",
            config_sha256=CONFIG_SHA,
        )
    assert (destination / "checkpoint.json").read_text() == '{"keep": true}'
    assert not (tmp_path / "escape.json").exists()


def test_history_is_event_append_deduped_and_retained_for_180_days():
    old = [
        {"event_id": "expired", "observed_at": "2025-01-01T00:00:00Z"},
        {"event_id": "same", "observed_at": "2026-07-29T00:00:00Z", "price": 100},
    ]
    current = [
        {"event_id": "same", "observed_at": "2026-07-29T00:00:00Z", "price": 100},
        {"event_id": "new", "observed_at": "2026-07-30T00:00:00Z", "price": 99},
    ]
    merged = merge_history_events(old, current, now="2026-07-30T00:00:00Z")
    assert [row["event_id"] for row in merged] == ["same", "new"]


def test_restore_state_cli_is_available():
    result = subprocess.run(
        [sys.executable, "scripts/restore_state.py", "--help"],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0


def test_state_writer_emits_complete_atomic_v4_state(tmp_path):
    destination = tmp_path / "state"
    write_state_directory(
        destination,
        repo="shuttlecock_price_monitor",
        branch="main",
        run_id="101",
        config_sha256=CONFIG_SHA,
        batch_id="shuttlecock_price_monitor:101:1",
        completed_task_ids=["taobao:model"],
        history=[{"event_id": "one", "observed_at": "2026-07-30T00:00:00Z"}],
    )
    assert json.loads((destination / "metadata.json").read_text())["schema_version"] == 4
    assert json.loads((destination / "checkpoint.json").read_text())[
        "completed_task_ids"
    ] == ["taobao:model"]
    assert json.loads((destination / "history.json").read_text())[0]["event_id"] == "one"


def test_restore_accepts_github_artifact_root_layout(tmp_path):
    root_layout = _archive(
        {
            "metadata.json": {
                "schema_version": 4,
                "repo": "shuttlecock_price_monitor",
                "branch": "main",
                "run_id": "100",
                "config_sha256": CONFIG_SHA,
            },
            "checkpoint.json": {"batch_id": "batch-100", "completed_task_ids": ["a"]},
            "history.json": [],
        }
    )
    restored = restore_state_archive(
        root_layout,
        tmp_path / "state",
        repo="shuttlecock_price_monitor",
        branch="main",
        current_run_id="101",
        config_sha256=CONFIG_SHA,
    )
    assert restored["run_id"] == "100"
