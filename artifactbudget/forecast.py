"""Clock-free retention math.

The hand-calculated acceptance cohort, over a 7-day horizon, is:

- A: 1 GiB, created 10 days before t0, expires 20 days after, unprotected
- B: 0.5 GiB, created 2 days before t0, expires 5 days after, protected
- C: 0.25 GiB, created 1 day before t0, expires 6 days after, unprotected

Current inventory is 1.75 GiB under every cap. Baseline storage is 264
GiB-hours. A 3-day cap is 72, a 7-day cap is 96, and a 30-day cap is 264.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from typing import Sequence

from artifactbudget.models import Artifact, ArtifactBudgetError, Diagnostic, format_timestamp

GIB = 1024**3
DECIMAL_GB = 1_000_000_000
CAP_DAYS = (3, 7, 30)
PROTECTED_SOON = timedelta(days=7)

ASSUMPTIONS: tuple[str, ...] = (
    "The report instant t0 comes from the manifest as_of value, or from --as-of. Forecast functions do not read the wall clock.",
    "The horizon is an integer number of 86400-second days. Storage-hours use the half-open window [t0, t0+H). The default horizon is 30 days.",
    "An artifact is expired at t0 when expired is true or its known expires_at is at or before t0. Expired rows are listed separately and are excluded from current retained bytes.",
    "If the expired flag and expires_at disagree, the row is still treated as expired and the conflict is warned.",
    "Baseline effective expiry is the imported expires_at. That is an expected schedule, not a guarantee of GitHub deletion processing.",
    "For an unprotected artifact with a known expiry, an N-day cap sets effective expiry to min(imported expiry, max(t0, created_at + N days)).",
    "Protected artifacts keep the imported baseline expiry under every cap. A protection label does not extend GitHub retention and does not preserve the file.",
    "A cap never lengthens retention. If created_at + N days is at or before t0, the scenario simulates the artifact leaving at t0. That is a hypothetical immediate removal, not an action this tool takes.",
    "At time t, modeled retained bytes sum sizes whose effective expiry is strictly later than t. At the exact expiry timestamp the artifact no longer contributes.",
    "Future storage for a known-expiry artifact is size_bytes times the seconds from t0 to min(effective expiry, t0+H), converted from exact byte-seconds. Values are rounded only for display.",
    "Scenario reduction compares a cap with the baseline of the same snapshot and horizon for the known-expiry cohort only. It is not a refund and not a guaranteed monetary saving.",
    "Unknown-expiry artifacts stay in the current inventory. They are excluded from point forecasts and from cap shortening. Their horizon contribution is bounded from zero to the full horizon.",
    "The model covers only the imported cohort. It includes no new uploads, restores, manual deletions, or size changes.",
    "GiB and GiB-hours divide by 1,073,741,824 (2^30), matching GitHub's documented binary billing gigabyte. Decimal GB is bytes/1,000,000,000 and is display-only. This report is not an account bill and does not convert to GB-months.",
    "A matching exported total_count does not prove that the snapshot is complete or atomic. Repositories absent from the manifest are outside coverage.",
    "Artifact names and policy reasons are untrusted text. HTML output escapes them, and CSV output neutralizes spreadsheet-formula prefixes.",
)

LIMITATIONS: tuple[str, ...] = (
    "ArtifactBudget never deletes artifacts and never changes GitHub retention, settings, workflows, or billing.",
    "A protection label only removes an artifact from shortened-retention simulations. It does not extend GitHub retention or preserve the file.",
    "Protected evidence can still expire on the imported schedule. Expiry within 7 days is called out in this report.",
    "Figures describe this imported snapshot only. They are not an invoice, a refund, or a forecast of new workflow runs, caches, or packages.",
    "This build has no live GitHub adapter. It reads local JSON exports and does not contact GitHub.",
)

AGE_BUCKETS: tuple[tuple[str, str], ...] = (
    ("under_1_day", "Under 1 day"),
    ("1_to_7_days", "1–7 days"),
    ("7_to_30_days", "7–30 days"),
    ("30_to_90_days", "30–90 days"),
    ("90_days_or_more", "90 days or more"),
)


class InstantRejected(ArtifactBudgetError):
    """The snapshot is not valid at the requested report instant."""

    def __init__(self, diagnostics: Sequence[Diagnostic]):
        self.diagnostics = tuple(diagnostics)
        super().__init__(
            "Report instant rejected. "
            + " ".join(item.message for item in self.diagnostics)
        )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def is_expired_at(artifact: Artifact, t0: datetime) -> bool:
    """Return True when the row is already expired at t0."""

    if artifact.expired:
        return True
    return artifact.expires_at is not None and artifact.expires_at <= t0


def is_unknown_expiry(artifact: Artifact) -> bool:
    """Unknown expiry is a missing date on a row that is not flagged expired."""

    return artifact.expires_at is None and not artifact.expired


def is_protected(artifact: Artifact) -> bool:
    return bool(artifact.protection_reason)


def age_bucket_key(created_at: datetime, t0: datetime) -> str:
    """Bucket age at t0. Bounds are half-open except the final open-ended bucket."""

    age = t0 - created_at
    if age < timedelta(0):
        raise ValueError("age is negative; the report instant should have been rejected")
    if age < timedelta(days=1):
        return "under_1_day"
    if age < timedelta(days=7):
        return "1_to_7_days"
    if age < timedelta(days=30):
        return "7_to_30_days"
    if age < timedelta(days=90):
        return "30_to_90_days"
    return "90_days_or_more"


def modeled_expiry(
    artifact: Artifact,
    t0: datetime,
    cap_days: int | None,
) -> datetime | None:
    """Return the scenario expiry, or None when expiry is unknown.

    None is uncertainty, not an expiry at t0. An instant at or before t0
    contributes no future storage and is not retained at that instant.
    """

    if is_expired_at(artifact, t0):
        if artifact.expires_at is not None and artifact.expires_at < t0:
            return artifact.expires_at
        return t0
    if artifact.expires_at is None:
        return None
    if cap_days is None or is_protected(artifact):
        return artifact.expires_at
    capped_at = artifact.created_at + timedelta(days=cap_days)
    return min(artifact.expires_at, max(t0, capped_at))


def is_hypothetical_immediate_removal(
    artifact: Artifact,
    t0: datetime,
    cap_days: int | None,
) -> bool:
    """True when a cap pulls a still-retained artifact down to t0."""

    if cap_days is None or is_protected(artifact) or is_unknown_expiry(artifact):
        return False
    if is_expired_at(artifact, t0):
        return False
    effective = modeled_expiry(artifact, t0, cap_days)
    return effective is not None and effective <= t0


def byte_seconds(size_bytes: int, start: datetime, end: datetime) -> Fraction:
    """Exact byte-seconds over [start, end). Negative and empty spans are zero."""

    if size_bytes < 0:
        raise ValueError("size_bytes must be nonnegative")
    if end <= start or size_bytes == 0:
        return Fraction(0)
    delta = end - start
    micros = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
    return Fraction(size_bytes * micros, 1_000_000)


def to_gib_hours(value: Fraction) -> Fraction:
    """Convert byte-seconds to GiB-hours using 2^30 bytes and 3600 seconds."""

    return value / Fraction(GIB) / Fraction(3600)


def retained_bytes_at(
    artifacts: Sequence[Artifact],
    t0: datetime,
    when: datetime,
    cap_days: int | None,
) -> int:
    """Known-expiry bytes whose modeled expiry is strictly later than ``when``."""

    total = 0
    for artifact in artifacts:
        effective = modeled_expiry(artifact, t0, cap_days)
        if effective is not None and effective > when:
            total += artifact.size_bytes
    return total


def instant_errors(artifacts: Sequence[Artifact], t0: datetime) -> tuple[Diagnostic, ...]:
    """Reject a report instant that contains artifacts created after t0."""

    t0 = _utc(t0)
    findings: list[Diagnostic] = []
    for artifact in artifacts:
        created = _utc(artifact.created_at)
        if created > t0:
            findings.append(
                Diagnostic(
                    level="error",
                    code="created_after_t0",
                    message=(
                        f"{artifact.repository} artifact {artifact.artifact_id} was created at "
                        f"{format_timestamp(created)}, after the report instant "
                        f"{format_timestamp(t0)}. This snapshot is not valid at that instant."
                    ),
                    repository=artifact.repository,
                    source_file=artifact.source_file,
                    artifact_id=artifact.artifact_id,
                    source_record_index=artifact.source_record_index,
                )
            )
    return tuple(findings)


def quality_warnings(artifacts: Sequence[Artifact], t0: datetime) -> tuple[Diagnostic, ...]:
    """Non-fatal findings that depend on the report instant."""

    t0 = _utc(t0)
    findings: list[Diagnostic] = []
    unknown = [artifact for artifact in artifacts if is_unknown_expiry(artifact)]
    if unknown:
        unknown_bytes = sum(artifact.size_bytes for artifact in unknown)
        findings.append(
            Diagnostic(
                level="warning",
                code="unknown_expiry",
                message=(
                    f"{len(unknown)} artifact(s) totaling {unknown_bytes} bytes have no expires_at. "
                    "They remain in the current inventory and are excluded from point forecasts "
                    "and from cap shortening. Their horizon contribution is bounded from zero "
                    "to a full-horizon contribution."
                ),
            )
        )
    soon_end = t0 + PROTECTED_SOON
    for artifact in artifacts:
        if (
            artifact.expired
            and artifact.expires_at is not None
            and artifact.expires_at > t0
        ):
            findings.append(
                _artifact_warning(
                    artifact,
                    "expiry_flag_conflict",
                    "marked expired=true but expires_at is after t0. It is excluded from current "
                    "retained bytes and from future point forecasts.",
                )
            )
        elif (
            not artifact.expired
            and artifact.expires_at is not None
            and artifact.expires_at <= t0
        ):
            findings.append(
                _artifact_warning(
                    artifact,
                    "expiry_flag_conflict",
                    "marked expired=false but expires_at is at or before t0. It is treated as expired.",
                )
            )
        if not is_protected(artifact):
            continue
        if is_expired_at(artifact, t0):
            findings.append(
                _artifact_warning(
                    artifact,
                    "protected_already_expired",
                    "is protected and already expired at t0. Protection did not preserve the file "
                    "and does not extend GitHub retention.",
                )
            )
        elif artifact.expires_at is not None and artifact.expires_at <= soon_end:
            findings.append(
                _artifact_warning(
                    artifact,
                    "protected_expires_soon",
                    f"is protected and expires at {format_timestamp(artifact.expires_at)}, "
                    "within 7 days of t0. Protection only excludes it from shortened-retention "
                    "simulations. GitHub can still delete it on the imported schedule.",
                )
            )
        elif is_unknown_expiry(artifact):
            findings.append(
                _artifact_warning(
                    artifact,
                    "protected_unknown_expiry",
                    "is protected but has no expires_at. Protection does not invent an expiry. "
                    "The row stays in the uncertainty bucket.",
                )
            )
    return tuple(findings)


def _artifact_warning(artifact: Artifact, code: str, detail: str) -> Diagnostic:
    return Diagnostic(
        level="warning",
        code=code,
        message=f"{artifact.repository} artifact {artifact.artifact_id} {detail}",
        repository=artifact.repository,
        source_file=artifact.source_file,
        artifact_id=artifact.artifact_id,
        source_record_index=artifact.source_record_index,
    )


@dataclass(frozen=True, slots=True)
class ArtifactForecast:
    artifact: Artifact
    expired_at_t0: bool
    unknown_expiry: bool
    retained: bool
    baseline_expiry: datetime | None
    cap_3_expiry: datetime | None
    cap_7_expiry: datetime | None
    cap_30_expiry: datetime | None
    immediate_removal_3: bool
    immediate_removal_7: bool
    immediate_removal_30: bool


@dataclass(frozen=True, slots=True)
class ScenarioForecast:
    name: str
    label: str
    cap_days: int | None
    known_byte_seconds: Fraction
    lower_byte_seconds: Fraction
    upper_byte_seconds: Fraction
    reduction_byte_seconds: Fraction
    retained_bytes_at_t0: int
    immediate_removal_bytes: int
    immediate_removal_count: int
    immediate_removals: tuple[tuple[str, int], ...]
    series: tuple[tuple[datetime, int], ...]


@dataclass(frozen=True, slots=True)
class Forecast:
    t0: datetime
    horizon_days: int
    horizon_end: datetime
    scenarios: tuple[ScenarioForecast, ...]
    artifacts: tuple[ArtifactForecast, ...]
    warnings: tuple[Diagnostic, ...]
    unknown_expiry_count: int
    unknown_expiry_bytes: int
    retained_count: int
    retained_bytes: int
    protected_count: int
    protected_bytes: int
    expired_count: int
    expired_bytes: int


def evaluate(
    artifacts: Sequence[Artifact],
    t0: datetime,
    horizon_days: int = 30,
) -> Forecast:
    """Forecast one cohort at a fixed instant. This function does not read the clock."""

    if isinstance(horizon_days, bool) or not isinstance(horizon_days, int) or horizon_days < 1:
        raise ValueError("horizon_days must be a positive integer")
    t0 = _utc(t0)
    normalized = tuple(_normalize(artifact) for artifact in artifacts)
    problems = instant_errors(normalized, t0)
    if problems:
        raise InstantRejected(problems)

    horizon_end = t0 + timedelta(days=horizon_days)
    rows = tuple(_row(artifact, t0) for artifact in normalized)
    unknown_rows = [row for row in rows if row.unknown_expiry]
    unknown_bytes = sum(row.artifact.size_bytes for row in unknown_rows)
    retained_rows = [row for row in rows if row.retained]
    protected_rows = [row for row in retained_rows if is_protected(row.artifact)]
    expired_rows = [row for row in rows if row.expired_at_t0]

    scenario_specs = (
        ("baseline", "Baseline (imported expiry)", None),
        ("cap_3", "3-day cap", 3),
        ("cap_7", "7-day cap", 7),
        ("cap_30", "30-day cap", 30),
    )
    known_by_name: dict[str, Fraction] = {}
    scenarios: list[ScenarioForecast] = []
    for name, label, cap in scenario_specs:
        known = _known_byte_seconds(normalized, t0, horizon_end, cap)
        known_by_name[name] = known
        extra = Fraction(unknown_bytes * horizon_days * 86400, 1)
        removals = tuple(
            sorted(
                (row.artifact.repository, row.artifact.artifact_id)
                for row in rows
                if is_hypothetical_immediate_removal(row.artifact, t0, cap)
            )
        )
        removal_bytes = sum(
            row.artifact.size_bytes
            for row in rows
            if is_hypothetical_immediate_removal(row.artifact, t0, cap)
        )
        baseline_known = known if name == "baseline" else known_by_name["baseline"]
        reduction = baseline_known - known
        if reduction < 0:
            raise AssertionError("a cap increased known-expiry storage; this is a bug")
        scenarios.append(
            ScenarioForecast(
                name=name,
                label=label,
                cap_days=cap,
                known_byte_seconds=known,
                lower_byte_seconds=known,
                upper_byte_seconds=known + extra,
                reduction_byte_seconds=reduction,
                retained_bytes_at_t0=retained_bytes_at(normalized, t0, t0, cap),
                immediate_removal_bytes=removal_bytes,
                immediate_removal_count=len(removals),
                immediate_removals=removals,
                series=_series(normalized, t0, horizon_end, cap),
            )
        )

    return Forecast(
        t0=t0,
        horizon_days=horizon_days,
        horizon_end=horizon_end,
        scenarios=tuple(scenarios),
        artifacts=rows,
        warnings=quality_warnings(normalized, t0),
        unknown_expiry_count=len(unknown_rows),
        unknown_expiry_bytes=unknown_bytes,
        retained_count=len(retained_rows),
        retained_bytes=sum(row.artifact.size_bytes for row in retained_rows),
        protected_count=len(protected_rows),
        protected_bytes=sum(row.artifact.size_bytes for row in protected_rows),
        expired_count=len(expired_rows),
        expired_bytes=sum(row.artifact.size_bytes for row in expired_rows),
    )


def _normalize(artifact: Artifact) -> Artifact:
    return replace(
        artifact,
        created_at=_utc(artifact.created_at),
        expires_at=None if artifact.expires_at is None else _utc(artifact.expires_at),
        snapshot_at=_utc(artifact.snapshot_at),
    )


def _row(artifact: Artifact, t0: datetime) -> ArtifactForecast:
    expired = is_expired_at(artifact, t0)
    unknown = is_unknown_expiry(artifact)
    return ArtifactForecast(
        artifact=artifact,
        expired_at_t0=expired,
        unknown_expiry=unknown,
        retained=not expired,
        baseline_expiry=modeled_expiry(artifact, t0, None),
        cap_3_expiry=modeled_expiry(artifact, t0, 3),
        cap_7_expiry=modeled_expiry(artifact, t0, 7),
        cap_30_expiry=modeled_expiry(artifact, t0, 30),
        immediate_removal_3=is_hypothetical_immediate_removal(artifact, t0, 3),
        immediate_removal_7=is_hypothetical_immediate_removal(artifact, t0, 7),
        immediate_removal_30=is_hypothetical_immediate_removal(artifact, t0, 30),
    )


def _known_byte_seconds(
    artifacts: Sequence[Artifact],
    t0: datetime,
    horizon_end: datetime,
    cap_days: int | None,
) -> Fraction:
    total = Fraction(0)
    for artifact in artifacts:
        effective = modeled_expiry(artifact, t0, cap_days)
        if effective is None:
            continue
        total += byte_seconds(artifact.size_bytes, t0, min(effective, horizon_end))
    return total


def _series(
    artifacts: Sequence[Artifact],
    t0: datetime,
    horizon_end: datetime,
    cap_days: int | None,
) -> tuple[tuple[datetime, int], ...]:
    events = {t0, horizon_end}
    modeled: list[tuple[Artifact, datetime]] = []
    for artifact in artifacts:
        effective = modeled_expiry(artifact, t0, cap_days)
        if effective is None:
            continue
        modeled.append((artifact, effective))
        if t0 < effective <= horizon_end:
            events.add(effective)
    points: list[tuple[datetime, int]] = []
    for instant in sorted(events):
        retained = sum(artifact.size_bytes for artifact, effective in modeled if effective > instant)
        points.append((instant, retained))
    return tuple(points)
