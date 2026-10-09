"""Parse local GitHub artifact-list exports. This module does not call GitHub."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path

from artifactbudget.models import (
    Artifact,
    ArtifactBudgetError,
    Diagnostic,
    RepositorySource,
    Snapshot,
    SourceFile,
    TimestampParseError,
    format_timestamp,
    is_repository_name,
    parse_timestamp,
    sorted_diagnostics,
)

MAX_SNAPSHOT_SPAN = timedelta(hours=24)


def load_snapshot(manifest_path: str | Path, *, display_path: str | None = None) -> Snapshot:
    """Load a manifest and the export pages it names.

    Export paths are resolved relative to the manifest directory and cannot
    escape it. Raised ``ArtifactBudgetError`` means the manifest itself cannot
    be used. Record-level problems are diagnostics on the returned snapshot.
    """

    display = display_path if display_path is not None else str(manifest_path)
    path = Path(manifest_path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ArtifactBudgetError(
            f"Could not read manifest {display}: {exc.strerror or exc}"
        ) from exc
    digest = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise ArtifactBudgetError(f"Manifest {display} is not valid UTF-8.") from exc
    except json.JSONDecodeError as exc:
        raise ArtifactBudgetError(
            f"Manifest {display} is not valid JSON ({exc.msg} at line {exc.lineno})."
        ) from exc
    if not isinstance(payload, dict):
        raise ArtifactBudgetError(f"Manifest {display} must be a JSON object.")
    version = payload.get("schema_version")
    if isinstance(version, bool) or version != 1:
        raise ArtifactBudgetError(f"Manifest {display} must set schema_version to 1.")
    try:
        as_of = parse_timestamp(payload.get("as_of"), field="as_of")
    except TimestampParseError as exc:
        raise ArtifactBudgetError(str(exc)) from exc
    repositories = payload.get("repositories")
    if not isinstance(repositories, list):
        raise ArtifactBudgetError(f"Manifest {display} must contain a repositories array.")

    manifest_dir = path.parent
    findings: list[Diagnostic] = []
    files: list[SourceFile] = []
    sources: list[RepositorySource] = []
    parsed: list[Artifact] = []
    used_files: set[str] = set()

    for index, entry in enumerate(repositories):
        repo_artifacts, repo_files, repo_source, repo_findings = _load_repository(
            entry,
            index=index,
            manifest_dir=manifest_dir,
            used_files=used_files,
        )
        findings.extend(repo_findings)
        files.extend(repo_files)
        parsed.extend(repo_artifacts)
        if repo_source is not None:
            sources.append(repo_source)

    artifacts, dedupe_findings = _deduplicate(parsed)
    findings.extend(dedupe_findings)
    findings.extend(_completeness(sources, files, artifacts))
    return Snapshot(
        as_of=as_of,
        artifacts=tuple(sorted(artifacts, key=lambda item: item.key())),
        diagnostics=sorted_diagnostics(findings),
        files=tuple(files),
        repositories=tuple(sorted(sources, key=lambda item: item.repository)),
        manifest_path=display,
        manifest_sha256=digest,
    )


def _load_repository(
    entry: object,
    *,
    index: int,
    manifest_dir: Path,
    used_files: set[str],
) -> tuple[list[Artifact], list[SourceFile], RepositorySource | None, list[Diagnostic]]:
    findings: list[Diagnostic] = []
    if not isinstance(entry, dict):
        findings.append(
            Diagnostic(
                level="error",
                code="manifest_invalid",
                message=f"Repository entry {index} is not an object.",
            )
        )
        return [], [], None, findings

    repository = entry.get("repository")
    if not is_repository_name(repository):
        findings.append(
            Diagnostic(
                level="error",
                code="manifest_invalid",
                message=(
                    f"Repository entry {index} needs repository as owner/name using letters, "
                    "numbers, dots, underscores, or hyphens."
                ),
            )
        )
        return [], [], None, findings
    assert isinstance(repository, str)

    complete = entry.get("complete")
    if not isinstance(complete, bool):
        findings.append(
            Diagnostic(
                level="error",
                code="manifest_invalid",
                message=f"{repository} needs complete as a boolean.",
                repository=repository,
            )
        )
        return [], [], None, findings
    try:
        snapshot_at = parse_timestamp(entry.get("snapshot_at"), field=f"{repository} snapshot_at")
    except TimestampParseError as exc:
        findings.append(
            Diagnostic(
                level="error",
                code="manifest_invalid",
                message=str(exc),
                repository=repository,
            )
        )
        return [], [], None, findings

    listed = entry.get("files")
    if not isinstance(listed, list) or not listed:
        findings.append(
            Diagnostic(
                level="error",
                code="manifest_invalid",
                message=f"{repository} needs a non-empty files array.",
                repository=repository,
            )
        )
        return [], [], None, findings

    unique_files: list[str] = []
    # Compare the confined, canonical identity rather than the spelling in the
    # manifest.  Otherwise ``page.json`` and ``./page.json`` read the same
    # export twice (and symlink aliases can do the same thing).
    seen_here: set[str] = set()
    for raw_name in listed:
        if not isinstance(raw_name, str) or raw_name == "" or raw_name.strip() != raw_name:
            findings.append(
                Diagnostic(
                    level="error",
                    code="path_rejected",
                    message=f"{repository} has a file path that is empty or padded with whitespace.",
                    repository=repository,
                )
            )
            continue
        try:
            identity = str(_resolve_export(manifest_dir, raw_name))
        except ValueError as exc:
            findings.append(
                Diagnostic(
                    level="error",
                    code="path_rejected",
                    message=f"{repository} file {raw_name} was rejected: {exc}",
                    repository=repository,
                    source_file=raw_name,
                )
            )
            continue
        if identity in seen_here:
            findings.append(
                Diagnostic(
                    level="error",
                    code="duplicate_file",
                    message=f"{repository} lists {raw_name} more than once.",
                    repository=repository,
                    source_file=raw_name,
                )
            )
            continue
        seen_here.add(identity)
        if identity in used_files:
            findings.append(
                Diagnostic(
                    level="error",
                    code="file_reused",
                    message=f"{raw_name} is listed for more than one repository.",
                    repository=repository,
                    source_file=raw_name,
                )
            )
            continue
        unique_files.append(raw_name)
    used_files.update(seen_here)

    artifacts: list[Artifact] = []
    file_rows: list[SourceFile] = []
    for page_number, relative in enumerate(unique_files, start=1):
        page_artifacts, source, page_findings = _load_page(
            repository=repository,
            relative=relative,
            page_number=page_number,
            manifest_dir=manifest_dir,
            snapshot_at=snapshot_at,
        )
        artifacts.extend(page_artifacts)
        file_rows.append(source)
        findings.extend(page_findings)

    source = RepositorySource(
        repository=repository,
        snapshot_at=snapshot_at,
        complete=complete,
        files=tuple(unique_files),
    )
    return artifacts, file_rows, source, findings


def _load_page(
    *,
    repository: str,
    relative: str,
    page_number: int,
    manifest_dir: Path,
    snapshot_at: object,
) -> tuple[list[Artifact], SourceFile, list[Diagnostic]]:
    findings: list[Diagnostic] = []
    empty = SourceFile(
        repository=repository,
        path=relative,
        sha256=None,
        page_number=page_number,
        accepted=0,
        rejected=0,
        parsed=False,
        total_count=None,
        total_count_valid=False,
    )
    try:
        target = _resolve_export(manifest_dir, relative)
    except ValueError as exc:
        findings.append(
            Diagnostic(
                level="error",
                code="path_rejected",
                message=f"{repository} file {relative} was rejected: {exc}",
                repository=repository,
                source_file=relative,
            )
        )
        return [], empty, findings
    try:
        raw = target.read_bytes()
    except OSError:
        findings.append(
            Diagnostic(
                level="error",
                code="missing_file",
                message=f"Missing export file {relative} for {repository}.",
                repository=repository,
                source_file=relative,
            )
        )
        return [], empty, findings

    digest = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError:
        findings.append(
            Diagnostic(
                level="error",
                code="invalid_json",
                message=f"{relative} for {repository} is not valid UTF-8.",
                repository=repository,
                source_file=relative,
            )
        )
        return [], _source(empty, digest), findings
    except json.JSONDecodeError as exc:
        findings.append(
            Diagnostic(
                level="error",
                code="invalid_json",
                message=(
                    f"{relative} for {repository} is not valid JSON "
                    f"({exc.msg} at line {exc.lineno})."
                ),
                repository=repository,
                source_file=relative,
            )
        )
        return [], _source(empty, digest), findings
    if not isinstance(payload, dict) or not isinstance(payload.get("artifacts"), list):
        findings.append(
            Diagnostic(
                level="error",
                code="invalid_page",
                message=f"{relative} for {repository} must be an object with an artifacts array.",
                repository=repository,
                source_file=relative,
            )
        )
        return [], _source(empty, digest, parsed=False), findings

    total_count, total_valid, total_findings = _total_count(payload.get("total_count", None), repository, relative)
    findings.extend(total_findings)
    accepted: list[Artifact] = []
    rejected = 0
    for record_index, record in enumerate(payload["artifacts"]):
        artifact, record_findings = _parse_record(
            record,
            repository=repository,
            relative=relative,
            record_index=record_index,
            snapshot_at=snapshot_at,
        )
        findings.extend(record_findings)
        if artifact is None:
            rejected += 1
        else:
            accepted.append(artifact)
    source = SourceFile(
        repository=repository,
        path=relative,
        sha256=digest,
        page_number=page_number,
        accepted=len(accepted),
        rejected=rejected,
        parsed=True,
        total_count=total_count,
        total_count_valid=total_valid,
    )
    return accepted, source, findings


def _source(empty: SourceFile, digest: str, *, parsed: bool = False) -> SourceFile:
    return SourceFile(
        repository=empty.repository,
        path=empty.path,
        sha256=digest,
        page_number=empty.page_number,
        accepted=0,
        rejected=0,
        parsed=parsed,
        total_count=None,
        total_count_valid=False,
    )


def _resolve_export(manifest_dir: Path, listed: str) -> Path:
    raw = Path(listed)
    if raw.is_absolute() or raw.drive or raw.root:
        raise ValueError("path must be relative to the manifest directory")
    if any(part == ".." for part in raw.parts):
        raise ValueError("path must not contain '..'")
    root = manifest_dir.resolve()
    resolved = (manifest_dir / raw).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("path escapes the manifest directory") from exc
    return resolved


def _total_count(
    value: object,
    repository: str,
    relative: str,
) -> tuple[int | None, bool, list[Diagnostic]]:
    if value is None:
        return None, False, [
            Diagnostic(
                level="warning",
                code="total_count_missing",
                message=(
                    f"{relative} for {repository} has no total_count. "
                    "A missing count cannot confirm coverage."
                ),
                repository=repository,
                source_file=relative,
            )
        ]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None, False, [
            Diagnostic(
                level="warning",
                code="invalid_total_count",
                message=(
                    f"{relative} for {repository} has a total_count that is not a "
                    "nonnegative integer. It was ignored."
                ),
                repository=repository,
                source_file=relative,
            )
        ]
    return value, True, []


def _parse_record(
    record: object,
    *,
    repository: str,
    relative: str,
    record_index: int,
    snapshot_at: object,
) -> tuple[Artifact | None, list[Diagnostic]]:
    findings: list[Diagnostic] = []
    prefix = f"{repository} {relative} record {record_index}"
    if not isinstance(record, dict):
        findings.append(
            _reject(prefix, "is not an object.", repository, relative, record_index, None)
        )
        return None, findings

    artifact_id = record.get("id")
    if isinstance(artifact_id, bool) or not isinstance(artifact_id, int) or artifact_id <= 0:
        findings.append(
            _reject(
                prefix,
                "has an invalid id. Expected a positive integer.",
                repository,
                relative,
                record_index,
                None,
            )
        )
        return None, findings

    name = record.get("name")
    if not isinstance(name, str):
        findings.append(
            _reject(prefix, "is missing a string name.", repository, relative, record_index, artifact_id)
        )
        return None, findings

    size = record.get("size_in_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        findings.append(
            _reject(
                prefix,
                "has a size_in_bytes that is not a nonnegative integer.",
                repository,
                relative,
                record_index,
                artifact_id,
            )
        )
        return None, findings

    expired = record.get("expired")
    if not isinstance(expired, bool):
        findings.append(
            _reject(
                prefix,
                "has an expired flag that is not a boolean.",
                repository,
                relative,
                record_index,
                artifact_id,
            )
        )
        return None, findings

    try:
        created_at = parse_timestamp(record.get("created_at"), field=f"{prefix} created_at")
    except TimestampParseError as exc:
        findings.append(
            _reject(prefix, str(exc), repository, relative, record_index, artifact_id)
        )
        return None, findings

    expires_raw = record.get("expires_at", None)
    expires_at = None
    if expires_raw is not None:
        try:
            expires_at = parse_timestamp(expires_raw, field=f"{prefix} expires_at")
        except TimestampParseError as exc:
            findings.append(
                _reject(prefix, str(exc), repository, relative, record_index, artifact_id)
            )
            return None, findings
    if expires_at is not None and created_at > expires_at:
        findings.append(
            _reject(
                prefix,
                "has created_at after expires_at.",
                repository,
                relative,
                record_index,
                artifact_id,
            )
        )
        return None, findings

    workflow_run_id, head_sha, head_branch, meta_findings = _metadata(
        record.get("workflow_run", None),
        repository=repository,
        relative=relative,
        record_index=record_index,
        artifact_id=artifact_id,
    )
    findings.extend(meta_findings)
    artifact = Artifact(
        repository=repository,
        artifact_id=artifact_id,
        name=name,
        size_bytes=size,
        created_at=created_at,
        expires_at=expires_at,
        expired=expired,
        workflow_run_id=workflow_run_id,
        head_sha=head_sha,
        head_branch=head_branch,
        snapshot_at=snapshot_at,  # type: ignore[arg-type]
        source_file=relative,
        source_record_index=record_index,
    )
    return artifact, findings


def _metadata(
    value: object,
    *,
    repository: str,
    relative: str,
    record_index: int,
    artifact_id: int,
) -> tuple[int | None, str | None, str | None, list[Diagnostic]]:
    if value is None:
        return None, None, None, []
    if not isinstance(value, dict):
        return None, None, None, [
            Diagnostic(
                level="warning",
                code="ignored_metadata",
                message=(
                    f"{repository} artifact {artifact_id} has a workflow_run that is not an object. "
                    "Optional metadata was ignored."
                ),
                repository=repository,
                source_file=relative,
                artifact_id=artifact_id,
                source_record_index=record_index,
            )
        ]
    run_id = value.get("id")
    parsed_id: int | None = None
    findings: list[Diagnostic] = []
    if run_id is not None:
        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
            findings.append(
                Diagnostic(
                    level="warning",
                    code="ignored_metadata",
                    message=(
                        f"{repository} artifact {artifact_id} has a workflow_run id that is not a "
                        "positive integer. The id was ignored."
                    ),
                    repository=repository,
                    source_file=relative,
                    artifact_id=artifact_id,
                    source_record_index=record_index,
                )
            )
        else:
            parsed_id = run_id
    head_sha = value.get("head_sha")
    head_branch = value.get("head_branch")
    if head_sha is not None and not isinstance(head_sha, str):
        findings.append(
            Diagnostic(
                level="warning",
                code="ignored_metadata",
                message=f"{repository} artifact {artifact_id} has a non-string head_sha. It was ignored.",
                repository=repository,
                source_file=relative,
                artifact_id=artifact_id,
                source_record_index=record_index,
            )
        )
        head_sha = None
    if head_branch is not None and not isinstance(head_branch, str):
        findings.append(
            Diagnostic(
                level="warning",
                code="ignored_metadata",
                message=(
                    f"{repository} artifact {artifact_id} has a non-string head_branch. It was ignored."
                ),
                repository=repository,
                source_file=relative,
                artifact_id=artifact_id,
                source_record_index=record_index,
            )
        )
        head_branch = None
    return parsed_id, head_sha, head_branch, findings


def _reject(
    prefix: str,
    detail: str,
    repository: str,
    relative: str,
    record_index: int,
    artifact_id: int | None,
) -> Diagnostic:
    return Diagnostic(
        level="error",
        code="record_rejected",
        message=f"{prefix} {detail}",
        repository=repository,
        source_file=relative,
        artifact_id=artifact_id,
        source_record_index=record_index,
    )


def _deduplicate(artifacts: list[Artifact]) -> tuple[list[Artifact], list[Diagnostic]]:
    grouped: dict[tuple[str, int], list[Artifact]] = {}
    for artifact in artifacts:
        grouped.setdefault(artifact.key(), []).append(artifact)
    kept: list[Artifact] = []
    findings: list[Diagnostic] = []
    for key in sorted(grouped):
        rows = grouped[key]
        repository, artifact_id = key
        identities = {row.identity() for row in rows}
        sources = ", ".join(f"{row.source_file}#{row.source_record_index}" for row in rows)
        if len(identities) > 1:
            findings.append(
                Diagnostic(
                    level="error",
                    code="conflicting_duplicate",
                    message=(
                        f"Conflicting records for {repository} artifact {artifact_id} "
                        f"({sources}). Neither record was imported."
                    ),
                    repository=repository,
                    artifact_id=artifact_id,
                )
            )
            continue
        kept.append(rows[0])
        if len(rows) > 1:
            findings.append(
                Diagnostic(
                    level="info",
                    code="identical_duplicate",
                    message=(
                        f"Ignored {len(rows) - 1} identical duplicate record(s) for "
                        f"{repository} artifact {artifact_id} ({sources}). "
                        "Kept the earliest source record."
                    ),
                    repository=repository,
                    artifact_id=artifact_id,
                    source_file=rows[0].source_file,
                    source_record_index=rows[0].source_record_index,
                )
            )
    return kept, findings


def _completeness(
    sources: list[RepositorySource],
    files: list[SourceFile],
    artifacts: list[Artifact],
) -> list[Diagnostic]:
    findings: list[Diagnostic] = []
    by_repo_files: dict[str, list[SourceFile]] = {}
    for source in files:
        by_repo_files.setdefault(source.repository, []).append(source)
    counts: dict[str, int] = {}
    for artifact in artifacts:
        counts[artifact.repository] = counts.get(artifact.repository, 0) + 1

    for source in sources:
        if not source.complete:
            findings.append(
                Diagnostic(
                    level="warning",
                    code="partial_repository",
                    message=(
                        f"Manifest marks {source.repository} incomplete (complete: false). "
                        "Totals cover only the listed pages."
                    ),
                    repository=source.repository,
                )
            )
        pages = by_repo_files.get(source.repository, [])
        values = [page.total_count for page in pages if page.total_count_valid and page.total_count is not None]
        distinct = counts.get(source.repository, 0)
        if len(set(values)) > 1:
            rendered = ", ".join(str(value) for value in values)
            findings.append(
                Diagnostic(
                    level="warning",
                    code="total_count_inconsistent",
                    message=(
                        f"{source.repository} export pages disagree on total_count ({rendered}). "
                        "Distinct imported count is "
                        f"{distinct}. Matching counts would still not prove an atomic snapshot."
                    ),
                    repository=source.repository,
                )
            )
        elif values and values[0] != distinct:
            findings.append(
                Diagnostic(
                    level="warning",
                    code="total_count_mismatch",
                    message=(
                        f"{source.repository} exported total_count {values[0]} but "
                        f"{distinct} distinct artifact(s) were imported. "
                        "This does not prove which side is incomplete."
                    ),
                    repository=source.repository,
                )
            )
        elif values:
            findings.append(
                Diagnostic(
                    level="info",
                    code="total_count_match",
                    message=(
                        f"{source.repository} distinct imported count matches exported "
                        f"total_count {values[0]}. A match does not prove a complete or atomic snapshot."
                    ),
                    repository=source.repository,
                )
            )

    if len(sources) >= 2:
        earliest = min(sources, key=lambda item: (item.snapshot_at, item.repository))
        latest = max(sources, key=lambda item: (item.snapshot_at, item.repository))
        if latest.snapshot_at - earliest.snapshot_at > MAX_SNAPSHOT_SPAN:
            findings.append(
                Diagnostic(
                    level="warning",
                    code="snapshot_span",
                    message=(
                        "Snapshot times span more than 24 hours: "
                        f"{earliest.repository} at {format_timestamp(earliest.snapshot_at)} through "
                        f"{latest.repository} at {format_timestamp(latest.snapshot_at)}."
                    ),
                )
            )
    return findings
