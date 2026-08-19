import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from breg_watch.archive import ArchiveError, GitHubReleaseArchive, LocalArchive, archive_asset_name
from breg_watch.companies import Company
from breg_watch.html_report import render_overview
from breg_watch.notifier import GitHubIssueNotifier
from breg_watch.store import Store


PDF = b"%PDF-1.4\nfixture\n%%EOF\n"


class FakeGitHubRunner:
    def __init__(self):
        self.commands = []
        self.assets = {}
        self.issue_url = None

    def __call__(self, args, **kwargs):
        self.commands.append(list(args))
        if args[:3] == ["gh", "release", "view"]:
            if not self.assets:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="not found")
            payload = {
                "assets": [
                    {
                        "name": name,
                        "url": asset["url"],
                        "size": len(asset["content"]),
                        "digest": "sha256:" + hashlib.sha256(asset["content"]).hexdigest(),
                    }
                    for name, asset in self.assets.items()
                ]
            }
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")
        if args[:3] == ["gh", "release", "create"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:3] == ["gh", "release", "upload"]:
            name = Path(args[4]).name
            self.assets[name] = {
                "url": f"https://github.invalid/releases/{name}",
                "content": Path(args[4]).read_bytes(),
            }
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:3] == ["gh", "issue", "list"]:
            payload = [] if self.issue_url is None else [{"url": self.issue_url}]
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")
        if args[:3] == ["gh", "issue", "create"]:
            self.issue_url = "https://github.invalid/issues/1"
            return subprocess.CompletedProcess(args, 0, stdout=self.issue_url + "\n", stderr="")
        raise AssertionError(f"Unexpected command: {args}")


class OutputTests(unittest.TestCase):
    def test_local_archive_uses_stable_name_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = LocalArchive(Path(directory))
            first = archive.archive(
                orgnr="000000019",
                report_id=5667197,
                year="2024",
                discovered_at="2026-07-15T10:00:00+00:00",
                content=PDF,
                content_type="application/pdf",
            )
            second = archive.archive(
                orgnr="000000019",
                report_id=5667197,
                year="2024",
                discovered_at="2026-07-15T10:00:00+00:00",
                content=PDF,
                content_type="application/pdf",
            )

            self.assertEqual(first, second)
            self.assertEqual(
                (Path(directory) / first.reference).read_bytes(),
                PDF,
            )
            self.assertEqual(first.sha256, hashlib.sha256(PDF).hexdigest())
            self.assertEqual(Path(first.reference).name, archive_asset_name("000000019", 5667197, "2024"))
            self.assertEqual(
                first.reference,
                "000000019/annual-account-000000019-2024-5667197.pdf",
            )

    def test_release_upload_is_idempotent_with_test_double(self):
        runner = FakeGitHubRunner()
        archive = GitHubReleaseArchive("owner/private-repo", runner=runner)

        first = archive.archive(
            orgnr="000000019",
            report_id=5667197,
            year="2024",
            discovered_at="2026-07-15T10:00:00+00:00",
            content=PDF,
            content_type="application/pdf",
        )
        second = archive.archive(
            orgnr="000000019",
            report_id=5667197,
            year="2024",
            discovered_at="2026-07-15T10:00:00+00:00",
            content=PDF,
            content_type="application/pdf",
        )

        uploads = [command for command in runner.commands if command[:3] == ["gh", "release", "upload"]]
        self.assertEqual(len(uploads), 1)
        self.assertEqual(first.reference, second.reference)
        self.assertEqual(first.kind, "github_release")

    def test_release_rejects_existing_asset_with_wrong_content(self):
        runner = FakeGitHubRunner()
        name = archive_asset_name("000000019", 5667197, "2024")
        runner.assets[name] = {
            "url": f"https://github.invalid/releases/{name}",
            "content": b"different PDF",
        }
        archive = GitHubReleaseArchive("owner/private-repo", runner=runner)

        with self.assertRaises(ArchiveError):
            archive.archive(
                orgnr="000000019",
                report_id=5667197,
                year="2024",
                discovered_at="2026-07-15T10:00:00+00:00",
                content=PDF,
                content_type="application/pdf",
            )

        uploads = [command for command in runner.commands if command[:3] == ["gh", "release", "upload"]]
        self.assertEqual(uploads, [])

    def test_issue_notification_is_idempotent_with_test_double(self):
        runner = FakeGitHubRunner()
        notifier = GitHubIssueNotifier("owner/private-repo", runner=runner)
        filings = [
            {
                "orgnr": "000000019",
                "company_name": "Eksempel ASA",
                "report_id": 5667197,
                "period_to": "2024-12-31",
                "document_status": "archived",
            }
        ]

        first = notifier.notify("run-1", filings)
        second = notifier.notify("run-1", filings)

        creates = [command for command in runner.commands if command[:3] == ["gh", "issue", "create"]]
        self.assertEqual(len(creates), 1)
        self.assertEqual(first, second)

    def test_public_runner_cannot_create_github_publishers(self):
        with patch.dict("os.environ", {"BREG_PUBLIC_RUNNER": "1"}, clear=False):
            with self.assertRaises(ValueError):
                GitHubReleaseArchive("owner/public-repo", runner=FakeGitHubRunner())
            with self.assertRaises(ValueError):
                GitHubIssueNotifier("owner/public-repo", runner=FakeGitHubRunner())

    def test_renders_actual_html_status_from_store(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "monitor.sqlite")
            store.initialize()
            try:
                store.sync_companies([Company("000000019", "A & B ASA", True)])
                filing_id, _ = store.discover_filing(
                    orgnr="000000019",
                    report_id=5667197,
                    journal_number="2025428073",
                    report_type="SELSKAP",
                    period_from="2024-01-01",
                    period_to="2024-12-31",
                    discovered_at="2026-07-15T10:00:00+00:00",
                )
                store.mark_document_pending(filing_id, "not_available")
                store.import_run(
                    {
                        "run_id": "run-success",
                        "started_at": "2026-07-15T09:00:00+00:00",
                        "finished_at": "2026-07-15T09:02:00+00:00",
                        "status": "success",
                        "trigger": "fixture",
                        "checked": 1,
                        "new_filings": 0,
                        "errors": [],
                    }
                )
                store.import_run(
                    {
                        "run_id": "run-html",
                        "started_at": "2026-07-15T10:00:00+00:00",
                        "finished_at": "2026-07-15T10:02:00+00:00",
                        "status": "partial",
                        "trigger": "fixture",
                        "checked": 1,
                        "new_filings": 1,
                        "errors": [{"orgnr": "000000019", "kind": "document_pending"}],
                    }
                )
                output = render_overview(store, root / "site")
            finally:
                store.close()

            html = output.read_text(encoding="utf-8")
            self.assertIn("A &amp; B ASA", html)
            self.assertIn("2024-01-01 – 2024-12-31", html)
            self.assertIn("Venter på dokument", html)
            self.assertIn("Siste vellykkede kjøring:</strong> 2026-07-15T09:02:00+00:00", html)
            self.assertIn("Siste kjøring:</strong> 2026-07-15T10:02:00+00:00", html)
            self.assertIn("2026-07-15T10:02:00+00:00", html)
            self.assertNotIn("<script", html)
            self.assertFalse(
                any(line.endswith(" ") for line in html.splitlines()),
                "Rendered HTML must not contain trailing whitespace",
            )


if __name__ == "__main__":
    unittest.main()
