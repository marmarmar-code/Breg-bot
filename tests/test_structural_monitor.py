import logging
import tempfile
import unittest
from io import StringIO
from pathlib import Path

from breg_watch.brreg import JsonResponse
from breg_watch.companies import Company
from breg_watch.notifier import NotificationError
from breg_watch.structural_monitor import (
    StructuralBrregClient,
    StructuralMonitorService,
    StructuralRepository,
    canonical_capital,
    diff_capital,
)


PARENT = "000000019"
SUB_OLD = "000000027"
SUB_NEW = "000000035"


def entity_capital(amount=100_000, shares=100):
    return {
        "organisasjonsnummer": PARENT,
        "navn": "FIKTIVT MEDIE AS",
        "kapital": {
            "belop": amount,
            "antallAksjer": shares,
            "type": "Aksjekapital",
            "valuta": "NOK",
            "innbetalt": amount,
            "bundet": amount,
            "fulltInnbetalt": True,
            "innfortDato": "2026-08-19",
        },
    }


def subunit(orgnr=SUB_OLD, name="FIKTIV REDAKSJON", place="OSLO"):
    return {
        "organisasjonsnummer": orgnr,
        "navn": name,
        "overordnetEnhet": PARENT,
        "oppstartsdato": "2020-01-01",
        "naeringskode1": {"kode": "58.130", "beskrivelse": "Utgivelse av aviser"},
        "beliggenhetsadresse": {"poststed": place, "kommune": "OSLO"},
    }


class FakeClient:
    def __init__(self):
        self.entity_payload = entity_capital()
        self.current_subunits = [subunit()]
        self.entity_events = []
        self.subunit_events = []
        self.subunit_details = {SUB_OLD: subunit()}
        self.announcement_items = []

    def entity_details(self, orgnr):
        if orgnr != PARENT:
            raise AssertionError(orgnr)
        return JsonResponse(self.entity_payload, b"{}")

    def entity_updates(self, orgnrs, *, after_time=None, after_id=None, size=1000):
        return list(self.entity_events)

    def subunits_for_parent(self, orgnr):
        if orgnr != PARENT:
            raise AssertionError(orgnr)
        return list(self.current_subunits)

    def subunit_updates(self, *, after_time=None, after_id=None, size=1000):
        return list(self.subunit_events)

    def subunit_detail(self, orgnr):
        return dict(self.subunit_details[orgnr])

    def announcements_for_date(self, date_value):
        return [dict(item) for item in self.announcement_items if item["date"] == date_value]


class RecordingNotifier:
    def __init__(self, error=None):
        self.messages = []
        self.error = error

    def send_text(self, text):
        self.messages.append(text)
        if self.error:
            raise self.error


class StructuralMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = StructuralRepository(Path(self.temp.name) / "data")
        self.client = FakeClient()
        self.company = Company(PARENT, "FIKTIVT MEDIE AS", True)
        self.times = iter(
            [
                "2026-08-19T11:00:00.000+00:00",
                "2026-08-19T12:00:00.000+00:00",
                "2026-08-19T13:00:00.000+00:00",
            ]
        )

    def tearDown(self):
        self.temp.cleanup()

    def service(self, notifier=None, *, logger=None, redact=False):
        return StructuralMonitorService(
            client=self.client,
            repository=self.repo,
            notifier=notifier,
            now=lambda: next(self.times),
            logger=logger,
            redact_identifiers=redact,
        )

    def test_first_run_is_silent_baseline_for_capital_subunits_and_announcements(self):
        self.client.announcement_items = [
            {
                "id": "20260000000001",
                "orgnr": PARENT,
                "type": "Fusjonsplan",
                "url": "https://example.invalid/1",
                "date": "19.08.2026",
            }
        ]
        notifier = RecordingNotifier()

        result = self.service(notifier).run([self.company])

        self.assertTrue(result["baseline"])
        self.assertEqual(result["alerts"], 0)
        self.assertEqual(notifier.messages, [])
        self.assertEqual(self.repo.read_capital(PARENT)["amount"], 100_000)
        self.assertEqual(self.repo.read_subunit(SUB_OLD)["parent"], PARENT)
        state = self.repo.read_state()
        self.assertTrue(state["baseline_complete"])
        self.assertIn("20260000000001", state["seen_announcement_ids"])

    def test_capital_new_subunit_and_fusion_announcement_are_combined_in_private_alert(self):
        notifier = RecordingNotifier()
        service = self.service(notifier)
        service.run([self.company])

        self.client.entity_payload = entity_capital(150_000, 150)
        self.client.entity_events = [
            {"oppdateringsid": 101, "organisasjonsnummer": PARENT}
        ]
        self.client.subunit_details[SUB_NEW] = subunit(SUB_NEW, "FIKTIV BERGEN", "BERGEN")
        self.client.subunit_events = [
            {
                "oppdateringsid": 202,
                "organisasjonsnummer": SUB_NEW,
                "endringstype": "Ny",
                "endringer": [{"op": "add", "path": "/overordnetEnhet", "value": PARENT}],
            }
        ]
        self.client.announcement_items = [
            {
                "id": "20260000000002",
                "orgnr": PARENT,
                "type": "Gjennomføring av fusjon",
                "url": "https://example.invalid/2",
                "date": "19.08.2026",
            }
        ]

        result = service.run([self.company])

        self.assertEqual(result["alerts"], 3)
        self.assertEqual(len(notifier.messages), 1)
        text = notifier.messages[0]
        self.assertIn("Kapital: 100 000 NOK → 150 000 NOK", text)
        self.assertIn("Antall aksjer: 100 → 150", text)
        self.assertIn("Ny underenhet: FIKTIV BERGEN", text)
        self.assertIn("Gjennomføring av fusjon", text)
        state = self.repo.read_state()
        self.assertEqual(state["capital_after_id"], 101)
        self.assertEqual(state["subunit_after_id"], 202)

    def test_irrelevant_subunit_is_not_stored_or_alerted(self):
        service = self.service(RecordingNotifier())
        service.run([self.company])
        other = "000000043"
        self.client.subunit_details[other] = {
            **subunit(other, "ANNEN UNDERENHET", "TRONDHEIM"),
            "overordnetEnhet": "000000051",
        }
        self.client.subunit_events = [
            {"oppdateringsid": 300, "organisasjonsnummer": other, "endringstype": "Ny"}
        ]

        result = service.run([self.company])

        self.assertEqual(result["alerts"], 0)
        self.assertIsNone(self.repo.read_subunit(other))

    def test_slack_failure_does_not_advance_structural_cursors(self):
        baseline = self.service(RecordingNotifier())
        baseline.run([self.company])
        before = self.repo.read_state()
        self.client.entity_payload = entity_capital(200_000, 200)
        self.client.entity_events = [
            {"oppdateringsid": 400, "organisasjonsnummer": PARENT}
        ]
        failing = StructuralMonitorService(
            client=self.client,
            repository=self.repo,
            notifier=RecordingNotifier(NotificationError("temporary")),
            now=lambda: "2026-08-19T12:00:00.000+00:00",
        )

        with self.assertRaises(NotificationError):
            failing.run([self.company])

        self.assertEqual(self.repo.read_state(), before)
        self.assertEqual(self.repo.read_capital(PARENT)["amount"], 100_000)

    def test_redacted_log_contains_no_counts_or_identifiers(self):
        stream = StringIO()
        logger = logging.getLogger(f"structural-{id(self)}")
        logger.propagate = False
        logger.handlers = [logging.StreamHandler(stream)]
        logger.setLevel(logging.INFO)

        self.service(RecordingNotifier(), logger=logger, redact=True).run([self.company])

        output = stream.getvalue()
        self.assertIn("Structural BRREG baseline completed", output)
        self.assertNotIn(PARENT, output)
        self.assertNotIn("FIKTIVT", output)
        self.assertNotIn("companies", output)

    def test_capital_diff_ignores_only_registration_date_change(self):
        old = canonical_capital(entity_capital())
        changed = dict(old)
        changed["introduced_date"] = "2026-08-20"
        self.assertEqual(diff_capital(old, changed), [])


class AnnouncementParserTests(unittest.TestCase):
    def test_parser_keeps_only_fusion_and_fission_announcements(self):
        markup = """
        <a href="hent_en.jsp?kid=20260000000001&amp;sokeverdi=000000019&amp;spraak=nb">Fusjonsplan</a>
        <a href="hent_en.jsp?kid=20260000000002&amp;sokeverdi=000000027&amp;spraak=nb">Kapital</a>
        <a href="hent_en.jsp?kid=20260000000003&amp;sokeverdi=000000035&amp;spraak=nb">Gjennomføring av fisjon</a>
        """
        client = StructuralBrregClient(
            timeout=1,
            max_attempts=1,
            request_interval=0,
        )
        client._request_html = lambda url: markup

        items = client.announcements_for_date("19.08.2026")

        self.assertEqual([item["id"] for item in items], ["20260000000001", "20260000000003"])
        self.assertTrue(all("hent_en.jsp" in item["url"] for item in items))


if __name__ == "__main__":
    unittest.main()
