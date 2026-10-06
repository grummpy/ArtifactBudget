"""HTML escaping, CSV neutralization, totals, and deterministic rendering."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from artifactbudget import __version__
from artifactbudget.forecast import GIB, evaluate
from artifactbudget.importer import load_snapshot
from artifactbudget.policy import apply_policy
from artifactbudget.report import (
    build_documents,
    format_decimal_gb,
    format_gib,
    neutralize,
    render_html,
)

from support import gh_artifact, repo_entry, write_manifest, write_page


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
