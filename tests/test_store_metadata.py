import json
import tempfile
import unittest
from pathlib import Path

from breg_watch.companies import Company
from breg_watch.metadata import MetadataRepository, rebuild_database
from breg_watch.store import Store


class StoreAndMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db_path = self.root / "state" / "monitor.sqlite"
        self.store = Store(self.db_path)
        self.store.initialize()
        self.companies = [Company("000000019", "Eksempel ASA", True)]
        self.store.sync_companies(self.companies)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def test_filing_unique_key_prevents_duplicate_events(self):
        first_id, first_created = self.store.discover_filing(
            orgnr="000000019",
            report_id=5667197,
            journal_number="2025428073",
            report_type="SELSKAP",
            period_from="2024-01-01",
            period_to="2024-12-31",
            discovered_at="2026-07-15T10:00:00+00:00",
        )
        second_id, second_created = self.store.discover_filing(
            orgnr="000000019",
            report_id=5667197,
            journal_number="changed",
            report_type="SELSKAP",
            period_from="2024-01-01",
            period_to="2024-12-31",
            discovered_at="2026-07-15T11:00:00+00:00",
        )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first_id, second_id)
        self.assertEqual(len(self.store.list_filings()), 1)

    def test_document_status_and_notifications_are_idempotent(self):
        filing_id, _ = self.store.discover_filing(
            orgnr="000000019",
            report_id=5667197,
            journal_number="2025428073",
            report_type="SELSKAP",
            period_from="2024-01-01",
            period_to="2024-12-31",
            discovered_at="2026-07-15T10:00:00+00:00",
        )
        self.store.mark_document_pending(filing_id, "not_available")
        self.store.mark_document(
            filing_id=filing_id,
            source_url="https://example.invalid/source.pdf",
            archive_kind="local",
            archive_reference="documents/report.pdf",
            sha256="a" * 64,
            size_bytes=42,
            content_type="application/pdf",
            archived_at="2026-07-15T10:05:00+00:00",
        )

        first = self.store.record_notification(
            filing_id, "github_issue", "new_filing", "https://github.invalid/issues/1"
        )
        second = self.store.record_notification(
            filing_id, "github_issue", "new_filing", "https://github.invalid/issues/2"
        )

        filing = self.store.get_filing("000000019", 5667197)
        self.assertEqual(filing["document_status"], "archived")
        self.assertTrue(first)
        self.assertFalse(second)

    def test_metadata_preserves_raw_responses_and_rebuilds_database(self):
        repository = MetadataRepository(self.root / "data")
        summary = {
            "orgnr": "000000019",
            "company_name": "Eksempel ASA",
            "report_id": 5667197,
            "journal_number": "2025428073",
            "report_type": "SELSKAP",
            "period_from": "2024-01-01",
            "period_to": "2024-12-31",
            "discovered_at": "2026-07-15T10:00:00+00:00",
            "document": {
                "status": "archived",
                "source_url": "https://example.invalid/source.pdf",
                "archive_kind": "local",
                "archive_reference": "documents/report.pdf",
                "sha256": "b" * 64,
                "size_bytes": 99,
                "content_type": "application/pdf",
                "archived_at": "2026-07-15T10:05:00+00:00",
            },
            "notifications": [
                {
                    "channel": "github_issue",
                    "kind": "new_filing",
                    "remote_reference": "https://github.invalid/issues/1",
                }
            ],
        }
        repository.write_event(
            orgnr="000000019",
            report_id=5667197,
            latest_raw=b'[{"id":5667197}]',
            detail_raw=b'{"id":5667197}',
            summary=summary,
        )
        repository.write_run(
            {
                "run_id": "run-1",
                "started_at": "2026-07-15T10:00:00+00:00",
                "finished_at": "2026-07-15T10:10:00+00:00",
                "status": "success",
                "trigger": "manual",
                "checked": 1,
                "new_filings": 1,
                "errors": [],
            }
        )

        event_dir = self.root / "data" / "events" / "000000019" / "5667197"
        self.assertEqual((event_dir / "latest.json").read_bytes(), b'[{"id":5667197}]')
        self.assertEqual((event_dir / "detail.json").read_bytes(), b'{"id":5667197}')

        rebuilt_path = self.root / "rebuilt.sqlite"
        rebuild_database(rebuilt_path, [], repository)
        rebuilt = Store(rebuilt_path)
        rebuilt.initialize()
        try:
            filing = rebuilt.get_filing("000000019", 5667197)
            self.assertEqual(filing["journal_number"], "2025428073")
            self.assertEqual(filing["document_status"], "archived")
            self.assertEqual(rebuilt.last_successful_run()["run_id"], "run-1")
            self.assertTrue(
                rebuilt.notification_exists(5667197, "000000019", "github_issue", "new_filing")
            )
        finally:
            rebuilt.close()

    def test_atomic_metadata_writes_leave_no_temporary_files(self):
        repository = MetadataRepository(self.root / "data")
        repository.write_run(
            {
                "run_id": "run-2",
                "started_at": "2026-07-15T11:00:00+00:00",
                "finished_at": "2026-07-15T11:01:00+00:00",
                "status": "partial",
                "trigger": "schedule",
                "checked": 0,
                "new_filings": 0,
                "errors": [{"orgnr": "000000019", "kind": "timeout"}],
            }
        )

        current = json.loads((self.root / "data" / "runs" / "current.json").read_text())
        self.assertEqual(current["status"], "partial")
        self.assertEqual(list((self.root / "data").rglob("*.tmp")), [])

    def test_rebuild_preserves_check_status_and_deferred_document(self):
        repository = MetadataRepository(self.root / "data")
        repository.write_event(
            orgnr="000000019",
            report_id=5667197,
            latest_raw=b'[{"id":5667197}]',
            detail_raw=b'{"id":5667197}',
            summary={
                "orgnr": "000000019",
                "company_name": "Eksempel ASA",
                "report_id": 5667197,
                "journal_number": "2025428073",
                "report_type": "SELSKAP",
                "period_from": "2024-01-01",
                "period_to": "2024-12-31",
                "discovered_at": "2026-07-15T10:00:00+00:00",
                "discovery_mode": "baseline",
                "document": {"status": "deferred", "error": "baseline"},
            },
        )
        repository.write_check_status(
            {
                "orgnr": "000000019",
                "baseline_completed_at": "2026-07-15T10:00:00+00:00",
                "last_report_id": 5667197,
                "last_checked_at": "2026-07-15T11:00:00+00:00",
                "last_success_at": "2026-07-15T10:00:00+00:00",
                "last_error": "TransientBrregError",
            }
        )

        rebuilt_path = self.root / "rebuilt-checks.sqlite"
        rebuild_database(rebuilt_path, self.companies, repository)
        rebuilt = Store(rebuilt_path)
        rebuilt.initialize()
        try:
            filing = rebuilt.get_filing("000000019", 5667197)
            status = rebuilt.get_check_status("000000019")
            self.assertEqual(filing["document_status"], "deferred")
            self.assertEqual(filing["document_error"], "baseline")
            self.assertEqual(
                status["baseline_completed_at"],
                "2026-07-15T10:00:00+00:00",
            )
            self.assertEqual(status["last_report_id"], 5667197)
            self.assertEqual(
                status["last_checked_at"],
                "2026-07-15T11:00:00+00:00",
            )
            self.assertEqual(
                status["last_success_at"],
                "2026-07-15T10:00:00+00:00",
            )
            self.assertEqual(status["last_error"], "TransientBrregError")
        finally:
            rebuilt.close()


if __name__ == "__main__":
    unittest.main()
