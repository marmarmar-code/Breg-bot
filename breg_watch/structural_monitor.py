from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from .brreg import BrregClient, BrregError, InvalidResponse, TransientBrregError
from .companies import Company, CompanyListError, load_companies
from .notifier import NotificationError, SlackNotifier


PUBLIC_RUNNER_ENV = "BREG_PUBLIC_RUNNER"
OSLO = ZoneInfo("Europe/Oslo")
ANNOUNCEMENT_URL = "https://w2.brreg.no/kunngjoring/kombisok.jsp"
ANNOUNCEMENT_LINK_RE = re.compile(
    r'href="(?P<href>[^"]*hent_en\.jsp\?kid=(?P<kid>\d+)(?:&amp;|&)sokeverdi=(?P<orgnr>\d{9})[^"]*)"[^>]*>(?P<label>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)


class StructuralRepository:
    def __init__(self, metadata_dir: str | Path) -> None:
        self.root = Path(metadata_dir) / "structural"
        self.state_path = self.root / "state.json"
        self.capital_dir = self.root / "capital"
        self.subunits_dir = self.root / "subunits"

    def read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Structural state must be a JSON object")
        return data

    def write_state(self, state: dict[str, Any]) -> None:
        self._write_json(self.state_path, state)

    def read_capital(self, orgnr: str) -> dict[str, Any] | None:
        return self._read_json(self.capital_dir / f"{orgnr}.json")

    def write_capital(self, orgnr: str, value: dict[str, Any]) -> None:
        self._write_json(self.capital_dir / f"{orgnr}.json", value)

    def read_subunit(self, orgnr: str) -> dict[str, Any] | None:
        return self._read_json(self.subunits_dir / f"{orgnr}.json")

    def write_subunit(self, orgnr: str, value: dict[str, Any]) -> None:
        self._write_json(self.subunits_dir / f"{orgnr}.json", value)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"Structural snapshot must be a JSON object: {path.name}")
        return data

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)


class StructuralBrregClient(BrregClient):
    def subunits_for_parent(self, parent_orgnr: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 0
        while True:
            params = urllib.parse.urlencode(
                {"overordnetEnhet": parent_orgnr, "page": page, "size": 1000, "sort": "organisasjonsnummer"}
            )
            result = self._json(
                self._request(f"{self.registry_base_url}/underenheter?{params}")
            )
            if not isinstance(result.data, dict):
                raise InvalidResponse("Subunit search response must be a JSON object")
            embedded = result.data.get("_embedded", {})
            page_items = embedded.get("underenheter", []) if isinstance(embedded, dict) else []
            if not isinstance(page_items, list):
                raise InvalidResponse("Subunit search response has invalid items")
            for item in page_items:
                if isinstance(item, dict):
                    items.append(item)
                else:
                    raise InvalidResponse("Subunit search item must be a JSON object")
            page_info = result.data.get("page", {})
            total_pages = page_info.get("totalPages", 1) if isinstance(page_info, dict) else 1
            if not isinstance(total_pages, int) or page + 1 >= total_pages:
                break
            page += 1
        return items

    def subunit_detail(self, orgnr: str) -> dict[str, Any]:
        response = self._request(
            f"{self.registry_base_url}/underenheter/{orgnr}",
            accepted_statuses=(404, 410),
        )
        if response.status in {404, 410}:
            return {
                "organisasjonsnummer": orgnr,
                "_registryStatus": "removed" if response.status == 410 else "unknown",
            }
        result = self._json(response)
        if not isinstance(result.data, dict) or result.data.get("organisasjonsnummer") != orgnr:
            raise InvalidResponse("Subunit detail response has an unexpected identity")
        return result.data

    def subunit_updates(
        self,
        *,
        after_time: str | None = None,
        after_id: int | None = None,
        size: int = 1000,
    ) -> list[dict[str, Any]]:
        if (after_time is None) == (after_id is None):
            raise ValueError("Exactly one subunit update cursor must be supplied")
        events: dict[int, dict[str, Any]] = {}
        page = 0
        while True:
            params: dict[str, Any] = {
                "includeChanges": "true",
                "page": page,
                "size": size,
                "sort": "id,ASC",
            }
            if after_id is not None:
                params["oppdateringsid"] = after_id + 1
            else:
                params["dato"] = after_time
            query = urllib.parse.urlencode(params)
            result = self._json(
                self._request(f"{self.registry_base_url}/oppdateringer/underenheter?{query}")
            )
            if not isinstance(result.data, dict):
                raise InvalidResponse("Subunit updates response must be a JSON object")
            embedded = result.data.get("_embedded", {})
            items = embedded.get("oppdaterteUnderenheter", []) if isinstance(embedded, dict) else []
            if not isinstance(items, list):
                raise InvalidResponse("Subunit updates response has invalid items")
            for item in items:
                event_id = item.get("oppdateringsid") if isinstance(item, dict) else None
                if not isinstance(event_id, int):
                    raise InvalidResponse("Subunit update has invalid id")
                events[event_id] = item
            page_info = result.data.get("page", {})
            total_pages = page_info.get("totalPages", 1) if isinstance(page_info, dict) else 1
            if not isinstance(total_pages, int) or page + 1 >= total_pages:
                break
            page += 1
        return [events[key] for key in sorted(events)]

    def announcements_for_date(self, date_value: str) -> list[dict[str, str]]:
        query = urllib.parse.urlencode({"datoFra": date_value})
        url = f"{ANNOUNCEMENT_URL}?{query}"
        text = self._request_html(url)
        results: list[dict[str, str]] = []
        for match in ANNOUNCEMENT_LINK_RE.finditer(text):
            label = _clean_html(match.group("label"))
            if "fusjon" not in label.casefold() and "fisjon" not in label.casefold():
                continue
            href = html.unescape(match.group("href"))
            if href.startswith("http://") or href.startswith("https://"):
                detail_url = href
            else:
                detail_url = urllib.parse.urljoin(ANNOUNCEMENT_URL, href)
            results.append(
                {
                    "id": match.group("kid"),
                    "orgnr": match.group("orgnr"),
                    "type": label,
                    "url": detail_url,
                    "date": date_value,
                }
            )
        return results

    def _request_html(self, url: str) -> str:
        last_error: BaseException | None = None
        for attempt in range(1, self.max_attempts + 1):
            self._pace()
            request = urllib.request.Request(
                url,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "User-Agent": "breg-watch/0.1",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    if response.status != 200:
                        raise BrregError(f"BRREG announcements returned HTTP {response.status}")
                    body = response.read(4 * 1024 * 1024 + 1)
                    if len(body) > 4 * 1024 * 1024:
                        raise InvalidResponse("BRREG announcements response is unexpectedly large")
                    charset = response.headers.get_content_charset() or "utf-8"
                    return body.decode(charset, errors="replace")
            except urllib.error.HTTPError as exc:
                if exc.code == 429 or 500 <= exc.code < 600:
                    last_error = exc
                else:
                    raise BrregError(f"BRREG announcements returned HTTP {exc.code}") from exc
            except (TimeoutError, OSError, urllib.error.URLError) as exc:
                last_error = exc
            if attempt < self.max_attempts:
                time.sleep(float(2 ** (attempt - 1)))
        raise TransientBrregError("BRREG announcements request failed after bounded retry") from last_error


class StructuralMonitorService:
    def __init__(
        self,
        *,
        client: StructuralBrregClient,
        repository: StructuralRepository,
        notifier: Any | None,
        now: Callable[[], str] | None = None,
        logger: logging.Logger | None = None,
        redact_identifiers: bool = False,
    ) -> None:
        self.client = client
        self.repository = repository
        self.notifier = notifier
        self.now = now or _now
        self.logger = logger or logging.getLogger("breg_watch.structural")
        self.redact_identifiers = redact_identifiers

    def run(self, companies: Iterable[Company]) -> dict[str, Any]:
        active = sorted((company for company in companies if company.active), key=lambda item: item.orgnr)
        now = self.now()
        state = self.repository.read_state()
        if not state.get("baseline_started_at"):
            state["baseline_started_at"] = now
            state["capital_baselined"] = []
            state["subunit_parents_baselined"] = []
            state["seen_announcement_ids"] = []
            self.repository.write_state(state)

        if not state.get("baseline_complete"):
            return self._baseline(active, state, now)

        company_by_orgnr = {company.orgnr: company for company in active}
        parent_orgnrs = set(company_by_orgnr)
        staged_capital: dict[str, dict[str, Any]] = {}
        staged_subunits: dict[str, dict[str, Any]] = {}
        state_next = json.loads(json.dumps(state))
        alerts: list[str] = []

        self._baseline_new_companies(active, state_next)

        entity_events = self.client.entity_updates(
            sorted(parent_orgnrs),
            after_id=state.get("capital_after_id")
            if isinstance(state.get("capital_after_id"), int)
            else None,
            after_time=None
            if isinstance(state.get("capital_after_id"), int)
            else str(state.get("capital_after_time") or state["baseline_started_at"]),
        )
        for event in entity_events:
            orgnr = event.get("organisasjonsnummer")
            if not isinstance(orgnr, str) or orgnr not in parent_orgnrs:
                continue
            previous = self.repository.read_capital(orgnr) or {}
            current = canonical_capital(self.client.entity_details(orgnr).data)
            changes = diff_capital(previous, current)
            if changes:
                name = company_by_orgnr[orgnr].name
                alerts.append(_format_company_changes(name, orgnr, changes))
            staged_capital[orgnr] = current
        if entity_events:
            state_next["capital_after_id"] = max(
                int(event["oppdateringsid"]) for event in entity_events
            )
            state_next.pop("capital_after_time", None)

        subunit_events = self.client.subunit_updates(
            after_id=state.get("subunit_after_id")
            if isinstance(state.get("subunit_after_id"), int)
            else None,
            after_time=None
            if isinstance(state.get("subunit_after_id"), int)
            else str(state.get("subunit_after_time") or state["baseline_started_at"]),
        )
        subunit_index = state_next.setdefault("subunit_index", {})
        if not isinstance(subunit_index, dict):
            raise ValueError("subunit_index must be a JSON object")

        for event in subunit_events:
            sub_orgnr = event.get("organisasjonsnummer")
            if not isinstance(sub_orgnr, str):
                continue
            previous = self.repository.read_subunit(sub_orgnr)
            previous_parent = previous.get("parent") if isinstance(previous, dict) else None
            event_parent = _parent_from_changes(event.get("endringer"))
            event_type = str(event.get("endringstype") or "")
            relevant = previous_parent in parent_orgnrs or event_parent in parent_orgnrs

            current_raw: dict[str, Any] | None = None
            if not relevant and event_type == "Ny":
                current_raw = self.client.subunit_detail(sub_orgnr)
                relevant = current_raw.get("overordnetEnhet") in parent_orgnrs
            if not relevant:
                continue

            if current_raw is None:
                current_raw = self.client.subunit_detail(sub_orgnr)
            current = canonical_subunit(current_raw)
            if previous is None:
                if current.get("parent") in parent_orgnrs:
                    alerts.append(
                        _format_subunit_new(current, company_by_orgnr.get(str(current.get("parent"))))
                    )
            else:
                changes = diff_subunit(previous, current, event_type=event_type)
                if changes:
                    parent = str(previous.get("parent") or current.get("parent") or "")
                    parent_company = company_by_orgnr.get(parent)
                    parent_name = parent_company.name if parent_company else parent
                    alerts.append(
                        _format_subunit_changes(
                            parent_name,
                            str(previous.get("name") or current.get("name") or "Underenhet"),
                            sub_orgnr,
                            changes,
                        )
                    )
            staged_subunits[sub_orgnr] = current
            if current.get("parent"):
                subunit_index[sub_orgnr] = current["parent"]
            else:
                subunit_index.pop(sub_orgnr, None)

        if subunit_events:
            state_next["subunit_after_id"] = max(
                int(event["oppdateringsid"]) for event in subunit_events
            )
            state_next.pop("subunit_after_time", None)

        announcements = self._announcement_changes(
            active,
            state,
            state_next,
            now,
        )
        alerts.extend(announcements)

        if alerts and self.notifier is not None:
            self.notifier.send_text(format_slack_alert(alerts))

        for orgnr, value in staged_capital.items():
            self.repository.write_capital(orgnr, value)
        for orgnr, value in staged_subunits.items():
            self.repository.write_subunit(orgnr, value)
        state_next["last_checked_at"] = now
        self.repository.write_state(state_next)

        if self.redact_identifiers:
            self.logger.info("Structural BRREG monitor completed")
        else:
            self.logger.info(
                "Structural BRREG monitor completed; entity_events=%s subunit_events=%s alerts=%s",
                len(entity_events),
                len(subunit_events),
                len(alerts),
            )
        return {"status": "success", "baseline": False, "alerts": len(alerts)}

    def _baseline(self, active: list[Company], state: dict[str, Any], now: str) -> dict[str, Any]:
        capital_done = set(str(value) for value in state.get("capital_baselined", []))
        parents_done = set(str(value) for value in state.get("subunit_parents_baselined", []))
        subunit_index = state.setdefault("subunit_index", {})
        if not isinstance(subunit_index, dict):
            raise ValueError("subunit_index must be a JSON object")

        for company in active:
            if company.orgnr not in capital_done:
                capital = canonical_capital(self.client.entity_details(company.orgnr).data)
                self.repository.write_capital(company.orgnr, capital)
                capital_done.add(company.orgnr)
                state["capital_baselined"] = sorted(capital_done)
                self.repository.write_state(state)

            if company.orgnr not in parents_done:
                for raw in self.client.subunits_for_parent(company.orgnr):
                    subunit = canonical_subunit(raw)
                    sub_orgnr = str(subunit.get("orgnr") or "")
                    if not sub_orgnr:
                        continue
                    self.repository.write_subunit(sub_orgnr, subunit)
                    subunit_index[sub_orgnr] = company.orgnr
                parents_done.add(company.orgnr)
                state["subunit_parents_baselined"] = sorted(parents_done)
                state["subunit_index"] = subunit_index
                self.repository.write_state(state)

        today = _oslo_date(now)
        today_dt = _parse_date(today)
        baseline_dates = [
            value.strftime("%d.%m.%Y")
            for value in _date_range(today_dt - timedelta(days=7), today_dt)
        ]
        current_announcements = self._fetch_announcements_for_dates(
            baseline_dates, {c.orgnr for c in active}
        )
        state["seen_announcement_ids"] = sorted(
            set(str(value) for value in state.get("seen_announcement_ids", []))
            | {item["id"] for item in current_announcements}
        )[-5000:]
        state["announcement_last_date"] = today
        state["capital_after_time"] = state["baseline_started_at"]
        state["subunit_after_time"] = state["baseline_started_at"]
        state["baseline_complete"] = True
        state["baseline_completed_at"] = now
        state["last_checked_at"] = now
        self.repository.write_state(state)

        if self.redact_identifiers:
            self.logger.info("Structural BRREG baseline completed")
        else:
            self.logger.info("Structural BRREG baseline completed for %s companies", len(active))
        return {"status": "success", "baseline": True, "alerts": 0}

    def _baseline_new_companies(self, active: list[Company], state: dict[str, Any]) -> None:
        capital_done = set(str(value) for value in state.get("capital_baselined", []))
        parents_done = set(str(value) for value in state.get("subunit_parents_baselined", []))
        subunit_index = state.setdefault("subunit_index", {})
        for company in active:
            changed = False
            if company.orgnr not in capital_done:
                self.repository.write_capital(
                    company.orgnr,
                    canonical_capital(self.client.entity_details(company.orgnr).data),
                )
                capital_done.add(company.orgnr)
                changed = True
            if company.orgnr not in parents_done:
                for raw in self.client.subunits_for_parent(company.orgnr):
                    subunit = canonical_subunit(raw)
                    sub_orgnr = str(subunit.get("orgnr") or "")
                    if sub_orgnr:
                        self.repository.write_subunit(sub_orgnr, subunit)
                        subunit_index[sub_orgnr] = company.orgnr
                parents_done.add(company.orgnr)
                changed = True
            if changed:
                state["capital_baselined"] = sorted(capital_done)
                state["subunit_parents_baselined"] = sorted(parents_done)
                state["subunit_index"] = subunit_index
                self.repository.write_state(state)

    def _announcement_changes(
        self,
        active: list[Company],
        state: dict[str, Any],
        state_next: dict[str, Any],
        now: str,
    ) -> list[str]:
        today = _oslo_date(now)
        previous = str(state.get("announcement_last_date") or today)
        dates = _date_range(max(_parse_date(previous) - timedelta(days=1), _parse_date(today) - timedelta(days=7)), _parse_date(today))
        watched = {company.orgnr for company in active}
        company_by_orgnr = {company.orgnr: company for company in active}
        items = self._fetch_announcements_for_dates(
            [date.strftime("%d.%m.%Y") for date in dates],
            watched,
        )
        seen = set(str(value) for value in state.get("seen_announcement_ids", []))
        new_items = [item for item in items if item["id"] not in seen]
        for item in items:
            seen.add(item["id"])
        state_next["seen_announcement_ids"] = sorted(seen)[-5000:]
        state_next["announcement_last_date"] = today
        alerts = []
        for item in sorted(new_items, key=lambda value: (value["date"], value["id"])):
            company = company_by_orgnr.get(item["orgnr"])
            if company is None:
                continue
            alerts.append(
                f"*{company.name}* ({item['orgnr']})\n"
                f"– {item['type']} registrert {item['date']}\n"
                f"– {item['url']}"
            )
        return alerts

    def _fetch_announcements_for_dates(
        self,
        dates: list[str],
        watched: set[str],
    ) -> list[dict[str, str]]:
        dedup: dict[str, dict[str, str]] = {}
        for date_value in dates:
            for item in self.client.announcements_for_date(date_value):
                if item["orgnr"] in watched:
                    dedup[item["id"]] = item
        return list(dedup.values())


def canonical_capital(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise InvalidResponse("Entity payload must be a JSON object")
    capital = payload.get("kapital")
    if not isinstance(capital, dict):
        return {}
    return {
        "amount": _number(capital.get("belop")),
        "shares": _integer(capital.get("antallAksjer")),
        "type": _clean(capital.get("type")),
        "currency": _clean(capital.get("valuta")),
        "paid": _number(capital.get("innbetalt")),
        "bound": _number(capital.get("bundet")),
        "fully_paid": bool(capital.get("fulltInnbetalt")) if capital.get("fulltInnbetalt") is not None else None,
        "introduced_date": _clean(capital.get("innfortDato")),
    }


def diff_capital(previous: Any, current: Any) -> list[str]:
    old = previous if isinstance(previous, dict) else {}
    new = current if isinstance(current, dict) else {}
    changes: list[str] = []
    if old.get("amount") != new.get("amount") and (old.get("amount") is not None or new.get("amount") is not None):
        changes.append(
            f"Kapital: {_money(old.get('amount'), old.get('currency'))} → "
            f"{_money(new.get('amount'), new.get('currency'))}"
        )
    if old.get("shares") != new.get("shares") and (old.get("shares") is not None or new.get("shares") is not None):
        changes.append(f"Antall aksjer: {_fmt_int(old.get('shares'))} → {_fmt_int(new.get('shares'))}")
    if old.get("paid") != new.get("paid") and (old.get("paid") is not None or new.get("paid") is not None):
        changes.append(
            f"Innbetalt kapital: {_money(old.get('paid'), old.get('currency'))} → "
            f"{_money(new.get('paid'), new.get('currency'))}"
        )
    if old.get("bound") != new.get("bound") and (old.get("bound") is not None or new.get("bound") is not None):
        changes.append(
            f"Bundet kapital: {_money(old.get('bound'), old.get('currency'))} → "
            f"{_money(new.get('bound'), new.get('currency'))}"
        )
    if old.get("type") and new.get("type") and old.get("type") != new.get("type"):
        changes.append(f"Kapitaltype: {old['type']} → {new['type']}")
    return changes


def canonical_subunit(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise InvalidResponse("Subunit payload must be a JSON object")
    address = payload.get("beliggenhetsadresse")
    industry = payload.get("naeringskode1")
    return {
        "orgnr": _clean(payload.get("organisasjonsnummer")),
        "name": _clean(payload.get("navn")),
        "parent": _clean(payload.get("overordnetEnhet")),
        "start_date": _clean(payload.get("oppstartsdato")),
        "closed_date": _clean(payload.get("nedleggelsesdato")),
        "ownership_date": _clean(payload.get("datoEierskifte")),
        "industry": _coded(industry),
        "place": _place(address),
        "removed": payload.get("_registryStatus") == "removed",
    }


def diff_subunit(previous: Any, current: Any, *, event_type: str) -> list[str]:
    old = previous if isinstance(previous, dict) else {}
    new = current if isinstance(current, dict) else {}
    changes: list[str] = []
    if event_type in {"Sletting", "Fjernet"}:
        changes.append("Underenheten er slettet/fjernet fra registeret")
    if not old.get("closed_date") and new.get("closed_date"):
        changes.append(f"Nedlagt: {new['closed_date']}")
    if old.get("name") and new.get("name") and old.get("name") != new.get("name"):
        changes.append(f"Navn: {old['name']} → {new['name']}")
    if old.get("parent") and new.get("parent") and old.get("parent") != new.get("parent"):
        changes.append(f"Overordnet enhet: {old['parent']} → {new['parent']}")
    if old.get("industry") != new.get("industry") and old.get("industry") and new.get("industry"):
        changes.append(f"Næringskode: {_coded_display(old['industry'])} → {_coded_display(new['industry'])}")
    if old.get("place") != new.get("place") and old.get("place") and new.get("place"):
        changes.append(f"Sted: {old['place']} → {new['place']}")
    return changes


def format_slack_alert(alerts: list[str]) -> str:
    return "*Nye strukturelle BRREG-endringer*\n\n" + "\n\n".join(alerts) + "\n\nKilde: Brønnøysundregistrene"


def _format_company_changes(name: str, orgnr: str, changes: list[str]) -> str:
    return f"*{name}* ({orgnr})\n" + "\n".join(f"– {change}" for change in changes)


def _format_subunit_new(value: dict[str, Any], parent: Company | None) -> str:
    parent_name = parent.name if parent else str(value.get("parent") or "")
    details = [f"Ny underenhet: {value.get('name') or 'Uten navn'} ({value.get('orgnr') or ''})"]
    if value.get("place"):
        details.append(f"Sted: {value['place']}")
    if value.get("start_date"):
        details.append(f"Oppstart: {value['start_date']}")
    return f"*{parent_name}*\n" + "\n".join(f"– {detail}" for detail in details)


def _format_subunit_changes(parent_name: str, name: str, orgnr: str, changes: list[str]) -> str:
    return f"*{parent_name}* – {name} ({orgnr})\n" + "\n".join(f"– {change}" for change in changes)


def _parent_from_changes(changes: Any) -> str | None:
    if not isinstance(changes, list):
        return None
    for change in changes:
        if not isinstance(change, dict):
            continue
        path = str(change.get("path") or "")
        value = change.get("value")
        if path.endswith("/overordnetEnhet") and isinstance(value, str) and len(value) == 9 and value.isdigit():
            return value
    return None


def _clean_html(value: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())


def _clean(value: Any) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _coded(value: Any) -> dict[str, str | None] | None:
    if not isinstance(value, dict):
        return None
    code = _clean(value.get("kode"))
    description = _clean(value.get("beskrivelse"))
    if not code and not description:
        return None
    return {"code": code, "description": description}


def _coded_display(value: dict[str, str | None]) -> str:
    if value.get("code") and value.get("description"):
        return f"{value['code']} {value['description']}"
    return str(value.get("description") or value.get("code") or "ukjent")


def _place(address: Any) -> str | None:
    if not isinstance(address, dict):
        return None
    municipality = _clean(address.get("kommune"))
    poststed = _clean(address.get("poststed"))
    if municipality and poststed and municipality.casefold() != poststed.casefold():
        return f"{poststed}, {municipality}"
    return poststed or municipality


def _money(value: Any, currency: Any) -> str:
    if value is None:
        return "ikke registrert"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    rendered = f"{value:,}".replace(",", " ") if isinstance(value, int) else str(value)
    return f"{rendered} {currency or ''}".strip()


def _fmt_int(value: Any) -> str:
    return "ikke registrert" if value is None else f"{value:,}".replace(",", " ")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _oslo_date(now_value: str) -> str:
    parsed = datetime.fromisoformat(now_value.replace("Z", "+00:00"))
    return parsed.astimezone(OSLO).strftime("%d.%m.%Y")


def _parse_date(value: str) -> datetime:
    return datetime.strptime(value, "%d.%m.%Y")


def _date_range(start: datetime, end: datetime) -> list[datetime]:
    values: list[datetime] = []
    current = start
    while current.date() <= end.date():
        values.append(current)
        current += timedelta(days=1)
    return values


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m breg_watch.structural_monitor")
    parser.add_argument("--companies", default="companies.csv")
    parser.add_argument("--metadata-dir", default="data")
    parser.add_argument("--notify", choices=("none", "slack"), default="none")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--request-interval", type=float, default=0.25)
    parser.add_argument("--redact-output", action="store_true")
    return parser


def _enforce_public_policy(args: argparse.Namespace) -> None:
    if os.environ.get(PUBLIC_RUNNER_ENV) != "1":
        return
    if args.notify != "slack" or not args.redact_output:
        raise ValueError(
            "Public runner policy requires private Slack structural notifications and redacted output"
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _enforce_public_policy(args)
        companies = load_companies(args.companies)
        notifier = (
            SlackNotifier(os.environ.get("SLACK_WEBHOOK_URL", ""))
            if args.notify == "slack"
            else None
        )
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        service = StructuralMonitorService(
            client=StructuralBrregClient(
                timeout=args.timeout,
                max_attempts=args.max_attempts,
                request_interval=args.request_interval,
            ),
            repository=StructuralRepository(args.metadata_dir),
            notifier=notifier,
            redact_identifiers=args.redact_output,
        )
        summary = service.run(companies)
        visible = {"status": summary.get("status", "unknown")} if args.redact_output else summary
        print(json.dumps(visible, ensure_ascii=False, sort_keys=True))
        return 0 if summary.get("status") == "success" else 1
    except (BrregError, CompanyListError, NotificationError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
