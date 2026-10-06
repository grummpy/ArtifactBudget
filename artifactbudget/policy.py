"""Exact artifact-id protection. Names and branches are never treated as policy."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

from artifactbudget.models import (
    Artifact,
    Diagnostic,
    Snapshot,
    is_repository_name,
    sorted_diagnostics,
)


def apply_policy(snapshot: Snapshot, policy_path: str | Path | None) -> Snapshot:
    """Attach exact protection reasons. Unknown ids warn; they do not fail closed."""

    if policy_path is None:
        return snapshot
    display = str(policy_path)
    path = Path(policy_path)
    findings: list[Diagnostic] = []
    try:
        raw = path.read_bytes()
    except OSError as exc:
        findings.append(
            Diagnostic(
                level="error",
                code="policy_unreadable",
                message=f"Could not read policy file {display}: {exc.strerror or exc}",
            )
        )
        return _with_policy(snapshot, display, None, findings)

    digest = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError:
        findings.append(
            Diagnostic(
                level="error",
                code="policy_invalid",
                message=f"Policy file {display} is not valid UTF-8.",
            )
        )
        return _with_policy(snapshot, display, digest, findings)
    except json.JSONDecodeError as exc:
        findings.append(
            Diagnostic(
                level="error",
                code="policy_invalid",
                message=f"Policy file {display} is not valid JSON ({exc.msg} at line {exc.lineno}).",
            )
        )
        return _with_policy(snapshot, display, digest, findings)

    reasons, entry_findings = _parse_policy(payload, display)
    findings.extend(entry_findings)
    if any(item.level == "error" and item.code == "policy_schema" for item in entry_findings):
        return _with_policy(snapshot, display, digest, findings)

    imported = {artifact.key(): artifact for artifact in snapshot.artifacts}
    applied: dict[tuple[str, int], str] = {}
    for key, reason in reasons.items():
        if key not in imported:
            repository, artifact_id = key
            findings.append(
                Diagnostic(
                    level="warning",
                    code="unknown_policy_target",
                    message=(
                        f"Policy entry {repository} artifact {artifact_id} "
                        f"({reason}) matches no imported artifact. It may be absent, "
                        "rejected, or removed as a conflicting duplicate. "
                        "Protection was not inferred from the artifact name."
                    ),
                    repository=repository,
                    artifact_id=artifact_id,
                )
            )
            continue
        applied[key] = reason

    updated = tuple(
        replace(artifact, protection_reason=applied.get(artifact.key()))
        for artifact in snapshot.artifacts
    )
    return _with_policy(snapshot, display, digest, findings, updated)


def _parse_policy(payload: object, display: str) -> tuple[dict[tuple[str, int], str], list[Diagnostic]]:
    findings: list[Diagnostic] = []
    if not isinstance(payload, dict):
        findings.append(
            Diagnostic(
                level="error",
                code="policy_schema",
                message=f"Policy file {display} must be a JSON object.",
            )
        )
        return {}, findings
    version = payload.get("schema_version")
    if isinstance(version, bool) or version != 1:
        findings.append(
            Diagnostic(
                level="error",
                code="policy_schema",
                message=f"Policy file {display} must set schema_version to 1.",
            )
        )
        return {}, findings
    entries = payload.get("protected_artifacts")
    if not isinstance(entries, list):
        findings.append(
            Diagnostic(
                level="error",
                code="policy_schema",
                message=f"Policy file {display} must contain a protected_artifacts array.",
            )
        )
        return {}, findings

    reasons: dict[tuple[str, int], str] = {}
    blocked: set[tuple[str, int]] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            findings.append(
                Diagnostic(
                    level="error",
                    code="policy_entry",
                    message=f"Policy entry {index} is not an object.",
                    source_record_index=index,
                )
            )
            continue
        repository = entry.get("repository")
        artifact_id = entry.get("artifact_id")
        reason = entry.get("reason")
        if not is_repository_name(repository):
            findings.append(
                Diagnostic(
                    level="error",
                    code="policy_entry",
                    message=(
                        f"Policy entry {index} needs repository as owner/name using letters, "
                        "numbers, dots, underscores, or hyphens."
                    ),
                    source_record_index=index,
                )
            )
            continue
        if isinstance(artifact_id, bool) or not isinstance(artifact_id, int) or artifact_id <= 0:
            findings.append(
                Diagnostic(
                    level="error",
                    code="policy_entry",
                    message=f"Policy entry {index} needs artifact_id as a positive integer.",
                    repository=repository,
                    source_record_index=index,
                )
            )
            continue
        if not isinstance(reason, str) or reason.strip() == "":
            findings.append(
                Diagnostic(
                    level="error",
                    code="policy_entry",
                    message=f"Policy entry {index} needs a non-empty reason string.",
                    repository=repository,
                    artifact_id=artifact_id,
                    source_record_index=index,
                )
            )
            continue
        key = (repository, artifact_id)
        if key in blocked:
            findings.append(
                Diagnostic(
                    level="error",
                    code="conflicting_policy",
                    message=(
                        f"Policy entry {index} for {repository} artifact {artifact_id} "
                        "was ignored because earlier entries for that id already conflict."
                    ),
                    repository=repository,
                    artifact_id=artifact_id,
                    source_record_index=index,
                )
            )
            continue
        previous = reasons.get(key)
        if previous is None:
            reasons[key] = reason
            continue
        if previous == reason:
            findings.append(
                Diagnostic(
                    level="info",
                    code="duplicate_policy_entry",
                    message=(
                        f"Ignored a repeated policy entry for {repository} artifact {artifact_id}."
                    ),
                    repository=repository,
                    artifact_id=artifact_id,
                    source_record_index=index,
                )
            )
            continue
        findings.append(
            Diagnostic(
                level="error",
                code="conflicting_policy",
                message=(
                    f"Policy entries for {repository} artifact {artifact_id} disagree "
                    f"({previous!r} vs {reason!r}). Neither reason was applied."
                ),
                repository=repository,
                artifact_id=artifact_id,
                source_record_index=index,
            )
        )
        reasons.pop(key, None)
        blocked.add(key)
    return reasons, findings


def _with_policy(
    snapshot: Snapshot,
    display: str,
    digest: str | None,
    findings: list[Diagnostic],
    artifacts: tuple[Artifact, ...] | None = None,
) -> Snapshot:
    return Snapshot(
        as_of=snapshot.as_of,
        artifacts=snapshot.artifacts if artifacts is None else artifacts,
        diagnostics=sorted_diagnostics((*snapshot.diagnostics, *findings)),
        files=snapshot.files,
        repositories=snapshot.repositories,
        manifest_path=snapshot.manifest_path,
        manifest_sha256=snapshot.manifest_sha256,
        policy_path=display,
        policy_sha256=digest,
    )
