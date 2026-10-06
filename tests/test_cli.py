"""CLI exit codes, output confinement, the bundled demo, and offline behavior."""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import sys
from pathlib import Path

from artifactbudget import __version__
from artifactbudget.cli import main

from support import ROOT, gh_artifact, repo_entry, write_manifest, write_page

EXAMPLES = ROOT / "examples"


def test_version_matches_project_metadata():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{__version__}"' in text


def test_help_and_usage():
    assert main(["--help"]) == 0
    assert main(["validate", "--help"]) == 0
    assert main([]) == 2


def test_validate_and_report_examples(tmp_path):
    code = main(
        [
            "validate",
            "--manifest",
            str(EXAMPLES / "manifest.json"),
            "--policy",
            str(EXAMPLES / "protection.json"),
        ]
    )
    assert code == 0
    out = tmp_path / "report"
    report_code = main(
        [
            "report",
            "--manifest",
            "examples/manifest.json",
            "--policy",
            "examples/protection.json",
            "--out",
            str(out),
        ]
    )
    assert report_code == 0
    payload = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert payload["horizon_days"] == 30
    assert payload["current"]["retained_bytes"] == 3943694336
    assert payload["current"]["expired_bytes"] == 41943040
    assert payload["current"]["imported_bytes"] == 3985637376
    assert payload["current"]["protected_bytes"] == 2163212288
    assert payload["current"]["unknown_expiry_bytes"] == 31457280
    raw = 0
    seen = set()
    for repo, filename in (
        ("demo/platform-engine", "platform-engine-page-1.json"),
        ("demo/platform-engine", "platform-engine-page-2.json"),
        ("demo/web-console", "web-console-page-1.json"),
        ("demo/mobile-app", "mobile-app-page-1.json"),
        ("demo/docs-site", "docs-site-page-1.json"),
    ):
        page = json.loads((EXAMPLES / filename).read_text(encoding="utf-8"))
        for artifact in page["artifacts"]:
            key = (repo, artifact["id"])
            if key in seen:
                continue
            seen.add(key)
            raw += artifact["size_in_bytes"]
    assert raw == payload["current"]["imported_bytes"]
    html = (out / "report.html").read_text(encoding="utf-8")
    assert 'data-status="review"' in html
    assert "demo/mobile-app" in html
    assert "incomplete" in html
    assert "&lt;nightly&gt;" in html
    assert "api.github.com" not in html
    assert "https://" not in html
    assert "<script" not in html
    assert "hypothetical immediate removal" in html
    assert str((EXAMPLES / "platform-engine-page-1.json").resolve()) not in html
    import csv

    with (out / "artifacts.csv").open(encoding="utf-8", newline="") as handle:
        parsed = list(csv.DictReader(handle))
    assert sum(int(row["size_bytes"]) for row in parsed) == payload["current"]["imported_bytes"]


def test_overwrite_symlink_and_unrelated_files(tmp_path):
    write_page(
        tmp_path,
        "page.json",
        [gh_artifact(1, "a", 4, "2026-10-01T00:00:00Z", "2026-10-20T00:00:00Z")],
        1,
    )
    manifest = write_manifest(tmp_path, [repo_entry("demo/alpha", ["page.json"])])
    out = tmp_path / "out"
    out.mkdir()
    (out / "notes.txt").write_text("keep", encoding="utf-8")
    args = [
        "report",
        "--manifest",
        str(manifest),
        "--out",
        str(out),
    ]
    assert main(args) == 0
    assert (out / "notes.txt").read_text(encoding="utf-8") == "keep"
    digest = hashlib.sha256((out / "report.json").read_bytes()).hexdigest()
    assert main(args) == 1
    assert hashlib.sha256((out / "report.json").read_bytes()).hexdigest() == digest
    assert main(args + ["--overwrite"]) == 0
    assert (out / "notes.txt").read_text(encoding="utf-8") == "keep"

    linked = tmp_path / "linked-out"
    linked.symlink_to(out, target_is_directory=True)
    assert main(["report", "--manifest", str(manifest), "--out", str(linked), "--overwrite"]) == 1
    file_link = out / "report.json"
    file_link.unlink()
    file_link.symlink_to(out / "notes.txt")
    assert main(args + ["--overwrite"]) == 1
    assert (out / "notes.txt").read_text(encoding="utf-8") == "keep"


def test_conflicting_duplicate_writes_nothing(tmp_path):
    write_page(
        tmp_path,
        "a.json",
        [gh_artifact(1, "a", 4, "2026-10-01T00:00:00Z", "2026-10-20T00:00:00Z")],
        1,
    )
    write_page(
        tmp_path,
        "b.json",
        [gh_artifact(1, "a", 9, "2026-10-01T00:00:00Z", "2026-10-20T00:00:00Z")],
        1,
    )
    manifest = write_manifest(tmp_path, [repo_entry("demo/alpha", ["a.json", "b.json"])])
    out = tmp_path / "out"
    before = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in tmp_path.glob("*.json")
    }
    assert main(["report", "--manifest", str(manifest), "--out", str(out)]) == 1
    assert not out.exists()
    after = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in tmp_path.glob("*.json")
    }
    assert before == after


def test_bad_horizon_and_as_of_override(tmp_path):
    write_page(
        tmp_path,
        "page.json",
        [gh_artifact(1, "a", 4, "2026-10-05T00:00:00Z", "2026-10-20T00:00:00Z")],
        1,
    )
    manifest = write_manifest(tmp_path, [repo_entry("demo/alpha", ["page.json"])])
    out = tmp_path / "out"
    assert main(["report", "--manifest", str(manifest), "--horizon-days", "0", "--out", str(out)]) == 1
    assert not out.exists()
    code = main(
        [
            "report",
            "--manifest",
            str(manifest),
            "--as-of",
            "2026-10-04T00:00:00Z",
            "--out",
            str(out),
        ]
    )
    assert code == 1
    assert not out.exists()


def test_missing_policy_file(tmp_path):
    write_page(
        tmp_path,
        "page.json",
        [gh_artifact(1, "a", 4, "2026-10-01T00:00:00Z", "2026-10-20T00:00:00Z")],
        1,
    )
    manifest = write_manifest(tmp_path, [repo_entry("demo/alpha", ["page.json"])])
    assert main(["validate", "--manifest", str(manifest), "--policy", str(tmp_path / "nope.json")]) == 1


def test_offline_validate_does_not_connect(monkeypatch):
    attempts = []

    class Guard(socket.socket):
        def connect(self, address):  # type: ignore[override]
            attempts.append(address)
            raise AssertionError(f"unexpected connect to {address}")

        def connect_ex(self, address):  # type: ignore[override]
            attempts.append(address)
            raise AssertionError(f"unexpected connect to {address}")

    monkeypatch.setattr(socket, "socket", Guard)
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("create_connection")))
    code = main(
        [
            "validate",
            "--manifest",
            str(EXAMPLES / "manifest.json"),
            "--policy",
            str(EXAMPLES / "protection.json"),
        ]
    )
    assert code == 0
    assert attempts == []


def test_module_entrypoint():
    completed = subprocess.run(
        [sys.executable, "-m", "artifactbudget", "validate", "--manifest", "examples/manifest.json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "demo/mobile-app" in completed.stdout


def test_package_has_no_network_or_mutation_api():
    source = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "artifactbudget").glob("*.py"))
    for banned in (
        "urllib",
        "requests",
        "http.client",
        "socket",
        "subprocess",
        "os.system",
        "datetime.now",
        "utcnow",
        "delete_artifact",
    ):
        assert banned not in source
    assert "def validate" not in source or "validate" in source
    assert '"report"' in source or "'report'" in source
