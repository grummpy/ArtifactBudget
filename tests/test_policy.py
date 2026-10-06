"""Exact-id protection matching."""

from artifactbudget.importer import load_snapshot
from artifactbudget.policy import apply_policy

from support import gh_artifact, repo_entry, write_manifest, write_page


def _snapshot(tmp_path):
    write_page(
        tmp_path,
        "a.json",
        [gh_artifact(5, "release-evidence", 10, "2026-10-01T00:00:00Z", "2026-10-20T00:00:00Z")],
        1,
    )
    write_page(
        tmp_path,
        "b.json",
        [gh_artifact(5, "other", 3, "2026-10-01T00:00:00Z", "2026-10-20T00:00:00Z")],
        1,
    )
    manifest = write_manifest(
        tmp_path,
        [repo_entry("demo/alpha", ["a.json"]), repo_entry("demo/beta", ["b.json"])],
    )
    return load_snapshot(manifest)


def test_exact_match_does_not_cross_repositories_or_names(tmp_path):
    snapshot = _snapshot(tmp_path)
    policy = tmp_path / "policy.json"
    policy.write_text(
        """
        {"schema_version": 1, "protected_artifacts": [
          {"repository": "demo/alpha", "artifact_id": 5, "reason": "Release evidence"}
        ]}
        """,
        encoding="utf-8",
    )
    applied = apply_policy(snapshot, policy)
    reasons = {(item.repository, item.artifact_id): item.protection_reason for item in applied.artifacts}
    assert reasons[("demo/alpha", 5)] == "Release evidence"
    assert reasons[("demo/beta", 5)] is None
    assert applied.ok


def test_unknown_policy_target_warns(tmp_path):
    snapshot = _snapshot(tmp_path)
    policy = tmp_path / "policy.json"
    policy.write_text(
        """
        {"schema_version": 1, "protected_artifacts": [
          {"repository": "demo/alpha", "artifact_id": 99, "reason": "Missing"}
        ]}
        """,
        encoding="utf-8",
    )
    applied = apply_policy(snapshot, policy)
    assert applied.ok
    assert all(item.protection_reason is None for item in applied.artifacts)
    assert any(item.code == "unknown_policy_target" for item in applied.diagnostics)


def test_missing_policy_file_is_an_error(tmp_path):
    snapshot = _snapshot(tmp_path)
    applied = apply_policy(snapshot, tmp_path / "missing.json")
    assert not applied.ok
    assert any(item.code == "policy_unreadable" for item in applied.errors)


def test_conflicting_policy_reasons_apply_neither(tmp_path):
    snapshot = _snapshot(tmp_path)
    policy = tmp_path / "policy.json"
    policy.write_text(
        """
        {"schema_version": 1, "protected_artifacts": [
          {"repository": "demo/alpha", "artifact_id": 5, "reason": "One"},
          {"repository": "demo/alpha", "artifact_id": 5, "reason": "Two"},
          {"repository": "demo/alpha", "artifact_id": 5, "reason": "Three"}
        ]}
        """,
        encoding="utf-8",
    )
    applied = apply_policy(snapshot, policy)
    assert not applied.ok
    assert all(item.protection_reason is None for item in applied.artifacts)
    assert any(item.code == "conflicting_policy" for item in applied.errors)
