"""Shared builders for synthetic exports."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from artifactbudget.models import Artifact

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 10, 6, tzinfo=timezone.utc)
GIB = 1024**3


def art(**overrides) -> Artifact:
    base = dict(
        repository="fixture/cohort",
        artifact_id=1,
        name="A",
        size_bytes=GIB,
        created_at=T0 - timedelta(days=10),
        expires_at=T0 + timedelta(days=20),
        expired=False,
        workflow_run_id=None,
        head_sha=None,
        head_branch=None,
        snapshot_at=T0,
        source_file="page.json",
        source_record_index=0,
        protection_reason=None,
    )
    base.update(overrides)
    return Artifact(**base)


def gh_artifact(
    artifact_id: int,
    name: str,
    size: int,
    created: str,
    expires: str | None,
    *,
    expired: bool = False,
    **extra,
) -> dict:
    row = {
        "id": artifact_id,
        "name": name,
        "size_in_bytes": size,
        "expired": expired,
        "created_at": created,
        "expires_at": expires,
    }
    row.update(extra)
    return row


def write_manifest(
    directory: Path,
    repositories: list[dict],
    *,
    as_of: str = "2026-10-06T00:00:00Z",
) -> Path:
    path = directory / "manifest.json"
    path.write_text(
        json.dumps(
            {"schema_version": 1, "as_of": as_of, "repositories": repositories},
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def write_page(directory: Path, name: str, artifacts: list[dict], total_count: int | None = None) -> None:
    payload: dict = {"artifacts": artifacts}
    if total_count is not None:
        payload["total_count"] = total_count
    (directory / name).write_text(json.dumps(payload), encoding="utf-8")


def repo_entry(
    repository: str,
    files: list[str],
    *,
    complete: bool = True,
    snapshot_at: str = "2026-10-06T00:00:00Z",
) -> dict:
    return {
        "repository": repository,
        "snapshot_at": snapshot_at,
        "complete": complete,
        "files": files,
    }
