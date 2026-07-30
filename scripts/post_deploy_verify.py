#!/usr/bin/env python3
"""Verify the public Pages payload through normal TLS against the built manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin


MAX_FILE_BYTES = 25 * 1024 * 1024


def compare_manifests(expected: dict, actual: dict) -> list[str]:
    return [
        field
        for field in ("schema_version", "batch_id", "source_sha", "mode", "run_id", "run_attempt", "files")
        if expected.get(field) != actual.get(field)
    ]


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "pages-post-deploy-verify"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise OSError("public file exceeds size limit")
    return data


def verify(base_url: str, expected: dict) -> None:
    actual = json.loads(_get(urljoin(base_url.rstrip("/") + "/", "manifest.json")))
    mismatches = compare_manifests(expected, actual)
    if mismatches:
        raise ValueError(f"public manifest mismatch: {','.join(mismatches)}")
    for name, metadata in expected["files"].items():
        path = PurePosixPath(str(name))
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe manifest path: {name}")
        digest = hashlib.sha256(_get(urljoin(base_url.rstrip("/") + "/", str(path)))).hexdigest()
        if digest != metadata["sha256"]:
            raise ValueError(f"public file hash mismatch: {name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--expected", required=True)
    parser.add_argument("--attempts", type=int, default=6)
    args = parser.parse_args()
    expected = json.loads(Path(args.expected).read_text(encoding="utf-8"))
    error: Exception | None = None
    for attempt in range(args.attempts):
        try:
            verify(args.base_url, expected)
            print(f"POST_DEPLOY_VERIFIED batch={expected['batch_id']} source_sha={expected['source_sha']}")
            return 0
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            error = exc
            if attempt + 1 < args.attempts:
                time.sleep(10)
    print(f"POST_DEPLOY_FAILED {type(error).__name__}: {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
