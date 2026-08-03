from pathlib import Path
import re

import yaml


WORKFLOW = Path(__file__).parents[1] / ".github/workflows/shuttle-monitor.yml"
CI = Path(__file__).parents[1] / ".github/workflows/ci.yml"
THIRD_PARTY_ALLOWLIST = {"softprops/action-gh-release"}


def _uses(workflow: dict) -> list[str]:
    return [
        step["uses"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if "uses" in step
    ]


def test_workflow_has_separate_ordered_build_deploy_verify_gate_alert_release_jobs():
    raw = WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.safe_load(raw)
    jobs = workflow["jobs"]
    assert {"build", "deploy", "post_verify", "product_gate", "alert", "release"} <= jobs.keys()
    assert jobs["deploy"]["needs"] == "build"
    assert jobs["post_verify"]["needs"] == "deploy"
    assert jobs["product_gate"]["needs"] == "post_verify"
    assert "product_gate" in jobs["alert"]["needs"]
    assert jobs["release"]["needs"] == "product_gate"
    assert jobs["deploy"]["environment"]["name"] == "github-pages"
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert "*/30 * * * *" in raw
    assert "workflow_run" in raw


def test_build_restores_state_tests_early_artifacts_then_structural_gate_and_pages():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["build"]["steps"]
    names = [step["name"] for step in steps]
    assert names.index("Offline contract tests") < names.index("Restore prior state")
    assert names.index("Restore prior state") < names.index("Build complete live round")
    assert names.index("Build complete live round") < names.index("Upload early raw evidence")
    assert names.index("Upload early state") < names.index("Structural publish gate")
    assert names.index("Structural publish gate") < names.index("Upload Pages artifact")
    raw = WORKFLOW.read_text(encoding="utf-8")
    assert "github.run_id" in raw and "github.run_attempt" in raw and "github.sha" in raw
    assert "github.event.workflow_run.head_sha || github.sha" in raw
    assert "ref: ${{ env.SOURCE_SHA }}" in raw
    assert "raw-evidence-" in raw and "state-" in raw and "pages-payload-" in raw
    assert "--live --output" in raw
    assert 'title="monitor-blocked:$fingerprint"' in raw
    assert "gh issue close" in raw
    configure = next(
        step for step in steps if step.get("name", "").startswith("Configure Pages")
    )
    assert configure["continue-on-error"] is True


def test_action_refs_follow_official_major_and_maintained_third_party_allowlist():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for ref in _uses(workflow):
        action, version = ref.rsplit("@", 1)
        if action.startswith("actions/"):
            assert re.fullmatch(r"v[1-9]\d*", version), ref
        else:
            assert action in THIRD_PARTY_ALLOWLIST, ref
            assert version in {"main", "master"}, ref


def test_job_permissions_are_minimal_and_ci_is_read_only():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert workflow["jobs"]["build"]["permissions"] == {
        "contents": "read",
        "actions": "read",
        "id-token": "write",
        "attestations": "write",
    }
    assert workflow["jobs"]["deploy"]["permissions"] == {
        "contents": "read",
        "pages": "write",
        "id-token": "write",
    }
    assert workflow["jobs"]["alert"]["permissions"] == {
        "contents": "read",
        "actions": "read",
        "issues": "write",
    }
    ci = yaml.safe_load(CI.read_text(encoding="utf-8"))
    assert ci["permissions"] == {"contents": "read"}
