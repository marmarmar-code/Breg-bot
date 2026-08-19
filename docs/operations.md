# Sikker drift

Den planlagte monitoren kjører i det offentlige `Breg-bot`-repositoryet for å
bruke offentlig GitHub Actions. **Public-repoet er kun runner og kildekode; all
produksjonsinformasjon er privat.**

## Absolutte sikkerhetsgrenser

Følgende skal aldri publiseres i public-repoet, Actions-artifacts, Issues,
Releases, Pages, cache eller offentlige logger:

- produksjonens organisasjonsnumre og selskapsnavn;
- hele eller deler av watchlisten, inkludert antall overvåkede selskaper;
- funn, rapport-ID-er, journalnumre eller hendelsesmetadata;
- PDF-er, rå BRREG-responser eller generert HTML/status;
- Slack-webhook, deploy-nøkkel eller andre credentials.

Ved tvil skal workflowen feile lukket i stedet for å publisere data.

## Produksjonsflyt

1. Actions sjekker ut privat `Breg-bot-runtime` med en avgrenset deploy-nøkkel.
2. `companies.csv` materialiseres midlertidig fra `BREG_COMPANIES_CSV_B64` med
   privat filmodus.
3. Før videre behandling registreres alle orgnumre og selskapsnavn med GitHub
   Actions `add-mask`, slik at utilsiktet senere loggoutput også redigeres.
4. Runtime-databasen bygges fra privat metadata.
5. Monitoren kjører med `BREG_PUBLIC_RUNNER=1`, `--archive local`,
   `--notify slack` og `--redact-output`.
6. Nye funn sendes kun til Slack via `SLACK_WEBHOOK_URL`.
7. Dokumenter finnes bare i den midlertidige runneren og forsvinner etter jobben.
8. `data/` og `site/` committes kun tilbake til det private runtime-repositoryet.

`BREG_PUBLIC_RUNNER=1` er en kodebasert fail-closed sperre. Koden nekter public
runner å bruke GitHub-arkiv, GitHub-Issues, usladdet output eller enkelt-orgnr-
kjøring, selv om noen senere endrer CLI-argumentene i workflowen.

## Offentlige Actions

Public-workflowen har bare `contents: read` på `Breg-bot`. Den bruker ikke
`GITHUB_TOKEN` til produksjonspublisering og skal ikke ha `issues: write`,
`contents: write` eller `actions: write` i normal drift.

Private Git-operasjoner kjøres stille. Feil fra pull/push og lagringskontroll
rapporteres generisk slik at private filstier eller størrelser ikke havner i
public-loggen. Programoutput i redigert modus inneholder bare overordnet status.

Pull requests og forks har ingen produksjonstrigger og får ikke produksjonssecrets.

## Endringskontroll

Alle kode- og workflow-endringer skal bestå:

```bash
python3 scripts/check_public_safety.py
python3 -m breg_watch validate --companies companies.example.csv
python3 -m compileall -q breg_watch tests
python3 -m unittest discover -s tests -v
```

`check_public_safety.py` er en sikkerhetsgate og skal ikke svekkes for å få en
endring gjennom. Hvis en legitim endring kolliderer med gaten, må sikkerhetsgrensen
vurderes først.
