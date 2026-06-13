# DEV.md — Mode développement local

> Objectif : `git clone` → `task dev` → tester un cycle complet **plan → confirm → apply → cascade** en moins de 5 minutes, sans compte Google, sans AWS, sans repo GitHub.

---

## 1. Principe : tout est réel, sauf les frontières externes

Le mode dev fait tourner les **vrais composants** (API, scheduler, state machine, worker, state backend, audit) et ne remplace que les trois dépendances externes :

| Dépendance | En prod | En dev local |
|---|---|---|
| Auth Google | OIDC accounts.google.com | **Dev login** flag-gaté (§3) |
| Repos Git | GitHub/GitLab | **Repos fixtures locaux** `file://` + Gitea optionnel (§4) |
| Cloud AWS | STS/providers réels | **Providers sans cloud** (`local_file`, `random`, `null`) + LocalStack optionnel (§6) |

Tout le reste (Postgres, Garage, le protocole worker, les hooks, les mocks, la staleness) fonctionne à l'identique de la prod. C'est volontaire : le mode dev teste la plateforme, pas une simulation de la plateforme.

---

## 2. Stack docker compose

```yaml
# deploy/docker-compose.dev.yml
services:
  postgres:        # 18, port 5432, volume nommé
  garage:          # S3 local (Garage), API S3 sur :3900, admin CLI `garage` ; bucket stackd auto-créé au seed
  api:             # uv run uvicorn --reload, monte ./api, port 8000
  front:           # vite dev server, monte ./front, port 5173, proxy /api → api
  worker:          # agent en mode --reload (watchfiles), runner=local (§5)
  # profils optionnels :
  gitea:           # profile "git"  : webhooks réels en local (§4)
  localstack:      # profile "aws"  : S3/RDS/IAM simulés (§6)
```

```bash
task dev            # compose up (services de base) + migrations + seed
task dev-git        # + Gitea (test des webhooks)
task dev-aws        # + LocalStack
task seed           # (re)crée les données de démo, idempotent
task reset          # down -v + dev : repart de zéro
task logs           # logs agrégés colorés (api, worker)
task test           # pytest + vitest
task e2e            # scénario complet automatisé (§7)
```

Orchestration par **[Task](https://taskfile.dev)** (`Taskfile.yml` à la racine) plutôt que Make : syntaxe YAML lisible, multiplateforme (binaire Go unique, pas de dépendance à GNU Make), `deps`/`status` natifs pour l'idempotence, namespaces par module, et `task --list` auto-documenté. Extrait :

```yaml
# Taskfile.yml
version: '3'
dotenv: ['.env']
vars:
  COMPOSE: docker compose -f deploy/docker-compose.dev.yml

tasks:
  dev:
    desc: Stack locale complète (compose + migrations + seed)
    deps: [env]
    cmds:
      - "{{.COMPOSE}} up -d --wait"
      - task: migrate
      - task: seed

  env:
    desc: Génère .env et les clés de dev au premier lancement
    cmds: [./scripts/bootstrap-env.sh]
    status: [test -f .env]          # idempotent : ne refait rien si .env existe

  seed:
    desc: Données de démo (idempotent)
    cmds: ["{{.COMPOSE}} exec api uv run python -m app.seed"]

  push-change:
    desc: Simule un merge dans demo-network (test staleness)
    dir: .dev/repos/demo-network
    cmds:
      - date >> CHANGELOG.txt
      - git add -A && git commit -m "feat: simulated merge"

  e2e:
    desc: Scénario complet §7 (non-régression)
    deps: [dev]
    cmds: [pytest tests/e2e -v]
```

`.env.example` → `.env` copié automatiquement par `task dev` : clés de dev (encryption key, JWT secret, OIDC keypair) **générées au premier lancement** et stockées dans `.dev/` (gitignored). Aucune valeur secrète committée, aucune saisie manuelle.

---

## 3. Auth en dev : le dev login

Faire marcher le vrai flow Google en local est possible (client OAuth avec redirect `http://localhost:8000`) mais pénible pour du test rapide. Le mode dev ajoute donc un bypass **explicitement gaté** :

```
STACKD_DEV_AUTH=true        # refusé si STACKD_ENV=production (assert au démarrage)
```

- La page login affiche, sous le bouton Google, un panneau "Dev login" avec **trois personas** couvrant les permissions de §2.4 : `admin@dev.local` (admin, tier prod, destroy), `alice@dev.local` (approver, tier prod), `bob@dev.local` (writer, tier staging — **ne peut pas confirmer prod**). Un clic = session.
- Trois personas avec des tiers distincts : indispensable pour tester le cas « apply partout sauf prod » (bob confirme dev/staging, refusé sur prod), le 4-eyes prod (bob déclenche, alice confirme) et l'audit ("qui a apply quoi" n'a de sens qu'à plusieurs).
- Les sessions, rôles et audit events sont strictement les mêmes qu'avec Google — seul le `google_sub` est synthétique (`dev:admin`).
- Le vrai flow Google reste testable en dev en renseignant `GOOGLE_CLIENT_ID/SECRET` (les deux coexistent).

Garde-fou : le build de prod de l'image API **supprime le module dev_auth** (pas seulement le flag) — l'oubli de configuration ne peut pas exposer le bypass.

---

## 4. Repos Git fixtures

`task seed` crée dans `.dev/repos/` deux dépôts Git **locaux et committables** :

```
.dev/repos/demo-network/      # stack amont
  main.tf                     #   random_pet + local_file, outputs: network_name, cidr
  outputs.tf
  .stackd.yml                 #   hook after_plan d'exemple (jq sur plan.json, mode warn)
.dev/repos/demo-app/          # stack avale
  main.tf                     #   local_file qui consomme TF_VAR_network_name
  variables.tf
```

- Les stacks de seed pointent dessus en `repo_url: file:///repos/demo-network` (le volume est monté dans l'API et le worker). `repo_auth_kind: none`.
- **Simuler "une PR est mergée"** sans GitHub : `task push-change` commite une modification dans demo-network → le polling de staleness (réduit à **15 s** en dev via `STACKD_HEAD_POLL_INTERVAL`) fait apparaître le chip `↑1`. Parfait pour tester le scénario "apply à 9h, merge à 9h15".
- **Webhooks réels** : `task dev-git` lance Gitea, `task seed-gitea` y pousse les fixtures et configure le webhook vers l'API. Sinon, `task webhook` envoie un payload push signé HMAC via curl — suffisant pour développer le handler.

---

## 5. Worker en dev

- Lancé par compose avec `STACKD_RUNNER=local` : les commandes terraform s'exécutent directement dans le conteneur worker (image incluant OpenTofu + jq + git). **Pas de Docker-in-Docker** — la complexité du runner `docker` se teste séparément (`task test-runner-docker`, nécessite le socket).
- Binaire outil : OpenTofu pré-installé dans l'image dev (pas de téléchargement au premier run).
- Hot reload de l'agent (watchfiles) : modifier `worker/` relance la boucle de poll proprement (le job en cours termine).
- Multi-workers pour tester la concurrence et l'affinité : `docker compose up --scale worker=3`.

---

## 6. Terraform sans AWS (et avec, si besoin)

**Par défaut** : les fixtures n'utilisent que `random`, `null`, `local_file` et `time` — un apply crée des fichiers dans le workspace, zéro credential, zéro coût, exécution en secondes. Suffisant pour tester **toute la plateforme** (la plateforme orchestre terraform, elle ne dépend pas de ce que terraform crée).

**Profil `aws` (LocalStack)** : pour tester des stacks AWS-réalistes (S3, SQS, IAM...), `task dev-aws` + le variable set `localstack` seedé (endpoints overrides). Limites connues : LocalStack ne couvre pas tout et **ne valide pas l'OIDC workload** (STS AssumeRoleWithWebIdentity y est superficiel).

**OIDC workload en dev** : l'issuer tourne (JWKS sur `localhost:8000/oidc/jwks`, tokens signés au claim) — on teste la **génération et les claims** des tokens (tests unitaires + `task show-token` qui décode le JWT d'un run). L'échange STS réel se teste contre un vrai compte AWS sandbox, pas en local : documenté comme hors périmètre du mode dev.

---

## 7. Seed et scénario de démo

`task seed` crée (idempotent) :

```
space "demo"
├── variable set common        (TF_VAR_org=demo, auto_attach)
├── variable set region-local  (TF_VAR_region=local-1)
├── stack demo-network  → envs: dev (tier dev), prod (tier prod, protected, 4-eyes)
├── stack demo-app      → envs: dev (tier dev), prod (tier prod, protected)
├── dépendances : network/dev → app/dev, network/prod → app/prod
│     output_references: network_name → TF_VAR_network_name
│                        (mock: "mock-network")
└── worker pool "local" (token écrit dans .dev/, consommé par le worker compose)
```

`task e2e` rejoue le parcours complet en API (et sert de test de non-régression) :

```
 1. login bob → trigger network/dev            → plan → unconfirmed
 2. bob confirme (tier dev, ok)                 → apply → finished
 3. cascade automatique                         → app/dev plan (input réel injecté)
 4. trigger app/prod AVANT network/prod         → plan avec MOCK (badge violet)
 5. tentative de confirm du run mocké           → refus attendu (allow_mock_apply=false)
 6. trigger network/prod (bob) → bob tente de confirmer → refus (tier staging < prod)
 7. alice confirme network/prod (tier prod, 4-eyes ok) → cascade app/prod
 8. task push-change                            → chip ↑1 sous 15 s
 9. vérification des audit events               → triggered/confirmed/applied
                                                   avec les bons acteurs et tiers
```

Si les 9 étapes passent, le cœur du produit fonctionne. C'est aussi le script de démo à dérouler devant quelqu'un.

---

## 8. Confort de dev

- **API** : OpenAPI/Swagger sur `/docs` (désactivé en prod), logs **JSON structurés par défaut** (`STACKD_LOG_FORMAT=pretty` pour une lecture locale ; `STACKD_LOG_LEVEL=DEBUG` pour aussi capter lectures/polls/heartbeats), erreurs avec traceback. Buffer consultable via `/api/v1/logs` + page **Workers & health**.
- **Timings raccourcis** en dev : heartbeat 5 s, offline 15 s, poll staleness 15 s, affinité apply 10 s — les comportements asynchrones se testent sans attendre.
- **Front** : MSW (Mock Service Worker) optionnel pour développer l'UI sans backend (`pnpm dev:mock`) — les handlers rejouent des fixtures de runs/logs, y compris un run "en cours" qui streame. Utile pour itérer sur la visionneuse et le rail sans déclencher de vrais runs.
- **Storybook/Ladle** : `pnpm storybook` — les composants identitaires (PhaseRail, StateBadge, LogViewer avec fixture ANSI) se développent isolément.
- **DB** : `task psql` (shell), `task db-reset` (drop + migrations + seed). Le stockage S3 local (Garage) s'inspecte via la CLI `aws s3 --endpoint-url http://localhost:3900` ou `task s3-ls` ; pas de console web (Garage s'administre en CLI).
- **Données de logs réalistes** : la fixture demo-network inclut une ressource `time_sleep` de 10 s — les runs durent assez longtemps pour voir le streaming, le follow-tail et l'annulation.

---

## 9. Ce que le mode dev ne couvre pas (assumé)

| Hors périmètre dev local | Où ça se teste |
|---|---|
| Échange STS réel (OIDC workload) | compte AWS sandbox + module Terraform fourni |
| Runner Docker (isolation par conteneur) | `task test-runner-docker` (socket requis) ou CI |
| Webhooks GitHub/GitLab réels | Gitea (profil git) couvre 95 % ; le reste en staging |
| Charge (N workers × M runs) | scénario k6 en CI, pas en local |
| Thème clair / responsive mobile | Storybook + revue manuelle |
