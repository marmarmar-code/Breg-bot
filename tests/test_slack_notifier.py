import json
import unittest

from breg_watch.notifier import NotificationError, SlackNotifier


class FakeSlackTransport:
    def __init__(self, statuses=None):
        self.statuses = list(statuses or [200])
        self.calls = []

    def __call__(self, url, payload, timeout):
        self.calls.append((url, payload, timeout))
        return self.statuses.pop(0)


class SlackNotifierTests(unittest.TestCase):
    def test_rejects_non_slack_webhook(self):
        with self.assertRaises(ValueError):
            SlackNotifier("https://example.com/services/a/b/c")

    def test_send_text_posts_json_without_exposing_url_in_payload(self):
        transport = FakeSlackTransport()
        url = "https://hooks.slack.com/services/T000/B000/SECRET"
        notifier = SlackNotifier(url, transport=transport, sleep=lambda _: None)

        notifier.send_text("Breg-bot test")

        self.assertEqual(len(transport.calls), 1)
        called_url, payload, timeout = transport.calls[0]
        self.assertEqual(called_url, url)
        self.assertEqual(json.loads(payload.decode("utf-8")), {"text": "Breg-bot test"})
        self.assertNotIn("SECRET", payload.decode("utf-8"))
        self.assertGreater(timeout, 0)

    def test_notify_formats_standardized_annual_account_alert_with_brreg_link(self):
        transport = FakeSlackTransport()
        notifier = SlackNotifier(
            "https://hooks.slack.com/services/T000/B000/SECRET",
            transport=transport,
            sleep=lambda _: None,
        )
        filings = [
            {
                "company_name": "Eksempel AS",
                "orgnr": "000000019",
                "report_id": 123,
                "period_to": "2025-12-31",
                "archive_reference": "documents/local-report.pdf",
            }
        ]

        reference = notifier.notify("run-1", filings)

        payload = json.loads(transport.calls[0][1].decode("utf-8"))["text"]
        self.assertIn("*BRREG-VARSEL · NYTT ÅRSREGNSKAP*", payload)
        self.assertIn("*Eksempel AS*", payload)
        self.assertIn("Org.nr. 000000019", payload)
        self.assertIn("Periode til: 2025-12-31", payload)
        self.assertIn("BRREG-ID: 123", payload)
        self.assertIn(
            "https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/000000019/2025",
            payload,
        )
        self.assertIn("Åpne årsregnskapet hos BRREG →", payload)
        self.assertNotIn("documents/local-report.pdf", payload)
        self.assertEqual(reference, "slack:run-1")
        self.assertEqual(notifier.channel, "slack")

    def test_notify_prefers_stored_brreg_source_url(self):
        transport = FakeSlackTransport()
        notifier = SlackNotifier(
            "https://hooks.slack.com/services/T000/B000/SECRET",
            transport=transport,
            sleep=lambda _: None,
        )
        filings = [
            {
                "company_name": "Eksempel AS",
                "orgnr": "000000019",
                "report_id": 123,
                "period_to": "2025-12-31",
                "source_url": "https://data.brreg.no/example/report.pdf",
            }
        ]

        notifier.notify("run-2", filings)

        payload = json.loads(transport.calls[0][1].decode("utf-8"))["text"]
        self.assertIn("https://data.brreg.no/example/report.pdf", payload)

    def test_retries_temporary_slack_failure(self):
        transport = FakeSlackTransport([500, 200])
        sleeps = []
        notifier = SlackNotifier(
            "https://hooks.slack.com/services/T000/B000/SECRET",
            transport=transport,
            sleep=sleeps.append,
        )

        notifier.send_text("retry")

        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(sleeps, [1.0])

    def test_fails_on_permanent_slack_rejection(self):
        transport = FakeSlackTransport([400])
        notifier = SlackNotifier(
            "https://hooks.slack.com/services/T000/B000/SECRET",
            transport=transport,
            sleep=lambda _: None,
        )

        with self.assertRaises(NotificationError):
            notifier.send_text("bad")


if __name__ == "__main__":
    unittest.main()
