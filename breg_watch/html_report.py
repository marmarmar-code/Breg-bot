from __future__ import annotations

import html
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .store import Store


def render_overview(store: Store, output_directory: str | Path) -> Path:
    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    filings = store.list_filings()
    last_success = store.last_successful_run()
    latest_run = store.latest_run()
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for filing in filings:
        status = (
            "Arkivert"
            if filing["document_status"] == "archived"
            else "Venter på dokument"
        )
        if filing.get("archive_reference"):
            reference = str(filing["archive_reference"])
            if filing.get("archive_kind") == "local":
                reference = f"../documents/{reference}"
            status_html = (
                f'<a href="{html.escape(reference, quote=True)}">'
                f"{html.escape(status)}</a>"
            )
        else:
            status_html = html.escape(status)
        period = f"{filing.get('period_from') or 'ukjent'} – {filing.get('period_to') or 'ukjent'}"
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(filing['company_name']))}<br><small>{html.escape(str(filing['orgnr']))}</small></td>"
            f"<td>{html.escape(period)}</td>"
            f"<td>{html.escape(str(filing['report_id']))}</td>"
            f"<td>{status_html}</td>"
            f"<td>{html.escape(str(filing['discovered_at']))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="5">Ingen regnskap er oppdaget ennå.</td></tr>')

    success_status = "Ingen vellykkede kjøringer"
    latest_status = "Ingen fullførte kjøringer"
    error_html = ""
    if last_success:
        success_status = str(last_success.get("finished_at") or last_success["started_at"])
    if latest_run:
        latest_status = str(latest_run.get("finished_at") or latest_run["started_at"])
        try:
            errors = json.loads(latest_run.get("errors_json") or "[]")
        except json.JSONDecodeError:
            errors = [{"kind": "invalid_stored_error_status"}]
        if errors:
            items = "".join(
                f"<li>{html.escape(str(error.get('orgnr', 'ukjent')))}: "
                f"{html.escape(str(error.get('kind', 'feil')))}</li>"
                for error in errors
            )
            error_html = f"<h2>Feil og ventende arbeid</h2><ul>{items}</ul>"

    document = f"""<!doctype html>
<html lang="nb">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Breg Watch</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 75rem; padding: 0 1rem; color: #202124; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ border-bottom: 1px solid #d8dadd; padding: .75rem; text-align: left; vertical-align: top; }}
    th {{ background: #f4f5f6; }}
    .status {{ background: #eef3f8; padding: 1rem; margin-bottom: 1.5rem; }}
    @media (max-width: 45rem) {{ table {{ font-size: .85rem; }} th, td {{ padding: .4rem; }} }}
  </style>
</head>
<body>
  <h1>Breg Watch</h1>
  <div class="status"><strong>Siste vellykkede kjøring:</strong> {html.escape(success_status)}<br>
  <strong>Siste kjøring:</strong> {html.escape(latest_status)}<br>
  <small>Oversikten ble generert {html.escape(generated_at)}.</small></div>
{error_html}
  <h2>Oppdagede årsregnskap</h2>
  <table>
    <thead><tr><th>Selskap</th><th>Regnskapsperiode</th><th>BRREG-ID</th><th>Dokument</th><th>Oppdaget</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</body>
</html>
"""
    target = output_directory / "index.html"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=output_directory, delete=False) as temporary:
        temporary.write(document)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, target)
    return target
