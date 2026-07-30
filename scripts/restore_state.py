#!/usr/bin/env python3
"""Restore the newest safe state artifact from a prior main-branch run."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shuttle_monitor.state import InvalidStateArtifact, restore_state_archive


API = "https://api.github.com"
MAX_RESPONSE = 25 * 1024 * 1024


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and urlsplit(req.full_url).netloc != urlsplit(newurl).netloc:
            redirected.remove_header("Authorization")
        return redirected


def _request(url: str, token: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "shuttle-state-restore",
        },
    )
    with urllib.request.build_opener(SafeRedirectHandler()).open(request, timeout=60) as response:
        data = response.read(MAX_RESPONSE + 1)
    if len(data) > MAX_RESPONSE:
        raise OSError("GitHub artifact response exceeds size limit")
    return data


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--current-run-id", required=True)
    parser.add_argument("--config-sha256", required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--token", default=os.getenv("GH_TOKEN", ""))
    args = parser.parse_args()
    if not args.token:
        print("No GH_TOKEN; state restore skipped.")
        return 0
    payload = json.loads(
        _request(f"{API}/repos/{args.repo}/actions/artifacts?per_page=100", args.token)
    )
    artifacts = sorted(
        (
            item
            for item in payload.get("artifacts", [])
            if str(item.get("name", "")).startswith("state-")
            and not item.get("expired")
            and str((item.get("workflow_run") or {}).get("id")) != args.current_run_id
            and (item.get("workflow_run") or {}).get("head_branch") == args.branch
        ),
        key=lambda item: str(item.get("created_at", "")),
        reverse=True,
    )
    for artifact in artifacts:
        try:
            archive = _request(str(artifact["archive_download_url"]), args.token)
            metadata = restore_state_archive(
                archive,
                args.destination,
                repo="shuttlecock_price_monitor",
                branch=args.branch,
                current_run_id=args.current_run_id,
                config_sha256=args.config_sha256,
            )
        except (KeyError, OSError, InvalidStateArtifact, json.JSONDecodeError):
            continue
        print(f"Restored state from prior run {metadata['run_id']}.")
        return 0
    print("No semantically valid prior state artifact found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
