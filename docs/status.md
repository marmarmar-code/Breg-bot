# Offentlig prosjektstatus

Dette repositoryet inneholder offentlig kildekode og den planlagte Actions-runneren.
Produksjonslisten, historikk, generert status, dokumenter og funn ligger ikke her.

Produksjonsjobben kjører timevis og sender nye funn privat til Slack. Public Actions
bruker maskering av produksjonsidentiteter, minimal redigert output og read-only
`GITHUB_TOKEN`-rettigheter.

Sikkerhetsgrensen er testbar: `scripts/check_public_safety.py` og enhetstestene skal
feile hvis workflowen igjen forsøker public Issues/Releases, artifacts, write-token,
usladdet output eller andre kjente lekkasjeveier.

Se `SECURITY.md` for den autoritative datagrensen.
