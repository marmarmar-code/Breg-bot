# Security policy: zero production disclosure

`Breg-bot` er offentlig. Derfor behandles hele repositoryet og alle GitHub-flater
knyttet til det som offentlig informasjon.

## Produksjonsdata som alltid er private

Følgende skal aldri finnes i Git-historikk, filer, Issues, Releases, artifacts,
Pages, cache, Actions-output eller jobbsammendrag i dette repositoryet:

- identiteten til overvåkede selskaper, inkludert orgnr og navn;
- watchlisten eller antall overvåkede selskaper;
- hvilke selskaper som har gitt nye treff eller når konkrete treff oppstår;
- report-ID, journalnummer, rå BRREG-responser, hendelsesmetadata eller privat status;
- årsregnskaps-PDF-er eller andre nedlastede dokumenter;
- Slack-webhook, deploy-nøkkel, tokens eller andre credentials.

Produksjonsstate lagres i privat `Breg-bot-runtime`. Varsler sendes privat til Slack.

## Påkrevde forsvarslag

Public produksjonsworkflow skal samtidig ha alle disse egenskapene:

1. `GITHUB_TOKEN` er read-only (`contents: read`).
2. Ingen produksjons-Issues, Releases, artifacts, Pages eller cache.
3. Reviewed public `main` sjekkes ut separat og må bestå `scripts/check_public_safety.py` før produksjonsdata åpnes.
4. `breg_watch/` i privat runtime må være byte-for-byte identisk med reviewed public source før jobben fortsetter.
5. `companies.csv` materialiseres kun midlertidig fra secret med `umask 077`.
6. Alle orgnumre og selskapsnavn registreres med Actions `add-mask` før behandling.
7. Den private watchlisten kryssjekkes mot public source; ethvert orgnr eller selskapsnavn i public tree stopper jobben uten å logge identiteten.
8. `BREG_PUBLIC_RUNNER=1` er satt.
9. Monitoren kjører med `--archive local`, `--notify slack` og `--redact-output`.
10. Public runner tillater ikke enkelt-orgnr-kjøring.
11. Offentlig programoutput er minimal; detaljer ligger kun i privat runtime, og private Git-kommandoer kjøres stille.
12. Ingen produksjonsfiler lastes opp eller committes til public-repoet.

GitHub-publiseringsklassene har i tillegg egne runtime-sperrer som avviser public runner, selv om CLI-laget skulle bli omgått.

Disse lagene er komplementære. Én enkelt mekanisme, for eksempel `.gitignore` eller
loggredigering alene, er ikke tilstrekkelig.

## Endringsregel

`scripts/check_public_safety.py` er en sikkerhetsgate. Den skal kjøres i CI og skal
ikke svekkes bare for å få en annen endring gjennom. En endring som medfører
produksjonsdata på en offentlig GitHub-flate skal avvises eller redesignes.

Runtime-koden og public-koden skal ikke drive fra hverandre. En produksjonsjobb med kode-drift skal feile lukket før watchlisten materialiseres.

Ved usikkerhet: fail closed og behold data privat.
