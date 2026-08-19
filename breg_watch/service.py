from __future__ import annotations

import logging
import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .archive import ArchiveError
from .brreg import BrregClient, BrregError, InvalidResponse
from .companies import Company
from .html_report import render_overview
from .metadata import MetadataRepository
from .notifier import NotificationError
from .store import Store


class MonitorService:
    def __init__(
        self,
        *,
        store: Store,
        metadata: MetadataRepository,
        client: BrregClient,
        archive: Any,
        notifier: Any | None,
        site_directory: str | Path,
        clock: Callable[[], str] | None = None,
        id_factory: Callable[[], str] | None = None,
        logger: logging.Logger | None = None,
        redact_identifiers: bool = False,
    ) -> None:
        self.store = store
        self.metadata = metadata
        self.client = client
        self.archive = archive
        self.notifier = notifier
        self.site_directory = Path(site_directory)
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self.id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self.logger = logger or logging.getLogger("breg_watch")
        self.redact_identifiers = redact_identifiers

    def run(
        self,
        companies: Iterable[Company],
        *,
        trigger: str,
        requested_orgnr: str | None = None,
    ) -> dict[str, Any]:
        companies = list(companies)
        self.store.sync_companies(companies)
        selected = self._select_companies(companies, requested_orgnr)
        run_id = self.id_factory()
        started_at = self.clock()
        errors: list[dict[str, str]] = []
        new_filings: list[dict[str, Any]] = []
        notification_candidates: list[dict[str, Any]] = []
        checked = 0

        for company in selected:
            baseline = not self._is_baselined(company.orgnr)
            try:
                created = self._process_company(
                    company,
                    run_id,
                    new_filings,
                    notification_candidates,
                    errors,
                    baseline=baseline,
                )
                checked += 1
                if not self.redact_identifiers:
                    self.logger.info(
                        "Checked organisation %s; new_filing=%s",
                        company.orgnr,
                        created,
                    )
            except BrregError as exc:
                kind = type(exc).__name__
                errors.append({"orgnr": company.orgnr, "kind": kind})
                now = self.clock()
                self._record_check_status(
                    orgnr=company.orgnr,
                    checked_at=now,
                    report_id=None,
                    error=kind,
                )
                self.store.record_attempt(
                    run_id=run_id,
                    orgnr=company.orgnr,
                    operation="latest_or_detail",
                    status="error",
                    detail=kind,
                    created_at=now,
                )
                self.logger.warning(
                    "BRREG check failed for %s (%s)",
                    self._log_identifier(company.orgnr),
                    kind,
                )

        if notification_candidates and self.notifier is not None:
            self._notify_candidates(notification_candidates, errors)

        summary = {
            "run_id": run_id,
            "started_at": started_at,
            "finished_at": self.clock(),
            "status": "partial" if errors else "success",
            "trigger": trigger,
            "requested_orgnr": requested_orgnr,
            "checked": checked,
            "new_filings": len(new_filings),
            "errors": errors,
        }
        self.store.import_run(summary)
        self.metadata.write_run(summary)
        render_overview(self.store, self.site_directory)
        return summary

    def _log_identifier(self, orgnr: str) -> str:
        return "monitored company" if self.redact_identifiers else orgnr

    def _notify_candidates(
        self,
        candidates: list[dict[str, Any]],
        errors: list[dict[str, str]],
    ) -> None:
        grouped: dict[str, list[dict[str, Any]]] = {}
        unkeyed: list[dict[str, Any]] = []
        for filing in candidates:
            event_summary = self.metadata.read_event_summary(
                str(filing["orgnr"]), int(filing["report_id"])
            )
            notification_key = event_summary.get("notification_key")
            if notification_key:
                grouped.setdefault(str(notification_key), []).append(filing)
            else:
                unkeyed.append(filing)

        if unkeyed:
            notification_key = self._notification_key(unkeyed)
            grouped[notification_key] = unkeyed
            for filing in unkeyed:
                event_summary = self.metadata.read_event_summary(
                    str(filing["orgnr"]), int(filing["report_id"])
                )
                event_summary["notification_pending"] = True
                event_summary["notification_key"] = notification_key
                self.metadata.update_event_summary(
                    str(filing["orgnr"]), int(filing["report_id"]), event_summary
                )

        channel = str(getattr(self.notifier, "channel", "notification"))
        for notification_key, filings in grouped.items():
            try:
                remote_reference = self.notifier.notify(notification_key, filings)
                for filing in filings:
                    self.store.record_notification(
                        int(filing["id"]), channel, "new_filing", remote_reference
                    )
                    event_summary = self.metadata.read_event_summary(
                        str(filing["orgnr"]), int(filing["report_id"])
                    )
                    notifications = event_summary.setdefault("notifications", [])
                    record = {
                        "channel": channel,
                        "kind": "new_filing",
                        "remote_reference": remote_reference,
                    }
                    if record not in notifications:
                        notifications.append(record)
                    event_summary["notification_pending"] = False
                    self.metadata.update_event_summary(
                        str(filing["orgnr"]), int(filing["report_id"]), event_summary
                    )
            except NotificationError as exc:
                for filing in filings:
                    event_summary = self.metadata.read_event_summary(
                        str(filing["orgnr"]), int(filing["report_id"])
                    )
                    event_summary["notification_pending"] = True
                    self.metadata.update_event_summary(
                        str(filing["orgnr"]), int(filing["report_id"]), event_summary
                    )
                    errors.append(
                        {"orgnr": str(filing["orgnr"]), "kind": type(exc).__name__}
                    )
                self.logger.warning(
                    "%s notification failed for batch %s (%s)",
                    channel,
                    notification_key,
                    type(exc).__name__,
                )

    def _process_company(
        self,
        company: Company,
        run_id: str,
        new_filings: list[dict[str, Any]],
        notification_candidates: list[dict[str, Any]],
        errors: list[dict[str, str]],
        *,
        baseline: bool,
    ) -> bool:
        latest_response = self.client.latest(company.orgnr)
        now = self.clock()
        if not latest_response.data:
            self._record_check_status(
                orgnr=company.orgnr,
                checked_at=now,
                report_id=None,
                error=None,
                baseline_completed_at=now,
            )
            self.store.record_attempt(
                run_id=run_id,
                orgnr=company.orgnr,
                operation="latest",
                status="success_no_report",
                detail=None,
                created_at=now,
            )
            return False

        latest = self._choose_latest(latest_response.data)
        report_id = int(latest["id"])
        filing = self.store.get_filing(company.orgnr, report_id)
        needs_metadata = not self.metadata.has_event(company.orgnr, report_id)
        created = filing is None

        if created or needs_metadata:
            detail_response = self.client.detail(company.orgnr, report_id)
            detail = detail_response.data
            period = detail.get("regnskapsperiode") or latest.get("regnskapsperiode") or {}
            filing_id, actually_created = self.store.discover_filing(
                orgnr=company.orgnr,
                report_id=report_id,
                journal_number=detail.get("journalnr") or latest.get("journalnr"),
                report_type=detail.get("regnskapstype") or latest.get("regnskapstype"),
                period_from=period.get("fraDato"),
                period_to=period.get("tilDato"),
                discovered_at=now,
            )
            created = actually_created
            document = (
                {"status": "deferred", "error": "baseline"}
                if baseline
                else {"status": "pending", "error": "not_attempted"}
            )
            summary = {
                "orgnr": company.orgnr,
                "company_name": company.name,
                "report_id": report_id,
                "journal_number": detail.get("journalnr") or latest.get("journalnr"),
                "report_type": detail.get("regnskapstype") or latest.get("regnskapstype"),
                "period_from": period.get("fraDato"),
                "period_to": period.get("tilDato"),
                "discovered_at": now,
                "document": document,
            }
            if baseline:
                summary["discovery_mode"] = "baseline"
                self.store.mark_document_deferred(filing_id, "baseline")
            if created and not baseline and self.notifier is not None:
                summary["notification_pending"] = True
                summary["notification_key"] = self._run_notification_key(run_id)
            self.metadata.write_event(
                orgnr=company.orgnr,
                report_id=report_id,
                latest_raw=latest_response.raw,
                detail_raw=detail_response.raw,
                summary=summary,
            )
            filing = self.store.get_filing(company.orgnr, report_id)
        else:
            filing_id = int(filing["id"])
            summary = self.metadata.read_event_summary(company.orgnr, report_id)

        if filing is None:
            raise RuntimeError("Discovered filing could not be read")
        filing_id = int(filing["id"])
        if filing["document_status"] == "pending":
            pending_error = self._archive_document(
                company=company,
                filing_id=filing_id,
                filing=filing,
                summary=summary,
            )
            if pending_error:
                errors.append({"orgnr": company.orgnr, "kind": "document_pending"})
                self.store.record_attempt(
                    run_id=run_id,
                    orgnr=company.orgnr,
                    operation="document",
                    status="pending",
                    detail=pending_error,
                    created_at=self.clock(),
                )

        current = self.store.get_filing(company.orgnr, report_id)
        if current is None:
            raise RuntimeError("Filing disappeared during processing")
        check_error = "document_pending" if current["document_status"] == "pending" else None
        self._record_check_status(
            orgnr=company.orgnr,
            checked_at=self.clock(),
            report_id=report_id,
            error=check_error,
            baseline_completed_at=self.clock(),
        )
        self.store.record_attempt(
            run_id=run_id,
            orgnr=company.orgnr,
            operation="latest",
            status="success",
            detail=None,
            created_at=self.clock(),
        )
        if created and not baseline:
            current["company_name"] = company.name
            new_filings.append(current)
        if (
            not baseline
            and self.notifier is not None
            and (created or summary.get("notification_pending") is True)
        ):
            if "company_name" not in current:
                current["company_name"] = company.name
            notification_candidates.append(current)
        return created and not baseline

    def _is_baselined(self, orgnr: str) -> bool:
        status = self.metadata.read_check_status(orgnr)
        return bool(
            status and status.get("baseline_completed_at")
        ) or self.metadata.has_company_event(orgnr)

    def _record_check_status(
        self,
        *,
        orgnr: str,
        checked_at: str,
        report_id: int | None,
        error: str | None,
        baseline_completed_at: str | None = None,
    ) -> None:
        self.store.update_check_status(
            orgnr=orgnr,
            checked_at=checked_at,
            report_id=report_id,
            error=error,
            baseline_completed_at=baseline_completed_at,
        )
        status = self.store.get_check_status(orgnr)
        if status is None:
            raise RuntimeError("Check status could not be read after update")
        self.metadata.write_check_status(status)

    def _archive_document(
        self,
        *,
        company: Company,
        filing_id: int,
        filing: dict[str, Any],
        summary: dict[str, Any],
    ) -> str | None:
        period_to = filing.get("period_to") or summary.get("period_to")
        if not isinstance(period_to, str) or len(period_to) < 4:
            error = "missing_period_year"
            self.store.mark_document_pending(filing_id, error)
            self._update_pending_summary(company.orgnr, int(filing["report_id"]), summary, error)
            return error
        year = period_to[:4]
        source_url = f"{self.client.base_url}/aarsregnskap/kopi/{company.orgnr}/{year}"
        try:
            years = self.client.available_years(company.orgnr).data
            if year not in years:
                raise BrregError("Annual-report year is not available")
            pdf = self.client.annual_report(company.orgnr, year)
            archived = self.archive.archive(
                orgnr=company.orgnr,
                report_id=int(filing["report_id"]),
                year=year,
                discovered_at=str(summary["discovered_at"]),
                content=pdf.body,
                content_type=pdf.content_type,
            )
        except (BrregError, ArchiveError) as exc:
            error = type(exc).__name__
            self.store.mark_document_pending(filing_id, error)
            self._update_pending_summary(company.orgnr, int(filing["report_id"]), summary, error)
            return error

        archived_at = self.clock()
        self.store.mark_document(
            filing_id=filing_id,
            source_url=source_url,
            archive_kind=archived.kind,
            archive_reference=archived.reference,
            sha256=archived.sha256,
            size_bytes=archived.size_bytes,
            content_type=archived.content_type,
            archived_at=archived_at,
        )
        summary["document"] = {
            "status": "archived",
            "source_url": source_url,
            "archive_kind": archived.kind,
            "archive_reference": archived.reference,
            "sha256": archived.sha256,
            "size_bytes": archived.size_bytes,
            "content_type": archived.content_type,
            "archived_at": archived_at,
        }
        self.metadata.update_event_summary(company.orgnr, int(filing["report_id"]), summary)
        return None

    def _update_pending_summary(
        self,
        orgnr: str,
        report_id: int,
        summary: dict[str, Any],
        error: str,
    ) -> None:
        summary["document"] = {"status": "pending", "error": error}
        self.metadata.update_event_summary(orgnr, report_id, summary)

    @staticmethod
    def _choose_latest(items: list[Any]) -> dict[str, Any]:
        if not all(isinstance(item, dict) and isinstance(item.get("id"), int) for item in items):
            raise InvalidResponse("Latest-report list contains an invalid item")
        return max(
            items,
            key=lambda item: (
                str((item.get("regnskapsperiode") or {}).get("tilDato") or ""),
                int(item["id"]),
            ),
        )

    @staticmethod
    def _select_companies(
        companies: list[Company], requested_orgnr: str | None
    ) -> list[Company]:
        if requested_orgnr:
            matches = [company for company in companies if company.orgnr == requested_orgnr]
            if not matches:
                raise ValueError("Requested organisation number is not in the company list")
            return matches
        return [company for company in companies if company.active]

    @staticmethod
    def _notification_key(filings: list[dict[str, Any]]) -> str:
        event_keys = sorted(f"{filing['orgnr']}:{filing['report_id']}" for filing in filings)
        digest = hashlib.sha256("|".join(event_keys).encode("utf-8")).hexdigest()[:20]
        return f"filings-{digest}"

    @staticmethod
    def _run_notification_key(run_id: str) -> str:
        digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:20]
        return f"run-{digest}"