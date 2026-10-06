"""Command wiring for validate and report. No network and no GitHub mutations."""

from __future__ import annotations

import argparse
import sys

from artifactbudget import __version__
from artifactbudget.forecast import InstantRejected, evaluate, instant_errors, quality_warnings
from artifactbudget.importer import load_snapshot
from artifactbudget.models import ArtifactBudgetError, TimestampParseError, format_timestamp, parse_timestamp
from artifactbudget.policy import apply_policy
from artifactbudget.report import OUTPUT_NAMES, build_documents, validation_text, write_report


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""

    _configure_stdio()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return 0 if code is None else int(code)
    try:
        if args.command == "validate":
            return _validate(args)
        if args.command == "report":
            return _report(args)
    except InstantRejected as exc:
        sys.stdout.write(validation_text(exc.diagnostics, heading="report"))
        print("Report not written.", file=sys.stderr)
        return 1
    except ArtifactBudgetError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 2


def console_main() -> None:
    """Setuptools entry point. Propagates the exit code."""

    sys.exit(main())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="artifactbudget",
        description=(
            "Offline inventory and retention forecast for exported GitHub Actions "
            "artifact metadata. Reads local JSON only. Does not delete artifacts "
            "or change GitHub settings or billing."
        ),
    )
    parser.add_argument("--version", action="version", version=f"artifactbudget {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser(
        "validate",
        help="Validate a manifest, export pages, and optional protection policy",
    )
    _add_inputs(validate)

    report = commands.add_parser(
        "report",
        help="Write a JSON, CSV, and self-contained HTML retention report",
    )
    _add_inputs(report)
    report.add_argument(
        "--horizon-days",
        type=int,
        default=30,
        help="Forecast horizon in 86400-second days (default: 30)",
    )
    report.add_argument(
        "--as-of",
        help="Timezone-aware report instant. Defaults to the manifest as_of value.",
    )
    report.add_argument(
        "--out",
        required=True,
        help="Directory to write report files into. Created if missing.",
    )
    report.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace report files already present in the output directory.",
    )
    return parser


def _add_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", required=True, help="Path to the snapshot manifest JSON")
    parser.add_argument("--policy", help="Optional exact-id protection policy JSON")


def _validate(args: argparse.Namespace) -> int:
    snapshot, t0 = _load(args)
    extra = (*instant_errors(snapshot.artifacts, t0), *quality_warnings(snapshot.artifacts, t0))
    sys.stdout.write(_acceptance_text(snapshot))
    sys.stdout.write(validation_text((*snapshot.diagnostics, *extra), heading="validation"))
    if snapshot.errors or any(item.level == "error" for item in extra):
        return 1
    return 0


def _report(args: argparse.Namespace) -> int:
    if isinstance(args.horizon_days, bool) or args.horizon_days < 1:
        raise ArtifactBudgetError("horizon-days must be a positive integer")
    snapshot, t0 = _load(args, as_of=args.as_of)
    extra_errors = instant_errors(snapshot.artifacts, t0)
    if snapshot.errors or extra_errors:
        warnings = quality_warnings(snapshot.artifacts, t0)
        sys.stdout.write(_acceptance_text(snapshot))
        sys.stdout.write(
            validation_text((*snapshot.diagnostics, *extra_errors, *warnings), heading="report")
        )
        print("Report not written.", file=sys.stderr)
        return 1
    try:
        forecast = evaluate(snapshot.artifacts, t0, args.horizon_days)
    except OverflowError as exc:
        raise ArtifactBudgetError("horizon-days is too large to place on the calendar") from exc
    documents = build_documents(snapshot, forecast)
    write_report(args.out, documents, overwrite=args.overwrite)
    print(f"Wrote {len(OUTPUT_NAMES)} files to {args.out}")
    for name in OUTPUT_NAMES:
        print(f"  {name}")
    warning_count = sum(
        1 for item in (*snapshot.diagnostics, *forecast.warnings) if item.level == "warning"
    )
    print(
        f"Warnings: {warning_count}. Open {args.out}/report.html locally. "
        "ArtifactBudget did not contact GitHub."
    )
    return 0


def _load(args: argparse.Namespace, as_of: str | None = None):
    snapshot = load_snapshot(args.manifest, display_path=args.manifest)
    snapshot = apply_policy(snapshot, args.policy)
    if as_of is None:
        return snapshot, snapshot.as_of
    try:
        t0 = parse_timestamp(as_of, field="--as-of")
    except TimestampParseError as exc:
        raise ArtifactBudgetError(str(exc)) from exc
    return snapshot, t0


def _acceptance_text(snapshot) -> str:
    counts: dict[str, int] = {}
    for artifact in snapshot.artifacts:
        counts[artifact.repository] = counts.get(artifact.repository, 0) + 1
    pages: dict[str, list] = {}
    for source in snapshot.files:
        pages.setdefault(source.repository, []).append(source)
    lines = [
        f"Manifest: {snapshot.manifest_path}",
        f"SHA-256: {snapshot.manifest_sha256}",
        f"As of: {format_timestamp(snapshot.as_of)}",
        "",
        "Repositories",
    ]
    if not snapshot.repositories:
        lines.append("- none")
    for source in snapshot.repositories:
        repo_pages = pages.get(source.repository, [])
        accepted = sum(page.accepted for page in repo_pages)
        rejected = sum(page.rejected for page in repo_pages)
        lines.append(
            f"- {source.repository} complete={str(source.complete).lower()} "
            f"snapshot_at={format_timestamp(source.snapshot_at)} "
            f"pages={len(repo_pages)} accepted={accepted} rejected={rejected} "
            f"distinct={counts.get(source.repository, 0)}"
        )
        for page in repo_pages:
            digest = page.sha256 or "-"
            lines.append(
                f"  page {page.page_number} {page.path} sha256={digest} "
                f"accepted={page.accepted} rejected={page.rejected} "
                f"parsed={str(page.parsed).lower()}"
            )
    lines.append("")
    return "\n".join(lines) + "\n"


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
