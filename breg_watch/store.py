from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .companies import Company


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS companies (
    orgnr TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    active INTEGER NOT NULL CHECK (active IN (0, 1))
);
CREATE TABLE IF NOT EXISTS filings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    orgnr TEXT NOT NULL REFERENCES companies(orgnr),
    report_id INTEGER NOT NULL,
    journal_number TEXT,
    report_type TEXT,
    period_from TEXT,
    period_to TEXT,
    discovered_at TEXT NOT NULL,
    document_status TEXT NOT NULL DEFAULT 'pending',
    document_error TEXT,
    UNIQUE (orgnr, report_id)
);
CREATE TABLE IF NOT EXISTS documents (
    filing_id INTEGER PRIMARY KEY REFERENCES filings(id) ON DELETE CASCADE,
    source_url TEXT NOT NULL,
    archive_kind TEXT NOT NULL,
    archive_reference TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    content_type TEXT NOT NULL,
    archived_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filing_id INTEGER NOT NULL REFERENCES filings(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    kind TEXT NOT NULL,
    remote_reference TEXT NOT NULL,
    UNIQUE (filing_id, channel, kind)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    trigger TEXT NOT NULL,
    requested_orgnr TEXT,
    checked INTEGER NOT NULL DEFAULT 0,
    new_filings INTEGER NOT NULL DEFAULT 0,
    errors_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS check_status (
    orgnr TEXT PRIMARY KEY REFERENCES companies(orgnr) ON DELETE CASCADE,
    baseline_completed_at TEXT,
    last_report_id INTEGER,
    last_checked_at TEXT,
    last_success_at TEXT,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    orgnr TEXT NOT NULL,
    operation TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.connection: sqlite3.Connection | None = None

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.connection is None:
            self.connection = sqlite3.connect(self.path)
            self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        columns = {
            str(row["name"])
            for row in self.connection.execute(
                "PRAGMA table_info(check_status)"
            ).fetchall()
        }
        if "baseline_completed_at" not in columns:
            self.connection.execute(
                "ALTER TABLE check_status ADD COLUMN baseline_completed_at TEXT"
            )
        self.connection.commit()

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def sync_companies(self, companies: Iterable[Company]) -> None:
        connection = self._connection()
        with connection:
            for company in companies:
                connection.execute(
                    """
                    INSERT INTO companies(orgnr, name, active) VALUES (?, ?, ?)
                    ON CONFLICT(orgnr) DO UPDATE SET name=excluded.name, active=excluded.active
                    """,
                    (company.orgnr, company.name, int(company.active)),
                )

    def discover_filing(
        self,
        *,
        orgnr: str,
        report_id: int,
        journal_number: str | None,
        report_type: str | None,
        period_from: str | None,
        period_to: str | None,
        discovered_at: str,
    ) -> tuple[int, bool]:
        connection = self._connection()
        with connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO filings(
                    orgnr, report_id, journal_number, report_type,
                    period_from, period_to, discovered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    orgnr,
                    report_id,
                    journal_number,
                    report_type,
                    period_from,
                    period_to,
                    discovered_at,
                ),
            )
            created = cursor.rowcount == 1
            row = connection.execute(
                "SELECT id FROM filings WHERE orgnr=? AND report_id=?",
                (orgnr, report_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("Filing insert could not be read back")
        return int(row["id"]), created

    def get_filing(self, orgnr: str, report_id: int) -> dict[str, Any] | None:
        row = self._connection().execute(
            """
            SELECT f.*, d.source_url, d.archive_kind, d.archive_reference,
                   d.sha256, d.size_bytes, d.content_type, d.archived_at
              FROM filings f
              LEFT JOIN documents d ON d.filing_id=f.id
             WHERE f.orgnr=? AND f.report_id=?
            """,
            (orgnr, report_id),
        ).fetchone()
        return dict(row) if row else None

    def list_filings(self) -> list[dict[str, Any]]:
        rows = self._connection().execute(
            """
            SELECT f.*, c.name AS company_name, d.source_url, d.archive_kind,
                   d.archive_reference, d.sha256, d.size_bytes,
                   d.content_type, d.archived_at
              FROM filings f
              JOIN companies c ON c.orgnr=f.orgnr
              LEFT JOIN documents d ON d.filing_id=f.id
             ORDER BY f.discovered_at DESC, f.orgnr
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_document_pending(self, filing_id: int, reason: str) -> None:
        connection = self._connection()
        with connection:
            connection.execute(
                "UPDATE filings SET document_status='pending', document_error=? WHERE id=?",
                (reason[:500], filing_id),
            )

    def mark_document_deferred(self, filing_id: int, reason: str) -> None:
        connection = self._connection()
        with connection:
            connection.execute(
                "UPDATE filings SET document_status='deferred', document_error=? WHERE id=?",
                (reason[:500], filing_id),
            )

    def mark_document(
        self,
        *,
        filing_id: int,
        source_url: str,
        archive_kind: str,
        archive_reference: str,
        sha256: str,
        size_bytes: int,
        content_type: str,
        archived_at: str,
    ) -> None:
        connection = self._connection()
        with connection:
            connection.execute(
                """
                INSERT INTO documents(
                    filing_id, source_url, archive_kind, archive_reference,
                    sha256, size_bytes, content_type, archived_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(filing_id) DO UPDATE SET
                    source_url=excluded.source_url,
                    archive_kind=excluded.archive_kind,
                    archive_reference=excluded.archive_reference,
                    sha256=excluded.sha256,
                    size_bytes=excluded.size_bytes,
                    content_type=excluded.content_type,
                    archived_at=excluded.archived_at
                """,
                (
                    filing_id,
                    source_url,
                    archive_kind,
                    archive_reference,
                    sha256,
                    size_bytes,
                    content_type,
                    archived_at,
                ),
            )
            connection.execute(
                "UPDATE filings SET document_status='archived', document_error=NULL WHERE id=?",
                (filing_id,),
            )

    def record_notification(
        self,
        filing_id: int,
        channel: str,
        kind: str,
        remote_reference: str,
    ) -> bool:
        connection = self._connection()
        with connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO notifications(filing_id, channel, kind, remote_reference)
                VALUES (?, ?, ?, ?)
                """,
                (filing_id, channel, kind, remote_reference),
            )
        return cursor.rowcount == 1

    def notification_exists(
        self,
        report_id: int,
        orgnr: str,
        channel: str,
        kind: str,
    ) -> bool:
        row = self._connection().execute(
            """
            SELECT 1
              FROM notifications n
              JOIN filings f ON f.id=n.filing_id
             WHERE f.report_id=? AND f.orgnr=? AND n.channel=? AND n.kind=?
            """,
            (report_id, orgnr, channel, kind),
        ).fetchone()
        return row is not None

    def import_run(self, summary: dict[str, Any]) -> None:
        connection = self._connection()
        import json

        with connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO runs(
                    run_id, started_at, finished_at, status, trigger,
                    requested_orgnr, checked, new_filings, errors_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    summary["run_id"],
                    summary["started_at"],
                    summary.get("finished_at"),
                    summary["status"],
                    summary["trigger"],
                    summary.get("requested_orgnr"),
                    int(summary.get("checked", 0)),
                    int(summary.get("new_filings", 0)),
                    json.dumps(summary.get("errors", []), ensure_ascii=False),
                ),
            )

    def last_successful_run(self) -> dict[str, Any] | None:
        row = self._connection().execute(
            """
            SELECT * FROM runs
             WHERE status='success' AND finished_at IS NOT NULL
             ORDER BY finished_at DESC LIMIT 1
            """
        ).fetchone()
        return dict(row) if row else None

    def latest_run(self) -> dict[str, Any] | None:
        row = self._connection().execute(
            "SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def update_check_status(
        self,
        *,
        orgnr: str,
        checked_at: str,
        report_id: int | None,
        error: str | None,
        baseline_completed_at: str | None = None,
    ) -> None:
        connection = self._connection()
        success_at = checked_at if error is None else None
        with connection:
            connection.execute(
                """
                INSERT INTO check_status(
                    orgnr, baseline_completed_at, last_report_id,
                    last_checked_at, last_success_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(orgnr) DO UPDATE SET
                    baseline_completed_at=COALESCE(
                        excluded.baseline_completed_at,
                        check_status.baseline_completed_at
                    ),
                    last_report_id=COALESCE(
                        excluded.last_report_id,
                        check_status.last_report_id
                    ),
                    last_checked_at=excluded.last_checked_at,
                    last_success_at=COALESCE(
                        excluded.last_success_at,
                        check_status.last_success_at
                    ),
                    last_error=excluded.last_error
                """,
                (
                    orgnr,
                    baseline_completed_at,
                    report_id,
                    checked_at,
                    success_at,
                    error,
                ),
            )

    def get_check_status(self, orgnr: str) -> dict[str, Any] | None:
        row = self._connection().execute(
            "SELECT * FROM check_status WHERE orgnr=?", (orgnr,)
        ).fetchone()
        return dict(row) if row else None

    def import_check_status(self, status: dict[str, Any]) -> None:
        connection = self._connection()
        with connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO check_status(
                    orgnr, baseline_completed_at, last_report_id,
                    last_checked_at, last_success_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(status["orgnr"]),
                    status.get("baseline_completed_at"),
                    status.get("last_report_id"),
                    status.get("last_checked_at"),
                    status.get("last_success_at"),
                    status.get("last_error"),
                ),
            )

    def record_attempt(
        self,
        *,
        run_id: str,
        orgnr: str,
        operation: str,
        status: str,
        detail: str | None,
        created_at: str,
    ) -> None:
        connection = self._connection()
        with connection:
            connection.execute(
                """
                INSERT INTO attempts(run_id, orgnr, operation, status, detail, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, orgnr, operation, status, detail[:500] if detail else None, created_at),
            )

    def list_attempts(self) -> list[dict[str, Any]]:
        rows = self._connection().execute("SELECT * FROM attempts ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def _connection(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("Store.initialize() must be called first")
        return self.connection
