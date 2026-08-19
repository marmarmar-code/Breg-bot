import tempfile
import unittest
from pathlib import Path

from breg_watch.archive import LocalArchive
from breg_watch.brreg import BrregClient, HttpResponse
from breg_watch.companies import Company
from breg_watch.metadata import MetadataRepository, rebuild_database
from breg_watch.service import MonitorService
from breg_watch.store import Store


FIXTURES = Path(__file__).parent / "fixtures"
PDF = b"%PDF-1.4\nfixture\n%%EOF\n"


class FixtureTransport:
    def __init__(self):
        self.calls = []

    def __call__(self, url, timeout, max_bytes=None):
        del max_bytes
        self.calls.append((url, timeout))
        if url.endswith("/000000019"):
            return self.json("latest_new.json")
        if url.endswith("/000000019/5667197"):
            return self.json("detail_5667197.json")
        if url.endswith("/000000019/aar"):
            return self.json("years_2024.json")
        if url.endswith("/000000019/2024"):
            return HttpResponse(
                200,
                {"Content-Type": "application/pdf"},
                PDF,
            )
        raise AssertionError(f"Unexpected fixture URL: {url}")

    @staticmethod
    def json(name):
        return HttpResponse(
            200,
            {"Content-Type": "application/json"},
            (FIXTURES / name).read_bytes(),
        )


class FixtureEndToEndTests(unittest.TestCase):
    def test_fixture_run_archives_renders_and_rebuilds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            companies = [Company("000000019", "Eksempel ASA", True)]
            metadata = MetadataRepository(root / "data")
            metadata.write_check_status(
                {
                    "orgnr": "000000019",
                    "baseline_completed_at": "2026-07-14T12:00:00+00:00",
                    "last_report_id": None,
                    "last_checked_at": "2026-07-14T12:00:00+00:00",
                    "last_success_at": "2026-07-14T12:00:00+00:00",
                    "last_error": None,
                }
            )
            store = Store(root / "state.sqlite")
            store.initialize()
            transport = FixtureTransport()
            service = MonitorService(
                store=store,
                metadata=metadata,
                client=BrregClient(transport=transport, request_interval=0, sleep=lambda _: None),
                archive=LocalArchive(root / "documents"),
                notifier=None,
                site_directory=root / "site",
                clock=lambda: "2026-07-15T12:00:00+00:00",
                id_factory=lambda: "fixture-e2e",
            )
            try:
                summary = service.run(companies, trigger="fixture")
                self.assertEqual(summary["status"], "success")
                self.assertEqual(summary["new_filings"], 1)
                self.assertEqual(store.get_filing("000000019", 5667197)["document_status"], "archived")
            finally:
                store.close()

            rebuilt_path = root / "rebuilt.sqlite"
            rebuild_database(rebuilt_path, companies, metadata)
            rebuilt = Store(rebuilt_path)
            rebuilt.initialize()
            try:
                self.assertEqual(len(rebuilt.list_filings()), 1)
                self.assertEqual(rebuilt.last_successful_run()["run_id"], "fixture-e2e")
            finally:
                rebuilt.close()

            self.assertEqual(len(transport.calls), 4)
            self.assertTrue(next((root / "documents").rglob("*.pdf")).is_file())
            html = (root / "site" / "index.html").read_text(encoding="utf-8")
            self.assertIn("Eksempel ASA", html)
            self.assertIn("Arkivert", html)
            self.assertIn(
                '../documents/000000019/annual-account-000000019-2024-5667197.pdf',
                html,
            )
            self.assertFalse(any(line.endswith(" ") for line in html.splitlines()))
            event_summary = metadata.read_event_summary("000000019", 5667197)
            self.assertFalse(Path(event_summary["document"]["archive_reference"]).is_absolute())


if __name__ == "__main__":
    unittest.main()
