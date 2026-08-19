from __future__ import annotations

import csv
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .brreg import RegisteredEntity


class CompanyListError(ValueError):
    """Raised when the version-controlled company list is invalid."""


@dataclass(frozen=True, slots=True)
class Company:
    orgnr: str
    name: str
    active: bool


def valid_orgnr(value: str) -> bool:
    if len(value) != 9 or not value.isascii() or not value.isdigit():
        return False
    weighted = sum(int(digit) * weight for digit, weight in zip(value[:8], (3, 2, 7, 6, 5, 4, 3, 2)))
    remainder = weighted % 11
    check_digit = 0 if remainder == 0 else 11 - remainder
    return check_digit != 10 and check_digit == int(value[-1])


def load_companies(path: str | Path) -> list[Company]:
    path = Path(path)
    try:
        handle = path.open("r", encoding="utf-8", newline="")
    except OSError as exc:
        raise CompanyListError(f"Cannot read company list: {path}") from exc

    with handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["orgnr", "name", "active"]:
            raise CompanyListError("Company list header must be: orgnr,name,active")
        companies: list[Company] = []
        seen: set[str] = set()
        for line_number, row in enumerate(reader, start=2):
            orgnr = (row.get("orgnr") or "").strip()
            name = (row.get("name") or "").strip()
            active_text = (row.get("active") or "").strip().lower()
            if not valid_orgnr(orgnr):
                raise CompanyListError(f"Invalid organisation number on line {line_number}: {orgnr}")
            if orgnr in seen:
                raise CompanyListError(f"Duplicate organisation number on line {line_number}: {orgnr}")
            if not name:
                raise CompanyListError(f"Missing company name on line {line_number}")
            if active_text not in {"true", "false"}:
                raise CompanyListError(f"Active must be true or false on line {line_number}")
            seen.add(orgnr)
            companies.append(Company(orgnr=orgnr, name=name, active=active_text == "true"))

    if not companies:
        raise CompanyListError("Company list must contain at least one company")
    return companies


def reconcile_company(company: Company, entity: RegisteredEntity) -> Company:
    if company.orgnr != entity.orgnr:
        raise CompanyListError("BRREG entity does not match the organisation number")
    if entity.status == "active":
        if not entity.name:
            raise CompanyListError("Active BRREG entity is missing its registered name")
        return Company(company.orgnr, entity.name, company.active)
    if entity.status in {"deleted", "unknown", "removed"}:
        return Company(company.orgnr, entity.name or company.name, False)
    raise CompanyListError(f"Unknown BRREG entity status: {entity.status}")


def write_companies(path: str | Path, companies: list[Company]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", dir=path.parent, delete=False
    ) as temporary:
        writer = csv.writer(temporary, lineterminator="\n")
        writer.writerow(["orgnr", "name", "active"])
        for company in companies:
            writer.writerow(
                [company.orgnr, company.name, "true" if company.active else "false"]
            )
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)
