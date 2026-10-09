"""HTML escaping, CSV neutralization, totals, and deterministic rendering."""

from __future__ import annotations

import csv
import hashlib
import json
import stat
from pathlib import Path

import pytest

from artifactbudget import __version__
from artifactbudget.forecast import GIB, evaluate
from artifactbudget.importer import load_snapshot
from artifactbudget.policy import apply_policy
from artifactbudget.report import (
    OUTPUT_NAMES,
    build_documents,
    format_decimal_gb,
    format_gib,
    neutralize,
    render_html,
    write_report,
)
from support import gh_artifact, repo_entry, write_manifest, write_page


def test_write_report_refuses_planted_predictable_temp_symlink(tmp_path):
    documents = {name: "fresh" for name in OUTPUT_NAMES}
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    # The historical predictable name must not be opened or followed.
    (tmp_path / ".report.json.tmp").symlink_to(sentinel)
    write_report(tmp_path, documents, overwrite=True)
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert (tmp_path / "report.json").read_text(encoding="utf-8") == "fresh"


def test_write_report_staging_failure_preserves_existing_reports(tmp_path, monkeypatch):
    original = {name: f"old {name}" for name in OUTPUT_NAMES}
    replacement = {name: f"new {name}" for name in OUTPUT_NAMES}
    for name, content in original.items():
        (tmp_path / name).write_text(content, encoding="utf-8")

    from artifactbudget import report

    real_mkstemp = report.tempfile.mkstemp
    calls = 0

    def interrupted_mkstemp(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated interrupted staging")
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(report.tempfile, "mkstemp", interrupted_mkstemp)
    with pytest.raises(OSError, match="interrupted staging"):
        write_report(tmp_path, replacement, overwrite=True)

    assert {name: (tmp_path / name).read_text(encoding="utf-8") for name in OUTPUT_NAMES} == original
    assert not list(tmp_path.glob(".*.tmp"))


def test_write_report_replacement_failure_rolls_back_existing_reports(tmp_path, monkeypatch):
    original = {name: f"old {name}" for name in OUTPUT_NAMES}
    replacement = {name: f"new {name}" for name in OUTPUT_NAMES}
    for name, content in original.items():
        (tmp_path / name).write_text(content, encoding="utf-8")

    from artifactbudget import report

    real_replace = report.os.replace
    replacements = 0

    def interrupted_replace(source, target):
        nonlocal replacements
        if Path(target).name in OUTPUT_NAMES and Path(source).suffix == ".tmp":
            replacements += 1
            if replacements == 2:
                raise OSError("simulated replacement interruption")
        return real_replace(source, target)

    monkeypatch.setattr(report.os, "replace", interrupted_replace)
    with pytest.raises(OSError, match="replacement interruption"):
        write_report(tmp_path, replacement, overwrite=True)

    assert {name: (tmp_path / name).read_text(encoding="utf-8") for name in OUTPUT_NAMES} == original
    assert not list(tmp_path.glob(".*.tmp"))
    assert not list(tmp_path.glob(".*.bak"))


def test_write_report_preserves_existing_permissions_on_overwrite(tmp_path):
    original = {name: f"old {name}" for name in OUTPUT_NAMES}
    replacement = {name: f"new {name}" for name in OUTPUT_NAMES}
    expected_modes = {}
    for index, (name, content) in enumerate(original.items()):
        target = tmp_path / name
        target.write_text(content, encoding="utf-8")
        mode = 0o640 if index % 2 else 0o600
        target.chmod(mode)
        expected_modes[name] = mode

    write_report(tmp_path, replacement, overwrite=True)

    assert {name: (tmp_path / name).read_text(encoding="utf-8") for name in OUTPUT_NAMES} == replacement
    assert {
        name: stat.S_IMODE((tmp_path / name).stat().st_mode) for name in OUTPUT_NAMES
    } == expected_modes


def test_write_report_new_outputs_keep_private_default_permissions(tmp_path):
    documents = {name: "fresh" for name in OUTPUT_NAMES}

    write_report(tmp_path, documents, overwrite=True)

    assert all(
        stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600 for name in OUTPUT_NAMES
    )

def _forecast_for(tmp_path: Path, artifacts: list[dict], policy: dict | None = None):
    write_page(tmp_path, "page.json", artifacts, len(artifacts))
    manifest = write_manifest(tmp_path, [repo_entry("demo/alpha", ["page.json"])])
    snapshot = load_snapshot(manifest, display_path="manifest.json")
    if policy is not None:
        policy_path = tmp_path / "policy.json"
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
        snapshot = apply_policy(snapshot, policy_path)
    forecast = evaluate(snapshot.artifacts, snapshot.as_of, horizon_days=7)
    return snapshot, forecast


def test_html_escapes_imported_markup(tmp_path):
    name = '</style><script>alert(1)</script><img src=x onerror=alert(1)>'
    reason = '<img src=x onerror=alert(1)>'
    snapshot, forecast = _forecast_for(
        tmp_path,
        [
            gh_artifact(
                1,
                name,
                10,
                "2026-10-01T00:00:00Z",
                "2026-10-20T00:00:00Z",
            )
        ],
        {
            "schema_version": 1,
            "protected_artifacts": [
                {"repository": "demo/alpha", "artifact_id": 1, "reason": reason}
            ],
        },
    )
    page = render_html(snapshot, forecast, (*snapshot.diagnostics, *forecast.warnings))
    assert "<script>" not in page
    assert "<img" not in page
    assert "&lt;script&gt;" in page
    assert "&lt;img" in page
    assert "https://" not in page
    assert "<link" not in page
    assert "src=\"http" not in page
    assert "url(http" not in page


def test_csv_neutralizes_formula_prefixes(tmp_path):
    names = ["=1+1", "+1", "-1", "@SUM(A1)", "\t=1", "safe, \"quoted\""]
    artifacts = [
        gh_artifact(index + 1, name, index, "2026-10-01T00:00:00Z", "2026-10-20T00:00:00Z")
        for index, name in enumerate(names)
    ]
    snapshot, forecast = _forecast_for(tmp_path, artifacts)
    documents = build_documents(snapshot, forecast)
    rows = list(csv.reader(documents["artifacts.csv"].splitlines()))
    exported = [row[2] for row in rows[1:]]
    assert exported[0] == "'=1+1"
    assert exported[1] == "'+1"
    assert exported[2] == "'-1"
    assert exported[3] == "'@SUM(A1)"
    assert exported[4].startswith("'\t")
    assert exported[5] == 'safe, "quoted"'
    assert neutralize("=cmd") == "'=cmd"
    assert neutralize("plain") == "plain"


def test_totals_hashes_version_and_repeatability(tmp_path):
    snapshot, forecast = _forecast_for(
        tmp_path,
        [
            gh_artifact(1, "one", 1000, "2026-10-01T00:00:00Z", "2026-10-08T00:00:00Z"),
            gh_artifact(2, "zero", 0, "2026-10-02T00:00:00Z", "2026-10-09T00:00:00Z"),
            gh_artifact(3, "old", 5, "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z", expired=True),
        ],
    )
    first = build_documents(snapshot, forecast)
    second = build_documents(snapshot, forecast)
    assert first == second
    payload = json.loads(first["report.json"])
    assert payload["tool"]["version"] == __version__
    assert sum(item["size_bytes"] for item in payload["artifacts"]) == payload["current"]["imported_bytes"]
    assert payload["current"]["retained_bytes"] + payload["current"]["expired_bytes"] == payload["current"]["imported_bytes"]
    page_hash = hashlib.sha256((tmp_path / "page.json").read_bytes()).hexdigest()
    listed = {item["path"]: item["sha256"] for item in payload["inputs"]["files"]}
    assert listed["page.json"] == page_hash
    assert str(tmp_path.resolve()) not in first["report.html"]
    assert "do not read the wall clock" in first["report.html"]
    assert "not a refund" in first["report.html"]
    assert "1,073,741,824" in first["report.html"]
    assert "hypothetical immediate removal" in first["report.html"]
    assert "does not prove" in first["report.html"]
    assert payload["artifacts"][0]["repository"] <= payload["artifacts"][-1]["repository"]
    assert [item["artifact_id"] for item in payload["artifacts"]] == [1, 2, 3]


def test_decimal_gb_is_display_only():
    assert format_decimal_gb(1_000_000_000) == "1"
    assert format_gib(1_000_000_000) != "1"
    assert format_gib(GIB) == "1"


def test_partial_import_is_not_a_neutral_status(tmp_path):
    write_page(
        tmp_path,
        "page.json",
        [gh_artifact(1, "only", 4, "2026-10-01T00:00:00Z", "2026-10-20T00:00:00Z")],
        9,
    )
    manifest = write_manifest(
        tmp_path,
        [repo_entry("demo/alpha", ["page.json"], complete=False)],
    )
    snapshot = load_snapshot(manifest, display_path="manifest.json")
    forecast = evaluate(snapshot.artifacts, snapshot.as_of, horizon_days=7)
    page = render_html(snapshot, forecast, (*snapshot.diagnostics, *forecast.warnings))
    assert 'data-status="review"' in page
    assert "healthy" not in page.lower()
    assert "incomplete" in page
