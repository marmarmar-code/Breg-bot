# Breg-bot

Breg-bot er offentlig kildekode og en offentlig GitHub Actions-runner for intern
overvåking av nye norske årsregnskap fra Brønnøysundregistrene.

**Dette repositoryet er en null-produksjonsdata-sone.** Ingen produksjonsidentiteter,
overvåkingsliste, funn, dokumenter, rådata, generert status eller credentials skal
noen gang publiseres her. Produksjonsstate ligger i det separate private
`Breg-bot-runtime`-repositoryet, og varsler sendes privat til Slack.

## Produksjonsflyt

Den planlagte workflowen kjører én gang i timen på minutt `:47` og:

1. sjekker ut privat runtime med en repository-avgrenset deploy-nøkkel;
2. materialiserer den private selskapslisten midlertidig fra en GitHub Secret;
3. registrerer alle organisasjonsnumre og selskapsnavn som maskerte Actions-verdier;
4. kjører monitoren med lokal, midlertidig dokumenthåndtering og redigert output;
5. sender nye funn via den private Slack-webhooken;
6. committer bare metadata og status tilbake til det private runtime-repositoryet.

Public-repoet skal ikke brukes til produksjons-Issues, Releases, artifacts, Pages,
cache eller dokumentlagring.

## Secrets

Produksjonsworkflowen bruker:

- `BREG_COMPANIES_CSV_B64`: privat selskapsliste;
- `RUNTIME_DEPLOY_KEY`: avgrenset tilgang til privat runtime;
- `SLACK_WEBHOOK_URL`: privat Slack-varsling.

Secret-verdiene skal aldri ligge i Git. Secret-navnene er kun konfigurasjonsnavn.

## Lokal kontroll

`companies.example.csv` og alle fixtures er fiktive testdata.

```bash
python3 scripts/check_public_safety.py
python3 -m breg_watch validate --companies companies.example.csv
python3 -m compileall -q breg_watch tests
python3 -m unittest discover -s tests -v
```

For egen privat lokal drift kan `companies.example.csv` kopieres til en ignorert
`companies.csv`. Reell `companies.csv`, `data/`, `site/`, `documents/`, databaser,
logger og PDF-er skal aldri committes til dette repositoryet.

Se [SECURITY.md](SECURITY.md) og [driftsgrensene](docs/operations.md).
