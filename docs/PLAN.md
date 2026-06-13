# PLAN.md — Plateforme d'orchestration Terraform (type Spacelift, version simplifiée)

> Nom de code projet : **Stackd** (placeholder, à renommer)
> Objectif : une plateforme self-hostable d'orchestration Terraform avec stacks multi-environnements, variable sets, runs, workers, hooks, credentials cloud dynamiques (OIDC), audit complet et dépendances inter-stacks.

---

## 1. Vision et périmètre

### 1.1 Ce qu'on construit (MVP)

Une plateforme qui permet de :

1. Déclarer des **stacks** : un repo Git + un sous-dossier = une unité d'infrastructure, déclinée en **environnements** (dev, staging, prod) ayant chacun leur state Terraform et leurs variables.
2. Factoriser la configuration via des **variable sets** : ensembles réutilisables de variables attachables à N stacks/environnements (inspiré des Contexts Spacelift / Variable Sets HCP).
3. Déclencher des **runs** (plan / apply) par environnement, manuellement ou via webhook Git.
4. Exécuter ces runs sur des **workers** distants (agents auto-hébergés, modèle pull) avec **logs streamés en direct** dans l'UI.
5. Personnaliser le cycle de vie via des **hooks** (commandes avant/après init/plan/apply, déclarées en YAML) avec checks bloquants ou soft-fail.
6. Fournir des **credentials cloud dynamiques par OIDC** : la plateforme signe un token d'identité par run, échangé contre un rôle IAM — zéro credential statique, ni côté plateforme, ni côté worker.
7. **Auditer** : qui a déclenché, confirmé, appliqué quoi, quand, sur quel environnement — trail immuable et consultable.
8. Gérer les **states dans S3**, exposés à Terraform via le **backend HTTP** de la plateforme (locking + tokens scoped), avec mode "bring your own S3 backend".
9. Gérer un **graphe de dépendances** entre environnements avec propagation des outputs et **mock outputs** pour le bootstrap (inspiré de Terragrunt).
10. S'authentifier avec un **compte Google** (OIDC), restreint au domaine de l'organisation.

### 1.2 Ce qu'on ne construit PAS dans le MVP (anti-scope)

| Hors scope MVP | Raison | Phase future |
|---|---|---|
| Policies OPA/Rego | Les hooks + checks couvrent 80 % des besoins de gouvernance v1 | Phase 7+ |
| Run tasks HTTP (webhooks externes bloquants type Infracost SaaS) | Les hooks par commande suffisent (Infracost CLI s'exécute en hook) | Phase 7 |
| Drift detection schedulée | Nécessite un scheduler robuste | Phase 7+ |
| Multi-IaC (Pulumi, CloudFormation) | Terraform/OpenTofu only | Jamais peut-être |
| SSO SAML / autres IdP que Google | Google OIDC suffit | Phase 7 |
| Worker pool public/partagé | Workers privés uniquement | Jamais (positionnement self-hosted) |
| OIDC vers GCP/Azure | AWS d'abord (audience cible), interface générique prévue | Phase 7 |
| Module registry privé, no-code provisioning | Gros chantier, faible valeur sans utilisateurs | — |
| Export SIEM de l'audit | L'audit DB + UI suffit | Phase 7 |

### 1.3 Principes directeurs

- **Pull, pas push** : les workers tirent les jobs depuis l'API.
- **L'API est la seule source de vérité** : workers stateless et jetables.
- **State machine explicite** pour les runs : chaque transition est un événement persisté → base du système d'audit.
- **Auditabilité par construction** : toute action mutante écrit un événement d'audit immuable.
- **Environnement = unité d'exécution** : stack = template (repo + code), environnement = instance (state + variables + protections). Un run appartient à un environnement.
- **Zéro credential statique comme objectif** : OIDC pour les humains (Google), OIDC pour les workloads (rôles IAM par run). Les secrets statiques restent possibles (variable sets sensibles) mais sont le fallback, pas la norme.
- **Configuration par couches** : variable set → stack → environnement, chaque couche pouvant écraser la précédente. Même logique pour les hooks.
- **Blast radius minimal** : un environnement = un state = un scope. Dépendances par outputs explicites uniquement.

---

## 2. Architecture cible (vue d'ensemble)

```
┌─────────────┐         ┌───────────────────────────────────┐
│   Front     │  HTTPS  │               API                 │
│  React/Vite │────────▶│             FastAPI               │
│  (SPA)      │  + WS   │                                   │
└─────────────┘         │  ┌────────┐  ┌─────────────────┐  │
       ▲                │  │ REST   │  │ Scheduler       │  │
       │ OIDC           │  │ /api   │  │ (DAG, queue)    │  │
┌──────┴──────┐         │  └────────┘  └─────────────────┘  │
│   Google    │  tokens │  ┌────────┐  ┌─────────────────┐  │
│  Identity   │────────▶│  │ Worker │  │ State HTTP      │  │
└─────────────┘         │  │ API    │  │ backend (S3)    │  │
                        │  └────────┘  └─────────────────┘  │
┌─────────────┐ webhook │  ┌────────┐  ┌─────────────────┐  │
│ GitHub /    │────────▶│  │ Audit  │  │ OIDC issuer     │  │
│ GitLab      │         │  └────────┘  │ (JWKS, tokens   │  │
└─────────────┘         │              │ workload / run) │  │
                        │              └─────────────────┘  │
┌─────────────┐  poll   └───────┬──────────────┬────────────┘
│  Worker 1   │────────▶ ┌──────▼─────┐ ┌──────▼─────┐
│  (agent)    │          │ PostgreSQL │ │     S3     │
├─────────────┤          │ (état app, │ │ (tfstate,  │
│  Worker 2   │──┐       │  audit)    │ │  logs,     │
└─────────────┘  │       └────────────┘ │  artifacts)│
                 │                      └────────────┘
                 │ AssumeRoleWithWebIdentity
                 │ (token workload signé par l'API)
                 ▼
          ┌────────────┐
          │  AWS STS   │──▶ credentials temporaires scoped au run
          └────────────┘
```

### 2.1 Composants

| Composant | Techno | Rôle |
|---|---|---|
| **Front** | React 19 + Vite 7 + TypeScript, TanStack Query, Tailwind v4 | SPA — design spécifié dans **DESIGN.md** |
| **API** | FastAPI (Python 3.13+), Pydantic v2, SQLAlchemy 2 async | REST + WS, auth Google, webhooks, worker API, state backend, audit, **issuer OIDC workload** |
| **Scheduler** | Module interne de l'API | DAG, queue, propagation des outputs |
| **Workers** | Agent Python (tout le MVP) ; réécriture Go = piste post-MVP, hors phases 0–6 | Poll, clone, hooks, terraform, échange OIDC→STS, streaming logs |
| **DB** | PostgreSQL 18 | Tout l'état applicatif + queue (`SKIP LOCKED`) |
| **Object storage** | S3 (Garage en dev) | tfstate versionné, logs archivés, artifacts |

### 2.2 Décision : states dans S3, exposés via backend HTTP

- **Stockage physique : S3** (durabilité, versioning, lifecycle, SSE-KMS).
- **Interface Terraform : backend HTTP** de la plateforme, qui écrit/lit dans S3 :
  1. **Credentials** : tokens scoped par run (RO pour les PR), aucun droit IAM sur le bucket de states distribué aux workers.
  2. **Locking** en Postgres, visible dans l'UI, force-unlock en un clic (audité).
  3. **Audit & versions** : chaque écriture liée au run qui l'a produite.
  4. **Contrôle** : refus de serial régressif, rétention pilotée.
- **Mode compatibilité** : `managed_state: false` pour les backends S3 existants.

### 2.3 Décision : credentials cloud dynamiques (OIDC workload identity)

La plateforme devient un **émetteur OIDC** (le même mécanisme que GitHub Actions OIDC) :

- L'API expose `/.well-known/openid-configuration` + JWKS publics.
- À chaque claim de job, l'API signe un **token workload** avec des claims riches : `sub=run:{env}:{stack}:{phase}`, `environment`, `stack`, `run_id`, `phase` (plan/apply), TTL court.
- Côté AWS : un Identity Provider OIDC + des rôles IAM dont la trust policy filtre sur ces claims. Exemple : le rôle apply de prod n'est assumable que si `sub` matche `run:prod:*:apply`.
- Le worker écrit le token dans un fichier et exporte `AWS_WEB_IDENTITY_TOKEN_FILE` + `AWS_ROLE_ARN` : les providers AWS le consomment nativement, **zéro code spécifique dans le code Terraform des utilisateurs**.
- Conséquences : plan et apply peuvent assumer des rôles différents (plan = ReadOnly + s3 modules, apply = droits d'écriture), un plan de PR ne peut physiquement pas modifier l'infra, et la rotation de credentials disparaît du modèle.

---

## 3. Phases d'implémentation

### Phase 0 — Fondations + Auth Google (1,5 semaine)

- [ ] Monorepo : `api/`, `worker/`, `front/`, `deploy/`, `docs/`
- [ ] `docker-compose.yml` dev : Postgres, Garage (S3 local), API hot-reload, front Vite — **mode dev complet spécifié dans DEV.md** (dev login 3 personas, repos fixtures `file://`, seed + scénario e2e, timings raccourcis)
- [ ] FastAPI : healthcheck, settings, modules (`auth/`, `stacks/`, `environments/`, `variable_sets/`, `runs/`, `workers/`, `audit/`, `oidc/`, ...)
- [ ] **Auth Google OIDC** (Authorization Code + PKCE) : flow complet, upsert sur `google_sub`, restriction `hd` au domaine, sessions JWT + refresh rotatif (table `refresh_tokens`, détection de réutilisation → révocation de famille, SPECS §2.5), CSRF sur `/auth/refresh`, bootstrap premier admin
- [ ] Migrations Alembic, modèles `User`, `RefreshToken`, **`Space` (space `default` créé au bootstrap, SPECS §3.0)** — toutes les FK `space_id` en dépendent dès Phase 1
- [ ] Front : scaffold + **mise en place du design system de DESIGN.md** (tokens, thème, composants de base) + **Storybook/Ladle** des composants identitaires (PhaseRail, StateBadge, ProvenanceBadge...) — le contrat visuel exigé par DESIGN.md §8, page login
- [ ] CI GitHub Actions, `CLAUDE.md` racine

**Livrable : `docker compose up` → Sign in with Google → coquille de l'app au design final.**

---

### Phase 1 — Stacks + Environnements + Variable sets (3 semaines)

**Objectif : le modèle de configuration en couches complet.**

- [ ] Modèle `Stack` (template : repo, project_root, tool, version)
- [ ] Modèle `Environment` (instance : **tier dev/staging/prod**, branche, autodeploy, protected, 4-eyes, managed_state, labels, position)
- [ ] **Permissions d'apply par tier** (voir SPECS §2.4) : `users.max_apply_tier` + `users.can_destroy` ; helper `can_apply(user, env)` appelé dans la transition `unconfirmed → confirmed` ; `protected` recentré sur ses effets propres (confirmation forcée + 4-eyes), le contrôle d'accès passant au tier ; page admin Users (rôle, tier, destroy) + audit des changements
- [ ] **Variable sets** (voir SPECS §3.4) :
  - ensembles nommés de variables (terraform + environment), au niveau space
  - attachables à des stacks (→ tous leurs envs) ou à des environnements précis
  - `auto_attach: true` = attaché à toutes les stacks du space (ex. `common-aws`)
  - priorité d'attachement pour ordonner les sets entre eux
  - résolution finale : **variable sets (par priorité) < stack < environnement**
  - UI : badge de provenance sur chaque variable résolue ("héritée de `common-aws`", "écrasée ici")
- [ ] Variables stack + overrides env (modèle existant, intégré à la résolution)
- [ ] Intégration Git par token/deploy key (chiffré), endpoint check-repo
- [ ] Chiffrement AES-256-GCM des valeurs sensibles (write-only)
- [ ] Front : liste stacks × envs, wizard de création, page stack, **page Variable Sets** (CRUD + liste des attachements + "où est utilisé ce set")
- [ ] Audit : CRUD stacks/envs/variables/variable sets + attachements

**Livrable : un set `common-aws` (région, tags par défaut, token Datadog) attaché à 3 stacks, surchargé ponctuellement par un env.**

---

### Phase 2 — Runs + Workers + Logs + Hooks (3,5 semaines) ⭐ phase critique

#### 2a. State machine des runs
- [ ] Modèle `Run` (par environnement) + `RunEvent`
- [ ] États : `queued → preparing → planning → [checking] → unconfirmed → confirmed → applying → finished` (+ `failed`, `discarded`, `canceled`)
- [ ] Trigger manuel, confirm/discard (gardé par `can_apply` : tier + rôle, voir §2.4 ; 4-eyes auto sur tier prod)
- [ ] Concurrence : 1 run actif par environnement

#### 2b. API Worker (protocole pull)
- [ ] Register/heartbeat/claim (`SKIP LOCKED`), events, logs chunkés, artifacts
- [ ] Détection worker mort → `worker_lost`

#### 2c. L'agent worker
- [ ] Boucle claim → clone → setup outil → **hooks** → init → plan → upload → confirmation → **hooks** → apply → report
- [ ] Workspace éphémère, runner Docker, masquage des secrets dans les logs

#### 2d. Hooks & checks (custom flows, voir SPECS §8)
- [ ] Déclaration **YAML** à deux endroits, fusionnés : fichier `.stackd.yml` dans le repo (versionné avec le code) + hooks définis au niveau stack/environnement dans l'UI (gouvernance imposée par la plateforme, non contournable par une PR)
- [ ] Points d'ancrage : `before_init`, `after_init`, `before_plan`, `after_plan`, `before_apply`, `after_apply`
- [ ] Chaque hook = une commande exécutée dans le workspace, avec accès en lecture à `plan.json` (pour les checks post-plan : tfsec, checkov, infracost, scripts maison)
- [ ] Modes d'échec : `fail` (run → failed), `warn` (continue, warning visible, confirmation manuelle forcée même si autodeploy)
- [ ] Logs des hooks intégrés à la visionneuse (sections dédiées)

#### 2e. Logs des jobs Terraform
- [ ] Live : WebSocket multiplexé, suivi ligne à ligne par phase
- [ ] Visionneuse : virtualisée, ANSI, recherche, ancres partageables, follow-tail
- [ ] Stockage deux étages (DB chaude 7 j → S3 gz 1 an), téléchargement, rétention configurable

#### 2f. Front runs
- [ ] Page run : timeline des phases (hooks inclus), visionneuse, résumé du plan, barre d'action
- [ ] Page **/queue** : runs en cours et en attente avec **raison de blocage** calculée par l'API (run actif sur l'env, env verrouillé, aucun worker compatible, réservation d'affinité apply) — cf. DESIGN.md §5.5

**Livrable : un run avec hook `after_plan: infracost breakdown` en mode warn, logs en direct, apply confirmé.**

---

### Phase 3 — States S3 via backend HTTP + Audit trail UI (2 semaines)

#### 3a. State managé
- [ ] Protocole HTTP backend complet (GET/POST/LOCK/UNLOCK/423), stockage S3 SSE-KMS
- [ ] Versioning applicatif lié aux runs, locking Postgres visible UI, force-unlock audité
- [ ] Tokens scoped par run (RO pour proposed), injection auto `-backend-config`
- [ ] Mode "bring your own S3 backend"
- [ ] Front : onglet State par env (versions, lock, download admin)

#### 3b. Audit : "qui a apply quoi"
- [ ] `audit_events` complété : trigger, confirm (identité Google), discard, apply, force-unlock, rotations, rôles
- [ ] Page /audit filtrable + export CSV, onglet Activity par env, vue par utilisateur
- [ ] Immutabilité, rétention 2 ans

**Livrable : "qui a appliqué quoi sur prod la semaine dernière, avec quel plan ?" en 10 secondes.**

---

### Phase 4 — Dépendances + outputs + mock outputs (2,5 semaines) ⭐ le différenciateur

- [ ] Dépendances entre **environnements** + helper "lier les homonymes"
- [ ] `OutputReference` (mapping output amont → variable avale), anti-cycle
- [ ] Capture des outputs après apply, hash, jamais les sensibles
- [ ] **Mock outputs** (inspiré Terragrunt, voir SPECS §9.3) :
  - chaque `output_reference` peut porter une `mock_value`
  - utilisée quand l'amont n'a **jamais produit** l'output (bootstrap d'une nouvelle cascade) ou sur les **proposed runs** si l'amont n'est pas appliqué
  - un run qui a consommé au moins un mock est marqué `used_mocks: true` : badge visible, **apply interdit** (plan-only de validation) sauf opt-in explicite par env
  - résout le problème de l'œuf et la poule : écrire `app/dev` et planifier sa config avant que `network/dev` n'ait tourné
- [ ] Scheduler de propagation : cascade topologique, politiques, multi-parents, arrêt sur échec
- [ ] Run groups + vue graphe ; la cascade ne contourne jamais les protections

**Livrable : planifier `app/dev` avec `vpc_id = "vpc-mock00000"` avant le premier apply de `network/dev`, puis cascade réelle.**

---

### Phase 5 — Webhooks Git + runs proposés (2 semaines)

- [ ] Webhook GitHub/GitLab : HMAC, mapping branche → environnements, filtrage par `project_root`
- [ ] **Retard Git (staleness)** : suivi du `head_sha` par env (webhook + polling ls-remote 15 min + refresh manuel), chip `↑N` sur les envs en retard, bandeau "plan obsolète" + re-plan sur les runs unconfirmed dépassés — voir SPECS §9.6 / DESIGN §5.1-5.2
- [ ] Push → runs tracked ; PR → **proposed runs** plan-only (state RO, secrets non injectés par défaut, mocks autorisés)
- [ ] (Optionnel) Commentaire de PR : résumé du plan + résultats des checks (hooks warn/fail)

**Livrable : `git push` → plans automatiques + checks visibles dans la PR.**

---

### Phase 6 — Credentials cloud dynamiques OIDC (1,5 semaine)

- [ ] **Issuer OIDC** : `/.well-known/openid-configuration`, JWKS (clés RS256, rotation), signature des tokens workload au claim
- [ ] Modèle `CloudIntegration` par environnement : provider `aws`, `plan_role_arn`, `apply_role_arn` (voir SPECS §10)
- [ ] Agent : écriture du token, export `AWS_WEB_IDENTITY_TOKEN_FILE`/`AWS_ROLE_ARN` (ou AssumeRoleWithWebIdentity explicite + export des 3 variables si besoin de compat)
- [ ] Documentation + module Terraform fourni : créer l'Identity Provider AWS + des rôles d'exemple avec trust policies filtrées sur les claims
- [ ] UI : config de l'intégration par env, indicateur "credentials dynamiques" vs "variables statiques", test de l'AssumeRole
- [ ] Audit : `cloud_integration.created/updated`, rôle assumé tracé dans le contexte du run

**Livrable : un env prod sans aucun secret AWS stocké nulle part — le plan assume un rôle ReadOnly, l'apply un rôle d'écriture, trust policy à l'appui.**

---

### Phase 7 — Production-ready (continu)

- [ ] RBAC par space, mapping groupes Google — étend les permissions par tier (§2.4) vers des périmètres par space/équipe, et permet l'env « sensible mais pas prod » que le tier linéaire ne couvre pas
- [ ] **Environment matrix** (piste, non specifiée) : déclarer `{eu-west-1, us-east-1} × {dev, prod}` sur une stack et générer/synchroniser les environnements correspondants — le multi-région se fait par convention de nommage + variable sets au MVP
- [ ] Run tasks HTTP (webhooks externes bloquants), policies avancées (OPA) si besoin réel
- [ ] OIDC vers GCP/Azure (interface `CloudIntegration` déjà générique)
- [ ] Drift detection schedulée
- [ ] Export audit, rétention/purge automatisées
- [ ] Observabilité plateforme (Prometheus, OTel, dashboard Datadog)
- [ ] Helm chart / module Terraform de déploiement (dogfooding)
- [ ] **Réécriture Go du worker** (binaire unique, distribution facilitée) — l'agent Python du MVP reste la référence fonctionnelle ; le port Go ne change pas le protocole (§7)

---

## 4. Estimation globale

| Phase | Durée (1 dev, temps partiel réaliste) |
|---|---|
| 0 — Fondations + Google OIDC | 1,5 semaine |
| 1 — Stacks + Envs + Variable sets | 3 semaines |
| 2 — Runs + Workers + Logs + Hooks | 3,5 semaines |
| 3 — State S3/HTTP + Audit UI | 2 semaines |
| 4 — Dépendances + outputs + mocks | 2,5 semaines |
| 5 — Webhooks + proposed runs | 2 semaines |
| 6 — OIDC workload credentials | 1,5 semaine |
| **Total MVP démontrable** | **~16 semaines** |

> Jalon intermédiaire : fin de Phase 2 (≈ 8 semaines) = produit utilisable en solo avec logs live et hooks. Phases 3–6 = les différenciateurs (audit, cascade+mocks, zéro credential).

---

## 5. Risques et décisions ouvertes

| Risque / décision | Impact | Mitigation / position actuelle |
|---|---|---|
| Sécurité de l'exécution (providers/provisioners = code arbitraire) | Élevé | Workers self-hosted, Docker par run, pas de multi-tenant. Les hooks plateforme (non contournables) sont le garde-fou de gouvernance |
| Hooks du repo (`.stackd.yml`) modifiables par PR | Moyen | Les hooks **plateforme** (stack/env) s'exécutent toujours et ne sont pas contournables ; les checks de sécurité critiques vont là, pas dans le repo |
| Issuer OIDC : compromission de la clé de signature = accès à tous les rôles | Élevé | Clés en KMS (signature `kms:Sign`, clé jamais en mémoire) ou volume chiffré, rotation avec chevauchement, JWKS avec kid, TTL tokens ≤ durée du run, claims précis dans les trust policies (wildcard `sub` toléré sur le seul segment stack, jamais tier/phase) |
| Issuer OIDC et state backend dans le même process API | Moyen | Au MVP, toutes les surfaces (API humaine, worker API, state backend, issuer OIDC, webhooks) partagent un process → une faille sur l'une approche la clé de signature. Mitigation forte = KMS (la clé privée ne réside jamais dans le process). Isolation du signataire dans un service dédié = piste Phase 7 si le modèle de menace l'exige |
| Mock outputs appliqués par erreur | Moyen | `used_mocks` → apply interdit par défaut, badge UI très visible, opt-in par env uniquement |
| Dépendance à Google pour l'auth | Moyen | Interface `AuthProvider` abstraite, autres IdP en Phase 7 |
| Explosion des arêtes de dépendance par env | Faible | Helper homonymes + conventions de nommage |
| Postgres comme queue à grande échelle | Faible (MVP) | Interface `JobQueue` abstraite |
| Licence Terraform (BUSL) | Moyen | **OpenTofu first**, Terraform en option utilisateur |

---

## 6. Socle technique — versions de référence et améliorations débloquées

Versions cibles (juin 2026). Même choix de techno qu'à l'origine, version courante. Le tableau lie chaque bump à ce qu'il **débloque concrètement** dans Stackd.

| Composant | Initial | Cible | Ce que ça débloque (et où) |
|---|---|---|---|
| **PostgreSQL** | 16 | **18** (GA 25/09/2025) | `uuidv7()` natif → `DEFAULT` côté DB, fin de la génération applicative d'ID (SPECS §1). I/O asynchrone (seq scans / vacuum 2-3×) → scans d'audit et `run_logs` plus rapides (mettre `io_method=worker`/`io_uring`). `RETURNING` ancien+nouveau tuple → `transition()` émet le `run_event` `from→to` en une requête (SPECS §4.2). OAUTHBEARER et colonnes générées virtuelles : non requis au MVP |
| **Python** | 3.12 | **3.13** (3.14 adoptable) | meilleurs messages d'erreur, typing affiné, REPL. Free-threading (`python3.14t`, officiel en 3.14) **non pertinent** pour une API async I/O-bound — à n'envisager que si le worker devient CPU-bound (peu probable : il délègue à terraform) |
| **React** | 18 | **19** (GA début 2025) | Actions + `useActionState` → gestion native pending/erreur des formulaires, sert directement l'état `loading` des actions (DESIGN §7) et les wizards (DESIGN §5.7). `ref` comme prop → moins de `forwardRef` dans les 7 composants identitaires. Metadata document → `title` par run partageable. **À éviter : `useOptimistic` sur l'état des runs** — l'invariant est que les events WS *invalident* les queries, ils ne patchent pas le cache (DESIGN §6). **React Compiler** : opt-in à évaluer (auto-mémoïsation utile sur rail/logs très rafraîchis), pas un prérequis |
| **Vite** | (non figé) | **7.x** | Node 20.19+/22.12+. Tailwind v4 via le plugin **`@tailwindcss/vite`** (pas PostCSS — évite le conflit connu) |
| **Tailwind** | v4 | **v4.1.x** | déjà le bon major ; intégration `@tailwindcss/vite`. Aucun changement de contrat (tokens CSS, DESIGN §8) |
| **OpenTofu** (image dev) | (non figé) | **1.12.x** | l'image dev pré-installe une version récente ; `tool_version` reste piloté **par stack** (SPECS §3.1, choix utilisateur). 1.10 a introduit la **distribution OCI** des modules/providers — piste si on héberge des modules privés (sinon hors scope) |

**Inchangés (déjà au bon major)** : FastAPI, Pydantic v2, SQLAlchemy 2 async, Alembic, uv, TanStack Query v5, react-flow + dagre, react-virtuoso, anser, pnpm.

**Règle de mise à jour** : ces cibles sont des **planchers** au démarrage, pas une politique de suivi continu. Pas de bump de major en cours de phase sans raison (une faille, une feature requise) ; les minors/patches suivent `uv.lock` / `pnpm-lock.yaml` committés. Le seul bump à effet structurel sur le code déjà spécifié est **PG16→18** (ID natifs + `RETURNING`), reflété dans SPECS §1 et §4.2.
