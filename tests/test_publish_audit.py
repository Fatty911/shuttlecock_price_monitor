import json
import subprocess
import sys
from pathlib import Path

from shuttle_monitor.audit import (
    audit_envelope,
    build_file_manifest,
    product_quality_gate,
    verify_file_manifest,
)
from shuttle_monitor.contracts import build_envelope
from scripts.post_deploy_verify import compare_manifests


def test_product_gate_requires_a_verified_live_success_on_every_platform():
    statuses = [
        {
            "task_id": "taobao:yonex-as20",
            "platform": "taobao",
            "outcome": "success",
            "mode": "live",
            "detail_verified": True,
            "native_product_id": "10001",
            "price": 118.0,
            "product_url": "https://detail.tmall.com/item.htm?id=10001",
        },
        {
            "task_id": "jd:yonex-as20",
            "platform": "jd",
            "outcome": "blocked",
            "mode": "live",
        },
        {
            "task_id": "pdd:yonex-as20",
            "platform": "pdd",
            "outcome": "blocked",
            "mode": "live",
        },
    ]

    assert product_quality_gate(statuses) is False


def test_product_gate_passes_only_when_all_three_platforms_have_live_detail_evidence():
    statuses = [
        {
            "task_id": f"{platform}:yonex-as20",
            "platform": platform,
            "outcome": "success",
            "mode": "live",
            "detail_verified": True,
            "native_product_id": str(index),
            "price": 100.0 + index,
            "product_url": url,
        }
        for index, (platform, url) in enumerate(
            (
                ("taobao", "https://detail.tmall.com/item.htm?id=10001"),
                ("jd", "https://item.jd.com/10002.html"),
                ("pdd", "https://mobile.yangkeduo.com/goods.html?goods_id=10003"),
            ),
            start=1,
        )
    ]

    assert product_quality_gate(statuses) is True


def _blocked_envelope() -> tuple[dict, list[str]]:
    task_ids = [
        f"{platform}:model-{index}"
        for platform in ("taobao", "jd", "pdd")
        for index in range(23)
    ]
    statuses = [
        {
            "task_id": task_id,
            "platform": task_id.split(":", 1)[0],
            "outcome": "blocked",
            "attempts": 1,
            "started_at": "2026-07-30T00:00:00Z",
            "finished_at": "2026-07-30T00:00:01Z",
            "source_url": "https://search.example.invalid/",
            "final_url": "https://search.example.invalid/",
            "rejection_reason": "challenge",
            "evidence_hash": "a" * 64,
            "parser_version": "shuttle-v4",
        }
        for task_id in task_ids
    ]
    return (
        build_envelope(
            repo="shuttlecock_price_monitor",
            run_id="1",
            run_attempt="1",
            source_sha="b" * 40,
            config_sha256="c" * 64,
            started_at="2026-07-30T00:00:00Z",
            finished_at="2026-07-30T00:01:00Z",
            mode="live",
            baseline_batch_id=None,
            expected_tasks=69,
            statuses=statuses,
            prices=[],
            evidence_sha256="d" * 64,
            audit_status="blocked",
        ),
        task_ids,
    )


def test_audit_is_always_emitted_and_platform_zero_has_stable_fingerprint():
    envelope, task_ids = _blocked_envelope()
    first = audit_envelope(envelope, task_ids)
    second = audit_envelope(envelope, task_ids)
    assert first["structure_status"] == "pass"
    assert first["product_status"] == "blocked"
    assert {row["code"] for row in first["violations"]} >= {
        "platform_success_zero:taobao",
        "platform_success_zero:jd",
        "platform_success_zero:pdd",
    }
    assert first["fingerprint"] == second["fingerprint"]


def test_success_without_official_detail_identity_blocks_structure():
    envelope, task_ids = _blocked_envelope()
    row = envelope["statuses"][0]
    row.update(
        {
            "outcome": "success",
            "mode": "live",
            "price": 99.0,
            "product_url": "https://evil.example/item.htm?id=1",
            "native_product_id": "1",
            "detail_verified": False,
            "rejection_reason": None,
        }
    )
    envelope["prices"] = [{"task_id": row["task_id"]}]
    envelope["summary"].update({"success": 1, "blocked": 68})
    report = audit_envelope(envelope, task_ids)
    assert report["structure_status"] == "blocked"
    assert any(item["code"] == "success_evidence_invalid" for item in report["violations"])


def test_file_manifest_detects_payload_tampering(tmp_path):
    payload = tmp_path / "data"
    payload.mkdir()
    (payload / "status.json").write_text(json.dumps([{"ok": True}]), encoding="utf-8")
    manifest = build_file_manifest(tmp_path, ["data/status.json"], batch_id="batch-1", source_sha="e" * 40)
    assert verify_file_manifest(tmp_path, manifest) == []
    (payload / "status.json").write_text("[]", encoding="utf-8")
    assert verify_file_manifest(tmp_path, manifest) == ["data/status.json"]


def test_post_deploy_manifest_comparison_binds_batch_sha_and_files():
    expected = {
        "schema_version": 4,
        "batch_id": "batch-1",
        "source_sha": "a" * 40,
        "files": {"data/status.json": {"sha256": "b" * 64, "size": 2}},
    }
    assert compare_manifests(expected, dict(expected)) == []
    actual = {**expected, "source_sha": "c" * 40}
    assert compare_manifests(expected, actual) == ["source_sha"]


def test_fixture_output_cannot_pass_structure_or_product_cli_gates(tmp_path):
    root = Path(__file__).parents[1]
    site = tmp_path / "site"
    built = subprocess.run(
        [
            sys.executable,
            "-m",
            "shuttle_monitor.monitor",
            "--output",
            "--site-dir",
            str(site),
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, built.stderr
    for gate in ("--structure-gate", "--quality-gate"):
        checked = subprocess.run(
            [
                sys.executable,
                "-m",
                "shuttle_monitor.monitor",
                gate,
                "--site-dir",
                str(site),
            ],
            cwd=root,
            capture_output=True,
            text=True,
        )
        assert checked.returncode == 1
