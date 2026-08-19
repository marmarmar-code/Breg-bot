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

    def test_notify_formats_annual_account_alert(self):
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
                "archive_reference": "https://github.invalid/report.pdf",
            }
        ]

        reference = notifier.notify("run-1", filings)

        payload = json.loads(transport.calls[0][1].decode("utf-8"))["text"]
        self.assertIn("Eksempel AS", payload)
        self.assertIn("2025-12-31", payload)
        self.assertIn("https://github.invalid/report.pdf", payload)
        self.assertEqual(reference, "slack:run-1")
        self.assertEqual(notifier.channel, "slack")

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
