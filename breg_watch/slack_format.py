from __future__ import annotations

from collections.abc import Iterable


def format_alert_block(
    *,
    kind: str,
    company_name: str,
    orgnr: str | None,
    changes: Iterable[str],
    source_url: str | None = None,
    source_label: str = "Se hos Brønnøysundregistrene →",
) -> str:
    lines = [
        f"*BRREG-VARSEL · {_escape(kind.upper())}*",
        f"*{_escape(company_name)}*",
    ]
    if orgnr:
        lines.append(f"Org.nr. {_escape(orgnr)}")
    lines.append("")
    lines.extend(f"– {_escape(str(change))}" for change in changes if str(change).strip())
    lines.append("")
    if source_url and source_url.startswith("https://"):
        lines.append(f"<{source_url}|{_escape(source_label)}>")
    else:
        lines.append("Kilde: Brønnøysundregistrene")
    return "\n".join(lines).rstrip()


def combine_alert_blocks(blocks: Iterable[str]) -> str:
    clean = [block.strip() for block in blocks if block and block.strip()]
    return "\n\n——————————\n\n".join(clean)


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
