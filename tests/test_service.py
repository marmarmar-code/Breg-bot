import tempfile
import unittest
import logging
from io import StringIO
from pathlib import Path

from breg_watch.archive import LocalArchive
from breg_watch.brreg import BinaryResponse, BrregError, JsonResponse, TransientBrregError
from breg_watch.companies import Company
from breg_watch.metadata import MetadataRepository
from breg_watch.notifier import NotificationError
from breg_watch.service import MonitorService
from breg_watch.store import Store


LATEST = {
    "id": 5667197,
    "journalnr": "2025428073",
    "regnskapstype": "SELSKAP",
    "regnskapsperiode": {"fraDato": "2024-01-01", "tilDato": "2024-12-31"},
}
DETAIL = dict(LATEST)
PDF = b"%PDF-1.4\nfixture\n%%EOF\n"


class FakeClient:
    base_url = "https://data.brreg.no/regnskapsregisteret/regnskap"

    def __init__(self, latest_by_org=None, pdf_results=None):
        self.latest_by_org = latest_by_org or {"000000019": [LATEST]}
        self.pdf_results = list(pdf_results or [BinaryResponse(PDF, "application/pdf")])
        self.calls = []

    def latest(self, orgnr):
        self.calls.append(("latest", orgnr))
        value = self.latest_by_org[orgnr]
        if isinstance(value, BaseException):
            raise value
        return JsonResponse(value, (str(value).replace("'", '"')).encode())

    def detail(self, orgnr, report_id):
        self.calls.append(("detail", orgnr, report_id))
        return JsonResponse(DETAIL, b'{"id":5667197,"regnskapsperiode":{"fraDato":"2024-01-01","tilDato":"2024-12-31"}}')

    def available_years(self, orgnr):
        self.calls.append(("years", orgnr))
        return JsonResponse(["2024"], b'["2024"]')

    def annual_report(self, orgnr, year):
        self.calls.append(("pdf", orgnr, year))
        result = self.pdf_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class CountingNotifier:
    channel = "github_issue"

    def __init__(self):
        self.calls = []

    def notify(self, run_id, filings):
        self.calls.append((run_id, filings))
        return f"https://github.invalid/issues/{len(self.calls)}"


class FlakyNotifier(CountingNotifier):
    def notify(self, run_id, filings):
        self.calls.append((run_id, filings))
        if len(self.calls) == 1:
            raise NotificationError("temporary")
        return "https://github.invalid/issues/retried"


class InspectingNotifier(CountingNotifier):
    def __init__(self, metadata):
        super().__init__()
        self.metadata = metadata
        self.observed = []

    def notify(self, notification_key, filings):
        filing = filings[0]
        summary = self.metadata.read_event_summary(
            str(filing["orgnr"]), int(filing["report_id"])
        )
        self.observed.append(
            (summary.get("notification_pending"), summary.get("notification_key"))
        )
        return super().notify(notification_key, filings)


class RecordingMetadata(MetadataRepository):
    def __init__(self, root):
        super().__init__(root)
        self.initial_notification_keys = []

    def write_event(self, **kwargs):
        self.initial_notification_keys.append(kwargs["summary"].get("notification_key"))
        return super().write_event(**kwargs)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "state.sqlite")
        self.store.initialize()
        self.metadata = MetadataRepository(self.root / "data")
        self.companies = [Company("000000019", "Eksempel ASA", True)]

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def service(self, client, notifier=None, *, logger=None, redact_identifiers=False):
        if logger is None:
            logger = logging.getLogger(f"test-service-{id(self)}")
            logger.disabled = True
        return MonitorService(
            store=self.store,
            metadata=self.metadata,
            client=client,
            archive=LocalArchive(self.root / "documents"),
            notifier=notifier,
            site_directory=self.root / "site",
            clock=lambda: "2026-07-15T10:00:00+00:00",
            id_factory=lambda: f"run-{len(list((self.root / 'data' / 'runs').glob('*/*.json'))) + 1}",
            logger=logger,
            redact_identifiers=redact_identifiers,
        )

    def mark_baseline_complete(self, *orgnrs):
        for orgnr in orgnrs or ("000000019",):
            self.metadata.write_check_status(
                {
                    "orgnr": orgnr,
                    "baseline_completed_at": "2026-07-14T10:00:00+00:00",
                    "last_report_id": None,
                    "last_checked_at": "2026-07-14T10:00:00+00:00",
                    "last_success_at": "2026-07-14T10:00:00+00:00",
                    "last_error": None,
                }
            )

    def test_new_filing_is_archived_and_repeated_run_is_idempotent(self):
        self.mark_baseline_complete()
        client = FakeClient(pdf_results=[BinaryResponse(PDF, "application/pdf")])
        notifier = CountingNotifier()
        service = self.service(client, notifier)

        first = service.run(self.companies, trigger="fixture")
        second = service.run(self.companies, trigger="fixture")

        self.assertEqual(first["new_filings"], 1)
        self.assertEqual(second["new_filings"], 0)
        self.assertEqual(len(self.store.list_filings()), 1)
        self.assertEqual(len(notifier.calls), 1)
        self.assertEqual(len(list((self.root / "documents").rglob("*.pdf"))), 1)
        event = self.root / "data" / "events" / "000000019" / "5667197"
        self.assertTrue((event / "latest.json").exists())
        self.assertTrue((event / "detail.json").exists())
        self.assertIn("github_issue", (event / "summary.json").read_text())
        self.assertIn("Eksempel ASA", (self.root / "site" / "index.html").read_text())

    def test_first_observation_is_deferred_without_pdf_or_notification(self):
        client = FakeClient(pdf_results=[])
        notifier = CountingNotifier()

        result = self.service(client, notifier).run(self.companies, trigger="schedule")

        filing = self.store.get_filing("000000019", 5667197)
        event = self.metadata.read_event_summary("000000019", 5667197)
        check = self.metadata.read_check_status("000000019")
        self.assertEqual(result["new_filings"], 0)
        self.assertEqual(filing["document_status"], "deferred")
        self.assertEqual(event["discovery_mode"], "baseline")
        self.assertEqual(event["document"], {"status": "deferred", "error": "baseline"})
        self.assertEqual(check["baseline_completed_at"], "2026-07-15T10:00:00+00:00")
        self.assertEqual(
            client.calls,
            [("latest", "000000019"), ("detail", "000000019", 5667197)],
        )
        self.assertEqual(notifier.calls, [])
        self.assertFalse((self.root / "documents").exists())

    def test_failed_first_observation_remains_unbaselined_and_is_retried(self):
        client = FakeClient(
            latest_by_org={"000000019": TransientBrregError("timeout")},
            pdf_results=[],
        )
        service = self.service(client)

        first = service.run(self.companies, trigger="schedule")
        failed_check = self.metadata.read_check_status("000000019")
        client.latest_by_org["000000019"] = []
        second = service.run(self.companies, trigger="schedule")
        successful_check = self.metadata.read_check_status("000000019")

        self.assertEqual(first["status"], "partial")
        self.assertIsNone(failed_check["baseline_completed_at"])
        self.assertEqual(failed_check["last_error"], "TransientBrregError")
        self.assertEqual(second["status"], "success")
        self.assertEqual(
            successful_check["baseline_completed_at"],
            "2026-07-15T10:00:00+00:00",
        )

    def test_missing_pdf_is_completed_by_later_run_without_duplicate_event(self):
        self.mark_baseline_complete()
        client = FakeClient(
            pdf_results=[BrregError("Annual-report PDF is not available"), BinaryResponse(PDF, "application/pdf")]
        )
        notifier = CountingNotifier()
        service = self.service(client, notifier)

        first = service.run(self.companies, trigger="fixture")
        first_filing = self.store.get_filing("000000019", 5667197)
        second = service.run(self.companies, trigger="fixture")
        second_filing = self.store.get_filing("000000019", 5667197)

        self.assertEqual(first["status"], "partial")
        self.assertEqual(first_filing["document_status"], "pending")
        self.assertEqual(second["new_filings"], 0)
        self.assertEqual(second_filing["document_status"], "archived")
        self.assertEqual(len(notifier.calls), 1)

    def test_no_available_accounts_is_successful_and_creates_no_event(self):
        client = FakeClient(latest_by_org={"000000019": []}, pdf_results=[])
        result = self.service(client).run(self.companies, trigger="fixture")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["new_filings"], 0)
        self.assertEqual(self.store.list_filings(), [])

    def test_failure_for_one_company_does_not_stop_other_companies(self):
        companies = self.companies + [Company("000000027", "FIKTIVT ANNET AS", True)]
        client = FakeClient(
            latest_by_org={
                "000000019": TransientBrregError("timeout"),
                "000000027": [],
            },
            pdf_results=[],
        )
        result = self.service(client).run(companies, trigger="fixture")

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["errors"][0]["orgnr"], "000000019")
        self.assertIn(("latest", "000000027"), client.calls)
        self.assertEqual(self.store.get_check_status("000000027")["last_error"], None)
        self.assertGreaterEqual(len(self.store.list_attempts()), 2)

    def test_public_action_logs_redact_monitored_identifiers(self):
        stream = StringIO()
        logger = logging.getLogger(f"test-redacted-service-{id(self)}")
        logger.disabled = False
        logger.propagate = False
        logger.handlers = [logging.StreamHandler(stream)]
        orgnr = self.companies[0].orgnr
        client = FakeClient(
            latest_by_org={orgnr: TransientBrregError("timeout")},
            pdf_results=[],
        )

        self.service(client, logger=logger, redact_identifiers=True).run(
            self.companies, trigger="schedule"
        )

        output = stream.getvalue()
        self.assertIn("monitored company", output)
        self.assertNotIn(orgnr, output)
        self.assertNotIn("Eksempel ASA", output)

    def test_manual_orgnr_runs_only_the_explicit_company(self):
        companies = self.companies + [Company("000000027", "FIKTIVT ANNET AS", True)]
        client = FakeClient(latest_by_org={"000000019": [], "000000027": []}, pdf_results=[])
        result = self.service(client).run(
            companies, trigger="manual", requested_orgnr="000000027"
        )
        self.assertEqual(result["checked"], 1)
        self.assertEqual(client.calls, [("latest", "000000027")])

    def test_failed_issue_notification_is_retried_without_new_filing(self):
        self.mark_baseline_complete()
        client = FakeClient(pdf_results=[BinaryResponse(PDF, "application/pdf")])
        notifier = FlakyNotifier()
        service = self.service(client, notifier)

        first = service.run(self.companies, trigger="fixture")
        second = service.run(self.companies, trigger="fixture")

        self.assertEqual(first["status"], "partial")
        self.assertEqual(second["new_filings"], 0)
        self.assertEqual(len(notifier.calls), 2)
        self.assertEqual(notifier.calls[0][0], notifier.calls[1][0])
        self.assertTrue(
            self.store.notification_exists(5667197, "000000019", "github_issue", "new_filing")
        )
        event_summary = self.metadata.read_event_summary("000000019", 5667197)
        self.assertFalse(event_summary["notification_pending"])

    def test_notification_intent_is_persisted_before_external_issue_call(self):
        self.mark_baseline_complete()
        client = FakeClient(pdf_results=[BinaryResponse(PDF, "application/pdf")])
        notifier = InspectingNotifier(self.metadata)

        self.service(client, notifier).run(self.companies, trigger="fixture")

        self.assertEqual(len(notifier.calls), 1)
        notification_key, filings = notifier.calls[0]
        self.assertEqual(len(filings), 1)
        self.assertEqual(notifier.observed, [(True, notification_key)])

    def test_multiple_new_filings_create_one_issue_for_the_run(self):
        self.metadata = RecordingMetadata(self.root / "data")
        self.mark_baseline_complete("000000019", "000000027")
        companies = self.companies + [Company("000000027", "FIKTIVT ANNET AS", True)]
        second_latest = dict(LATEST, id=6000000)
        client = FakeClient(
            latest_by_org={
                "000000019": [LATEST],
                "000000027": [second_latest],
            },
            pdf_results=[
                BinaryResponse(PDF, "application/pdf"),
                BinaryResponse(PDF, "application/pdf"),
            ],
        )
        notifier = CountingNotifier()

        self.service(client, notifier).run(companies, trigger="fixture")

        self.assertEqual(len(notifier.calls), 1)
        notification_key, filings = notifier.calls[0]
        self.assertEqual(len(filings), 2)
        first = self.metadata.read_event_summary("000000019", 5667197)
        second = self.metadata.read_event_summary("000000027", 6000000)
        self.assertEqual(first["notification_key"], notification_key)
        self.assertEqual(second["notification_key"], notification_key)
        self.assertEqual(
            self.metadata.initial_notification_keys,
            [notification_key, notification_key],
        )


if __name__ == "__main__":
    unittest.main()
