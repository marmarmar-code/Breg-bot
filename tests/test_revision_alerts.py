import json
import tempfile
import unittest
from pathlib import Path

from breg_watch.archive import LocalArchive
from breg_watch.brreg import BinaryResponse, JsonResponse
from breg_watch.companies import Company
from breg_watch.metadata import MetadataRepository
from breg_watch.notifier import SlackNotifier
from breg_watch.service import MonitorService
from breg_watch.store import Store


ORGNR = "000000000"
PDF = b"%PDF-1.4\nsynthetic fixture\n%%EOF\n"

OLD = {
    "id": 1001,
    "journalnr": "synthetic-old",
    "regnskapstype": "SELSKAP",
    "regnskapsperiode": {"fraDato": "2024-01-01", "tilDato": "2024-12-31"},
    "egenkapitalGjeld": {
        "egenkapital": {"sumEgenkapital": 1000000.0},
        "gjeldOversikt": {"sumGjeld": 500000.0},
    },
    "eiendeler": {"sumEiendeler": 1500000.0},
    "resultatregnskapResultat": {
        "ordinaertResultatFoerSkattekostnad": 400000.0,
        "aarsresultat": 300000.0,
        "finansresultat": {
            "nettoFinans": 50000.0,
            "finansinntekt": {"sumFinansinntekter": 60000.0},
            "finanskostnad": {"sumFinanskostnad": 10000.0},
        },
        "driftsresultat": {
            "driftsresultat": 350000.0,
            "driftsinntekter": {"sumDriftsinntekter": 2000000.0},
            "driftskostnad": {"sumDriftskostnad": 1650000.0},
        },
    },
}

REVISION = {
    **OLD,
    "id": 1002,
    "journalnr": "synthetic-revision",
    "egenkapitalGjeld": {
        "egenkapital": {"sumEgenkapital": 950000.0},
        "gjeldOversikt": {"sumGjeld": 550000.0},
    },
    "resultatregnskapResultat": {
        **OLD["resultatregnskapResultat"],
        "ordinaertResultatFoerSkattekostnad": 350000.0,
        "aarsresultat": 250000.0,
        "driftsresultat": {
            **OLD["resultatregnskapResultat"]["driftsresultat"],
            "driftsresultat": 300000.0,
            "driftskostnad": {"sumDriftskostnad": 1700000.0},
        },
    },
}

NEW_PERIOD = {
    **REVISION,
    "id": 1003,
    "journalnr": "synthetic-new-period",
    "regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"},
}


class VersionedClient:
    base_url = "https://data.brreg.no/regnskapsregisteret/regnskap"

    def __init__(self, current):
        self.current = current

    def latest(self, orgnr):
        payload = [self.current]
        return JsonResponse(payload, json.dumps(payload).encode("utf-8"))

    def detail(self, orgnr, report_id):
        return JsonResponse(self.current, json.dumps(self.current).encode("utf-8"))

    def available_years(self, orgnr):
        return JsonResponse(["2024", "2025"], b'["2024","2025"]')

    def annual_report(self, orgnr, year):
        return BinaryResponse(PDF, "application/pdf")


class RevisionAlertTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "state.sqlite")
        self.store.initialize()
        self.metadata = MetadataRepository(self.root / "data")
        self.company = Company(ORGNR, "Syntetisk Eksempel AS", True)
        self.payloads = []

        def transport(url, payload, timeout):
            self.payloads.append(json.loads(payload.decode("utf-8"))["text"])
            return 200

        self.notifier = SlackNotifier(
            "https://hooks.slack.com/services/test/test/test",
            transport=transport,
            sleep=lambda _: None,
        )

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def service(self, client):
        return MonitorService(
            store=self.store,
            metadata=self.metadata,
            client=client,
            archive=LocalArchive(self.root / "documents"),
            notifier=self.notifier,
            site_directory=self.root / "site",
            clock=lambda: "2026-09-15T08:00:00+00:00",
            id_factory=lambda: f"run-{len(list((self.root / 'data' / 'runs').glob('*/*.json'))) + 1}",
        )

    def test_same_period_new_report_id_is_revision_with_changes(self):
        client = VersionedClient(OLD)
        service = self.service(client)

        baseline = service.run([self.company], trigger="fixture")
        client.current = REVISION
        result = service.run([self.company], trigger="fixture")

        summary = self.metadata.read_event_summary(ORGNR, 1002)
        labels = {change["label"] for change in summary["changes"]}

        self.assertEqual(baseline["new_filings"], 0)
        self.assertEqual(result["new_filings"], 1)
        self.assertEqual(summary["filing_kind"], "revision")
        self.assertEqual(summary["previous_report_id"], 1001)
        self.assertIn("Driftskostnader", labels)
        self.assertIn("Driftsresultat", labels)
        self.assertIn("Årsresultat", labels)
        self.assertEqual(len(self.payloads), 1)
        self.assertIn("NY VERSJON AV ÅRSREGNSKAP", self.payloads[0])
        self.assertIn("Tidligere BRREG-ID: 1001", self.payloads[0])
        self.assertIn("Ny BRREG-ID: 1002", self.payloads[0])
        self.assertIn(
            "Driftsresultat: 350 000 kr → 300 000 kr",
            self.payloads[0],
        )
        self.assertTrue(
            self.store.notification_exists(1002, ORGNR, "slack", "revised_filing")
        )

    def test_new_period_remains_new_annual_account(self):
        client = VersionedClient(OLD)
        service = self.service(client)

        service.run([self.company], trigger="fixture")
        client.current = NEW_PERIOD
        result = service.run([self.company], trigger="fixture")

        summary = self.metadata.read_event_summary(ORGNR, 1003)

        self.assertEqual(result["new_filings"], 1)
        self.assertEqual(summary["filing_kind"], "new")
        self.assertNotIn("previous_report_id", summary)
        self.assertEqual(len(self.payloads), 1)
        self.assertIn("NYTT ÅRSREGNSKAP", self.payloads[0])
        self.assertNotIn("NY VERSJON AV ÅRSREGNSKAP", self.payloads[0])


if __name__ == "__main__":
    unittest.main()
