import logging
import tempfile
import unittest
from io import StringIO
from pathlib import Path

from breg_watch.brreg import JsonResponse
from breg_watch.companies import Company
from breg_watch.notifier import NotificationError
from breg_watch.registry_monitor import RegistryMonitorService, RegistryRepository


ORGNR = "000000019"


def entity(name="FIKTIVT MEDIE AS", *, bankrupt=False, industry="58.130"):
    return {
        "organisasjonsnummer": ORGNR,
        "navn": name,
        "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap"},
        "naeringskode1": {"kode": industry, "beskrivelse": "Utgivelse av aviser"},
        "konkurs": bankrupt,
        "underAvvikling": False,
        "underTvangsavviklingEllerTvangsopplosning": False,
    }


def roles(manager="Ada Test", chair="Bjørn Eksempel"):
    def person(code, first, last):
        return {
            "type": {"kode": code},
            "person": {"navn": {"fornavn": first, "etternavn": last}},
        }

    manager_first, manager_last = manager.split(" ", 1)
    chair_first, chair_last = chair.split(" ", 1)
    return {
        "rollegrupper": [
            {
                "roller": [
                    person("DAGL", manager_first, manager_last),
                    person("LEDE", chair_first, chair_last),
                    person("MEDL", "Cecilie", "Fiktiv"),
                    person("REVI", "Ignorert", "Revisor"),
                ]
            }
        ]
    }


class FakeRegistryClient:
    def __init__(self):
        self.entity_payload = entity()
        self.roles_payload = roles()
        self.entity_events = []
        self.role_events = []
        self.update_calls = []

    def entity_details(self, orgnr):
        self.assert_orgnr(orgnr)
        return JsonResponse(self.entity_payload, b"{}")

    def roles(self, orgnr):
        self.assert_orgnr(orgnr)
        return JsonResponse(self.roles_payload, b"{}")

    def entity_updates(self, orgnrs, *, after_time=None, after_id=None, size=1000):
        self.update_calls.append(("entity", list(orgnrs), after_time, after_id, size))
        return list(self.entity_events)

    def role_updates(self, orgnrs, *, after_time=None, after_id=None, size=1000):
        self.update_calls.append(("roles", list(orgnrs), after_time, after_id, size))
        return list(self.role_events)

    @staticmethod
    def assert_orgnr(orgnr):
        if orgnr != ORGNR:
            raise AssertionError(orgnr)


class RecordingNotifier:
    def __init__(self, error=None):
        self.messages = []
        self.error = error

    def send_text(self, text):
        self.messages.append(text)
        if self.error is not None:
            raise self.error


class RegistryMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = RegistryRepository(self.root / "data")
        self.client = FakeRegistryClient()
        self.company = Company(ORGNR, "FIKTIVT MEDIE AS", True)
        self.times = iter(
            [
                "2026-08-19T11:00:00.000Z",
                "2026-08-19T11:00:00.001Z",
                "2026-08-19T12:00:00.000Z",
                "2026-08-19T13:00:00.000Z",
            ]
        )

    def tearDown(self):
        self.temporary.cleanup()

    def service(self, notifier=None, *, logger=None, redact=False):
        return RegistryMonitorService(
            client=self.client,
            repository=self.repository,
            notifier=notifier,
            clock=lambda: next(self.times),
            logger=logger,
            redact_identifiers=redact,
        )

    def test_first_run_creates_silent_complete_baseline(self):
        notifier = RecordingNotifier()

        result = self.service(notifier).run([self.company])

        self.assertTrue(result["baseline"])
        self.assertEqual(result["alerts"], 0)
        self.assertEqual(notifier.messages, [])
        state = self.repository.read_state()
        self.assertTrue(state["baseline_complete"])
        self.assertEqual(state["entity_after_time"], "2026-08-19T11:00:00.000Z")
        snapshot = self.repository.read_snapshot(ORGNR)
        self.assertEqual(snapshot["entity"]["name"], "FIKTIVT MEDIE AS")
        self.assertEqual(snapshot["roles"]["DAGL"], ["Ada Test"])
        self.assertNotIn("REVI", snapshot["roles"])

    def test_entity_and_role_changes_send_one_before_after_slack_alert(self):
        notifier = RecordingNotifier()
        service = self.service(notifier)
        service.run([self.company])

        self.client.entity_payload = entity("FIKTIVT NYTT NAVN AS", bankrupt=True)
        self.client.roles_payload = roles(manager="Dina Ny")
        self.client.entity_events = [{"oppdateringsid": 101, "organisasjonsnummer": ORGNR}]
        self.client.role_events = [{"id": 202, "data": {"organisasjonsnummer": ORGNR}}]

        result = service.run([self.company])

        self.assertFalse(result["baseline"])
        self.assertEqual(result["alerts"], 1)
        self.assertEqual(len(notifier.messages), 1)
        message = notifier.messages[0]
        self.assertIn("FIKTIVT MEDIE AS → FIKTIVT NYTT NAVN AS", message)
        self.assertIn("Konkurs: ja", message)
        self.assertIn("Daglig leder: Ada Test → Dina Ny", message)
        state = self.repository.read_state()
        self.assertEqual(state["entity_after_id"], 101)
        self.assertEqual(state["role_after_id"], 202)
        self.assertEqual(
            self.repository.read_snapshot(ORGNR)["roles"]["DAGL"], ["Dina Ny"]
        )

    def test_failed_slack_does_not_advance_snapshot_or_cursors(self):
        baseline = self.service(RecordingNotifier())
        baseline.run([self.company])
        before = self.repository.read_snapshot(ORGNR)
        state_before = self.repository.read_state()

        self.client.roles_payload = roles(manager="Dina Ny")
        self.client.role_events = [{"id": 203, "data": {"organisasjonsnummer": ORGNR}}]
        failing = RegistryMonitorService(
            client=self.client,
            repository=self.repository,
            notifier=RecordingNotifier(NotificationError("temporary")),
            clock=lambda: "2026-08-19T12:00:00.000Z",
        )

        with self.assertRaises(NotificationError):
            failing.run([self.company])

        self.assertEqual(self.repository.read_snapshot(ORGNR), before)
        self.assertEqual(self.repository.read_state(), state_before)

    def test_redacted_logs_reveal_no_identity_counts_or_findings(self):
        stream = StringIO()
        logger = logging.getLogger(f"registry-redacted-{id(self)}")
        logger.disabled = False
        logger.propagate = False
        logger.handlers = [logging.StreamHandler(stream)]
        logger.setLevel(logging.INFO)

        self.service(RecordingNotifier(), logger=logger, redact=True).run([self.company])

        output = stream.getvalue()
        self.assertIn("Registry baseline completed", output)
        self.assertNotIn(ORGNR, output)
        self.assertNotIn("FIKTIVT MEDIE", output)
        self.assertNotIn("1 monitored", output)
        self.assertNotIn("alerts=", output)


if __name__ == "__main__":
    unittest.main()
