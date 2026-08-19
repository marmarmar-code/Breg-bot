from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


class ArchiveError(RuntimeError):
    """A document could not be archived safely."""


@dataclass(frozen=True, slots=True)
class ArchiveResult:
    kind: str
    reference: str
    sha256: str
    size_bytes: int
    content_type: str


@dataclass(frozen=True, slots=True)
class RemoteAsset:
    url: str
    size: int | None
    digest: str | None


def archive_asset_name(orgnr: str, report_id: int, year: str) -> str:
    return f"annual-account-{orgnr}-{year}-{report_id}.pdf"


class LocalArchive:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def archive(
        self,
        *,
        orgnr: str,
        report_id: int,
        year: str,
        discovered_at: str,
        content: bytes,
        content_type: str,
    ) -> ArchiveResult:
        del discovered_at
        name = archive_asset_name(orgnr, report_id, year)
        target = self.root / orgnr / name
        digest = hashlib.sha256(content).hexdigest()
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            existing_digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if existing_digest != digest:
                raise ArchiveError(f"Archive collision for {name}")
        else:
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, target)
        return ArchiveResult(
            kind="local",
            reference=f"{orgnr}/{name}",
            sha256=digest,
            size_bytes=len(content),
            content_type=content_type,
        )


Runner = Callable[..., subprocess.CompletedProcess[str]]


class GitHubReleaseArchive:
    def __init__(self, repository: str, *, runner: Runner = subprocess.run) -> None:
        if os.environ.get("BREG_PUBLIC_RUNNER") == "1":
            raise ValueError("GitHub Release archival is forbidden on the public runner")
        if "/" not in repository:
            raise ValueError("GitHub repository must have owner/name format")
        self.repository = repository
        self.runner = runner

    def archive(
        self,
        *,
        orgnr: str,
        report_id: int,
        year: str,
        discovered_at: str,
        content: bytes,
        content_type: str,
    ) -> ArchiveResult:
        name = archive_asset_name(orgnr, report_id, year)
        tag = f"annual-accounts-{discovered_at[:7]}"
        digest = hashlib.sha256(content).hexdigest()
        existing = self._assets(tag)
        if existing is not None and name in existing:
            self._verify_asset(tag, name, existing[name], digest, len(content))
            return ArchiveResult(
                kind="github_release",
                reference=existing[name].url,
                sha256=digest,
                size_bytes=len(content),
                content_type=content_type,
            )

        if existing is None:
            created = self._run(
                [
                    "gh",
                    "release",
                    "create",
                    tag,
                    "--repo",
                    self.repository,
                    "--title",
                    f"Annual accounts {discovered_at[:7]}",
                    "--notes",
                    "Archive managed by Breg Watch.",
                ]
            )
            if created.returncode != 0:
                raise ArchiveError("Could not create GitHub Release")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / name
            path.write_bytes(content)
            uploaded = self._run(
                ["gh", "release", "upload", tag, str(path), "--repo", self.repository]
            )
        if uploaded.returncode != 0:
            raise ArchiveError("Could not upload GitHub Release asset")
        uploaded_assets = self._assets(tag)
        if not uploaded_assets or name not in uploaded_assets:
            raise ArchiveError("Uploaded GitHub Release asset could not be verified")
        uploaded_asset = uploaded_assets[name]
        self._verify_asset(tag, name, uploaded_asset, digest, len(content))
        return ArchiveResult(
            kind="github_release",
            reference=uploaded_asset.url,
            sha256=digest,
            size_bytes=len(content),
            content_type=content_type,
        )

    def _assets(self, tag: str) -> dict[str, RemoteAsset] | None:
        result = self._run(
            ["gh", "release", "view", tag, "--repo", self.repository, "--json", "assets"]
        )
        if result.returncode != 0:
            return None
        try:
            payload = json.loads(result.stdout)
            return {
                str(asset["name"]): RemoteAsset(
                    url=str(asset["url"]),
                    size=int(asset["size"]) if asset.get("size") is not None else None,
                    digest=str(asset["digest"]) if asset.get("digest") else None,
                )
                for asset in payload.get("assets", [])
            }
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ArchiveError("Invalid response from GitHub Release query") from exc

    def _verify_asset(
        self,
        tag: str,
        name: str,
        asset: RemoteAsset,
        expected_digest: str,
        expected_size: int,
    ) -> None:
        if asset.size is not None and asset.size != expected_size:
            raise ArchiveError(f"Existing Release asset has wrong size: {name}")
        if asset.digest is not None:
            if asset.digest.lower() != f"sha256:{expected_digest}":
                raise ArchiveError(f"Existing Release asset has wrong digest: {name}")
            return

        with tempfile.TemporaryDirectory() as directory:
            downloaded = self._run(
                [
                    "gh",
                    "release",
                    "download",
                    tag,
                    "--repo",
                    self.repository,
                    "--pattern",
                    name,
                    "--dir",
                    directory,
                ]
            )
            path = Path(directory) / name
            if downloaded.returncode != 0 or not path.is_file():
                raise ArchiveError(f"Existing Release asset could not be verified: {name}")
            content = path.read_bytes()
        if len(content) != expected_size or hashlib.sha256(content).hexdigest() != expected_digest:
            raise ArchiveError(f"Existing Release asset content does not match: {name}")

    def _run(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return self.runner(args, check=False, capture_output=True, text=True)
