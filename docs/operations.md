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
- registry-cursors, rolle-/selskaps-snapshots eller hvem som har endret rolle;
- PDF-er, rå BRREG-responser eller generert HTML/status;
- Slack-webhook, deploy-nøkkel eller andre credentials.

Ved tvil skal workflowen feile lukket i stedet for å publisere data.

## Produksjonsflyt

1. Actions sjekker først public source og kjører `scripts/check_public_safety.py`.
2. Privat `Breg-bot-runtime` sjekkes ut med en avgrenset deploy-nøkkel, og
   `breg_watch/` må være byte-for-byte identisk med reviewed public source.
3. `companies.csv` materialiseres midlertidig fra `BREG_COMPANIES_CSV_B64` med
   privat filmodus.
4. Før videre behandling registreres alle orgnumre og selskapsnavn med GitHub
   Actions `add-mask`, og watchlisten kryssjekkes mot public source.
5. Runtime-databasen bygges fra privat metadata.
6. Årsregnskapsmonitoren kjører med `BREG_PUBLIC_RUNNER=1`, `--archive local`,
   `--notify slack` og `--redact-output`.
7. Registry-monitoren kjører med `run-registry --notify slack --redact-output`.
8. Nye funn sendes kun til Slack via `SLACK_WEBHOOK_URL`.
9. Dokumenter finnes bare i den midlertidige runneren og forsvinner etter jobben.
10. `data/` og `site/` committes kun tilbake til det private runtime-repositoryet.

`BREG_PUBLIC_RUNNER=1` er en kodebasert fail-closed sperre. Koden nekter public
runner å bruke GitHub-arkiv, GitHub-Issues, usladdet output eller enkelt-orgnr-
kjøring. Registry-kommandoen krever privat Slack og redigert output på public runner.

## Registry-state og baseline

Registry-overvåkingen har sin autoritative state under privat `data/registry/`:

- `state.json` inneholder baseline-status og BRREG-cursors;
- `snapshots/<orgnr>.json` inneholder kanonisk selskaps- og rollesnapshot.

Første kjøring oppretter en stille baseline for aktive selskaper og sender **ingen
varsler**. Baseline kan gjenopptas hvis en kjøring avbrytes. Et nytt selskap som
senere legges til watchlisten får også et stille første snapshot.

Etter baseline brukes BRREGs endringsstrømmer som trigger. Full enhets-/rolledata
hentes bare for organisasjoner som har relevante update-events. Snapshot og cursor
flyttes først etter at eventuelt Slack-varsel er levert. En midlertidig Slack-feil
fører derfor til retry av samme endring ved neste kjøring, ikke tap av varselet.

V1 følger offentlig registrerte roller for daglig leder, styreleder, nestleder og
styremedlemmer. Bare offentlig navn og rolle lagres; fødselsnummer skal aldri hentes
eller persisteres.

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
python3 -m compileall -q breg_watch tests scripts
python3 -m unittest discover -s tests -v
```

`check_public_safety.py` er en sikkerhetsgate og skal ikke svekkes for å få en
endring gjennom. Hvis en legitim endring kolliderer med gaten, må sikkerhetsgrensen
vurderes først.
