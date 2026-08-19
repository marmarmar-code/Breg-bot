from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Sequence

from .slack_format import combine_alert_blocks, format_alert_block


class NotificationError(RuntimeError):
    """A notification could not be delivered."""


Runner = Callable[..., subprocess.CompletedProcess[str]]
SlackTransport = Callable[[str, bytes, float], int]


def urllib_slack_transport(url: str, payload: bytes, timeout: float) -> int:
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


class GitHubIssueNotifier:
    channel = "github_issue"

    def __init__(self, repository: str, *, runner: Runner = subprocess.run) -> None:
        if os.environ.get("BREG_PUBLIC_RUNNER") == "1":
            raise ValueError("GitHub Issue notifications are forbidden on the public runner")
        if "/" not in repository:
            raise ValueError("GitHub repository must have owner/name format")
        self.repository = repository
        self.runner = runner

    def notify(self, run_id: str, filings: list[dict[str, Any]]) -> str:
        if not filings:
            raise ValueError("A notification requires at least one new filing")
        marker = f"<!-- breg-watch:{run_id} -->"
        existing = self._run(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                self.repository,
                "--state",
                "all",
                "--search",
                f'"{marker}" in:body',
                "--json",
                "url",
            ]
        )
        if existing.returncode != 0:
            raise NotificationError("Could not query existing GitHub Issues")
        try:
            matches = json.loads(existing.stdout)
        except json.JSONDecodeError as exc:
            raise NotificationError("Invalid response from GitHub Issue query") from exc
        if matches:
            return str(matches[0]["url"])

        lines = [marker, "", "New annual accounts were detected:", ""]
        for filing in filings:
            lines.append(
                f"- {filing['company_name']} ({filing['orgnr']}), "
                f"period ending {filing.get('period_to') or 'unknown'}, "
                f"BRREG ID {filing['report_id']}, document {filing['document_status']}"
            )
        created = self._run(
            [
                "gh",
                "issue",
                "create",
                "--repo",
                self.repository,
                "--title",
                f"New annual accounts: {len(filings)} ({run_id})",
                "--body",
                "\n".join(lines),
            ]
        )
        if created.returncode != 0:
            raise NotificationError("Could not create GitHub Issue")
        url = created.stdout.strip().splitlines()[-1] if created.stdout.strip() else ""
        if not url.startswith("https://"):
            raise NotificationError("GitHub Issue command did not return a URL")
        return url

    def _run(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return self.runner(args, check=False, capture_output=True, text=True)


class SlackNotifier:
    channel = "slack"

    def __init__(
        self,
        webhook_url: str,
        *,
        transport: SlackTransport = urllib_slack_transport,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 10.0,
        max_attempts: int = 3,
    ) -> None:
        candidate = webhook_url.strip()
        parsed = urllib.parse.urlparse(candidate)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"hooks.slack.com", "hooks.slack-gov.com"}
            or not parsed.path.startswith("/services/")
        ):
            raise ValueError("SLACK_WEBHOOK_URL is missing or invalid")
        if timeout <= 0 or max_attempts < 1:
            raise ValueError("Invalid Slack notifier configuration")
        self.webhook_url = candidate
        self.transport = transport
        self.sleep = sleep
        self.timeout = timeout
        self.max_attempts = max_attempts

    def notify(self, run_id: str, filings: list[dict[str, Any]]) -> str:
        if not filings:
            raise ValueError("A notification requires at least one new filing")
        blocks = []
        for filing in filings:
            period_to = str(filing.get("period_to") or "ukjent")
            source_url = filing.get("source_url")
            if not isinstance(source_url, str) or not source_url.startswith("https://"):
                source_url = _annual_report_url(filing)
            blocks.append(
                format_alert_block(
                    kind="NYTT ÅRSREGNSKAP",
                    company_name=str(filing["company_name"]),
                    orgnr=str(filing["orgnr"]),
                    changes=(
                        f"Periode til: {period_to}",
                        f"BRREG-ID: {filing['report_id']}",
                    ),
                    source_url=source_url,
                    source_label="Åpne årsregnskapet hos BRREG →",
                )
            )
        self.send_text(combine_alert_blocks(blocks))
        return f"slack:{run_id}"

    def send_text(self, text: str) -> None:
        payload = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
        last_status: int | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                status = self.transport(self.webhook_url, payload, self.timeout)
            except (OSError, TimeoutError, urllib.error.URLError) as exc:
                if attempt == self.max_attempts:
                    raise NotificationError(
                        "Slack request failed after temporary transport errors"
                    ) from exc
                self.sleep(float(2 ** (attempt - 1)))
                continue
            last_status = status
            if status == 200:
                return
            if status == 429 or 500 <= status < 600:
                if attempt < self.max_attempts:
                    self.sleep(float(2 ** (attempt - 1)))
                    continue
                raise NotificationError(
                    f"Slack remained temporarily unavailable (HTTP {status})"
                )
            raise NotificationError(f"Slack rejected the notification (HTTP {status})")
        raise NotificationError(
            f"Slack notification failed (HTTP {last_status})"
            if last_status is not None
            else "Slack notification failed"
        )


def _annual_report_url(filing: dict[str, Any]) -> str | None:
    orgnr = str(filing.get("orgnr") or "")
    period_to = str(filing.get("period_to") or "")
    if len(orgnr) != 9 or not orgnr.isdigit() or len(period_to) < 4 or not period_to[:4].isdigit():
        return None
    year = period_to[:4]
    return (
        "https://data.brreg.no/regnskapsregisteret/regnskap/"
        f"aarsregnskap/kopi/{orgnr}/{year}"
    )
