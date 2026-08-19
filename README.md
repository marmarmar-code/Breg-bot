# Breg-bot

Breg-bot er offentlig kildekode og en offentlig GitHub Actions-runner for intern
overvåking av Brønnøysundregistrene.

Monitoren dekker:

- nye norske årsregnskap;
- viktige selskapsendringer i Enhetsregisteret;
- endringer i daglig leder, styreleder, nestleder og styremedlemmer.

**Dette repositoryet er en null-produksjonsdata-sone.** Ingen produksjonsidentiteter,
overvåkingsliste, funn, dokumenter, snapshots, cursors, rådata, generert status eller
credentials skal noen gang publiseres her. Produksjonsstate ligger i det separate
private `Breg-bot-runtime`-repositoryet, og varsler sendes privat til Slack.

## Produksjonsflyt

Den planlagte workflowen kjører én gang i timen på minutt `:47` og:

1. kontrollerer at public source består sikkerhetsgaten;
2. sjekker ut privat runtime og krever at produksjonskoden er identisk med public source;
3. materialiserer den private selskapslisten midlertidig fra en GitHub Secret;
4. maskerer alle organisasjonsnumre og selskapsnavn før videre behandling;
5. sjekker årsregnskap og BRREGs endringsstrømmer med redigert public output;
6. henter full selskaps-/rolledata bare for selskaper som faktisk har endret seg;
7. sender nye funn via den private Slack-webhooken;
8. committer bare privat metadata/status tilbake til `Breg-bot-runtime`.

Den første registry-kjøringen er alltid en **stille baseline**: dagens selskaps- og
rolledata lagres privat uten varsler. Nye selskaper som senere legges til watchlisten
får samme stille første snapshot. Først senere reelle forskjeller varsles.

Public-repoet skal ikke brukes til produksjons-Issues, Releases, artifacts, Pages,
cache eller dokumentlagring.

## Varslede registry-endringer

V1 varsler deterministisk om:

- navneendring;
- konkurs, avvikling, tvangsavvikling/-oppløsning og sletting/fjerning;
- endret organisasjonsform;
- endret primær næringskode;
- daglig leder, styreleder, nestleder og styremedlemmer inn/ut.

Rolleovervåkingen bruker kun offentlig navn/rolle. Fødselsnummer eller andre
beskyttede personidentifikatorer hentes eller lagres ikke.

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
python3 -m compileall -q breg_watch tests scripts
python3 -m unittest discover -s tests -v
```

For egen privat lokal drift kan `companies.example.csv` kopieres til en ignorert
`companies.csv`. Reell `companies.csv`, `data/`, `site/`, `documents/`, databaser,
logger og PDF-er skal aldri committes til dette repositoryet.

Se [SECURITY.md](SECURITY.md) og [driftsgrensene](docs/operations.md).
