"""TDD test: setup_proxy_runtime.py must be importable and --help must exit 0."""
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from custom_scripts.setup_proxy_runtime import (
    MIHOMO_ASSET_SHA256,
    MIHOMO_ASSET_URL,
    verify_mihomo_archive,
    write_runtime_files,
    parse_proxy_secret,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_setup_proxy_runtime_help():
    """Running setup_proxy_runtime.py --help from repo root must succeed."""
    result = subprocess.run(
        [sys.executable, "custom_scripts/setup_proxy_runtime.py", "--help"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=30,
    )
    msg = (
        "Expected exit 0, got %d\n"
        "STDOUT:\n%s\n"
        "STDERR:\n%s"
    ) % (result.returncode, result.stdout, result.stderr)
    assert result.returncode == 0, msg


def test_mihomo_is_version_platform_and_checksum_pinned():
    assert "/v1.19.29/" in MIHOMO_ASSET_URL
    assert "linux-amd64-compatible-v1.19.29.gz" in MIHOMO_ASSET_URL
    assert MIHOMO_ASSET_SHA256 == "5612e698e96c8b8ad15abc4c0a4f098eba9234354b4f248cb97f2528e215b094"
    with pytest.raises(ValueError, match="checksum"):
        verify_mihomo_archive(b"not-the-pinned-archive")


def test_runtime_files_never_persist_subscription_urls(tmp_path):
    secret = "https://subscription.example/private?token=secret"
    proxy_config = tmp_path / "runtime.json"
    clash_config = tmp_path / "mihomo" / "config.yaml"
    write_runtime_files(
        proxy_config,
        clash_config,
        [secret],
        [],
        [{"name": "node", "type": "ss", "server": "127.0.0.1", "port": 443, "cipher": "x", "password": "y"}],
    )
    assert secret not in proxy_config.read_text(encoding="utf-8")


def test_yaml_serialization_treats_malicious_node_name_as_data(tmp_path):
    proxy_config = tmp_path / "runtime.json"
    clash_config = tmp_path / "mihomo" / "config.yaml"
    malicious = 'node\nexternal-controller: "0.0.0.0:9999"'
    write_runtime_files(
        proxy_config,
        clash_config,
        [],
        [],
        [{"name": malicious, "type": "ss", "server": "127.0.0.1", "port": 443, "cipher": "x", "password": "y"}],
    )
    parsed = yaml.safe_load(clash_config.read_text(encoding="utf-8"))
    assert parsed["proxies"][0]["name"] == malicious
    assert parsed["external-controller"] == "127.0.0.1:9090"
    assert parsed["log-level"] == "silent"


def test_invalid_subscription_secret_is_never_echoed(capsys):
    secret = "not-a-url-token=super-secret"
    assert parse_proxy_secret(secret) == ([], [])
    assert secret not in capsys.readouterr().out


def test_no_self_hosted_fallback_landing_nodes():
    """爬虫出口只允许机场订阅节点：源码与 workflow 不得引用自建 VPS 代理（如 DMIT）。"""
    source = (REPO_ROOT / "custom_scripts" / "setup_proxy_runtime.py").read_text(encoding="utf-8")
    assert "DMIT" not in source
    workflow = (REPO_ROOT / ".github" / "workflows" / "shuttle-monitor.yml").read_text(encoding="utf-8")
    assert "DMIT" not in workflow
