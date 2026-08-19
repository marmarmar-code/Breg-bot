import json
import unittest
from pathlib import Path

from breg_watch.brreg import (
    BrregClient,
    BrregError,
    HttpResponse,
    InvalidResponse,
    RegisteredEntity,
    TransientBrregError,
)


FIXTURES = Path(__file__).parent / "fixtures"


class QueueTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []
        self.max_bytes = []

    def __call__(self, url, timeout, max_bytes=None):
        self.urls.append(url)
        self.max_bytes.append(max_bytes)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def response(status, body, content_type="application/json", headers=None):
    all_headers = {"Content-Type": content_type}
    all_headers.update(headers or {})
    return HttpResponse(status=status, headers=all_headers, body=body)


class BrregClientTests(unittest.TestCase):
    def client(self, responses, sleeps=None, max_attempts=3):
        transport = QueueTransport(responses)
        sleeps = sleeps if sleeps is not None else []
        client = BrregClient(
            transport=transport,
            sleep=sleeps.append,
            timeout=1,
            max_attempts=max_attempts,
            request_interval=0,
        )
        return client, transport

    def test_reads_latest_detail_years_and_pdf_formats(self):
        latest = (FIXTURES / "latest_new.json").read_bytes()
        detail = (FIXTURES / "detail_5667197.json").read_bytes()
        years = (FIXTURES / "years_2024.json").read_bytes()
        pdf = b"%PDF-1.4\nfixture\n%%EOF\n"
        client, transport = self.client(
            [
                response(200, latest),
                response(200, detail),
                response(200, years),
                response(200, pdf, "application/pdf"),
            ]
        )

        latest_result = client.latest("000000019")
        detail_result = client.detail("000000019", 5667197)
        years_result = client.available_years("000000019")
        pdf_result = client.annual_report("000000019", "2024")

        self.assertEqual(latest_result.data[0]["id"], 5667197)
        self.assertEqual(json.loads(latest_result.raw)[0]["journalnr"], "2025428073")
        self.assertEqual(detail_result.data["id"], 5667197)
        self.assertEqual(years_result.data, ["2023", "2024"])
        self.assertTrue(pdf_result.body.startswith(b"%PDF-"))
        self.assertIn("/000000019/5667197", transport.urls[1])
        self.assertIn("/aarsregnskap/kopi/000000019/2024", transport.urls[3])
        self.assertEqual(transport.max_bytes[3], 100 * 1024 * 1024)

    def test_accepts_document_api_octet_stream_media_type(self):
        client, _ = self.client(
            [response(200, b"%PDF-1.4\nfixture", "application/octet-stream")]
        )

        result = client.annual_report("000000019", "2024")

        self.assertEqual(result.content_type, "application/octet-stream")

    def test_latest_404_means_no_available_report(self):
        client, _ = self.client([response(404, b"")])
        result = client.latest("000000019")
        self.assertEqual(result.data, [])

    def test_available_years_404_means_no_available_document(self):
        client, _ = self.client([response(404, b"")])
        result = client.available_years("000000019")
        self.assertEqual(result.data, [])

    def test_retries_429_using_retry_after(self):
        sleeps = []
        client, _ = self.client(
            [
                response(429, b"", headers={"Retry-After": "7"}),
                response(200, b"[]"),
            ],
            sleeps=sleeps,
        )
        self.assertEqual(client.latest("000000019").data, [])
        self.assertEqual(sleeps, [7.0])

    def test_retries_5xx_and_timeout_then_raises(self):
        client, _ = self.client(
            [response(503, b""), TimeoutError("late"), response(500, b"")]
        )
        with self.assertRaises(TransientBrregError):
            client.latest("000000019")

    def test_rejects_invalid_json_and_non_pdf_document(self):
        client, _ = self.client([response(200, b"not-json")])
        with self.assertRaises(InvalidResponse):
            client.latest("000000019")

        client, _ = self.client([response(200, b"html", "text/html")])
        with self.assertRaises(InvalidResponse):
            client.annual_report("000000019", "2024")

    def test_non_retryable_4xx_is_reported(self):
        client, _ = self.client([response(403, b"forbidden")])
        with self.assertRaises(BrregError):
            client.detail("000000019", 1)

    def test_reads_canonical_entity_name_by_organisation_number(self):
        body = json.dumps(
            {"organisasjonsnummer": "000000019", "navn": "FIKTIVT SELSKAP AS"}
        ).encode()
        client, transport = self.client([response(200, body)])

        entity = client.registered_entity("000000019")

        self.assertEqual(entity, RegisteredEntity("000000019", "FIKTIVT SELSKAP AS", "active"))
        self.assertIn("/enhetsregisteret/api/enheter/000000019", transport.urls[0])

    def test_classifies_deleted_unknown_and_removed_entities(self):
        deleted = json.dumps(
            {
                "organisasjonsnummer": "000000019",
                "navn": "FIKTIVT SELSKAP AS",
                "slettedato": "2026-01-01",
            }
        ).encode()
        client, _ = self.client(
            [response(200, deleted), response(404, b""), response(410, b"")]
        )

        self.assertEqual(client.registered_entity("000000019").status, "deleted")
        self.assertEqual(client.registered_entity("000000019").status, "unknown")
        self.assertEqual(client.registered_entity("000000019").status, "removed")

    def test_rejects_entity_response_for_a_different_organisation_number(self):
        body = json.dumps(
            {"organisasjonsnummer": "000000027", "navn": "FEIL ENHET AS"}
        ).encode()
        client, _ = self.client([response(200, body)])

        with self.assertRaises(InvalidResponse):
            client.registered_entity("000000019")


if __name__ == "__main__":
    unittest.main()
