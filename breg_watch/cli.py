from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Sequence

from .archive import GitHubReleaseArchive, LocalArchive
from .brreg import BrregClient, BrregError
from .companies import (
    Company,
    CompanyListError,
    load_companies,
    reconcile_company,
    write_companies,
)
from .html_report import render_overview
from .metadata import MetadataRepository, rebuild_database
from .notifier import GitHubIssueNotifier, NotificationError, SlackNotifier
from .registry_monitor import RegistryMonitorService, RegistryRepository
from .service import MonitorService
from .store import Store


PUBLIC_RUNNER_ENV = "BREG_PUBLIC_RUNNER"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="breg-watch")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="Validate companies.csv")
    validate.add_argument("--companies", default="companies.csv")
    validate.add_argument("--online", action="store_true")
    validate.add_argument("--update", action="store_true")
    validate.add_argument("--timeout", type=float, default=20.0)
    validate.add_argument("--max-attempts", type=int, default=3)
    validate.add_argument("--request-interval", type=float, default=0.25)

    rebuild = subparsers.add_parser("rebuild", help="Rebuild SQLite from Git metadata")
    rebuild.add_argument("--companies", default="companies.csv")
    rebuild.add_argument("--metadata-dir", default="data")
    rebuild.add_argument("--db", default="state/monitor.sqlite")

    render = subparsers.add_parser("render", help="Regenerate the static overview")
    render.add_argument("--db", default="state/monitor.sqlite")
    render.add_argument("--site-dir", default="site")

    budget = subparsers.add_parser("budget-check", help="Stop before Git storage grows too large")
    budget.add_argument("--max-bytes", type=int, default=800_000_000)
    budget.add_argument("paths", nargs="*", default=["data", "site"])

    subparsers.add_parser("test-slack", help="Send a Slack smoke-test notification")

    run = subparsers.add_parser("run", help="Check active companies for annual accounts")
    run.add_argument("--companies", default="companies.csv")
    run.add_argument("--metadata-dir", default="data")
    run.add_argument("--db", default="state/monitor.sqlite")
    run.add_argument("--documents-dir", default="documents")
    run.add_argument("--site-dir", default="site")
    run.add_argument("--orgnr")
    run.add_argument("--trigger", default="manual")
    run.add_argument("--archive", choices=("local", "github"), default="local")
    run.add_argument("--notify", choices=("none", "github", "slack"), default="none")
    run.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    run.add_argument("--timeout", type=float, default=20.0)
    run.add_argument("--max-attempts", type=int, default=3)
    run.add_argument("--request-interval", type=float, default=0.25)
    run.add_argument("--max-document-bytes", type=int, default=100 * 1024 * 1024)
    run.add_argument(
        "--redact-output",
        action="store_true",
        help="Hide monitored organisation identifiers from stdout and logs",
    )

    registry = subparsers.add_parser(
        "run-registry", help="Check BRREG entity and role changes for active companies"
    )
    registry.add_argument("--companies", default="companies.csv")
    registry.add_argument("--metadata-dir", default="data")
    registry.add_argument("--notify", choices=("none", "slack"), default="none")
    registry.add_argument("--timeout", type=float, default=20.0)
    registry.add_argument("--max-attempts", type=int, default=3)
    registry.add_argument("--request-interval", type=float, default=0.25)
    registry.add_argument(
        "--redact-output",
        action="store_true",
        help="Hide monitored organisation identifiers from stdout and logs",
    )
    return parser


def directory_size(paths: Sequence[str | Path]) -> int:
    total = 0
    for raw_path in paths:
        path = Path(raw_path)
        if path.is_file():
            total += path.stat().st_size
        elif path.is_dir():
            total += sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    return total


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            companies = load_companies(args.companies)
            if args.update and not args.online:
                raise ValueError("--update requires --online")
            if args.online:
                return _validate_online(args, companies)
            print(f"Validated {len(companies)} companies")
            return 0
        if args.command == "rebuild":
            companies = load_companies(args.companies)
            rebuild_database(args.db, companies, MetadataRepository(args.metadata_dir))
            print(f"Rebuilt SQLite database at {args.db}")
            return 0
        if args.command == "render":
            store = Store(args.db)
            store.initialize()
            try:
                output = render_overview(store, args.site_dir)
            finally:
                store.close()
            print(f"Rendered {output}")
            return 0
        if args.command == "budget-check":
            used = directory_size(args.paths)
            print(f"Version-controlled data size: {used} bytes (limit {args.max_bytes})")
            return 0 if used <= args.max_bytes else 2
        if args.command == "test-slack":
            webhook = os.environ.get("SLACK_WEBHOOK_URL", "")
            SlackNotifier(webhook).send_text(
                "Breg-bot: Slack-varsling er koblet til og fungerer."
            )
            print("Slack test notification sent")
            return 0
        if args.command == "run":
            return _run_monitor(args)
        if args.command == "run-registry":
            return _run_registry_monitor(args)
    except (BrregError, CompanyListError, NotificationError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    parser.error("Unknown command")
    return 2


def _validate_online(args: argparse.Namespace, companies: list[Company]) -> int:
    client = BrregClient(
        timeout=args.timeout,
        max_attempts=args.max_attempts,
        request_interval=args.request_interval,
    )
    reconciled = []
    name_changes = []
    deactivated = []
    for company in companies:
        entity = client.registered_entity(company.orgnr)
        updated = reconcile_company(company, entity)
        reconciled.append(updated)
        if updated.name != company.name:
            name_changes.append(
                {
                    "orgnr": company.orgnr,
                    "old_name": company.name,
                    "new_name": updated.name,
                }
            )
        if company.active and not updated.active:
            deactivated.append(
                {
                    "orgnr": company.orgnr,
                    "name": updated.name,
                    "reason": entity.status,
                }
            )
    if args.update:
        write_companies(args.companies, reconciled)
    report = {
        "validated": len(reconciled),
        "active": sum(company.active for company in reconciled),
        "name_changes": name_changes,
        "deactivated": deactivated,
        "updated": bool(args.update),
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


def _enforce_public_runner_policy(args: argparse.Namespace) -> None:
    if os.environ.get(PUBLIC_RUNNER_ENV) != "1":
        return
    if (
        args.archive != "local"
        or args.notify != "slack"
        or not args.redact_output
        or args.orgnr is not None
    ):
        raise ValueError(
            "Public runner policy requires local-only documents, private Slack notifications, "
            "redacted output, and full-list monitoring"
        )


def _enforce_registry_public_runner_policy(args: argparse.Namespace) -> None:
    if os.environ.get(PUBLIC_RUNNER_ENV) != "1":
        return
    if args.notify != "slack" or not args.redact_output:
        raise ValueError(
            "Public runner policy requires private Slack registry notifications and redacted output"
        )


def _run_monitor(args: argparse.Namespace) -> int:
    _enforce_public_runner_policy(args)
    companies = load_companies(args.companies)
    metadata = MetadataRepository(args.metadata_dir)
    db_path = Path(args.db)
    if not db_path.exists():
        rebuild_database(db_path, companies, metadata)
    store = Store(db_path)
    store.initialize()
    try:
        if args.archive == "github":
            if not args.repository:
                raise ValueError("--repository is required for GitHub Release archival")
            archive = GitHubReleaseArchive(args.repository)
        else:
            archive = LocalArchive(args.documents_dir)

        if args.notify == "github":
            if not args.repository:
                raise ValueError("--repository is required for GitHub Issue notifications")
            notifier = GitHubIssueNotifier(args.repository)
        elif args.notify == "slack":
            webhook = os.environ.get("SLACK_WEBHOOK_URL", "")
            notifier = SlackNotifier(webhook)
        else:
            notifier = None

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        service = MonitorService(
            store=store,
            metadata=metadata,
            client=BrregClient(
                timeout=args.timeout,
                max_attempts=args.max_attempts,
                request_interval=args.request_interval,
                max_document_bytes=args.max_document_bytes,
            ),
            archive=archive,
            notifier=notifier,
            site_directory=args.site_dir,
            redact_identifiers=args.redact_output,
        )
        summary = service.run(
            companies,
            trigger=args.trigger,
            requested_orgnr=args.orgnr or None,
        )
        print(
            json.dumps(
                _summary_for_output(summary, redact=args.redact_output),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0 if summary["status"] == "success" else 1
    finally:
        store.close()


def _run_registry_monitor(args: argparse.Namespace) -> int:
    _enforce_registry_public_runner_policy(args)
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
    service = RegistryMonitorService(
        client=BrregClient(
            timeout=args.timeout,
            max_attempts=args.max_attempts,
            request_interval=args.request_interval,
        ),
        repository=RegistryRepository(args.metadata_dir),
        notifier=notifier,
        redact_identifiers=args.redact_output,
    )
    summary = service.run(companies)
    print(
        json.dumps(
            _summary_for_output(summary, redact=args.redact_output),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if summary["status"] == "success" else 1


def _summary_for_output(summary: dict, *, redact: bool) -> dict:
    if not redact:
        return summary
    return {"status": str(summary.get("status") or "unknown")}
