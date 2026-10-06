"""Normalized records shared by import, policy, forecast, and report."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

__all__ = [
    "Artifact",
    "ArtifactBudgetError",
    "Diagnostic",
    "RepositorySource",
    "Snapshot",
    "SourceFile",
    "TimestampParseError",
    "diagnostic_sort_key",
    "format_timestamp",
    "is_repository_name",
    "parse_timestamp",
    "sorted_diagnostics",
]

_REPOSITORY_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ArtifactBudgetError(Exception):
    """A problem that stops the command before a normal report can be written."""


class TimestampParseError(ValueError):
    """A timestamp was missing, naive, or not ISO-8601."""


def parse_timestamp(value: object, *, field: str) -> datetime:
    """Parse a timezone-aware ISO-8601 timestamp and return UTC.

    Naive values are rejected. A trailing Z is accepted. The wall clock is not
    consulted.
    """

    if not isinstance(value, str) or value == "" or value.strip() != value:
        raise TimestampParseError(f"{field} must be a timezone-aware ISO-8601 string")
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise TimestampParseError(f"{field} is not a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise TimestampParseError(f"{field} must include a timezone offset")
    return parsed.astimezone(timezone.utc)


def format_timestamp(value: datetime | None) -> str | None:
    """Format a timezone-aware datetime as UTC with a Z suffix."""

    if value is None:
        return None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("refusing to format a naive datetime")
    text = value.astimezone(timezone.utc).isoformat()
    if text.endswith("+00:00"):
        text = text[:-6] + "Z"
    return text


def is_repository_name(value: object) -> bool:
    """Return True for an owner/name string with a conservative character set."""

    return isinstance(value, str) and _REPOSITORY_NAME.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class Artifact:
    """One normalized artifact row.

    ``size_bytes`` is the internal name for GitHub's ``size_in_bytes``.
    ``protection_reason`` is set only from an exact policy match.
    """

    repository: str
    artifact_id: int
    name: str
    size_bytes: int
    created_at: datetime
    expires_at: datetime | None
    expired: bool
    workflow_run_id: int | None
    head_sha: str | None
    head_branch: str | None
    snapshot_at: datetime
    source_file: str
    source_record_index: int
    protection_reason: str | None = None

    def key(self) -> tuple[str, int]:
        return (self.repository, self.artifact_id)

    def identity(self) -> tuple[object, ...]:
        """Fields that must match for two rows to be the same artifact record."""

        return (
            self.repository,
            self.artifact_id,
            self.name,
            self.size_bytes,
            self.created_at,
            self.expires_at,
            self.expired,
            self.workflow_run_id,
            self.head_sha,
            self.head_branch,
        )


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """A single validation, policy, or forecast finding."""

    level: str
    code: str
    message: str
    repository: str | None = None
    source_file: str | None = None
    artifact_id: int | None = None
    source_record_index: int | None = None

    def __post_init__(self) -> None:
        if self.level not in {"error", "warning", "info"}:
            raise ValueError(f"unknown diagnostic level {self.level!r}")


def diagnostic_sort_key(item: Diagnostic) -> tuple[object, ...]:
    level_order = {"error": 0, "warning": 1, "info": 2}
    return (
        level_order[item.level],
        item.code,
        item.repository or "",
        -1 if item.artifact_id is None else item.artifact_id,
        item.source_file or "",
        -1 if item.source_record_index is None else item.source_record_index,
        item.message,
    )


def sorted_diagnostics(items: Iterable[Diagnostic]) -> tuple[Diagnostic, ...]:
    return tuple(sorted(items, key=diagnostic_sort_key))


@dataclass(frozen=True, slots=True)
class SourceFile:
    """One export page named by the manifest."""

    repository: str
    path: str
    sha256: str | None
    page_number: int
    accepted: int
    rejected: int
    parsed: bool
    total_count: int | None
    total_count_valid: bool


@dataclass(frozen=True, slots=True)
class RepositorySource:
    """Manifest metadata for one repository. This is not proof of coverage."""

    repository: str
    snapshot_at: datetime
    complete: bool
    files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A validated import plus the diagnostics discovered along the way."""

    as_of: datetime
    artifacts: tuple[Artifact, ...]
    diagnostics: tuple[Diagnostic, ...]
    files: tuple[SourceFile, ...]
    repositories: tuple[RepositorySource, ...]
    manifest_path: str
    manifest_sha256: str
    policy_path: str | None = None
    policy_sha256: str | None = None

    @property
    def errors(self) -> tuple[Diagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.level == "error")

    @property
    def ok(self) -> bool:
        return not self.errors
