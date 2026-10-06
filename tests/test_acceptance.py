"""Hand-calculated A/B/C cohort from the build sheet."""

from __future__ import annotations

import json
from datetime import timedelta
from fractions import Fraction

from artifactbudget.cli import main
from artifactbudget.forecast import evaluate, to_gib_hours
from artifactbudget.importer import load_snapshot
from artifactbudget.policy import apply_policy

from support import ROOT, T0

ABC = ROOT / "tests" / "fixtures" / "abc"
GIB = 1024**3
CURRENT = 1879048192  # 1.75 GiB


def _load():
    snapshot = load_snapshot(ABC / "manifest.json", display_path="tests/fixtures/abc/manifest.json")
    return apply_policy(snapshot, ABC / "policy.json")


def _scenario(forecast, name):
    return next(item for item in forecast.scenarios if item.name == name)


def test_hand_calculated_hours_and_inventory():
    snapshot = _load()
    assert snapshot.ok
    forecast = evaluate(snapshot.artifacts, snapshot.as_of, horizon_days=7)
    assert forecast.t0 == T0
    assert forecast.retained_bytes == CURRENT
    assert forecast.retained_count == 3
    assert forecast.protected_bytes == GIB // 2
    assert to_gib_hours(_scenario(forecast, "baseline").known_byte_seconds) == 264
    assert to_gib_hours(_scenario(forecast, "cap_3").known_byte_seconds) == 72
    assert to_gib_hours(_scenario(forecast, "cap_7").known_byte_seconds) == 96
    assert to_gib_hours(_scenario(forecast, "cap_30").known_byte_seconds) == 264

    by_id = {row.artifact.artifact_id: row for row in forecast.artifacts}
    assert by_id[2].artifact.protection_reason == "Release evidence"
    for row in by_id.values():
        if row.artifact.artifact_id == 2:
            assert row.cap_3_expiry == row.cap_7_expiry == row.cap_30_expiry == row.baseline_expiry
            assert row.baseline_expiry == T0 + timedelta(days=5)
        else:
            assert row.artifact.protection_reason is None
    assert by_id[1].cap_3_expiry == T0
    assert by_id[1].immediate_removal_3
    assert by_id[1].immediate_removal_7
    assert not by_id[1].immediate_removal_30
    assert _scenario(forecast, "cap_3").immediate_removals == (("fixture/cohort", 1),)
    assert _scenario(forecast, "cap_3").retained_bytes_at_t0 == (GIB // 2) + (GIB // 4)
    for scenario in forecast.scenarios:
        assert scenario.known_byte_seconds <= _scenario(forecast, "baseline").known_byte_seconds


def test_step_series_matches_the_hand_calculation():
    snapshot = _load()
    forecast = evaluate(snapshot.artifacts, snapshot.as_of, horizon_days=7)
    baseline = dict(_scenario(forecast, "baseline").series)
    cap3 = dict(_scenario(forecast, "cap_3").series)
    assert baseline[T0] == CURRENT
    assert baseline[T0 + timedelta(days=5)] == GIB + (GIB // 4)
    assert baseline[T0 + timedelta(days=6)] == GIB
    assert baseline[T0 + timedelta(days=7)] == GIB
    assert cap3[T0] == (GIB // 2) + (GIB // 4)
    assert cap3[T0 + timedelta(days=2)] == GIB // 2
    assert cap3[T0 + timedelta(days=5)] == 0
    assert cap3[T0 + timedelta(days=7)] == 0
    for scenario in forecast.scenarios:
        total = Fraction(0)
        series = scenario.series
        for (left, value), (right, _ignored) in zip(series, series[1:]):
            delta = right - left
            micros = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
            total += Fraction(value * micros, 1_000_000)
        assert total == scenario.known_byte_seconds


def test_cli_report_json_matches_the_hand_calculation(tmp_path):
    out = tmp_path / "out"
    before = (ABC / "page.json").read_bytes()
    code = main(
        [
            "report",
            "--manifest",
            str(ABC / "manifest.json"),
            "--policy",
            str(ABC / "policy.json"),
            "--horizon-days",
            "7",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    assert (ABC / "page.json").read_bytes() == before
    payload = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert payload["current"]["retained_bytes"] == CURRENT
    assert payload["current"]["retained_gib"] == "1.75"
    by_name = {item["name"]: item for item in payload["scenarios"]}
    assert by_name["baseline"]["known_gib_hours"] == 264
    assert by_name["cap_3"]["known_gib_hours"] == 72
    assert by_name["cap_7"]["known_gib_hours"] == 96
    assert by_name["cap_30"]["known_gib_hours"] == 264
    assert by_name["cap_3"]["reduction_gib_hours"] == 192
    imported = sum(item["size_bytes"] for item in payload["artifacts"])
    assert imported == payload["current"]["imported_bytes"]
    assert (
        payload["current"]["retained_bytes"] + payload["current"]["expired_bytes"]
        == payload["current"]["imported_bytes"]
    )
    html = (out / "report.html").read_text(encoding="utf-8")
    assert "hypothetical immediate removal" in html
    assert "api.github.com" not in html
    assert "<script" not in html
