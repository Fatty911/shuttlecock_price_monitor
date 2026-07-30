from pathlib import Path

from scripts.codex_delivery_gate import (
    AUTHORIZED_PATHS,
    validate_paths,
    validate_review_trailers,
)


ROOT = Path(__file__).parents[1]


def test_delivery_gate_rejects_generated_and_out_of_scope_paths():
    assert validate_paths(["shuttle_monitor/monitor.py", "tests/test_monitor.py"]) == []
    assert validate_paths(["site/data/status.json", "out/batch.json", "secrets.txt"]) == [
        "out/batch.json",
        "secrets.txt",
        "site/data/status.json",
    ]
    assert "AGENTS.md" in AUTHORIZED_PATHS


def test_post_commit_hook_is_non_force_direct_main_and_has_no_bypass():
    hook = (ROOT / ".githooks/post-commit").read_text(encoding="utf-8")
    assert "HEAD:main" in hook
    assert "--force" not in hook
    assert "--no-verify" not in hook
    assert "codex_delivery_gate.py" in hook
    assert "verify-commit" in hook


def test_agents_requires_exact_two_of_two_distinct_model_families():
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "N=2" in agents and "M=2" in agents
    assert "不同模型家族" in agents


def test_commit_review_trailers_require_two_distinct_pass_families_and_diff_hash():
    valid = {
        "Review-Model-Family-1": "family-a",
        "Review-Result-1": "PASS",
        "Review-Model-Family-2": "family-b",
        "Review-Result-2": "PASS",
        "Reviewed-Diff-SHA256": "a" * 64,
    }
    assert validate_review_trailers(valid, "a" * 64) == []
    invalid = {**valid, "Review-Model-Family-2": "family-a"}
    assert "families" in " ".join(validate_review_trailers(invalid, "a" * 64))
