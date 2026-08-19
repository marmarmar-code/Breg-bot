from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, Iterator

from .companies import Company
from .store import Store


class MetadataRepository:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def write_event(
        self,
        *,
        orgnr: str,
        report_id: int,
        latest_raw: bytes,
        detail_raw: bytes,
        summary: dict[str, Any],
    ) -> None:
        directory = self.root / "events" / orgnr / str(report_id)
        self._atomic_write(directory / "latest.json", latest_raw)
        self._atomic_write(directory / "detail.json", detail_raw)
        self.update_event_summary(orgnr, report_id, summary)

    def update_event_summary(self, orgnr: str, report_id: int, summary: dict[str, Any]) -> None:
        directory = self.root / "events" / orgnr / str(report_id)
        payload = json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        self._atomic_write(directory / "summary.json", payload)

    def has_event(self, orgnr: str, report_id: int) -> bool:
        return (self.root / "events" / orgnr / str(report_id) / "summary.json").is_file()

    def read_event_summary(self, orgnr: str, report_id: int) -> dict[str, Any]:
        path = self.root / "events" / orgnr / str(report_id) / "summary.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def write_run(self, summary: dict[str, Any]) -> Path:
        date = str(summary["started_at"])[:10]
        year = date[:4]
        run_id = str(summary["run_id"]).replace("/", "-")
        payload = json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        path = self.root / "runs" / year / f"{date}-{run_id}.json"
        self._atomic_write(path, payload)
        self._atomic_write(self.root / "runs" / "current.json", payload)
        return path

    def write_check_status(self, status: dict[str, Any]) -> Path:
        orgnr = str(status["orgnr"])
        payload = json.dumps(
            status, ensure_ascii=False, sort_keys=True, indent=2
        ).encode("utf-8") + b"\n"
        path = self.root / "checks" / f"{orgnr}.json"
        self._atomic_write(path, payload)
        return path

    def read_check_status(self, orgnr: str) -> dict[str, Any] | None:
        path = self.root / "checks" / f"{orgnr}.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def has_company_event(self, orgnr: str) -> bool:
        return any((self.root / "events" / orgnr).glob("*/summary.json"))

    def iter_event_summaries(self) -> Iterator[dict[str, Any]]:
        events = self.root / "events"
        if not events.exists():
            return
        for path in sorted(events.glob("*/*/summary.json")):
            yield json.loads(path.read_text(encoding="utf-8"))

    def iter_run_summaries(self) -> Iterator[dict[str, Any]]:
        runs = self.root / "runs"
        if not runs.exists():
            return
        for path in sorted(runs.glob("*/*.json")):
            yield json.loads(path.read_text(encoding="utf-8"))

    def iter_check_statuses(self) -> Iterator[dict[str, Any]]:
        checks = self.root / "checks"
        if not checks.exists():
            return
        for path in sorted(checks.glob("*.json")):
            yield json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)


def rebuild_database(
    db_path: str | Path,
    companies: Iterable[Company],
    repository: MetadataRepository,
) -> None:
    db_path = Path(db_path)
    companies = list(companies)
    known_orgnrs = {company.orgnr for company in companies}
    db_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = db_path.with_name(f".{db_path.name}.rebuild")
    if temporary_path.exists():
        temporary_path.unlink()
    store = Store(temporary_path)
    store.initialize()
    try:
        store.sync_companies(companies)
        for summary in repository.iter_event_summaries():
            orgnr = str(summary["orgnr"])
            if orgnr not in known_orgnrs:
                store.sync_companies(
                    [Company(orgnr, str(summary.get("company_name") or orgnr), False)]
                )
                known_orgnrs.add(orgnr)
            filing_id, _ = store.discover_filing(
                orgnr=orgnr,
                report_id=int(summary["report_id"]),
                journal_number=summary.get("journal_number"),
                report_type=summary.get("report_type"),
                period_from=summary.get("period_from"),
                period_to=summary.get("period_to"),
                discovered_at=str(summary["discovered_at"]),
            )
            document = summary.get("document") or {}
            if document.get("status") == "archived":
                store.mark_document(
                    filing_id=filing_id,
                    source_url=str(document["source_url"]),
                    archive_kind=str(document["archive_kind"]),
                    archive_reference=str(document["archive_reference"]),
                    sha256=str(document["sha256"]),
                    size_bytes=int(document["size_bytes"]),
                    content_type=str(document["content_type"]),
                    archived_at=str(document["archived_at"]),
                )
            elif document.get("status") == "deferred":
                store.mark_document_deferred(
                    filing_id, str(document.get("error", "baseline"))
                )
            else:
                store.mark_document_pending(filing_id, str(document.get("error", "not_archived")))
            for notification in summary.get("notifications", []):
                store.record_notification(
                    filing_id,
                    str(notification["channel"]),
                    str(notification["kind"]),
                    str(notification["remote_reference"]),
                )
        for status in repository.iter_check_statuses():
            orgnr = str(status["orgnr"])
            if orgnr not in known_orgnrs:
                continue
            store.import_check_status(status)
        for run in repository.iter_run_summaries():
            store.import_run(run)
    finally:
        store.close()
    os.replace(temporary_path, db_path)
