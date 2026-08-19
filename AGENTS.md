# Breg-bot

Formål: offentlig kildekode og gratis GitHub Actions-runner for en monitor der
**ingen produksjonsinformasjon skal bli offentlig**.

- `SECURITY.md` er autoritativ for datagrensen. `README.md` beskriver bruk og
  `docs/operations.md` sikker drift.
- Dette repositoryet er en null-produksjonsdata-sone. Opprett aldri reell
  `companies.csv`, produksjons-orgnr/-navn, `data/`, `site/`, PDF, logg, database,
  rå BRREG-data, funn, token eller annen produksjonsartefakt her.
- Produksjonsfunn skal aldri publiseres som GitHub Issues, Releases, artifacts,
  Pages, cache eller andre offentlige GitHub-flater. Varsling går privat til Slack.
- Ikke svekk eller fjern `BREG_PUBLIC_RUNNER`, `--redact-output`, `add-mask`,
  local-only dokumenthåndtering, watchlist/public-source-kryssjekken,
  runtime/public-source-ekvivalenskontrollen eller sikkerhetsgaten.
- `breg_watch/` i privat `Breg-bot-runtime` skal være byte-for-byte identisk med
  reviewed public source før produksjon får kjøre. Kode-drift skal stoppe jobben.
- Public workflow skal ha read-only `GITHUB_TOKEN`. `contents: write`,
  `issues: write` og `actions: write` er forbudt i normal produksjonsworkflow.
- Public workflow skal aldri ta organisasjonsnummer som manuelt input. Hele
  watchlisten materialiseres kun fra secret, maskeres før behandling og
  kryssjekkes mot public source uten å logge treffet.
- Private runtime-data kan bare committes tilbake til `Breg-bot-runtime`.
  Git-output som kan inneholde private filstier skal holdes stille.
- Eksempel- og testdata skal være åpenbart fiktive. Ikke kopier produksjonsdata
  eller andre ekte gyldige organisasjonsnumre inn i public fixtures, docs,
  kommentarer eller tester.
- Organisasjonsnummer er autoritativ identitet og skal aldri utledes fra navn.
- Gjør små, målrettede endringer. Sikkerhetsgrenser endres separat og eksplisitt.
- Minste kontroll ved enhver endring er:
  `python3 scripts/check_public_safety.py`,
  `python3 -m breg_watch validate --companies companies.example.csv`,
  `python3 -m compileall -q breg_watch tests scripts` og
  `python3 -m unittest discover -s tests -v`.
- Hvis en ønsket endring krever at produksjonsinformasjon blir offentlig, stopp i
  stedet for å implementere den.
