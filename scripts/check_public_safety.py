#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
ALLOWED_WORKFLOWS = {"ci.yml", "monitor.yml"}
FORBIDDEN_DIRECTORIES = {"data", "site", "documents", "state", "logs"}
ALLOWED_CSV = {"companies.example.csv"}
FORBIDDEN_SUFFIXES = {
    ".pdf",
    ".log",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".xlsx",
    ".xls",
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".7z",
}
SYNTHETIC_NINE_DIGIT = re.compile(r"(?:0000000\d{2}|123456789)\Z")
NINE_DIGIT = re.compile(r"(?<!\d)(\d{9})(?!\d)")
SECRET_PATTERNS = {
    "Slack webhook": re.compile(r"https://hooks\.slack(?:-gov)?\.com/services/[A-Za-z0-9/_-]{20,}"),
    "GitHub PAT": re.compile(r"(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})"),
    "Slack token": re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"),
    "private key": re.compile("BEGIN OPENSSH " + "PRIVATE KEY"),
}


def valid_orgnr(value: str) -> bool:
    if len(value) != 9 or not value.isascii() or not value.isdigit():
        return False
    digits = [int(char) for char in value]
    weighted = sum(digit * weight for digit, weight in zip(digits[:8], (3, 2, 7, 6, 5, 4, 3, 2)))
    remainder = weighted % 11
    check = 11 - remainder
    if check == 11:
        check = 0
    if check == 10:
        return False
    return digits[-1] == check


def text_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        files.append(path)
    return files


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def check_tree(errors: list[str]) -> None:
    for path in text_files():
        rel = path.relative_to(ROOT)
        rel_text = rel.as_posix()
        if any(part in FORBIDDEN_DIRECTORIES for part in rel.parts):
            errors.append(f"forbidden production directory tracked: {rel_text}")
        if path.name == "companies.csv":
            errors.append(f"production company list tracked: {rel_text}")
        if path.name == ".env" or path.name.startswith(".env."):
            errors.append(f"environment file tracked: {rel_text}")
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            errors.append(f"production-like artifact tracked: {rel_text}")
        if path.suffix.lower() == ".html":
            errors.append(f"generated HTML must not be tracked publicly: {rel_text}")
        if path.suffix.lower() == ".csv" and rel_text not in ALLOWED_CSV:
            errors.append(f"unexpected CSV tracked: {rel_text}")

        text = read_text(path)
        if text is None:
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                errors.append(f"possible {label} value in {rel_text}")
        for value in NINE_DIGIT.findall(text):
            if SYNTHETIC_NINE_DIGIT.fullmatch(value) is None and valid_orgnr(value):
                errors.append(f"valid non-synthetic Norwegian organisation number in {rel_text}")
                break


def require(text: str, needle: str, label: str, errors: list[str]) -> None:
    if needle not in text:
        errors.append(f"missing {label}")


def forbid(text: str, needle: str, label: str, errors: list[str]) -> None:
    if needle in text:
        errors.append(f"forbidden {label}")


def check_workflows(errors: list[str]) -> None:
    workflows = {path.name for path in WORKFLOW_DIR.glob("*.yml")}
    workflows |= {path.name for path in WORKFLOW_DIR.glob("*.yaml")}
    if workflows != ALLOWED_WORKFLOWS:
        errors.append("unexpected workflow set: " + ", ".join(sorted(workflows)))

    monitor = read_text(WORKFLOW_DIR / "monitor.yml") or ""
    ci = read_text(WORKFLOW_DIR / "ci.yml") or ""

    require(monitor, 'cron: "47 * * * *"', "hourly :47 schedule", errors)
    require(monitor, "contents: read", "read-only public token", errors)
    require(monitor, 'BREG_PUBLIC_RUNNER: "1"', "public-runner fail-closed mode", errors)
    require(monitor, "path: public-source", "reviewed public source checkout", errors)
    require(monitor, "python3 scripts/check_public_safety.py", "pre-production public safety check", errors)
    require(monitor, "diff -qr ../public-source/breg_watch breg_watch", "runtime/public code-equivalence gate", errors)
    require(monitor, "::add-mask::", "Actions masking of private identities", errors)
    require(monitor, "Reviewed public source contains a private monitored identity", "watchlist/public-source cross-check", errors)
    require(monitor, "--archive local", "local-only document handling", errors)
    require(monitor, "--notify slack", "private Slack notification path", errors)
    require(
        monitor,
        "python3 -m breg_watch.private_run_status registry",
        "hourly entity/role monitor with private run status",
        errors,
    )
    require(
        monitor,
        "python3 -m breg_watch.private_run_status structural",
        "hourly structural monitor with private run status",
        errors,
    )
    require(monitor, "Monitor entity and role changes privately", "private registry workflow step", errors)
    require(
        monitor,
        "Monitor capital, subunits and merger/demerger notices privately",
        "private structural workflow step",
        errors,
    )
    require(monitor, "--redact-output", "redacted public output", errors)
    require(monitor, "git add data site", "private runtime metadata persistence", errors)

    if monitor.count("--redact-output") < 3:
        errors.append("annual-account, registry and structural monitors must all use redacted output")
    if monitor.count("--notify slack") < 3:
        errors.append("annual-account, registry and structural monitors must all use private Slack")

    for needle, label in (
        ("contents: write", "public contents write permission"),
        ("issues: write", "public Issues write permission"),
        ("actions: write", "public Actions write permission"),
        ("--archive github", "GitHub Release archive on public runner"),
        ("--notify github", "GitHub Issue notification on public runner"),
        ("gh release", "release publishing command"),
        ("gh issue", "issue publishing command"),
        ("upload-artifact", "artifact upload"),
        ("actions/cache", "public cache"),
        ("inputs.orgnr", "public organisation-number input"),
        ("GITHUB_TOKEN:", "explicit public GitHub token exposure"),
        ("Private Git data size", "private runtime size in public summary"),
        ("--online", "online company reconciliation in public workflow"),
    ):
        forbid(monitor, needle, label, errors)

    require(ci, "contents: read", "read-only CI token", errors)
    require(ci, "persist-credentials: false", "non-persistent CI credentials", errors)
    require(ci, "python3 scripts/check_public_safety.py", "public safety CI gate", errors)
    for needle, label in (
        ("contents: write", "CI contents write permission"),
        ("issues: write", "CI Issues write permission"),
        ("actions: write", "CI Actions write permission"),
        ("secrets.", "production secret in CI"),
        ("schedule:", "scheduled CI with no need for secrets"),
        ("workflow_dispatch:", "manual CI with no need for secrets"),
        ("upload-artifact", "CI artifact upload"),
    ):
        forbid(ci, needle, label, errors)


def check_policy(errors: list[str]) -> None:
    security = read_text(ROOT / "SECURITY.md") or ""
    agents = read_text(ROOT / "AGENTS.md") or ""
    operations = read_text(ROOT / "docs" / "operations.md") or ""
    require(security, "zero production disclosure", "zero-disclosure SECURITY policy", errors)
    require(agents, "null-produksjonsdata-sone", "agent zero-production-data rule", errors)
    require(operations, "BREG_PUBLIC_RUNNER=1", "documented public-runner guard", errors)


def main() -> int:
    errors: list[str] = []
    check_tree(errors)
    check_workflows(errors)
    check_policy(errors)
    if errors:
        print("PUBLIC SAFETY CHECK FAILED", file=sys.stderr)
        for error in sorted(set(errors)):
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Public safety checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
