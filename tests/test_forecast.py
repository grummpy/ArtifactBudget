"""Clock-free forecast properties beyond the A/B/C acceptance cohort."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from fractions import Fraction

import pytest

from artifactbudget.forecast import (
    GIB,
    InstantRejected,
    age_bucket_key,
    evaluate,
    is_hypothetical_immediate_removal,
    retained_bytes_at,
    to_gib_hours,
)

from support import T0, art


def _hours(forecast, name: str) -> Fraction:
    scenario = next(item for item in forecast.scenarios if item.name == name)
    return to_gib_hours(scenario.known_byte_seconds)


def _integrate(series):
    total = Fraction(0)
    for (left, value), (right, _ignored) in zip(series, series[1:]):
        delta = right - left
        micros = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
        total += Fraction(value * micros, 1_000_000)
    return total


def test_exact_expiry_boundary_and_half_open_horizon():
    expires = T0 + timedelta(days=5)
    row = art(expires_at=expires, created_at=T0 - timedelta(days=1), size_bytes=100)
    assert retained_bytes_at([row], T0, expires - timedelta(seconds=1), None) == 100
    assert retained_bytes_at([row], T0, expires, None) == 0
    forecast = evaluate([row], T0, horizon_days=5)
    assert _hours(forecast, "baseline") == Fraction(100 * 5 * 24, GIB)
    assert forecast.scenarios[0].series[-1][1] == 0
    assert _integrate(forecast.scenarios[0].series) == forecast.scenarios[0].known_byte_seconds


def test_cap_never_lengthens_and_never_increases_storage():
    cohorts = [
        [art()],
        [art(expires_at=T0 + timedelta(days=1), created_at=T0 - timedelta(hours=1))],
        [art(protection_reason="keep", artifact_id=2, name="B", size_bytes=GIB // 2)],
        [art(expires_at=None, artifact_id=3, name="U", size_bytes=GIB // 4)],
        [
            art(),
            art(
                artifact_id=4,
                created_at=T0 - timedelta(days=40),
                expires_at=T0 + timedelta(days=2),
                size_bytes=10,
            ),
        ],
    ]
    for cohort in cohorts:
        forecast = evaluate(cohort, T0, horizon_days=30)
        baseline = next(item for item in forecast.scenarios if item.name == "baseline")
        for scenario in forecast.scenarios:
            assert scenario.known_byte_seconds <= baseline.known_byte_seconds
            assert _integrate(scenario.series) == scenario.known_byte_seconds
        short = art(expires_at=T0 + timedelta(days=1), created_at=T0, size_bytes=GIB)
        alone = evaluate([short], T0, horizon_days=30)
        assert _hours(alone, "baseline") == _hours(alone, "cap_30") == 24


def test_protected_artifact_keeps_baseline_expiry():
    protected = art(
        artifact_id=2,
        created_at=T0 - timedelta(days=2),
        expires_at=T0 + timedelta(days=5),
        protection_reason="Release evidence",
    )
    forecast = evaluate([protected], T0, horizon_days=7)
    row = forecast.artifacts[0]
    assert row.cap_3_expiry == row.cap_7_expiry == row.cap_30_expiry == row.baseline_expiry
    assert not is_hypothetical_immediate_removal(protected, T0, 3)


def test_unknown_expiry_bounds_are_excluded_from_the_point_forecast():
    known = art(size_bytes=GIB, created_at=T0 - timedelta(days=10), expires_at=T0 + timedelta(days=20))
    unknown = art(artifact_id=9, name="U", size_bytes=GIB, expires_at=None, created_at=T0 - timedelta(days=1))
    forecast = evaluate([known, unknown], T0, horizon_days=7)
    assert forecast.retained_bytes == 2 * GIB
    assert forecast.unknown_expiry_bytes == GIB
    baseline = next(item for item in forecast.scenarios if item.name == "baseline")
    cap3 = next(item for item in forecast.scenarios if item.name == "cap_3")
    assert to_gib_hours(baseline.known_byte_seconds) == 168
    assert baseline.lower_byte_seconds == baseline.known_byte_seconds
    assert to_gib_hours(baseline.upper_byte_seconds) == 168 + 168
    assert cap3.known_byte_seconds == 0
    assert to_gib_hours(cap3.upper_byte_seconds) == 168
    assert any(item.code == "unknown_expiry" for item in forecast.warnings)


def test_empty_and_zero_byte():
    empty = evaluate([], T0, horizon_days=7)
    assert empty.retained_bytes == 0
    assert empty.scenarios[0].known_byte_seconds == 0
    assert empty.scenarios[0].upper_byte_seconds == 0
    zero = evaluate([art(size_bytes=0, artifact_id=1)], T0, horizon_days=7)
    assert zero.retained_count == 1
    assert zero.retained_bytes == 0
    assert zero.scenarios[0].known_byte_seconds == 0


def test_leap_day_cap_uses_real_utc_days():
    t0 = datetime(2024, 2, 29, tzinfo=timezone.utc)
    row = art(
        created_at=datetime(2024, 2, 28, tzinfo=timezone.utc),
        expires_at=datetime(2024, 3, 10, tzinfo=timezone.utc),
        size_bytes=GIB,
        snapshot_at=t0,
    )
    forecast = evaluate([row], t0, horizon_days=7)
    # Created 2024-02-28, t0 2024-02-29. A 3-day cap lands on 2024-03-02: 48 GiB-hours.
    # Baseline runs to the 7-day horizon on 2024-03-07: 168 GiB-hours.
    assert next(item for item in forecast.scenarios if item.name == "cap_3").immediate_removal_count == 0
    assert _hours(forecast, "cap_3") == 48
    assert _hours(forecast, "baseline") == 168
    cap1_expiry_created_plus_one = datetime(2024, 2, 29, tzinfo=timezone.utc)
    assert row.created_at + timedelta(days=1) == cap1_expiry_created_plus_one
    immediate = evaluate(
        [art(created_at=t0 - timedelta(days=1), expires_at=t0 + timedelta(days=10), size_bytes=GIB, snapshot_at=t0)],
        t0,
        horizon_days=7,
    )
    # created one day before a 1-day... the fixed caps are 3/7/30.
    # Age is 1 day, so a 3-day cap does not remove it immediately.
    row3 = next(item for item in immediate.scenarios if item.name == "cap_3")
    assert row3.immediate_removal_count == 0
    assert row.created_at + timedelta(days=2) == datetime(2024, 3, 1, tzinfo=timezone.utc)


def test_expired_rows_and_flag_conflicts():
    expired = art(expired=True, expires_at=T0 - timedelta(days=1), size_bytes=50)
    conflict = art(
        artifact_id=2,
        expired=True,
        expires_at=T0 + timedelta(days=4),
        size_bytes=80,
    )
    silent = art(
        artifact_id=3,
        expired=False,
        expires_at=T0 - timedelta(hours=1),
        size_bytes=20,
    )
    forecast = evaluate([expired, conflict, silent], T0, horizon_days=7)
    assert forecast.retained_bytes == 0
    assert forecast.expired_bytes == 150
    assert forecast.scenarios[0].known_byte_seconds == 0
    codes = [item.code for item in forecast.warnings]
    assert codes.count("expiry_flag_conflict") == 2


def test_created_after_t0_rejects_the_instant():
    row = art(created_at=T0 + timedelta(seconds=1))
    with pytest.raises(InstantRejected) as caught:
        evaluate([row], T0, horizon_days=7)
    assert caught.value.diagnostics[0].code == "created_after_t0"


def test_age_buckets_are_half_open():
    assert age_bucket_key(T0, T0) == "under_1_day"
    assert age_bucket_key(T0 - timedelta(days=1) + timedelta(seconds=1), T0) == "under_1_day"
    assert age_bucket_key(T0 - timedelta(days=1), T0) == "1_to_7_days"
    assert age_bucket_key(T0 - timedelta(days=7), T0) == "7_to_30_days"
    assert age_bucket_key(T0 - timedelta(days=30), T0) == "30_to_90_days"
    assert age_bucket_key(T0 - timedelta(days=90), T0) == "90_days_or_more"


def test_horizon_limits_storage_hours():
    row = art(created_at=T0, expires_at=T0 + timedelta(days=10), size_bytes=2 * GIB)
    forecast = evaluate([row], T0, horizon_days=1)
    assert _hours(forecast, "baseline") == 48
    short = art(created_at=T0, expires_at=T0 + timedelta(hours=12), size_bytes=2 * GIB)
    short_forecast = evaluate([short], T0, horizon_days=30)
    assert _hours(short_forecast, "baseline") == 24


def test_forecast_source_does_not_read_the_clock():
    from pathlib import Path

    text = Path(__file__).resolve().parents[1].joinpath("artifactbudget", "forecast.py").read_text(encoding="utf-8")
    assert "datetime.now" not in text
    assert "utcnow" not in text
