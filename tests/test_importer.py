"""Importer validation, dedupe, completeness, and path confinement."""

from __future__ import annotations

import json

import pytest

from artifactbudget.importer import load_snapshot
from artifactbudget.models import ArtifactBudgetError

from support import gh_artifact, repo_entry, write_manifest, write_page


def test_timezone_and_leap_day_conversion(tmp_path):
    write_page(
        tmp_path,
        "page.json",
        [
            gh_artifact(
                1,
                "leap",
                10,
                "2024-02-29T04:00:00+04:00",
                "2024-03-01T00:00:00Z",
            )
        ],
        1,
    )
    manifest = write_manifest(
        tmp_path,
        [repo_entry("demo/alpha", ["page.json"], snapshot_at="2024-03-01T00:00:00Z")],
        as_of="2024-03-01T00:00:00Z",
    )
    snapshot = load_snapshot(manifest)
    assert snapshot.ok
    artifact = snapshot.artifacts[0]
    assert artifact.created_at.isoformat() == "2024-02-29T00:00:00+00:00"
    assert artifact.expires_at.isoformat() == "2024-03-01T00:00:00+00:00"


def test_invalid_calendar_day_is_rejected(tmp_path):
    write_page(
        tmp_path,
        "page.json",
        [gh_artifact(1, "bad", 1, "2023-02-29T00:00:00Z", "2023-03-02T00:00:00Z")],
        1,
    )
    manifest = write_manifest(tmp_path, [repo_entry("demo/alpha", ["page.json"])])
    snapshot = load_snapshot(manifest)
    assert not snapshot.ok
    assert snapshot.artifacts == ()
    assert any(item.code == "record_rejected" for item in snapshot.errors)


def test_negative_size_invalid_id_and_created_after_expiry(tmp_path):
    write_page(
        tmp_path,
        "page.json",
        [
            gh_artifact(1, "neg", -1, "2026-10-01T00:00:00Z", "2026-10-20T00:00:00Z"),
            gh_artifact(0, "zero-id", 5, "2026-10-01T00:00:00Z", "2026-10-20T00:00:00Z"),
            {
                "id": 1.0,
                "name": "float-id",
                "size_in_bytes": 5,
                "expired": False,
                "created_at": "2026-10-01T00:00:00Z",
                "expires_at": "2026-10-20T00:00:00Z",
            },
            {
                "id": "4",
                "name": "string-id",
                "size_in_bytes": 5,
                "expired": False,
                "created_at": "2026-10-01T00:00:00Z",
                "expires_at": "2026-10-20T00:00:00Z",
            },
            gh_artifact(5, "backwards", 5, "2026-10-20T00:00:00Z", "2026-10-01T00:00:00Z"),
            gh_artifact(6, "naive", 5, "2026-10-01T00:00:00", "2026-10-20T00:00:00Z"),
            gh_artifact(7, "ok-zero", 0, "2026-10-01T00:00:00Z", "2026-10-20T00:00:00Z"),
            gh_artifact(8, "unknown", 12, "2026-10-01T00:00:00Z", None),
        ],
        8,
    )
    manifest = write_manifest(tmp_path, [repo_entry("demo/alpha", ["page.json"])])
    snapshot = load_snapshot(manifest)
    assert not snapshot.ok
    kept = {artifact.artifact_id: artifact for artifact in snapshot.artifacts}
    assert set(kept) == {7, 8}
    assert kept[7].size_bytes == 0
    assert kept[8].expires_at is None
    assert sum(1 for item in snapshot.errors if item.code == "record_rejected") == 6


def test_empty_inventory(tmp_path):
    write_page(tmp_path, "page.json", [], 0)
    manifest = write_manifest(tmp_path, [repo_entry("demo/alpha", ["page.json"])])
    snapshot = load_snapshot(manifest)
    assert snapshot.ok
    assert snapshot.artifacts == ()


def test_export_aliases_are_rejected_by_canonical_identity(tmp_path):
    write_page(tmp_path, "page.json", [], 0)
    manifest = write_manifest(
        tmp_path, [repo_entry("demo/alpha", ["page.json", "./page.json"])]
    )
    snapshot = load_snapshot(manifest)
    assert not snapshot.ok
    assert len(snapshot.files) == 1
    assert any(item.code == "duplicate_file" for item in snapshot.errors)


def test_export_symlink_alias_is_rejected_by_canonical_identity(tmp_path):
    write_page(tmp_path, "page.json", [], 0)
    (tmp_path / "page-alias.json").symlink_to(tmp_path / "page.json")
    manifest = write_manifest(
        tmp_path, [repo_entry("demo/alpha", ["page.json", "page-alias.json"])]
    )
    snapshot = load_snapshot(manifest)
    assert not snapshot.ok
    assert len(snapshot.files) == 1
    assert any(item.code == "duplicate_file" for item in snapshot.errors)


def test_identical_overlap_and_conflict(tmp_path):
    row = gh_artifact(1, "same", 10, "2026-10-01T00:00:00Z", "2026-10-20T00:00:00Z")
    other = gh_artifact(1, "same", 11, "2026-10-01T00:00:00Z", "2026-10-20T00:00:00Z")
    write_page(tmp_path, "a.json", [row], 1)
    write_page(tmp_path, "b.json", [row], 1)
    manifest = write_manifest(tmp_path, [repo_entry("demo/alpha", ["a.json", "b.json"])])
    overlap = load_snapshot(manifest)
    assert overlap.ok
    assert len(overlap.artifacts) == 1
    assert overlap.artifacts[0].source_file == "a.json"
    assert any(item.code == "identical_duplicate" for item in overlap.diagnostics)
    assert any(item.code == "total_count_match" for item in overlap.diagnostics)

    write_page(tmp_path, "c.json", [other], 1)
    conflict_manifest = write_manifest(tmp_path, [repo_entry("demo/beta", ["a.json", "c.json"])])
    # a.json is already used by the previous manifest, so use a fresh directory.
    fresh = tmp_path / "conflict"
    fresh.mkdir()
    write_page(fresh, "a.json", [row], 1)
    write_page(fresh, "c.json", [other], 1)
    conflict_manifest = write_manifest(fresh, [repo_entry("demo/beta", ["a.json", "c.json"])])
    conflict = load_snapshot(conflict_manifest)
    assert conflict.artifacts == ()
    assert any(item.code == "conflicting_duplicate" for item in conflict.errors)


def test_same_artifact_id_in_two_repositories(tmp_path):
    row = gh_artifact(7, "shared", 4, "2026-10-01T00:00:00Z", "2026-10-20T00:00:00Z")
    write_page(tmp_path, "a.json", [row], 1)
    write_page(tmp_path, "b.json", [dict(row, name="other")], 1)
    manifest = write_manifest(
        tmp_path,
        [
            repo_entry("demo/alpha", ["a.json"]),
            repo_entry("demo/beta", ["b.json"]),
        ],
    )
    snapshot = load_snapshot(manifest)
    assert snapshot.ok
    assert sorted(artifact.repository for artifact in snapshot.artifacts) == ["demo/alpha", "demo/beta"]
    assert {artifact.artifact_id for artifact in snapshot.artifacts} == {7}


def test_partial_total_count_and_snapshot_span(tmp_path):
    write_page(
        tmp_path,
        "partial.json",
        [gh_artifact(1, "only", 3, "2026-10-01T00:00:00Z", "2026-10-20T00:00:00Z")],
        4,
    )
    write_page(
        tmp_path,
        "full.json",
        [gh_artifact(2, "full", 3, "2026-10-01T00:00:00Z", "2026-10-20T00:00:00Z")],
        1,
    )
    manifest = write_manifest(
        tmp_path,
        [
            repo_entry("demo/partial", ["partial.json"], complete=False, snapshot_at="2026-10-04T00:00:00Z"),
            repo_entry("demo/full", ["full.json"], snapshot_at="2026-10-06T00:00:00Z"),
        ],
    )
    snapshot = load_snapshot(manifest)
    codes = {item.code for item in snapshot.diagnostics}
    assert "partial_repository" in codes
    assert "total_count_mismatch" in codes
    assert "snapshot_span" in codes
    assert "total_count_match" in codes
    assert snapshot.ok


def test_exact_24_hour_span_is_not_a_warning(tmp_path):
    row = gh_artifact(1, "a", 1, "2026-10-01T00:00:00Z", "2026-10-20T00:00:00Z")
    write_page(tmp_path, "a.json", [row], 1)
    write_page(tmp_path, "b.json", [dict(row, id=2)], 1)
    manifest = write_manifest(
        tmp_path,
        [
            repo_entry("demo/alpha", ["a.json"], snapshot_at="2026-10-05T00:00:00Z"),
            repo_entry("demo/beta", ["b.json"], snapshot_at="2026-10-06T00:00:00Z"),
        ],
    )
    snapshot = load_snapshot(manifest)
    assert not any(item.code == "snapshot_span" for item in snapshot.diagnostics)


def test_inconsistent_total_count(tmp_path):
    row = gh_artifact(1, "a", 1, "2026-10-01T00:00:00Z", "2026-10-20T00:00:00Z")
    write_page(tmp_path, "a.json", [row], 2)
    write_page(tmp_path, "b.json", [], 3)
    manifest = write_manifest(tmp_path, [repo_entry("demo/alpha", ["a.json", "b.json"])])
    snapshot = load_snapshot(manifest)
    assert any(item.code == "total_count_inconsistent" for item in snapshot.diagnostics)


def test_invalid_json_and_missing_file(tmp_path):
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    manifest = write_manifest(
        tmp_path,
        [repo_entry("demo/alpha", ["broken.json", "missing.json"])],
    )
    snapshot = load_snapshot(manifest)
    codes = {item.code for item in snapshot.errors}
    assert "invalid_json" in codes
    assert "missing_file" in codes
    assert snapshot.artifacts == ()


def test_manifest_json_errors(tmp_path):
    bad = tmp_path / "manifest.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(ArtifactBudgetError, match="not valid JSON"):
        load_snapshot(bad)
    missing = tmp_path / "nope.json"
    with pytest.raises(ArtifactBudgetError, match="Could not read manifest"):
        load_snapshot(missing)


def test_path_and_symlink_escape_are_not_read(tmp_path):
    secret = tmp_path / "secret.json"
    secret.write_text(
        json.dumps(
            {
                "total_count": 1,
                "artifacts": [
                    gh_artifact(1, "SHOULD_NOT_APPEAR", 9, "2026-10-01T00:00:00Z", "2026-10-20T00:00:00Z")
                ],
            }
        ),
        encoding="utf-8",
    )
    manifest_dir = tmp_path / "exports"
    manifest_dir.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text(secret.read_text(encoding="utf-8"), encoding="utf-8")
    (manifest_dir / "link.json").symlink_to(outside)
    manifest = write_manifest(
        manifest_dir,
        [
            repo_entry(
                "demo/alpha",
                ["../secret.json", str(secret), "link.json"],
            )
        ],
    )
    snapshot = load_snapshot(manifest)
    rendered = " ".join(item.message for item in snapshot.diagnostics)
    assert snapshot.artifacts == ()
    assert "SHOULD_NOT_APPEAR" not in rendered
    assert all(source.sha256 is None for source in snapshot.files)
    assert any(item.code == "path_rejected" for item in snapshot.errors)


def test_html_name_is_preserved_not_inferred(tmp_path):
    write_page(
        tmp_path,
        "page.json",
        [gh_artifact(1, "<b>release-evidence</b>", 4, "2026-10-01T00:00:00Z", "2026-10-20T00:00:00Z")],
        1,
    )
    manifest = write_manifest(tmp_path, [repo_entry("demo/alpha", ["page.json"])])
    snapshot = load_snapshot(manifest)
    assert snapshot.artifacts[0].name == "<b>release-evidence</b>"
    assert snapshot.artifacts[0].protection_reason is None
