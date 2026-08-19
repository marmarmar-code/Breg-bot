from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.message import Message
from typing import Any, Callable, Mapping, Sequence


BASE_URL = "https://data.brreg.no/regnskapsregisteret/regnskap"
REGISTRY_BASE_URL = "https://data.brreg.no/enhetsregisteret/api"
ENTITY_BASE_URL = f"{REGISTRY_BASE_URL}/enheter"


class BrregError(RuntimeError):
    """Base error for BRREG requests."""


class TransientBrregError(BrregError):
    """A request failed after bounded retry."""


class InvalidResponse(BrregError):
    """The API response did not match the required minimal contract."""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class JsonResponse:
    data: Any
    raw: bytes


@dataclass(frozen=True, slots=True)
class BinaryResponse:
    body: bytes
    content_type: str


@dataclass(frozen=True, slots=True)
class RegisteredEntity:
    orgnr: str
    name: str | None
    status: str


Transport = Callable[[str, float, int | None], HttpResponse]


def urllib_transport(url: str, timeout: float, max_bytes: int | None = None) -> HttpResponse:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json, application/pdf, application/octet-stream",
            "User-Agent": "breg-watch/0.1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            headers: Message = response.headers
            return HttpResponse(
                status=response.status,
                headers={key: value for key, value in headers.items()},
                body=response.read(max_bytes + 1) if max_bytes is not None else response.read(),
            )
    except urllib.error.HTTPError as exc:
        return HttpResponse(
            status=exc.code,
            headers={key: value for key, value in exc.headers.items()},
            body=exc.read(max_bytes + 1) if max_bytes is not None else exc.read(),
        )


class BrregClient:
    def __init__(
        self,
        *,
        base_url: str = BASE_URL,
        entity_base_url: str = ENTITY_BASE_URL,
        registry_base_url: str = REGISTRY_BASE_URL,
        transport: Transport = urllib_transport,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        timeout: float = 20.0,
        max_attempts: int = 3,
        request_interval: float = 0.25,
        max_document_bytes: int = 100 * 1024 * 1024,
    ) -> None:
        if timeout <= 0 or max_attempts < 1 or request_interval < 0:
            raise ValueError("Invalid BRREG client timing configuration")
        self.base_url = base_url.rstrip("/")
        self.entity_base_url = entity_base_url.rstrip("/")
        self.registry_base_url = registry_base_url.rstrip("/")
        self.transport = transport
        self.sleep = sleep
        self.monotonic = monotonic
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.request_interval = request_interval
        self.max_document_bytes = max_document_bytes
        self._last_request_started: float | None = None

    def latest(self, orgnr: str) -> JsonResponse:
        response = self._request(f"{self.base_url}/{orgnr}", not_found_ok=True)
        if response.status == 404:
            return JsonResponse(data=[], raw=b"[]")
        result = self._json(response)
        if not isinstance(result.data, list):
            raise InvalidResponse("Latest-report response must be a JSON list")
        return result

    def detail(self, orgnr: str, report_id: int) -> JsonResponse:
        result = self._json(self._request(f"{self.base_url}/{orgnr}/{report_id}"))
        if not isinstance(result.data, dict) or result.data.get("id") != report_id:
            raise InvalidResponse("Detail response has an unexpected report id")
        return result

    def available_years(self, orgnr: str) -> JsonResponse:
        response = self._request(
            f"{self.base_url}/aarsregnskap/kopi/{orgnr}/aar", not_found_ok=True
        )
        if response.status == 404:
            return JsonResponse(data=[], raw=b"[]")
        result = self._json(response)
        if not isinstance(result.data, list) or not all(isinstance(year, str) for year in result.data):
            raise InvalidResponse("Available-years response must be a JSON list of strings")
        return result

    def annual_report(self, orgnr: str, year: str) -> BinaryResponse:
        response = self._request(
            f"{self.base_url}/aarsregnskap/kopi/{orgnr}/{year}",
            not_found_ok=True,
            max_bytes=self.max_document_bytes,
        )
        if response.status == 404:
            raise BrregError("Annual-report PDF is not available")
        content_type = self._header(response.headers, "Content-Type").split(";", 1)[0].strip().lower()
        if content_type not in {"application/pdf", "application/octet-stream"} or not response.body.startswith(b"%PDF-"):
            raise InvalidResponse("Annual-report response is not a PDF")
        if len(response.body) > self.max_document_bytes:
            raise InvalidResponse("Annual-report PDF exceeds configured size limit")
        return BinaryResponse(body=response.body, content_type=content_type)

    def registered_entity(self, orgnr: str) -> RegisteredEntity:
        response = self._request(
            f"{self.entity_base_url}/{orgnr}", accepted_statuses=(404, 410)
        )
        if response.status == 404:
            return RegisteredEntity(orgnr, None, "unknown")
        if response.status == 410:
            return RegisteredEntity(orgnr, None, "removed")
        result = self._json(response)
        if not isinstance(result.data, dict):
            raise InvalidResponse("Entity response must be a JSON object")
        response_orgnr = result.data.get("organisasjonsnummer")
        name = result.data.get("navn")
        if response_orgnr != orgnr or not isinstance(name, str) or not name.strip():
            raise InvalidResponse("Entity response has an unexpected identity")
        deleted = bool(result.data.get("slettedato") or result.data.get("erSlettet") is True)
        status = "deleted" if deleted else "active"
        return RegisteredEntity(orgnr, name.strip(), status)

    def entity_details(self, orgnr: str) -> JsonResponse:
        response = self._request(
            f"{self.registry_base_url}/enheter/{orgnr}", accepted_statuses=(404, 410)
        )
        if response.status in {404, 410}:
            status = "removed" if response.status == 410 else "unknown"
            payload = {"organisasjonsnummer": orgnr, "_registryStatus": status}
            raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            return JsonResponse(data=payload, raw=raw)
        result = self._json(response)
        if not isinstance(result.data, dict) or result.data.get("organisasjonsnummer") != orgnr:
            raise InvalidResponse("Entity detail response has an unexpected identity")
        return result

    def roles(self, orgnr: str) -> JsonResponse:
        response = self._request(
            f"{self.registry_base_url}/enheter/{orgnr}/roller", not_found_ok=True
        )
        if response.status == 404:
            return JsonResponse(data={"rollegrupper": []}, raw=b'{"rollegrupper":[]}')
        result = self._json(response)
        if not isinstance(result.data, dict) or not isinstance(result.data.get("rollegrupper", []), list):
            raise InvalidResponse("Roles response must contain rollegrupper")
        return result

    def entity_updates(
        self,
        orgnrs: Sequence[str],
        *,
        after_time: str | None = None,
        after_id: int | None = None,
        size: int = 1000,
    ) -> list[dict[str, Any]]:
        if not orgnrs:
            return []
        if (after_time is None) == (after_id is None):
            raise ValueError("Exactly one entity update cursor must be supplied")
        events: dict[int, dict[str, Any]] = {}
        for chunk in self._chunks(orgnrs, 150):
            page = 0
            while True:
                params: dict[str, Any] = {
                    "organisasjonsnummer": ",".join(chunk),
                    "includeChanges": "true",
                    "page": page,
                    "size": size,
                    "sort": "id,ASC",
                }
                if after_id is not None:
                    params["oppdateringsid"] = after_id + 1
                else:
                    params["dato"] = after_time
                result = self._json(
                    self._request(
                        f"{self.registry_base_url}/oppdateringer/enheter?{urllib.parse.urlencode(params, safe=',')}"
                    )
                )
                if not isinstance(result.data, dict):
                    raise InvalidResponse("Entity updates response must be a JSON object")
                embedded = result.data.get("_embedded", {})
                items = embedded.get("oppdaterteEnheter", []) if isinstance(embedded, dict) else []
                if not isinstance(items, list):
                    raise InvalidResponse("Entity updates response has invalid items")
                for item in items:
                    if not isinstance(item, dict) or not isinstance(item.get("oppdateringsid"), int):
                        raise InvalidResponse("Entity update has invalid id")
                    events[item["oppdateringsid"]] = item
                page_info = result.data.get("page", {})
                total_pages = page_info.get("totalPages", 1) if isinstance(page_info, dict) else 1
                if not isinstance(total_pages, int) or page + 1 >= total_pages:
                    break
                page += 1
        return [events[key] for key in sorted(events)]

    def role_updates(
        self,
        orgnrs: Sequence[str],
        *,
        after_time: str | None = None,
        after_id: int | None = None,
        size: int = 1000,
    ) -> list[dict[str, Any]]:
        if not orgnrs:
            return []
        if (after_time is None) == (after_id is None):
            raise ValueError("Exactly one role update cursor must be supplied")
        events: dict[int, dict[str, Any]] = {}
        for chunk in self._chunks(orgnrs, 150):
            chunk_after_id = after_id
            chunk_after_time = after_time
            while True:
                params: dict[str, Any] = {
                    "organisasjonsnummer": ",".join(chunk),
                    "size": size,
                }
                if chunk_after_id is not None:
                    params["afterId"] = chunk_after_id
                else:
                    params["afterTime"] = chunk_after_time
                result = self._json(
                    self._request(
                        f"{self.registry_base_url}/oppdateringer/roller?{urllib.parse.urlencode(params, safe=',')}"
                    )
                )
                if not isinstance(result.data, list):
                    raise InvalidResponse("Role updates response must be a JSON list")
                for item in result.data:
                    event_id = item.get("id") if isinstance(item, dict) else None
                    if not isinstance(event_id, int):
                        raise InvalidResponse("Role update has invalid id")
                    events[event_id] = item
                if len(result.data) < size:
                    break
                next_id = max(int(item["id"]) for item in result.data)
                if chunk_after_id == next_id:
                    raise InvalidResponse("Role update cursor did not advance")
                chunk_after_id = next_id
                chunk_after_time = None
        return [events[key] for key in sorted(events)]

    def _json(self, response: HttpResponse) -> JsonResponse:
        content_type = self._header(response.headers, "Content-Type").split(";", 1)[0].strip().lower()
        if content_type != "application/json" and not content_type.endswith("+json"):
            raise InvalidResponse(f"Unexpected JSON content type: {content_type or 'missing'}")
        try:
            data = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidResponse("Invalid JSON from BRREG") from exc
        return JsonResponse(data=data, raw=response.body)

    def _request(
        self,
        url: str,
        *,
        not_found_ok: bool = False,
        max_bytes: int | None = None,
        accepted_statuses: tuple[int, ...] = (),
    ) -> HttpResponse:
        last_error: BaseException | None = None
        for attempt in range(1, self.max_attempts + 1):
            self._pace()
            try:
                response = self.transport(url, self.timeout, max_bytes)
            except (TimeoutError, OSError, urllib.error.URLError) as exc:
                last_error = exc
                if attempt == self.max_attempts:
                    break
                self.sleep(float(2 ** (attempt - 1)))
                continue

            if (
                200 <= response.status < 300
                or (not_found_ok and response.status == 404)
                or response.status in accepted_statuses
            ):
                return response
            if response.status == 429 or 500 <= response.status < 600:
                last_error = TransientBrregError(f"BRREG returned HTTP {response.status}")
                if attempt == self.max_attempts:
                    break
                retry_after = self._header(response.headers, "Retry-After")
                try:
                    delay = float(retry_after) if retry_after else float(2 ** (attempt - 1))
                except ValueError:
                    delay = float(2 ** (attempt - 1))
                self.sleep(max(0.0, delay))
                continue
            raise BrregError(f"BRREG returned HTTP {response.status}")
        raise TransientBrregError("BRREG request failed after bounded retry") from last_error

    def _pace(self) -> None:
        now = self.monotonic()
        if self._last_request_started is not None:
            remaining = self.request_interval - (now - self._last_request_started)
            if remaining > 0:
                self.sleep(remaining)
                now = self.monotonic()
        self._last_request_started = now

    @staticmethod
    def _header(headers: Mapping[str, str], name: str) -> str:
        wanted = name.lower()
        for key, value in headers.items():
            if key.lower() == wanted:
                return value
        return ""

    @staticmethod
    def _chunks(values: Sequence[str], size: int) -> list[list[str]]:
        return [list(values[index : index + size]) for index in range(0, len(values), size)]
