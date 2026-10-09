"""JSON, CSV, and self-contained HTML for one forecast."""

from __future__ import annotations

import csv
import html
import io
import os
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction
from pathlib import Path

from artifactbudget import __version__
from artifactbudget.forecast import (
    AGE_BUCKETS,
    ASSUMPTIONS,
    DECIMAL_GB,
    GIB,
    LIMITATIONS,
    Forecast,
    age_bucket_key,
    is_protected,
    to_gib_hours,
)
from artifactbudget.models import (
    ArtifactBudgetError,
    Diagnostic,
    Snapshot,
    format_timestamp,
    sorted_diagnostics,
)

OUTPUT_NAMES = (
    "report.json",
    "report.html",
    "artifacts.csv",
    "repositories.csv",
    "scenarios.csv",
    "timeline.csv",
    "diagnostics.csv",
)

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def build_documents(snapshot: Snapshot, forecast: Forecast) -> dict[str, str]:
    """Return the report files as UTF-8 text, keyed by basename."""

    diagnostics = sorted_diagnostics((*snapshot.diagnostics, *forecast.warnings))
    documents = {
        "report.json": render_json(snapshot, forecast, diagnostics),
        "report.html": render_html(snapshot, forecast, diagnostics),
        "diagnostics.csv": _diagnostics_csv(diagnostics),
    }
    documents.update(_table_csvs(snapshot, forecast))
    missing = [name for name in OUTPUT_NAMES if name not in documents]
    if missing:
        raise ArtifactBudgetError(f"Internal error: missing report sections {missing}")
    return documents


def write_report(directory: str | Path, documents: dict[str, str], *, overwrite: bool) -> None:
    """Write report files inside ``directory`` and nowhere else.

    Existing report files are left untouched unless ``overwrite`` is set.
    Symlinked outputs are refused. Unrelated files in the directory stay.
    """

    directory = Path(directory)
    if directory.is_symlink():
        raise ArtifactBudgetError("Refusing to write through a symlinked output directory.")
    if directory.exists() and not directory.is_dir():
        raise ArtifactBudgetError(f"{directory} exists and is not a directory.")
    for name in OUTPUT_NAMES:
        if name not in documents:
            raise ArtifactBudgetError(f"Missing generated content for {name}.")
        target = directory / name
        if target.is_symlink():
            raise ArtifactBudgetError(f"Refusing to write through symlinked report file {name}.")
        if target.exists() and not target.is_file():
            raise ArtifactBudgetError(f"{name} exists in the output directory and is not a file.")
        if target.exists() and not overwrite:
            raise ArtifactBudgetError(
                f"{name} already exists in {directory}. Pass --overwrite to replace report files."
            )
    directory.mkdir(parents=True, exist_ok=True)
    staged: dict[str, Path] = {}
    backups: dict[str, Path] = {}
    replaced: list[str] = []
    committed = False
    try:
        # mkstemp is exclusive and does not follow a predictable planted name.
        # Stage every document before replacing anything, so an interrupted
        # render leaves the previous complete report untouched.
        for name in OUTPUT_NAMES:
            descriptor, temporary = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=directory)
            staged[name] = Path(temporary)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(documents[name])
        for name in OUTPUT_NAMES:
            target = directory / name
            if target.is_symlink():
                raise ArtifactBudgetError(f"Refusing to write through symlinked report file {name}.")
            # Keep a private, exclusive backup until every output has been
            # replaced.  A later replacement failure must not leave a mixed
            # report set behind.
            if target.exists():
                # ``mkstemp`` deliberately creates private files (0600),
                # which is the safe default for a newly-created report.  An
                # overwrite, however, replaces the contents of a report the
                # caller already made readable to a particular audience.
                # Preserve its ordinary permission bits on both the staged
                # replacement and its rollback copy.  Do not propagate set-id
                # or sticky bits to newly-created inodes.
                existing_mode = target.stat().st_mode & 0o777
                os.chmod(staged[name], existing_mode)
                descriptor, backup = tempfile.mkstemp(prefix=f".{name}.", suffix=".bak", dir=directory)
                backups[name] = Path(backup)
                # Windows exposes ``chmod`` but not ``fchmod`` on the
                # supported Python versions.  The backup came from mkstemp
                # and remains open here, so the pathname fallback is limited
                # to that exclusive file while preserving its ordinary mode.
                if hasattr(os, "fchmod"):
                    os.fchmod(descriptor, existing_mode)
                else:
                    os.chmod(backup, existing_mode)
                with target.open("rb") as source, os.fdopen(descriptor, "wb") as destination:
                    destination.write(source.read())
            os.replace(staged[name], target)
            staged.pop(name)
            replaced.append(name)
        committed = True
    finally:
        if not committed:
            # Restore in reverse order so a handled write error leaves the
            # complete prior report available, rather than a partial update.
            for name in reversed(replaced):
                target = directory / name
                backup = backups.pop(name, None)
                if backup is None:
                    try:
                        target.unlink()
                    except FileNotFoundError:
                        pass
                else:
                    os.replace(backup, target)
        for temporary in staged.values():
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        for backup in backups.values():
            try:
                backup.unlink()
            except FileNotFoundError:
                pass


def validation_text(diagnostics: tuple[Diagnostic, ...] | list[Diagnostic], *, heading: str) -> str:
    """Human-readable validation summary. The exit code is decided by the caller."""

    items = sorted_diagnostics(diagnostics)
    errors = sum(1 for item in items if item.level == "error")
    warnings = sum(1 for item in items if item.level == "warning")
    notes = sum(1 for item in items if item.level == "info")
    status = "invalid" if errors else "valid"
    lines = [
        f"ArtifactBudget {__version__} {heading}",
        f"Result: {status} ({errors} errors, {warnings} warnings, {notes} notes)",
        (
            "Valid means no records were rejected and no conflicting duplicates remain. "
            "Warnings can still change how the totals should be read."
        ),
        "",
    ]
    if not items:
        lines.append("No diagnostics.")
    else:
        lines.append("Diagnostics")
        for item in items:
            where = _where(item)
            suffix = f" [{where}]" if where else ""
            lines.append(f"[{item.level.upper()}] {item.code}{suffix}: {item.message}")
    lines.append("")
    return "\n".join(lines)


def render_json(
    snapshot: Snapshot,
    forecast: Forecast,
    diagnostics: tuple[Diagnostic, ...],
) -> str:
    import json

    payload = {
        "schema_version": 1,
        "tool": {"name": "artifactbudget", "version": __version__},
        "as_of": format_timestamp(forecast.t0),
        "horizon_days": forecast.horizon_days,
        "horizon_end": format_timestamp(forecast.horizon_end),
        "inputs": {
            "manifest": snapshot.manifest_path,
            "manifest_sha256": snapshot.manifest_sha256,
            "policy": snapshot.policy_path,
            "policy_sha256": snapshot.policy_sha256,
            "files": [
                {
                    "repository": source.repository,
                    "path": source.path,
                    "sha256": source.sha256,
                    "page_number": source.page_number,
                    "accepted": source.accepted,
                    "rejected": source.rejected,
                    "parsed": source.parsed,
                    "total_count": source.total_count,
                    "total_count_valid": source.total_count_valid,
                }
                for source in snapshot.files
            ],
        },
        "scope": {
            "repositories": [
                {
                    "repository": source.repository,
                    "snapshot_at": format_timestamp(source.snapshot_at),
                    "complete": source.complete,
                    "files": list(source.files),
                }
                for source in snapshot.repositories
            ],
            "coverage_note": (
                "Totals cover only repositories and pages named by the manifest. "
                "A matching total_count does not prove a complete or atomic snapshot."
            ),
        },
        "current": _current_dict(snapshot, forecast),
        "repositories": _repository_dicts(snapshot, forecast),
        "age_buckets": _age_dicts(forecast),
        "largest_artifacts": [
            _artifact_dict(row) for row in _largest(forecast, limit=10)
        ],
        "scenarios": [_scenario_dict(scenario) for scenario in forecast.scenarios],
        "expiration_timeline": [_timeline_dict(row, forecast.t0) for row in _timeline(forecast)],
        "protected_expiring_within_7_days": [
            _timeline_dict(row, forecast.t0)
            for row in sorted(
                (row for row in forecast.artifacts if _expires_soon(row, forecast.t0)),
                key=lambda row: (row.artifact.repository, row.artifact.artifact_id),
            )
        ],
        "policy_matches": [
            {
                "repository": row.artifact.repository,
                "artifact_id": row.artifact.artifact_id,
                "reason": row.artifact.protection_reason,
                "expires_at": format_timestamp(row.artifact.expires_at),
                "expired_at_t0": row.expired_at_t0,
                "expires_within_7_days": _expires_soon(row, forecast.t0),
            }
            for row in forecast.artifacts
            if is_protected(row.artifact)
        ],
        "diagnostics": [_diagnostic_dict(item) for item in diagnostics],
        "assumptions": list(ASSUMPTIONS),
        "limitations": list(LIMITATIONS),
        "artifacts": [_artifact_dict(row) for row in _sorted_rows(forecast)],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def render_html(
    snapshot: Snapshot,
    forecast: Forecast,
    diagnostics: tuple[Diagnostic, ...],
) -> str:
    status = _status(diagnostics)
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        (
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
            "style-src 'unsafe-inline'; img-src 'none'; script-src 'none'; connect-src 'none'\">"
        ),
        f'<meta name="generator" content="artifactbudget {__version__}">',
        "<title>ArtifactBudget retention forecast</title>",
        f"<style>{_CSS}</style>",
        "</head>",
        f'<body data-status="{status}">',
        '<a class="skip" href="#scope">Skip to snapshot scope</a>',
        "<header>",
        '<p class="eyebrow">ArtifactBudget</p>',
        "<h1>Actions artifact retention forecast</h1>",
        (
            f"<p>Version {_esc(__version__)}. Offline report for "
            f"<time datetime=\"{_esc(format_timestamp(forecast.t0))}\">"
            f"{_esc(format_timestamp(forecast.t0))}</time>. "
            "No artifacts were deleted and no GitHub settings were changed.</p>"
        ),
        "</header>",
        "<main>",
        _scope_section(snapshot, forecast, diagnostics, status),
        _current_section(forecast),
        _repository_section(snapshot, forecast),
        _forecast_section(forecast),
        _timeline_section(forecast),
        _closing_section(snapshot, forecast, diagnostics),
        "</main>",
        "<footer>",
        (
            f"<p>Generated by ArtifactBudget {_esc(__version__)}. "
            "This file is self-contained and does not load remote resources.</p>"
        ),
        "</footer>",
        "</body>",
        "</html>",
        "",
    ]
    return "\n".join(parts)


def _table_csvs(snapshot: Snapshot, forecast: Forecast) -> dict[str, str]:
    artifacts = _csv(
        [
            "repository",
            "artifact_id",
            "name",
            "size_bytes",
            "size_gib",
            "size_decimal_gb",
            "created_at",
            "expires_at",
            "expired_flag",
            "expired_at_t0",
            "unknown_expiry",
            "protection_reason",
            "workflow_run_id",
            "head_branch",
            "head_sha",
            "snapshot_at",
            "source_file",
            "source_record_index",
        ],
        [
            [
                row.artifact.repository,
                row.artifact.artifact_id,
                neutralize(row.artifact.name),
                row.artifact.size_bytes,
                format_gib(row.artifact.size_bytes),
                format_decimal_gb(row.artifact.size_bytes),
                format_timestamp(row.artifact.created_at),
                format_timestamp(row.artifact.expires_at) or "",
                _bool(row.artifact.expired),
                _bool(row.expired_at_t0),
                _bool(row.unknown_expiry),
                neutralize(row.artifact.protection_reason or ""),
                row.artifact.workflow_run_id if row.artifact.workflow_run_id is not None else "",
                neutralize(row.artifact.head_branch or ""),
                neutralize(row.artifact.head_sha or ""),
                format_timestamp(row.artifact.snapshot_at),
                neutralize(row.artifact.source_file),
                row.artifact.source_record_index,
            ]
            for row in _sorted_rows(forecast)
        ],
    )
    repositories = _csv(
        [
            "repository",
            "complete",
            "snapshot_at",
            "retained_count",
            "retained_bytes",
            "protected_bytes",
            "unknown_expiry_bytes",
            "expired_count",
            "expired_bytes",
            "distinct_artifacts",
        ],
        [
            [
                row["repository"],
                _bool(row["complete"]),
                row["snapshot_at"],
                row["retained_count"],
                row["retained_bytes"],
                row["protected_bytes"],
                row["unknown_expiry_bytes"],
                row["expired_count"],
                row["expired_bytes"],
                row["distinct_artifacts"],
            ]
            for row in _repository_dicts(snapshot, forecast)
        ],
    )
    scenarios = _csv(
        [
            "scenario",
            "cap_days",
            "known_byte_seconds",
            "known_gib_hours",
            "lower_byte_seconds",
            "upper_byte_seconds",
            "lower_gib_hours",
            "upper_gib_hours",
            "reduction_byte_seconds",
            "reduction_gib_hours",
            "retained_bytes_at_t0",
            "immediate_removal_bytes",
            "immediate_removal_count",
        ],
        [
            [
                scenario.name,
                "" if scenario.cap_days is None else scenario.cap_days,
                _fraction_cell(scenario.known_byte_seconds),
                _fraction_cell(to_gib_hours(scenario.known_byte_seconds)),
                _fraction_cell(scenario.lower_byte_seconds),
                _fraction_cell(scenario.upper_byte_seconds),
                _fraction_cell(to_gib_hours(scenario.lower_byte_seconds)),
                _fraction_cell(to_gib_hours(scenario.upper_byte_seconds)),
                _fraction_cell(scenario.reduction_byte_seconds),
                _fraction_cell(to_gib_hours(scenario.reduction_byte_seconds)),
                scenario.retained_bytes_at_t0,
                scenario.immediate_removal_bytes,
                scenario.immediate_removal_count,
            ]
            for scenario in forecast.scenarios
        ],
    )
    timeline = _csv(
        [
            "repository",
            "artifact_id",
            "name",
            "size_bytes",
            "protection_reason",
            "status",
            "baseline_expires_at",
            "cap_3_effective_expiry",
            "cap_7_effective_expiry",
            "cap_30_effective_expiry",
            "hypothetical_immediate_removal",
            "expires_within_7_days",
        ],
        [
            [
                row.artifact.repository,
                row.artifact.artifact_id,
                neutralize(row.artifact.name),
                row.artifact.size_bytes,
                neutralize(row.artifact.protection_reason or ""),
                _status_label(row),
                format_timestamp(row.baseline_expiry) or "",
                format_timestamp(row.cap_3_expiry) or "",
                format_timestamp(row.cap_7_expiry) or "",
                format_timestamp(row.cap_30_expiry) or "",
                _bool(row.immediate_removal_3 or row.immediate_removal_7 or row.immediate_removal_30),
                _bool(_expires_soon(row, forecast.t0)),
            ]
            for row in _timeline(forecast)
        ],
    )
    return {
        "artifacts.csv": artifacts,
        "repositories.csv": repositories,
        "scenarios.csv": scenarios,
        "timeline.csv": timeline,
    }


def _diagnostics_csv(diagnostics: tuple[Diagnostic, ...]) -> str:
    return _csv(
        ["level", "code", "repository", "artifact_id", "source_file", "source_record_index", "message"],
        [
            [
                item.level,
                item.code,
                neutralize(item.repository or ""),
                "" if item.artifact_id is None else item.artifact_id,
                neutralize(item.source_file or ""),
                "" if item.source_record_index is None else item.source_record_index,
                neutralize(item.message),
            ]
            for item in diagnostics
        ],
    )


def _csv(header: list[str], rows: list[list[object]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n", quoting=csv.QUOTE_NONNUMERIC)
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def neutralize(value: str) -> str:
    """Prefix spreadsheet-formula text so a spreadsheet treats it as text."""

    if value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def format_gib(size_bytes: int) -> str:
    return _format_decimal(Decimal(size_bytes) / Decimal(GIB))


def format_decimal_gb(size_bytes: int) -> str:
    """Decimal gigabytes for display only: bytes / 1_000_000_000."""

    return _format_decimal(Decimal(size_bytes) / Decimal(DECIMAL_GB))


def format_gib_hours(value: Fraction) -> str:
    return _format_decimal(Decimal(value.numerator) / Decimal(value.denominator))


def _format_decimal(value: Decimal, places: int = 4) -> str:
    quantum = Decimal("1").scaleb(-places)
    rounded = value.quantize(quantum, rounding=ROUND_HALF_UP)
    text = format(rounded, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text not in {"", "-0"} else "0"


def _fraction_cell(value: Fraction) -> int | str:
    if value.denominator == 1:
        return int(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def _json_number(value: Fraction) -> int | str:
    return _fraction_cell(value)


def _current_dict(snapshot: Snapshot, forecast: Forecast) -> dict[str, object]:
    imported = sum(row.artifact.size_bytes for row in forecast.artifacts)
    return {
        "retained_count": forecast.retained_count,
        "retained_bytes": forecast.retained_bytes,
        "retained_gib": format_gib(forecast.retained_bytes),
        "retained_decimal_gb": format_decimal_gb(forecast.retained_bytes),
        "protected_count": forecast.protected_count,
        "protected_bytes": forecast.protected_bytes,
        "protected_gib": format_gib(forecast.protected_bytes),
        "unknown_expiry_count": forecast.unknown_expiry_count,
        "unknown_expiry_bytes": forecast.unknown_expiry_bytes,
        "unknown_expiry_gib": format_gib(forecast.unknown_expiry_bytes),
        "expired_count": forecast.expired_count,
        "expired_bytes": forecast.expired_bytes,
        "imported_count": len(forecast.artifacts),
        "imported_bytes": imported,
        "repository_count": len(snapshot.repositories),
        "note": (
            "Current retained bytes are the actual snapshot at t0, including unknown expiry "
            "and excluding rows already expired. Hypothetical caps do not change this figure."
        ),
    }


def _scenario_dict(scenario: object) -> dict[str, object]:
    known_hours = to_gib_hours(scenario.known_byte_seconds)  # type: ignore[attr-defined]
    lower_hours = to_gib_hours(scenario.lower_byte_seconds)  # type: ignore[attr-defined]
    upper_hours = to_gib_hours(scenario.upper_byte_seconds)  # type: ignore[attr-defined]
    reduction_hours = to_gib_hours(scenario.reduction_byte_seconds)  # type: ignore[attr-defined]
    return {
        "name": scenario.name,  # type: ignore[attr-defined]
        "label": scenario.label,  # type: ignore[attr-defined]
        "cap_days": scenario.cap_days,  # type: ignore[attr-defined]
        "known_byte_seconds": _json_number(scenario.known_byte_seconds),  # type: ignore[attr-defined]
        "known_gib_hours": _json_number(known_hours),
        "known_gib_hours_display": format_gib_hours(known_hours),
        "lower_byte_seconds": _json_number(scenario.lower_byte_seconds),  # type: ignore[attr-defined]
        "upper_byte_seconds": _json_number(scenario.upper_byte_seconds),  # type: ignore[attr-defined]
        "lower_gib_hours": _json_number(lower_hours),
        "upper_gib_hours": _json_number(upper_hours),
        "reduction_byte_seconds": _json_number(scenario.reduction_byte_seconds),  # type: ignore[attr-defined]
        "reduction_gib_hours": _json_number(reduction_hours),
        "retained_bytes_at_t0": scenario.retained_bytes_at_t0,  # type: ignore[attr-defined]
        "immediate_removal_bytes": scenario.immediate_removal_bytes,  # type: ignore[attr-defined]
        "immediate_removal_count": scenario.immediate_removal_count,  # type: ignore[attr-defined]
        "immediate_removals": [
            {"repository": repository, "artifact_id": artifact_id}
            for repository, artifact_id in scenario.immediate_removals  # type: ignore[attr-defined]
        ],
        "series": [
            {
                "at": format_timestamp(instant),
                "day": _json_number(_days_between(scenario, instant)),
                "retained_bytes": retained,
            }
            for instant, retained in scenario.series  # type: ignore[attr-defined]
        ],
        "bound_note": (
            "Lower total equals the known-expiry point forecast (unknown expiry contributes 0). "
            "Upper total adds a full-horizon contribution for unknown-expiry bytes. "
            "Equivalent to GitHub's documented binary GB-hours, and not a monetary saving."
        ),
    }


def _days_between(scenario: object, instant: datetime) -> Fraction:
    # The scenario series is anchored at forecast.t0. Callers pass Forecast via closure
    # in _scenario_dict through the scenario only, so compute from the first series point.
    start = scenario.series[0][0]  # type: ignore[attr-defined]
    delta = instant - start
    micros = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
    return Fraction(micros, 86_400_000_000)


def _artifact_dict(row: object) -> dict[str, object]:
    artifact = row.artifact  # type: ignore[attr-defined]
    return {
        "repository": artifact.repository,
        "artifact_id": artifact.artifact_id,
        "name": artifact.name,
        "size_bytes": artifact.size_bytes,
        "size_gib": format_gib(artifact.size_bytes),
        "size_decimal_gb": format_decimal_gb(artifact.size_bytes),
        "created_at": format_timestamp(artifact.created_at),
        "expires_at": format_timestamp(artifact.expires_at),
        "expired_flag": artifact.expired,
        "expired_at_t0": row.expired_at_t0,  # type: ignore[attr-defined]
        "unknown_expiry": row.unknown_expiry,  # type: ignore[attr-defined]
        "retained": row.retained,  # type: ignore[attr-defined]
        "protection_reason": artifact.protection_reason,
        "workflow_run_id": artifact.workflow_run_id,
        "head_sha": artifact.head_sha,
        "head_branch": artifact.head_branch,
        "snapshot_at": format_timestamp(artifact.snapshot_at),
        "source_file": artifact.source_file,
        "source_record_index": artifact.source_record_index,
        "baseline_expiry": format_timestamp(row.baseline_expiry),  # type: ignore[attr-defined]
        "cap_3_expiry": format_timestamp(row.cap_3_expiry),  # type: ignore[attr-defined]
        "cap_7_expiry": format_timestamp(row.cap_7_expiry),  # type: ignore[attr-defined]
        "cap_30_expiry": format_timestamp(row.cap_30_expiry),  # type: ignore[attr-defined]
        "hypothetical_immediate_removal": {
            "cap_3": row.immediate_removal_3,  # type: ignore[attr-defined]
            "cap_7": row.immediate_removal_7,  # type: ignore[attr-defined]
            "cap_30": row.immediate_removal_30,  # type: ignore[attr-defined]
        },
    }


def _timeline_dict(row: object, t0: datetime) -> dict[str, object]:
    body = _artifact_dict(row)
    body["status"] = _status_label(row)
    body["expires_within_7_days"] = _expires_soon(row, t0)
    return body


def _diagnostic_dict(item: Diagnostic) -> dict[str, object]:
    return {
        "level": item.level,
        "code": item.code,
        "message": item.message,
        "repository": item.repository,
        "source_file": item.source_file,
        "artifact_id": item.artifact_id,
        "source_record_index": item.source_record_index,
    }


def _repository_dicts(snapshot: Snapshot, forecast: Forecast) -> list[dict[str, object]]:
    stats: dict[str, dict[str, object]] = {}
    for source in snapshot.repositories:
        stats[source.repository] = {
            "repository": source.repository,
            "complete": source.complete,
            "snapshot_at": format_timestamp(source.snapshot_at),
            "retained_count": 0,
            "retained_bytes": 0,
            "protected_bytes": 0,
            "unknown_expiry_bytes": 0,
            "expired_count": 0,
            "expired_bytes": 0,
            "distinct_artifacts": 0,
        }
    for row in forecast.artifacts:
        bucket = stats.setdefault(
            row.artifact.repository,
            {
                "repository": row.artifact.repository,
                "complete": False,
                "snapshot_at": format_timestamp(row.artifact.snapshot_at),
                "retained_count": 0,
                "retained_bytes": 0,
                "protected_bytes": 0,
                "unknown_expiry_bytes": 0,
                "expired_count": 0,
                "expired_bytes": 0,
                "distinct_artifacts": 0,
            },
        )
        bucket["distinct_artifacts"] = int(bucket["distinct_artifacts"]) + 1
        if row.expired_at_t0:
            bucket["expired_count"] = int(bucket["expired_count"]) + 1
            bucket["expired_bytes"] = int(bucket["expired_bytes"]) + row.artifact.size_bytes
        if row.retained:
            bucket["retained_count"] = int(bucket["retained_count"]) + 1
            bucket["retained_bytes"] = int(bucket["retained_bytes"]) + row.artifact.size_bytes
            if is_protected(row.artifact):
                bucket["protected_bytes"] = int(bucket["protected_bytes"]) + row.artifact.size_bytes
            if row.unknown_expiry:
                bucket["unknown_expiry_bytes"] = int(bucket["unknown_expiry_bytes"]) + row.artifact.size_bytes
    rows = list(stats.values())
    rows.sort(key=lambda item: (-int(item["retained_bytes"]), str(item["repository"])))
    for row in rows:
        row["retained_gib"] = format_gib(int(row["retained_bytes"]))
        row["share_display"] = _percent(int(row["retained_bytes"]), forecast.retained_bytes)
    return rows


def _age_dicts(forecast: Forecast) -> list[dict[str, object]]:
    counts = {key: {"count": 0, "bytes": 0} for key, _label in AGE_BUCKETS}
    for row in forecast.artifacts:
        if not row.retained:
            continue
        key = age_bucket_key(row.artifact.created_at, forecast.t0)
        counts[key]["count"] += 1
        counts[key]["bytes"] += row.artifact.size_bytes
    rendered = []
    for key, label in AGE_BUCKETS:
        rendered.append(
            {
                "key": key,
                "label": label,
                "count": counts[key]["count"],
                "bytes": counts[key]["bytes"],
                "gib": format_gib(counts[key]["bytes"]),
            }
        )
    return rendered


def _largest(forecast: Forecast, *, limit: int) -> list[object]:
    retained = [row for row in forecast.artifacts if row.retained]
    retained.sort(
        key=lambda row: (
            -row.artifact.size_bytes,
            row.artifact.repository,
            row.artifact.artifact_id,
        )
    )
    return retained[:limit]


def _sorted_rows(forecast: Forecast) -> list[object]:
    return sorted(
        forecast.artifacts,
        key=lambda row: (row.artifact.repository, row.artifact.artifact_id),
    )


def _timeline(forecast: Forecast) -> list[object]:
    return sorted(forecast.artifacts, key=_timeline_sort_key)


def _timeline_sort_key(row: object) -> tuple[object, ...]:
    expiry = row.artifact.expires_at or row.baseline_expiry  # type: ignore[attr-defined]
    return (
        bool(row.unknown_expiry),  # type: ignore[attr-defined]
        bool(row.expired_at_t0),  # type: ignore[attr-defined]
        expiry or datetime.max.replace(tzinfo=forecast_timezone(row)),
        row.artifact.repository,  # type: ignore[attr-defined]
        row.artifact.artifact_id,  # type: ignore[attr-defined]
    )


def forecast_timezone(row: object) -> object:
    return row.artifact.created_at.tzinfo  # type: ignore[attr-defined]


def _expires_soon(row: object, t0: datetime) -> bool:
    """Protected retained evidence whose imported expiry falls within 7 days after t0."""

    if not is_protected(row.artifact) or row.expired_at_t0:  # type: ignore[attr-defined]
        return False
    expires = row.artifact.expires_at  # type: ignore[attr-defined]
    if expires is None:
        return False
    return t0 < expires <= t0 + timedelta(days=7)


def _status_label(row: object) -> str:
    if row.expired_at_t0:  # type: ignore[attr-defined]
        return "expired"
    if row.unknown_expiry:  # type: ignore[attr-defined]
        return "unknown_expiry"
    return "retained"


def _status(diagnostics: tuple[Diagnostic, ...]) -> str:
    if any(item.level == "error" for item in diagnostics):
        return "errors"
    if any(item.level == "warning" for item in diagnostics):
        return "review"
    return "neutral"


def _where(item: Diagnostic) -> str:
    parts = []
    if item.repository:
        parts.append(item.repository)
    if item.artifact_id is not None:
        parts.append(f"artifact {item.artifact_id}")
    if item.source_file:
        parts.append(item.source_file)
    return " ".join(parts)


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _percent(part: int, whole: int) -> str:
    if whole <= 0:
        return "0"
    ratio = (Decimal(part) * Decimal(100) / Decimal(whole)).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP
    )
    return format(ratio, "f")


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _scope_section(
    snapshot: Snapshot,
    forecast: Forecast,
    diagnostics: tuple[Diagnostic, ...],
    status: str,
) -> str:
    warnings = [item for item in diagnostics if item.level == "warning"]
    errors = [item for item in diagnostics if item.level == "error"]
    if status == "errors":
        banner = (
            '<div class="banner banner-bad" id="warnings">'
            "<h2>Errors block a normal reading of this snapshot</h2>"
            "<p>Fix the rejected records before treating any total as the inventory.</p>"
            "</div>"
        )
    elif status == "review":
        banner = (
            '<div class="banner banner-review" id="warnings">'
            "<h2>Review required before using these totals</h2>"
            "<p>This report lists partial imports, uncertainty, and protection limits up front. "
            "It does not certify retention or coverage beyond the manifest.</p>"
            "</div>"
        )
    else:
        banner = (
            '<div class="banner banner-neutral" id="warnings">'
            "<h2>No warning-level diagnostics for the listed repositories</h2>"
            "<p>This report does not certify retention, snapshot atomicity, or coverage beyond the manifest.</p>"
            "</div>"
        )
    repo_rows = []
    for source in snapshot.repositories:
        completeness = "complete assertion: true" if source.complete else "complete assertion: false (incomplete)"
        repo_rows.append(
            "<tr>"
            f"<td>{_esc(source.repository)}</td>"
            f"<td>{_esc(completeness)}</td>"
            f"<td>{_esc(format_timestamp(source.snapshot_at))}</td>"
            f"<td>{len(source.files)}</td>"
            "</tr>"
        )
    warning_items = "".join(f"<li>{_esc(item.message)}</li>" for item in warnings) or "<li>None.</li>"
    error_items = "".join(f"<li>{_esc(item.message)}</li>" for item in errors)
    error_block = (
        f"<h3>Errors</h3><ul>{error_items}</ul>" if errors else ""
    )
    return (
        '<section id="scope">'
        "<h2>Snapshot scope</h2>"
        f"{banner}"
        "<dl class=\"facts\">"
        f"<div><dt>As of</dt><dd>{_esc(format_timestamp(forecast.t0))}</dd></div>"
        f"<div><dt>Horizon</dt><dd>{forecast.horizon_days} days, ending {_esc(format_timestamp(forecast.horizon_end))}</dd></div>"
        f"<div><dt>Repositories in the manifest</dt><dd>{len(snapshot.repositories)}</dd></div>"
        f"<div><dt>Tool</dt><dd>artifactbudget {_esc(__version__)}</dd></div>"
        "</dl>"
        "<p>Repositories absent from the manifest are outside this report. "
        "A matching exported total_count does not prove a complete or atomic snapshot. "
        "The completeness column repeats the manifest assertion.</p>"
        '<div class="wrap"><table>'
        "<caption>Manifest repositories and collection times</caption>"
        "<thead><tr><th scope=\"col\">Repository</th><th scope=\"col\">Completeness assertion</th>"
        "<th scope=\"col\">Snapshot time</th><th scope=\"col\">Pages</th></tr></thead>"
        f"<tbody>{''.join(repo_rows) or '<tr><td colspan=\"4\">No repositories.</td></tr>'}</tbody>"
        "</table></div>"
        f"{error_block}"
        "<h3>Warnings</h3>"
        f"<ul>{warning_items}</ul>"
        "</section>"
    )


def _current_section(forecast: Forecast) -> str:
    cards = [
        ("Current retained artifacts", str(forecast.retained_count), "Count still stored at t0"),
        ("Current retained bytes", f"{forecast.retained_bytes:,}", format_gib(forecast.retained_bytes) + " GiB"),
        ("Protected retained bytes", f"{forecast.protected_bytes:,}", "Subset of current retained bytes"),
        (
            "Unknown-expiry bytes",
            f"{forecast.unknown_expiry_bytes:,}",
            "Subset of current retained bytes, excluded from point forecasts",
        ),
        ("Already expired", str(forecast.expired_count), f"{forecast.expired_bytes:,} bytes, excluded"),
    ]
    rendered = []
    for title, primary, detail in cards:
        rendered.append(
            '<article class="card">'
            f"<h3>{_esc(title)}</h3>"
            f"<p class=\"metric\">{_esc(primary)}</p>"
            f"<p>{_esc(detail)}</p>"
            "</article>"
        )
    return (
        "<section id=\"current\">"
        "<h2>Current inventory</h2>"
        "<p>These figures are the snapshot at t0. Hypothetical 3-, 7-, and 30-day caps do not change them. "
        "Protected bytes and unknown-expiry bytes are subsets of current retained bytes and may overlap. "
        f"Decimal GB for the retained total, display only: {_esc(format_decimal_gb(forecast.retained_bytes))}.</p>"
        f"<div class=\"cards\">{''.join(rendered)}</div>"
        "</section>"
    )


def _repository_section(snapshot: Snapshot, forecast: Forecast) -> str:
    repos = _repository_dicts(snapshot, forecast)
    body = []
    for repo in repos:
        width = 0 if forecast.retained_bytes <= 0 else 240 * int(repo["retained_bytes"]) / forecast.retained_bytes
        body.append(
            "<tr>"
            f"<td>{_esc(repo['repository'])}</td>"
            f"<td>{_esc('incomplete' if not repo['complete'] else 'asserted complete')}</td>"
            f"<td class=\"num\">{int(repo['retained_count'])}</td>"
            f"<td class=\"num\">{int(repo['retained_bytes']):,}</td>"
            f"<td class=\"num\">{_esc(repo['retained_gib'])}</td>"
            f"<td class=\"num\">{_esc(repo['share_display'])}%</td>"
            f"<td class=\"num\">{int(repo['protected_bytes']):,}</td>"
            f"<td class=\"num\">{int(repo['unknown_expiry_bytes']):,}</td>"
            "<td>"
            f'<svg class="bar" viewBox="0 0 240 12" role="img" aria-label="Share of retained bytes {_esc(repo["share_display"])} percent">'
            f'<rect x="0" y="1" width="{width:.2f}" height="10" rx="2"></rect>'
            "</svg></td>"
            "</tr>"
        )
    largest = []
    for row in _largest(forecast, limit=10):
        age_days = (forecast.t0 - row.artifact.created_at).days
        largest.append(
            "<tr>"
            f"<td>{_esc(row.artifact.repository)}</td>"
            f"<td class=\"num\">{row.artifact.artifact_id}</td>"
            f"<td>{_esc(row.artifact.name)}</td>"
            f"<td class=\"num\">{row.artifact.size_bytes:,}</td>"
            f"<td class=\"num\">{_esc(format_gib(row.artifact.size_bytes))}</td>"
            f"<td class=\"num\">{age_days}</td>"
            f"<td>{_esc(format_timestamp(row.artifact.expires_at) or 'Unknown')}</td>"
            f"<td>{_esc(row.artifact.protection_reason or '')}</td>"
            "</tr>"
        )
    ages = []
    for bucket in _age_dicts(forecast):
        ages.append(
            "<tr>"
            f"<td>{_esc(bucket['label'])}</td>"
            f"<td class=\"num\">{bucket['count']}</td>"
            f"<td class=\"num\">{int(bucket['bytes']):,}</td>"
            f"<td class=\"num\">{_esc(bucket['gib'])}</td>"
            "</tr>"
        )
    return (
        "<section id=\"repositories\">"
        "<h2>Repository breakdown and largest artifacts</h2>"
        "<p>Repositories are ordered by retained bytes, then name. "
        "Incomplete means the manifest said <code>complete: false</code>.</p>"
        '<div class="wrap"><table>'
        "<caption>Retained bytes by repository</caption>"
        "<thead><tr>"
        "<th scope=\"col\">Repository</th><th scope=\"col\">Manifest</th><th scope=\"col\">Retained count</th>"
        "<th scope=\"col\">Retained bytes</th><th scope=\"col\">GiB</th><th scope=\"col\">Share</th>"
        "<th scope=\"col\">Protected bytes</th><th scope=\"col\">Unknown-expiry bytes</th><th scope=\"col\">Share bar</th>"
        "</tr></thead>"
        f"<tbody>{''.join(body) or '<tr><td colspan=\"9\">No repositories.</td></tr>'}</tbody>"
        "</table></div>"
        "<h3>Age of current retained artifacts</h3>"
        '<div class="wrap"><table>'
        "<caption>Age buckets use half-open day bounds at t0. The last bucket is 90 days or more.</caption>"
        "<thead><tr><th scope=\"col\">Age</th><th scope=\"col\">Count</th><th scope=\"col\">Bytes</th><th scope=\"col\">GiB</th></tr></thead>"
        f"<tbody>{''.join(ages)}</tbody></table></div>"
        "<h3>Largest retained artifacts</h3>"
        '<div class="wrap"><table>'
        "<caption>Up to 10 retained artifacts by size. The CSV has every row.</caption>"
        "<thead><tr><th scope=\"col\">Repository</th><th scope=\"col\">ID</th><th scope=\"col\">Name</th>"
        "<th scope=\"col\">Bytes</th><th scope=\"col\">GiB</th><th scope=\"col\">Age (days)</th>"
        "<th scope=\"col\">Expires</th><th scope=\"col\">Protection</th></tr></thead>"
        f"<tbody>{''.join(largest) or '<tr><td colspan=\"8\">No retained artifacts.</td></tr>'}</tbody>"
        "</table></div>"
        "</section>"
    )


def _forecast_section(forecast: Forecast) -> str:
    chart = _chart_svg(forecast)
    header = (
        "<tr><th scope=\"col\">Day</th><th scope=\"col\">At</th>"
        + "".join(
            f"<th scope=\"col\">{_esc(scenario.label)} bytes</th>" for scenario in forecast.scenarios
        )
        + "</tr>"
    )
    times = sorted({instant for scenario in forecast.scenarios for instant, _value in scenario.series})
    body = []
    for instant in times:
        cells = "".join(
            f"<td class=\"num\">{_value_at(scenario.series, instant):,}</td>"
            for scenario in forecast.scenarios
        )
        day = format_gib_hours(_days_between_forecast(forecast.t0, instant))
        body.append(
            "<tr>"
            f"<td class=\"num\">{_esc(day)}</td>"
            f"<td>{_esc(format_timestamp(instant))}</td>"
            f"{cells}</tr>"
        )
    scenario_rows = []
    for scenario in forecast.scenarios:
        known = to_gib_hours(scenario.known_byte_seconds)
        lower = to_gib_hours(scenario.lower_byte_seconds)
        upper = to_gib_hours(scenario.upper_byte_seconds)
        reduction = to_gib_hours(scenario.reduction_byte_seconds)
        removal = (
            f"{scenario.immediate_removal_count} artifact(s), {scenario.immediate_removal_bytes:,} bytes"
            if scenario.immediate_removal_count
            else "None"
        )
        scenario_rows.append(
            "<tr>"
            f"<td>{_esc(scenario.label)}</td>"
            f"<td class=\"num\">{_esc(format_gib_hours(known))}</td>"
            f"<td class=\"num\">{_esc(format_gib_hours(lower))}</td>"
            f"<td class=\"num\">{_esc(format_gib_hours(upper))}</td>"
            f"<td class=\"num\">{_esc(format_gib_hours(reduction))}</td>"
            f"<td class=\"num\">{scenario.retained_bytes_at_t0:,}</td>"
            f"<td>{_esc(removal)}</td>"
            "</tr>"
        )
    removal_notes = []
    for scenario in forecast.scenarios:
        if scenario.immediate_removal_count:
            removal_notes.append(
                f"<li>{_esc(scenario.label)}: {scenario.immediate_removal_count} artifact(s) "
                f"totaling {scenario.immediate_removal_bytes:,} bytes are already at least as old as the cap. "
                "The scenario simulates them leaving at t0. This is a hypothetical immediate removal, "
                "not a deletion ArtifactBudget performed.</li>"
            )
    removal_block = (
        f"<ul>{''.join(removal_notes)}</ul>" if removal_notes else "<p>No cap simulates an immediate removal for this cohort.</p>"
    )
    return (
        "<section id=\"forecast\">"
        "<h2>Baseline versus 3-, 7-, and 30-day caps</h2>"
        "<p>The chart and the first numeric column are the known-expiry point forecast, in bytes, "
        "from t0 through the horizon. Unknown-expiry bytes are not drawn. "
        "GiB-hours below are equivalent to GitHub's documented binary GB-hours. "
        "Reduction versus baseline is not a refund and not a guaranteed monetary saving.</p>"
        f"{chart}"
        '<ul class="legend">'
        '<li><span class="swatch swatch-baseline"></span> Baseline (dashed)</li>'
        '<li><span class="swatch swatch-30"></span> 30-day cap</li>'
        '<li><span class="swatch swatch-7"></span> 7-day cap</li>'
        '<li><span class="swatch swatch-3"></span> 3-day cap</li>'
        "</ul>"
        "<h3>Hypothetical immediate removal</h3>"
        f"{removal_block}"
        "<h3>Scenario totals</h3>"
        '<div class="wrap"><table>'
        "<caption>Known-expiry GiB-hours over the horizon, with an explicit unknown-expiry bound.</caption>"
        "<thead><tr>"
        "<th scope=\"col\">Scenario</th>"
        "<th scope=\"col\">Known GiB-hours</th>"
        "<th scope=\"col\">Lower bound</th>"
        "<th scope=\"col\">Upper bound</th>"
        "<th scope=\"col\">Reduction vs baseline</th>"
        "<th scope=\"col\">Known bytes retained at t0</th>"
        "<th scope=\"col\">Hypothetical immediate removal</th>"
        "</tr></thead>"
        f"<tbody>{''.join(scenario_rows)}</tbody></table></div>"
        "<p>The lower bound is the point forecast plus zero for unknown expiry. "
        "The upper bound adds the full-horizon contribution of unknown-expiry bytes. "
        f"Unknown-expiry bytes in this snapshot: {forecast.unknown_expiry_bytes:,}.</p>"
        "<h3>Retained bytes at each step</h3>"
        '<div class="wrap"><table id="forecast-table">'
        "<caption>Known-expiry retained bytes at each expiry step. "
        "At an exact expiry timestamp that artifact is already excluded.</caption>"
        f"<thead>{header}</thead><tbody>{''.join(body)}</tbody></table></div>"
        "</section>"
    )


def _days_between_forecast(start: datetime, instant: datetime) -> Fraction:
    delta = instant - start
    micros = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
    return Fraction(micros, 86_400_000_000)


def _value_at(series: tuple[tuple[datetime, int], ...], when: datetime) -> int:
    current = series[0][1]
    for instant, value in series:
        if instant <= when:
            current = value
        else:
            break
    return current


def _chart_svg(forecast: Forecast) -> str:
    width, height = 720, 320
    left, right, top, bottom = 64, 16, 16, 36
    plot_w = width - left - right
    plot_h = height - top - bottom
    max_bytes = max((value for scenario in forecast.scenarios for _instant, value in scenario.series), default=0)
    max_bytes = max(max_bytes, 1)
    span = forecast.horizon_days * 86400

    def xy(instant: datetime, value: int) -> tuple[float, float]:
        delta = instant - forecast.t0
        seconds = delta.days * 86400 + delta.seconds + delta.microseconds / 1_000_000
        x = left + (seconds / span) * plot_w
        y = top + plot_h - (value / max_bytes) * plot_h
        return x, y

    colors = {
        "baseline": "#d5dced",
        "cap_30": "#b7a6ff",
        "cap_7": "#7ee0d6",
        "cap_3": "#8ee8b8",
    }
    paths = []
    for scenario in forecast.scenarios:
        commands: list[str] = []
        for index, (instant, value) in enumerate(scenario.series):
            x, y = xy(instant, value)
            if index == 0:
                commands.append(f"M {x:.2f} {y:.2f}")
                continue
            prev_instant, prev_value = scenario.series[index - 1]
            _prev_x, prev_y = xy(prev_instant, prev_value)
            commands.append(f"L {x:.2f} {prev_y:.2f}")
            commands.append(f"L {x:.2f} {y:.2f}")
        dash = ' stroke-dasharray="6 4"' if scenario.name == "baseline" else ""
        paths.append(
            f'<path d="{" ".join(commands)}" fill="none" stroke="{colors[scenario.name]}" '
            f'stroke-width="2.5"{dash}></path>'
        )
    grid = []
    for fraction in (0, 0.5, 1):
        y = top + plot_h - fraction * plot_h
        label = format_gib(int(round(max_bytes * fraction)))
        grid.append(
            f'<line x1="{left}" y1="{y:.2f}" x2="{width - right}" y2="{y:.2f}" class="grid"></line>'
            f'<text x="{left - 8}" y="{y + 4:.2f}" text-anchor="end" class="axis">{_esc(label)}</text>'
        )
    ticks = [day for day in (0, 3, 7, 14, 30, 60, 90, forecast.horizon_days) if 0 <= day <= forecast.horizon_days]
    tick_labels = []
    seen_days: set[int] = set()
    for day in ticks:
        if day in seen_days:
            continue
        seen_days.add(day)
        x = left + (day / forecast.horizon_days) * plot_w
        tick_labels.append(
            f'<text x="{x:.2f}" y="{height - 12}" text-anchor="middle" class="axis">{day}</text>'
        )
    end_values = ", ".join(
        f"{scenario.label} {scenario.series[-1][1]:,} bytes" for scenario in forecast.scenarios
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-labelledby="forecast-chart-title">'
        '<title id="forecast-chart-title">Known-expiry storage step-down through the horizon</title>'
        f'<desc>Step lines of retained bytes. At the horizon: {_esc(end_values)}. '
        "The data table under this chart lists every plotted value.</desc>"
        f'<rect x="{left}" y="{top}" width="{plot_w}" height="{plot_h}" class="plot"></rect>'
        + "".join(grid)
        + "".join(paths)
        + "".join(tick_labels)
        + f'<text x="{left + plot_w / 2:.2f}" y="{height - 1}" text-anchor="middle" class="axis">Days from report</text>'
        "</svg>"
    )


def _timeline_section(forecast: Forecast) -> str:
    soon = []
    retained_rows = []
    expired_rows = []
    for row in _timeline(forecast):
        if row.expired_at_t0:
            expired_rows.append(row)
        else:
            retained_rows.append(row)
        if is_protected(row.artifact) and (
            row.expired_at_t0
            or (
                row.artifact.expires_at is not None
                and forecast.t0 < row.artifact.expires_at <= forecast.t0.replace() + _seven_days()
            )
        ):
            soon.append(row)
    soon_items = []
    for row in soon:
        soon_items.append(
            "<li>"
            f"<strong>{_esc(row.artifact.repository)} #{row.artifact.artifact_id}</strong> "
            f"{_esc(row.artifact.name)} — {_esc(row.artifact.protection_reason)}. "
            f"Imported expiry {_esc(format_timestamp(row.artifact.expires_at) or 'unknown')}. "
            "Protection does not preserve this file."
            "</li>"
        )
    if soon_items:
        callout = (
            '<div class="banner banner-review">'
            "<h3>Protected evidence expiring within 7 days, or already expired</h3>"
            "<p>A protection label only excludes an artifact from shortened-retention simulations. "
            "It does not extend GitHub retention. GitHub can still delete these files on the imported schedule.</p>"
            f"<ul>{''.join(soon_items)}</ul>"
            "</div>"
        )
    else:
        callout = (
            "<p>No protected artifact in this snapshot is already expired or due within 7 days. "
            "Protection would still not extend GitHub retention.</p>"
        )
    return (
        "<section id=\"timeline\">"
        "<h2>Projected expiration timeline</h2>"
        f"{callout}"
        "<h3>Retained artifacts</h3>"
        + _timeline_table(retained_rows, "Current retained artifacts and their scenario expiries")
        + "<h3>Already expired at t0</h3>"
        + _timeline_table(expired_rows, "Expired rows are excluded from current retained bytes and from future storage-hours")
        + "</section>"
    )


def _seven_days():
    from datetime import timedelta

    return timedelta(days=7)


def _timeline_table(rows: list[object], caption: str) -> str:
    body = []
    for row in rows:
        removal = []
        if row.immediate_removal_3:
            removal.append("3-day")
        if row.immediate_removal_7:
            removal.append("7-day")
        if row.immediate_removal_30:
            removal.append("30-day")
        removal_text = ", ".join(removal) if removal else ""
        body.append(
            "<tr>"
            f"<td>{_esc(row.artifact.repository)}</td>"
            f"<td class=\"num\">{row.artifact.artifact_id}</td>"
            f"<td>{_esc(row.artifact.name)}</td>"
            f"<td class=\"num\">{row.artifact.size_bytes:,}</td>"
            f"<td>{_esc(row.artifact.protection_reason or '')}</td>"
            f"<td>{_esc(_status_label(row))}</td>"
            f"<td>{_esc(format_timestamp(row.baseline_expiry) or 'Unknown')}</td>"
            f"<td>{_esc(format_timestamp(row.cap_3_expiry) or 'Unknown')}</td>"
            f"<td>{_esc(format_timestamp(row.cap_7_expiry) or 'Unknown')}</td>"
            f"<td>{_esc(format_timestamp(row.cap_30_expiry) or 'Unknown')}</td>"
            f"<td>{_esc(removal_text)}</td>"
            "</tr>"
        )
    return (
        '<div class="wrap"><table>'
        f"<caption>{_esc(caption)}</caption>"
        "<thead><tr>"
        "<th scope=\"col\">Repository</th><th scope=\"col\">ID</th><th scope=\"col\">Name</th>"
        "<th scope=\"col\">Bytes</th><th scope=\"col\">Protection</th><th scope=\"col\">Status</th>"
        "<th scope=\"col\">Baseline expiry</th><th scope=\"col\">3-day effective</th>"
        "<th scope=\"col\">7-day effective</th><th scope=\"col\">30-day effective</th>"
        "<th scope=\"col\">Hypothetical immediate removal</th>"
        "</tr></thead>"
        f"<tbody>{''.join(body) or '<tr><td colspan=\"11\">None.</td></tr>'}</tbody>"
        "</table></div>"
    )


def _closing_section(
    snapshot: Snapshot,
    forecast: Forecast,
    diagnostics: tuple[Diagnostic, ...],
) -> str:
    matches = []
    for row in _sorted_rows(forecast):
        if not is_protected(row.artifact):
            continue
        matches.append(
            "<tr>"
            f"<td>{_esc(row.artifact.repository)}</td>"
            f"<td class=\"num\">{row.artifact.artifact_id}</td>"
            f"<td>{_esc(row.artifact.protection_reason)}</td>"
            f"<td>{_esc(format_timestamp(row.artifact.expires_at) or 'Unknown')}</td>"
            "</tr>"
        )
    unknown_targets = [
        item
        for item in diagnostics
        if item.code == "unknown_policy_target"
    ]
    unknown_items = "".join(
        f"<li>{_esc(item.message)}</li>" for item in unknown_targets
    ) or "<li>None.</li>"
    rejected = [item for item in diagnostics if item.level == "error"]
    rejected_items = "".join(f"<li>{_esc(item.message)}</li>" for item in rejected) or "<li>No rejected records.</li>"
    assumptions = "".join(f"<li>{_esc(text)}</li>" for text in ASSUMPTIONS)
    limitations = "".join(f"<li>{_esc(text)}</li>" for text in LIMITATIONS)
    hashes = [
        "<tr>"
        f"<td>manifest</td><td>{_esc(snapshot.manifest_path)}</td>"
        f"<td><code>{_esc(snapshot.manifest_sha256)}</code></td></tr>"
    ]
    if snapshot.policy_path:
        hashes.append(
            "<tr><td>policy</td>"
            f"<td>{_esc(snapshot.policy_path)}</td>"
            f"<td><code>{_esc(snapshot.policy_sha256 or '')}</code></td></tr>"
        )
    for source in snapshot.files:
        hashes.append(
            "<tr>"
            f"<td>{_esc(source.repository)} page {source.page_number}</td>"
            f"<td>{_esc(source.path)}</td>"
            f"<td><code>{_esc(source.sha256 or '')}</code></td>"
            "</tr>"
        )
    notes = [item for item in diagnostics if item.level == "info"]
    note_items = "".join(f"<li>{_esc(item.message)}</li>" for item in notes) or "<li>None.</li>"
    return (
        "<section id=\"policy\">"
        "<h2>Policy matches, rejected records, and assumptions</h2>"
        "<h3>Limitations</h3>"
        f"<ul>{limitations}</ul>"
        "<h3>Protection matches</h3>"
        "<p>Matches use the exact repository and artifact id from the local policy file. "
        "Nothing was inferred from names or branches.</p>"
        '<div class="wrap"><table>'
        "<caption>Imported artifacts with a protection reason</caption>"
        "<thead><tr><th scope=\"col\">Repository</th><th scope=\"col\">ID</th>"
        "<th scope=\"col\">Reason</th><th scope=\"col\">Imported expiry</th></tr></thead>"
        f"<tbody>{''.join(matches) or '<tr><td colspan=\"4\">No protected artifacts were imported.</td></tr>'}</tbody>"
        "</table></div>"
        "<h3>Policy ids that matched nothing</h3>"
        f"<ul>{unknown_items}</ul>"
        "<h3>Rejected records</h3>"
        f"<ul>{rejected_items}</ul>"
        "<h3>Notes</h3>"
        f"<ul>{note_items}</ul>"
        "<h3>Calculation assumptions</h3>"
        f"<ol>{assumptions}</ol>"
        "<h3>Input SHA-256</h3>"
        '<div class="wrap"><table>'
        "<caption>Hashes of the manifest, policy, and export pages that were read</caption>"
        "<thead><tr><th scope=\"col\">Input</th><th scope=\"col\">Path</th><th scope=\"col\">SHA-256</th></tr></thead>"
        f"<tbody>{''.join(hashes)}</tbody></table></div>"
        "</section>"
    )


_CSS = """
:root {
  --bg: #0c1222;
  --card: #141b2e;
  --ink: #e8eef9;
  --muted: #b7c3da;
  --line: #2c3854;
  --amber: #ffcf70;
  --amber-bg: #2c2416;
  --bad: #ffb3bc;
  --bad-bg: #2c1820;
  --neutral-bg: #172033;
  --baseline: #d5dced;
  --cap-30: #b7a6ff;
  --cap-7: #7ee0d6;
  --cap-3: #8ee8b8;
  --bar: #7ee0d6;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font: 16px/1.5 "Segoe UI", system-ui, sans-serif;
}
header, main, footer { max-width: 1080px; margin: 0 auto; padding: 0 20px; }
header { padding-top: 28px; }
footer { padding: 12px 20px 40px; color: var(--muted); }
h1 { font-size: 2rem; line-height: 1.2; margin: 0 0 8px; }
h2 { margin: 0 0 12px; font-size: 1.4rem; }
h3 { margin: 20px 0 8px; font-size: 1.05rem; }
p { margin: 0 0 12px; }
.eyebrow { margin: 0; color: var(--cap-7); letter-spacing: 0.04em; text-transform: uppercase; font-size: 0.8rem; }
.skip {
  position: absolute; left: 12px; top: -40px; background: #fff; color: #000; padding: 6px 10px;
}
.skip:focus { top: 8px; }
section { margin: 28px 0; }
.banner { border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; margin: 12px 0; background: var(--neutral-bg); }
.banner-review { border-color: var(--amber); background: var(--amber-bg); }
.banner-bad { border-color: var(--bad); background: var(--bad-bg); }
.banner h2, .banner h3 { margin-top: 0; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; }
.card, .facts { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; }
.facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px 16px; margin: 12px 0; }
.facts div { margin: 0; }
dt { color: var(--muted); font-size: 0.85rem; }
dd { margin: 0; font-weight: 650; }
.metric { font-size: 1.35rem; font-weight: 700; margin: 4px 0; font-variant-numeric: tabular-nums; }
.wrap { overflow-x: auto; border: 1px solid var(--line); border-radius: 12px; }
table { width: 100%; border-collapse: collapse; background: var(--card); }
caption { caption-side: bottom; text-align: left; color: var(--muted); padding: 8px 10px 12px; }
th, td { padding: 8px 10px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
th { color: var(--muted); font-weight: 650; }
.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
code { font-family: ui-monospace, monospace; font-size: 0.86rem; word-break: break-all; }
svg { width: 100%; height: auto; display: block; }
.plot { fill: transparent; stroke: var(--line); }
.grid { stroke: var(--line); stroke-width: 1; }
.axis { fill: var(--muted); font-size: 12px; }
.bar rect { fill: var(--bar); }
.legend { display: flex; flex-wrap: wrap; gap: 12px 18px; padding: 0; list-style: none; }
.legend li { display: flex; align-items: center; gap: 8px; }
.swatch { width: 28px; height: 8px; display: inline-block; }
.swatch-baseline { background: repeating-linear-gradient(90deg, var(--baseline) 0 6px, transparent 6px 10px); height: 3px; }
.swatch-30 { background: var(--cap-30); }
.swatch-7 { background: var(--cap-7); }
.swatch-3 { background: var(--cap-3); }
@media (max-width: 720px) {
  h1 { font-size: 1.6rem; }
  .cards { grid-template-columns: 1fr; }
}
"""
