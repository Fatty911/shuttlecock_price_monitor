#!/usr/bin/env python3
"""Auditable staging and direct-main delivery gate; no commit is performed here."""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUTHORIZED_PATHS = {
    ".github/workflows/shuttle-monitor.yml",
    ".github/workflows/ci.yml",
    ".githooks/post-commit",
    "AGENTS.md",
    "README.md",
    "requirements.txt",
    "products.yaml",
    "shuttle_monitor/monitor.py",
    "shuttle_monitor/contracts.py",
    "shuttle_monitor/state.py",
    "shuttle_monitor/audit.py",
    "scripts/generate_clash_config.py",
    "scripts/codex_delivery_gate.py",
    "scripts/restore_state.py",
    "scripts/post_deploy_verify.py",
    "custom_scripts/setup_proxy_runtime.py",
    "web/index.html",
    "web/app.js",
    "web/styles.css",
    "tests/test_monitor.py",
    "tests/test_workflow.py",
    "tests/test_contracts.py",
    "tests/test_state_restore.py",
    "tests/test_publish_audit.py",
    "tests/test_pages.py",
    "tests/test_delivery_gate.py",
    "tests/test_setup_proxy_runtime.py",
    "docs/operations.md",
    "docs/schema.md",
}
AUTHORIZED_PREFIXES = ("tests/fixtures/",)
GENERATED_PREFIXES = ("site/", "out/", "state/", "raw/", ".cache/",)


def validate_paths(paths: list[str]) -> list[str]:
    bad: list[str] = []
    for raw in paths:
        path = raw.replace("\\", "/").removeprefix("./")
        if (
            path.startswith(GENERATED_PREFIXES)
            or (
                path not in AUTHORIZED_PATHS
                and not path.startswith(AUTHORIZED_PREFIXES)
            )
        ):
            bad.append(path)
    return sorted(set(bad))


def _git_paths(*args: str) -> list[str]:
    result = subprocess.run(
        ["git", *args, "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return sorted(
        part.decode("utf-8", "surrogateescape").replace("\\", "/")
        for part in result.stdout.split(b"\0")
        if part
    )


def dirty_paths() -> list[str]:
    return sorted(
        set(
            _git_paths("diff", "--name-only", "--no-renames")
            + _git_paths("diff", "--cached", "--name-only", "--no-renames")
            + _git_paths("ls-files", "--others", "--exclude-standard")
        )
    )


def staged_diff_sha256() -> str:
    result = subprocess.run(
        ["git", "diff", "--cached", "--binary", "--no-ext-diff"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return hashlib.sha256(result.stdout).hexdigest()


def validate_review_trailers(trailers: dict[str, str], diff_sha256: str) -> list[str]:
    failures: list[str] = []
    first = trailers.get("Review-Model-Family-1", "").strip()
    second = trailers.get("Review-Model-Family-2", "").strip()
    if not first or not second or first.casefold() == second.casefold():
        failures.append("review model families must be present and distinct")
    if trailers.get("Review-Result-1", "").strip().upper() != "PASS":
        failures.append("review result 1 must be PASS")
    if trailers.get("Review-Result-2", "").strip().upper() != "PASS":
        failures.append("review result 2 must be PASS")
    reviewed = trailers.get("Reviewed-Diff-SHA256", "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", reviewed) or reviewed != diff_sha256:
        failures.append("reviewed diff SHA-256 must match the committed diff")
    return failures


def _verify_commit() -> int:
    changed = _git_paths("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD")
    bad = validate_paths(changed)
    if bad:
        print("DELIVERY_GATE_FAILED unauthorized committed paths:")
        for path in bad:
            print(path)
        return 1
    diff = subprocess.run(
        ["git", "diff", "HEAD^", "HEAD", "--binary", "--no-ext-diff"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    diff_sha = hashlib.sha256(diff).hexdigest()
    raw_trailers = subprocess.run(
        ["git", "log", "-1", "--format=%B"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    trailers = {
        key.strip(): value.strip()
        for line in raw_trailers.splitlines()
        if ":" in line
        for key, value in [line.split(":", 1)]
    }
    failures = validate_review_trailers(trailers, diff_sha)
    if failures:
        print("DELIVERY_GATE_FAILED commit review evidence:")
        for failure in failures:
            print(failure)
        return 1
    print(f"DELIVERY_GATE_COMMIT_VERIFIED sha256={diff_sha}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("check", "stage", "verify-commit", "verify"))
    args = parser.parse_args()
    if args.command in {"check", "stage"}:
        paths = dirty_paths()
        bad = validate_paths(paths)
        if bad:
            print("DELIVERY_GATE_FAILED unauthorized paths:")
            for path in bad:
                print(path)
            return 1
        if args.command == "stage":
            for path in paths:
                subprocess.run(["git", "add", "-A", "--", path], cwd=ROOT, check=True)
        print(f"DELIVERY_GATE_CANDIDATE paths={len(paths)} sha256={staged_diff_sha256()}")
        return 0
    if args.command == "verify-commit":
        return _verify_commit()
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()
    remote = subprocess.run(
        ["git", "ls-remote", "origin", "refs/heads/main"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    if not remote or remote[0] != head:
        print(f"DELIVERY_GATE_FAILED local={head} remote={remote[0] if remote else 'missing'}")
        return 1
    print(f"DELIVERY_GATE_VERIFIED {head}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
