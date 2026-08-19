from __future__ import annotations

import json
import logging
import os
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .brreg import BrregClient, InvalidResponse
from .companies import Company


IMPORTANT_ROLES = {
    "DAGL": "Daglig leder",
    "LEDE": "Styreleder",
    "NEST": "Nestleder",
    "MEDL": "Styremedlem",
}
ROLE_ORDER = tuple(IMPORTANT_ROLES)


class RegistryRepository:
    """Private, Git-backed state for entity and role monitoring."""

    def __init__(self, metadata_dir: str | Path) -> None:
        self.root = Path(metadata_dir) / "registry"
        self.snapshots = self.root / "snapshots"
        self.state_path = self.root / "state.json"

    def read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Registry state must be a JSON object")
        return data

    def write_state(self, state: dict[str, Any]) -> None:
        self._write_json(self.state_path, state)

    def read_snapshot(self, orgnr: str) -> dict[str, Any] | None:
        path = self.snapshots / f"{orgnr}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Registry snapshot must be a JSON object")
        return data

    def write_snapshot(self, orgnr: str, snapshot: dict[str, Any]) -> None:
        self._write_json(self.snapshots / f"{orgnr}.json", snapshot)

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


class RegistryMonitorService:
    def __init__(
        self,
        *,
        client: BrregClient,
        repository: RegistryRepository,
        notifier: Any | None = None,
        clock: Callable[[], str] | None = None,
        logger: logging.Logger | None = None,
        redact_identifiers: bool = False,
    ) -> None:
        self.client = client
        self.repository = repository
        self.notifier = notifier
        self.clock = clock or _utc_now
        self.logger = logger or logging.getLogger("breg_watch.registry")
        self.redact_identifiers = redact_identifiers

    def run(self, companies: Iterable[Company]) -> dict[str, Any]:
        active = sorted((company for company in companies if company.active), key=lambda item: item.orgnr)
        now = self.clock()
        state = self.repository.read_state()
        if not state.get("baseline_started_at"):
            state["baseline_started_at"] = now
            state["baseline_orgnrs"] = []
            self.repository.write_state(state)

        if not state.get("baseline_complete"):
            return self._baseline(active, state, now)

        # Newly added companies get a silent first snapshot, matching annual-account baseline semantics.
        newly_baselined = 0
        for company in active:
            if self.repository.read_snapshot(company.orgnr) is None:
                self.repository.write_snapshot(company.orgnr, self._fetch_snapshot(company.orgnr))
                newly_baselined += 1

        orgnrs = [company.orgnr for company in active]
        entity_events = self.client.entity_updates(
            orgnrs,
            after_id=state.get("entity_after_id") if isinstance(state.get("entity_after_id"), int) else None,
            after_time=None
            if isinstance(state.get("entity_after_id"), int)
            else str(state.get("entity_after_time") or state["baseline_started_at"]),
        )
        role_events = self.client.role_updates(
            orgnrs,
            after_id=state.get("role_after_id") if isinstance(state.get("role_after_id"), int) else None,
            after_time=None
            if isinstance(state.get("role_after_id"), int)
            else str(state.get("role_after_time") or state["baseline_started_at"]),
        )

        entity_orgnrs = {
            str(event.get("organisasjonsnummer"))
            for event in entity_events
            if isinstance(event.get("organisasjonsnummer"), str)
        }
        role_orgnrs = {
            str(event.get("data", {}).get("organisasjonsnummer"))
            for event in role_events
            if isinstance(event.get("data"), dict)
            and isinstance(event.get("data", {}).get("organisasjonsnummer"), str)
        }
        affected = sorted((entity_orgnrs | role_orgnrs) & set(orgnrs))
        company_by_orgnr = {company.orgnr: company for company in active}

        staged: dict[str, dict[str, Any]] = {}
        alerts: list[dict[str, Any]] = []
        for orgnr in affected:
            previous = self.repository.read_snapshot(orgnr)
            if previous is None:
                staged[orgnr] = self._fetch_snapshot(orgnr)
                continue
            current = deepcopy(previous)
            changes: list[str] = []
            if orgnr in entity_orgnrs:
                entity = canonical_entity(self.client.entity_details(orgnr).data)
                changes.extend(diff_entity(previous.get("entity", {}), entity))
                current["entity"] = entity
            if orgnr in role_orgnrs:
                roles = canonical_roles(self.client.roles(orgnr).data)
                changes.extend(diff_roles(previous.get("roles", {}), roles))
                current["roles"] = roles
            current["captured_at"] = now
            staged[orgnr] = current
            if changes:
                alerts.append(
                    {
                        "orgnr": orgnr,
                        "company_name": _company_name(current, company_by_orgnr[orgnr].name),
                        "changes": changes,
                    }
                )

        # Snapshots/cursors advance only after private notification succeeds, so failures retry next run.
        if alerts and self.notifier is not None:
            self.notifier.send_text(format_slack_alert(alerts))

        for orgnr, snapshot in staged.items():
            self.repository.write_snapshot(orgnr, snapshot)

        if entity_events:
            state["entity_after_id"] = max(int(event["oppdateringsid"]) for event in entity_events)
            state.pop("entity_after_time", None)
        if role_events:
            state["role_after_id"] = max(int(event["id"]) for event in role_events)
            state.pop("role_after_time", None)
        state["last_checked_at"] = now
        self.repository.write_state(state)

        if self.redact_identifiers:
            self.logger.info("Registry monitor completed")
        else:
            self.logger.info("Registry monitor completed; affected=%s alerts=%s", affected, len(alerts))
        return {
            "status": "success",
            "baseline": False,
            "newly_baselined": newly_baselined,
            "entity_updates": len(entity_events),
            "role_updates": len(role_events),
            "affected": len(affected),
            "alerts": len(alerts),
        }

    def _baseline(
        self,
        active: list[Company],
        state: dict[str, Any],
        now: str,
    ) -> dict[str, Any]:
        completed = set(str(value) for value in state.get("baseline_orgnrs", []))
        for company in active:
            if company.orgnr in completed and self.repository.read_snapshot(company.orgnr) is not None:
                continue
            self.repository.write_snapshot(company.orgnr, self._fetch_snapshot(company.orgnr))
            completed.add(company.orgnr)
            state["baseline_orgnrs"] = sorted(completed)
            self.repository.write_state(state)

        active_orgnrs = {company.orgnr for company in active}
        if active_orgnrs.issubset(completed):
            state["baseline_complete"] = True
            state["baseline_completed_at"] = now
            state["entity_after_time"] = state["baseline_started_at"]
            state["role_after_time"] = state["baseline_started_at"]
            state["last_checked_at"] = now
            self.repository.write_state(state)

        if self.redact_identifiers:
            self.logger.info("Registry baseline completed")
        else:
            self.logger.info("Registry baseline completed for %s monitored companies", len(active))
        return {
            "status": "success",
            "baseline": True,
            "baselined": len(active_orgnrs & completed),
            "alerts": 0,
        }

    def _fetch_snapshot(self, orgnr: str) -> dict[str, Any]:
        return {
            "captured_at": self.clock(),
            "entity": canonical_entity(self.client.entity_details(orgnr).data),
            "roles": canonical_roles(self.client.roles(orgnr).data),
        }


def canonical_entity(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise InvalidResponse("Entity payload must be a JSON object")
    return {
        "name": _clean_string(payload.get("navn")),
        "organisation_form": _coded_value(payload.get("organisasjonsform")),
        "industry": _coded_value(payload.get("naeringskode1")),
        "bankrupt": bool(payload.get("konkurs")),
        "liquidating": bool(payload.get("underAvvikling")),
        "forced_liquidation": bool(payload.get("underTvangsavviklingEllerTvangsopplosning")),
        "deleted": bool(payload.get("slettedato") or payload.get("erSlettet") is True),
        "removed": payload.get("_registryStatus") == "removed",
    }


def canonical_roles(payload: Any) -> dict[str, list[str]]:
    if not isinstance(payload, dict):
        raise InvalidResponse("Roles payload must be a JSON object")
    result: dict[str, set[str]] = {code: set() for code in ROLE_ORDER}
    groups = payload.get("rollegrupper", [])
    if not isinstance(groups, list):
        raise InvalidResponse("Roles payload has invalid rollegrupper")
    for group in groups:
        roles = group.get("roller", []) if isinstance(group, dict) else []
        if not isinstance(roles, list):
            raise InvalidResponse("Role group has invalid roller")
        for role in roles:
            if not isinstance(role, dict) or role.get("avregistrert") is True:
                continue
            role_type = role.get("type")
            code = role_type.get("kode") if isinstance(role_type, dict) else None
            if code not in result:
                continue
            holder = _role_holder(role)
            if holder:
                result[code].add(holder)
    return {code: sorted(result[code], key=str.casefold) for code in ROLE_ORDER}


def diff_entity(previous: Any, current: Any) -> list[str]:
    old = previous if isinstance(previous, dict) else {}
    new = current if isinstance(current, dict) else {}
    changes: list[str] = []
    old_name = _clean_string(old.get("name"))
    new_name = _clean_string(new.get("name"))
    if old_name and new_name and old_name != new_name:
        changes.append(f"Navn: {old_name} → {new_name}")

    for key, label in (
        ("bankrupt", "Konkurs"),
        ("liquidating", "Avvikling"),
        ("forced_liquidation", "Tvangsavvikling/tvangsoppløsning"),
        ("deleted", "Slettet fra Enhetsregisteret"),
        ("removed", "Fjernet fra BRREG Åpne Data"),
    ):
        before = bool(old.get(key))
        after = bool(new.get(key))
        if before != after:
            changes.append(f"{label}: {'ja' if after else 'nei'}")

    old_form = _coded_value(old.get("organisation_form"))
    new_form = _coded_value(new.get("organisation_form"))
    if old_form.get("code") and new_form.get("code") and old_form != new_form:
        changes.append(f"Organisasjonsform: {_coded_display(old_form)} → {_coded_display(new_form)}")

    old_industry = _coded_value(old.get("industry"))
    new_industry = _coded_value(new.get("industry"))
    if old_industry.get("code") and new_industry.get("code") and old_industry != new_industry:
        changes.append(f"Næringskode: {_coded_display(old_industry)} → {_coded_display(new_industry)}")
    return changes


def diff_roles(previous: Any, current: Any) -> list[str]:
    old = previous if isinstance(previous, dict) else {}
    new = current if isinstance(current, dict) else {}
    changes: list[str] = []
    for code in ROLE_ORDER:
        old_people = set(str(value) for value in old.get(code, []) if value)
        new_people = set(str(value) for value in new.get(code, []) if value)
        removed = sorted(old_people - new_people, key=str.casefold)
        added = sorted(new_people - old_people, key=str.casefold)
        label = IMPORTANT_ROLES[code]
        if len(removed) == 1 and len(added) == 1:
            changes.append(f"{label}: {removed[0]} → {added[0]}")
        else:
            if removed:
                changes.append(f"{label} ut: {', '.join(removed)}")
            if added:
                changes.append(f"{label} inn: {', '.join(added)}")
    return changes


def format_slack_alert(alerts: list[dict[str, Any]]) -> str:
    lines = ["*Ny BRREG-endring oppdaget*", ""]
    for alert in alerts:
        lines.append(f"• *{alert['company_name']}* ({alert['orgnr']})")
        for change in alert["changes"]:
            lines.append(f"  – {change}")
        lines.append("")
    lines.append("Kilde: Brønnøysundregistrene")
    return "\n".join(lines).rstrip()


def _role_holder(role: dict[str, Any]) -> str | None:
    person = role.get("person")
    if isinstance(person, dict):
        name = person.get("navn")
        if isinstance(name, dict):
            parts = [
                _clean_string(name.get("fornavn")),
                _clean_string(name.get("mellomnavn")),
                _clean_string(name.get("etternavn")),
            ]
            value = " ".join(part for part in parts if part)
            if value:
                return value
    entity = role.get("enhet")
    if isinstance(entity, dict):
        name = entity.get("navn")
        if isinstance(name, list):
            value = " ".join(str(part).strip() for part in name if str(part).strip())
        else:
            value = _clean_string(name) or ""
        orgnr = _clean_string(entity.get("organisasjonsnummer"))
        if value and orgnr:
            return f"{value} ({orgnr})"
        return value or orgnr
    return None


def _coded_value(value: Any) -> dict[str, str | None]:
    if not isinstance(value, dict):
        return {"code": None, "description": None}
    return {
        "code": _clean_string(value.get("kode") or value.get("code")),
        "description": _clean_string(value.get("beskrivelse") or value.get("description")),
    }


def _coded_display(value: dict[str, str | None]) -> str:
    code = value.get("code") or "ukjent"
    description = value.get("description")
    return f"{code} ({description})" if description else code


def _company_name(snapshot: dict[str, Any], fallback: str) -> str:
    entity = snapshot.get("entity")
    if isinstance(entity, dict):
        name = _clean_string(entity.get("name"))
        if name:
            return name
    return fallback


def _clean_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
