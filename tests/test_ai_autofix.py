"""Tests for the AI auto-fix classifier and repair runner guards."""

from pathlib import Path

import pytest

from scripts.classify_shuttle_failure import classify
from scripts.self_repair_runner import (
    MAX_DELETED_LINES,
    TRUST_ROOT_PATHS,
    count_deleted_lines,
    patch_touches_trust_roots,
)


def test_external_block_is_reported_never_fixed():
    text = (
        "product_gate failed\n"
        "violations: platform_success_zero:jd\n"
        "canary-skip blocked platform_unreachable\n"
        "no_same_card_candidate\n"
    )
    classification, reason, should_diagnose = classify(text, "failure")
    assert classification == "expected_external_block"
    assert should_diagnose is False
    assert "只告警不修码" in reason


def test_risk_handler_and_js_shell_are_external_block():
    text = "jd blocked risk_handler requests-direct\nbrowser_circuit_open\n"
    classification, _, should_diagnose = classify(text, "failure")
    assert classification == "expected_external_block"
    assert should_diagnose is False


def test_proxy_degradation_is_reported_never_fixed():
    text = "所有代理节点连通性测试失败\n可用节点: 0/136\n"
    classification, reason, should_diagnose = classify(text, "failure")
    assert classification == "expected_proxy_degraded"
    assert should_diagnose is False


def test_site_breakage_enters_repair_path():
    text = (
        "Traceback (most recent call last):\n"
        "  File \"shuttle_monitor/monitor.py\", line 1050, in parse_product_cards\n"
        "AttributeError: 'NoneType' object has no attribute 'find'\n"
    )
    classification, _, should_diagnose = classify(text, "failure")
    assert classification == "site_breakage"
    assert should_diagnose is True


def test_ci_breakage_enters_repair_path():
    text = "FAILED tests/test_monitor.py::test_parser_binds_title_price - AssertionError\n"
    classification, _, should_diagnose = classify(text, "failure")
    assert classification == "ci_breakage"
    assert should_diagnose is True


def test_success_is_expected():
    classification, _, should_diagnose = classify("all green", "success")
    assert classification == "expected_success"
    assert should_diagnose is False


def test_empty_log_is_unknown_but_diagnosable():
    classification, _, should_diagnose = classify("", "failure")
    assert classification == "unknown"
    assert should_diagnose is True


def test_unknown_keeps_conservative_diagnosis_only():
    text = "some weird message with no known markers"
    classification, _, should_diagnose = classify(text, "failure")
    assert classification == "unknown"
    assert should_diagnose is True


def test_trust_root_paths_are_complete():
    assert "AGENTS.md" in TRUST_ROOT_PATHS
    assert ".github/workflows/shuttle-monitor.yml" in TRUST_ROOT_PATHS
    assert "products.yaml" in TRUST_ROOT_PATHS
    assert "requirements.txt" in TRUST_ROOT_PATHS
    assert "scripts/codex_delivery_gate.py" in TRUST_ROOT_PATHS


def test_patch_touching_trust_root_is_rejected():
    patch = (
        "diff --git a/products.yaml b/products.yaml\n"
        "--- a/products.yaml\n"
        "+++ b/products.yaml\n"
        "@@ -1,3 +1,4 @@\n"
        "-config_revision: 4\n"
        "+config_revision: 5\n"
    )
    assert patch_touches_trust_roots(patch) == ["products.yaml"]


def test_patch_touching_workflow_is_rejected():
    patch = (
        "diff --git a/.github/workflows/shuttle-monitor.yml b/.github/workflows/shuttle-monitor.yml\n"
        "--- a/.github/workflows/shuttle-monitor.yml\n"
        "+++ b/.github/workflows/shuttle-monitor.yml\n"
        "@@ -1,3 +1,4 @@\n"
        "-name: Shuttlecock price monitor\n"
        "+name: Shuttlecock monitor\n"
    )
    assert patch_touches_trust_roots(patch) == [".github/workflows/shuttle-monitor.yml"]


def test_patch_not_touching_trust_root_is_allowed():
    patch = (
        "diff --git a/shuttle_monitor/monitor.py b/shuttle_monitor/monitor.py\n"
        "--- a/shuttle_monitor/monitor.py\n"
        "+++ b/shuttle_monitor/monitor.py\n"
        "@@ -100,3 +100,4 @@\n"
        "-        return candidates\n"
        "+        return candidates or []\n"
    )
    assert patch_touches_trust_roots(patch) == []


def test_deletion_guard_counts_only_hunk_deletions():
    patch = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1,3 +1,3 @@\n"
        "-old1\n"
        "-old2\n"
        "+new1\n"
        "-old3\n"
    )
    assert count_deleted_lines(patch) == 3
    assert MAX_DELETED_LINES >= 3


def test_classifier_importable_without_llm():
    """The classifier must never import requests/opencode; it is pure regex."""
    import ast

    source = Path("scripts/classify_shuttle_failure.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
    assert "requests" not in imports
    assert "opencode" not in imports


def test_login_substring_does_not_false_positive():
    """login 正则必须精确匹配登录页，不能误判 login_handler 等代码符号。"""
    text = "login_handler called with invalid token"
    classification, _, should_diagnose = classify(text, "failure")
    assert classification == "unknown"  # 无外部阻断特征 → 保守诊断


def test_login_page_phrase_is_external_block():
    text = "jd blocked 请登录后继续 requests-direct\n"
    classification, _, should_diagnose = classify(text, "failure")
    assert classification == "expected_external_block"
    assert should_diagnose is False


def test_commit_trailers_use_actual_verdicts(tmp_path, monkeypatch):
    """trailers 必须用评审模型实际 verdict，禁止硬编码 PASS。"""
    import subprocess
    from scripts import self_repair_runner as sr

    repo = tmp_path / "wt"
    repo.mkdir()
    env = dict(__import__("os").environ)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = __import__("os").devnull
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("hello" + chr(10), encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "init"], cwd=repo, env=env, check=True)
    (repo / "f.txt").write_text("hello" + chr(10) + "world" + chr(10), encoding="utf-8")

    reviews = [
        {"provider": "nvidia-nim-kimi", "model": "moonshotai/kimi-k2.6", "verdict": "PASS", "reason": "ok"},
        {"provider": "nvidia-nim-glm", "model": "z-ai/glm-5.2", "verdict": "PASS", "reason": "ok"},
    ]
    monkeypatch.setenv("COMMIT_AUTHOR_NAME", "opencode-kimi-k3")
    monkeypatch.setenv("COMMIT_AUTHOR_EMAIL", "xuerui911@gmail.com")
    assert sr.commit_with_trailers(repo, "a" * 64, reviews, "test msg")
    log = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=repo, capture_output=True, text=True).stdout
    assert "Review-Result-1: PASS" in log
    assert "Review-Result-2: PASS" in log
    assert "Reviewed-Diff-SHA256: " + "a" * 64 in log
    author = subprocess.run(["git", "log", "-1", "--format=%an <%ae>"], cwd=repo, capture_output=True, text=True).stdout
    assert author.strip() == "opencode-kimi-k3 <xuerui911@gmail.com>"


def test_patch_touching_githooks_prefix_is_rejected():
    patch = (
        "diff --git a/.githooks/pre-commit b/.githooks/pre-commit\n"
        "--- a/.githooks/pre-commit\n"
        "+++ b/.githooks/pre-commit\n"
        "@@ -1,2 +1,3 @@\n"
        "-#!/bin/bash\n"
        "+#!/bin/bash\n"
        "+echo pwned\n"
    )
    assert patch_touches_trust_roots(patch) == [".githooks/pre-commit"]


def test_read_logs_caps_large_files(tmp_path):
    from scripts.classify_shuttle_failure import MAX_LOG_BYTES, read_logs

    big = tmp_path / "big.log"
    big.write_bytes(b"x" * (MAX_LOG_BYTES + 1000) + b"TAIL-MARKER")
    text = read_logs([str(big)])
    assert "TAIL-MARKER" in text
    assert len(text) <= MAX_LOG_BYTES + 100
