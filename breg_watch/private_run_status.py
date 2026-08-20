from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Sequence

from .brreg import BrregClient, BrregError
from .companies import CompanyListError, load_companies
from .notifier import NotificationError, SlackNotifier
from .registry_monitor import RegistryMonitorService, RegistryRepository
from .structural_monitor import StructuralBrregClient, StructuralMonitorService, StructuralRepository


PUBLIC_RUNNER_ENV = "BREG_PUBLIC_RUNNER"


class CountingStructuralBrregClient(StructuralBrregClient):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.entity_events = 0
        self.subunit_events = 0
        self.entity_refetches = 0
        self.subunit_refetches = 0

    def entity_updates(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        events = super().entity_updates(*args, **kwargs)
        self.entity_events = len(events)
        return events

    def subunit_updates(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        events = super().subunit_updates(*args, **kwargs)
        self.subunit_events = len(events)
        return events

    def entity_details(self, orgnr: str):
        self.entity_refetches += 1
        return super().entity_details(orgnr)

    def subunit_detail(self, orgnr: str) -> dict[str, Any]:
        self.subunit_refetches += 1
        return super().subunit_detail(orgnr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="private-run-status")
    parser.add_argument("monitor", choices=("registry", "structural"))
    parser.add_argument("--companies", default="companies.csv")
    parser.add_argument("--metadata-dir", default="data")
    parser.add_argument("--notify", choices=("none", "slack"), default="none")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--request-interval", type=float, default=0.25)
    parser.add_argument("--redact-output", action="store_true")
    return parser


def _enforce_public_runner_policy(args: argparse.Namespace) -> None:
    if os.environ.get(PUBLIC_RUNNER_ENV) != "1":
        return
    if args.notify != "slack" or not args.redact_output:
        raise ValueError(
            "Public runner policy requires private Slack notifications and redacted output"
        )


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


def _notifier(args: argparse.Namespace):
    if args.notify == "slack":
        return SlackNotifier(os.environ.get("SLACK_WEBHOOK_URL", ""))
    return None


def _registry(args: argparse.Namespace) -> dict[str, Any]:
    companies = load_companies(args.companies)
    repository = RegistryRepository(args.metadata_dir)
    service = RegistryMonitorService(
        client=BrregClient(
            timeout=args.timeout,
            max_attempts=args.max_attempts,
            request_interval=args.request_interval,
        ),
        repository=repository,
        notifier=_notifier(args),
        redact_identifiers=args.redact_output,
    )
    raw = service.run(companies)
    state = repository.read_state()
    return {
        "status": str(raw.get("status") or "unknown"),
        "checked_at": state.get("last_checked_at"),
        "baseline": bool(raw.get("baseline")),
        "newly_baselined": int(raw.get("newly_baselined") or 0),
        "entity_events": int(raw.get("entity_updates") or 0),
        "role_events": int(raw.get("role_updates") or 0),
        "affected": int(raw.get("affected") or 0),
        "alerts": int(raw.get("alerts") or 0),
    }


def _structural(args: argparse.Namespace) -> dict[str, Any]:
    companies = load_companies(args.companies)
    client = CountingStructuralBrregClient(
        timeout=args.timeout,
        max_attempts=args.max_attempts,
        request_interval=args.request_interval,
    )
    repository = StructuralRepository(args.metadata_dir)
    service = StructuralMonitorService(
        client=client,
        repository=repository,
        notifier=_notifier(args),
        redact_identifiers=args.redact_output,
    )
    raw = service.run(companies)
    state = repository.read_state()
    return {
        "status": str(raw.get("status") or "unknown"),
        "checked_at": state.get("last_checked_at"),
        "baseline": bool(raw.get("baseline")),
        "entity_events": client.entity_events,
        "subunit_events": client.subunit_events,
        "entity_refetches": client.entity_refetches,
        "subunit_refetches": client.subunit_refetches,
        "alerts": int(raw.get("alerts") or 0),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    output = Path(args.metadata_dir) / args.monitor / "last_run.json"
    try:
        _enforce_public_runner_policy(args)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        summary = _registry(args) if args.monitor == "registry" else _structural(args)
        _write_json(output, summary)
        visible = {"status": summary["status"]} if args.redact_output else summary
        print(json.dumps(visible, ensure_ascii=False, sort_keys=True))
        return 0 if summary["status"] == "success" else 1
    except (BrregError, CompanyListError, NotificationError, ValueError, OSError) as exc:
        _write_json(
            output,
            {
                "status": "error",
                "error_type": type(exc).__name__,
            },
        )
        if args.redact_output:
            print(f"Error: {type(exc).__name__}")
        else:
            print(f"Error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
