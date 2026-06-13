# SPECS.md — Spécifications techniques détaillées

> Compagnon de PLAN.md. Spécifie : **auth Google OIDC**, **modèle de données (stacks → environnements, variable sets)**, **state machine**, **logs**, **audit**, **hooks**, **protocole worker**, **state S3 via backend HTTP**, **dépendances + mock outputs**, **credentials cloud dynamiques OIDC**.

---

## 1. Conventions générales

- IDs : UUIDv7. **PostgreSQL 18** fournit `uuidv7()` natif → `DEFAULT uuidv7()` côté DB (ordre temporel des index préservé) ; la génération applicative reste possible pour fixer l'ID avant insert, jamais `gen_random_uuid()` (UUIDv4 non monotone).
- Timestamps : `timestamptz` UTC, suffixe `_at`.
- API REST : JSON, `/api/v1` (humains), `/worker/v1` (agents). Erreurs RFC 9457.
- Secrets au repos : AES-256-GCM, clé maître `STACKD_ENCRYPTION_KEY`. **Nonce 96 bits aléatoire par chiffrement**, stocké avec le ciphertext (`nonce || ciphertext || tag`) — jamais de réutilisation de nonce avec la même clé (perte de confidentialité GCM).
- Terraform/OpenTofu : invoqué exclusivement via CLI par le worker.

---

## 2. Authentification — Google OIDC

### 2.1 Flow

Authorization Code + PKCE, l'API est le client confidentiel :

```
1. Front  → GET /api/v1/auth/google/start  (state + nonce + verifier en session signée)
2. Front  → redirection accounts.google.com (scopes: openid email profile)
3. Google → /api/v1/auth/google/callback?code&state
4. API    → échange code+verifier, valide l'id_token (JWKS, iss, aud, exp, nonce)
5. API    → admission : email_verified == true, hd ∈ STACKD_ALLOWED_DOMAINS → sinon 403
6. API    → upsert User sur google_sub (stable, contrairement à l'email)
7. API    → session : access JWT 15 min (mémoire front, jamais en browser storage)
            + refresh httpOnly 14 j, persisté dans `refresh_tokens` (§2.5) avec
            rotation et détection de réutilisation → révocation de la famille
8. Front  → GET /api/v1/me
```

**CSRF** : l'access token voyage en header `Authorization: Bearer` (pas de risque CSRF sur les appels API). Le refresh repose sur un cookie httpOnly → `/auth/refresh` et `/auth/logout` exigent `SameSite=Strict` **et** un double-submit token CSRF (le seul couple d'endpoints porté par cookie). Cookie `Secure`, `Path=/api/v1/auth`.

Bootstrap : premier utilisateur d'un domaine autorisé = `admin`, suivants = `reader`. Pas d'auth locale ; interface interne `AuthProvider` pour d'autres IdP plus tard. Login/logout/refus → `audit_events`.

### 2.2 Table `users`

```
id uuid PK, google_sub text unique, email text, display_name, avatar_url,
role enum(admin|approver|writer|reader),    -- capacités globales (§2.3)
max_apply_tier enum(dev|staging|prod) nullable,  -- tier max où l'user peut confirmer un apply (§2.4)
can_destroy bool default false,             -- droit de déclencher/confirmer un run destroy (§2.4)
disabled bool, last_login_at, created_at
```

### 2.3 Rôles (capacités globales)

Le `role` porte ce qu'un utilisateur peut faire **en nature** (lire, gérer la config, administrer). *Sur quels environnements* il peut appliquer relève du tier (§2.4), pas du rôle.

| Action | reader | writer | approver | admin |
|---|---|---|---|---|
| Voir stacks/runs/logs/audit | ✅ | ✅ | ✅ | ✅ |
| Trigger un plan (tout env) | | ✅ | ✅ | ✅ |
| Confirmer un apply | | | ✅ | ✅ | ← **sous réserve du tier (§2.4)** |
| Gérer stacks/envs/variables/variable sets/hooks | | ✅ | ✅ | ✅ |
| Workers, force-unlock, rôles, cloud integrations, settings | | | | ✅ |

Trigger d'un plan ≠ confirmer : tout writer+ peut **préparer** un plan sur n'importe quel env (un plan ne change rien), y compris prod. Seule la **confirmation** de l'apply est gardée par le tier — ce qui laisse un writer monter un plan prod qu'un approver habilité confirmera.

### 2.4 Permissions par environnement — tier & destroy

Le besoin « apply partout sauf prod » dépend de l'environnement visé, pas seulement de l'utilisateur. On l'exprime par un **tier** sur l'environnement et un **plafond** sur l'utilisateur, plutôt que par un système de policies complet (suffisant pour la grande majorité des orgs ; le RBAC par space reste Phase 7).

- `environments.tier` enum(`dev`|`staging`|`prod`) — ordre implicite `dev < staging < prod`.
- `users.max_apply_tier` — tier maximum où l'utilisateur peut **confirmer un apply**. NULL = aucun (peut lire et planifier, jamais appliquer).
- Règle d'apply : `confirm` autorisé ssi `role ∈ {approver, admin}` **ET** `max_apply_tier >= env.tier`. Exemple : Bob `max_apply_tier=staging` confirme en dev/staging, refusé en prod ; Alice `prod` confirme partout.
- `users.can_destroy` — un run `type=destroy` (trigger ET confirm) exige `can_destroy=true` **en plus** de la règle de tier. Une destruction est plus dangereuse qu'un apply : droit distinct, explicite.

**Relation avec `protected`** : on **dissocie** sensibilité et droit d'accès, jusqu'ici mélangés. `environments.protected` ne porte plus que ses effets propres — forcer la confirmation (pas d'autodeploy) et activer le 4-eyes ; *qui* peut confirmer vient désormais du tier. Conséquence : un env peut être `tier=prod` sans être `protected` (apply restreint mais autodeploy possible pour les habilités) et inversement.

**Cohérence 4-eyes / tier** : pour les environnements `tier=prod`, l'auto-confirmation est interdite par défaut (le triggerer ≠ le confirmeur), que `require_second_pair_of_eyes` soit coché ou non — la règle découle du tier au lieu d'être un flag à maintenir partout.

**Portée du 4-eyes** : la règle « triggerer ≠ confirmeur » ne mord que sur les runs à triggerer **humain** (`triggered_by=manual`, `trigger_user_id` renseigné). Un run sans humain à l'origine (`webhook`, `dependency`) n'a pas de `trigger_user_id` : n'importe quel confirmeur habilité au tier peut le confirmer (il n'y a personne à opposer). Ce n'est pas un contournement — la garde d'accès reste le tier (`can_apply`) ; le 4-eyes ne fait qu'empêcher *une même personne* de déclencher **et** confirmer.

**Frontière à double verrou** : quand l'OIDC workload est actif (§10), la restriction d'apply prod doit **aussi** vivre dans la trust policy AWS (claim `sub` filtré sur `run:prod:*:apply`), pas seulement dans Stackd — sinon un contournement de l'API contourne le contrôle. Les deux couches expriment la même règle ; le `tier` alimente le `sub` du token (§10.2).

**Limite assumée** : le tier est linéaire (droits emboîtés : qui peut prod peut tout). Un env « sensible mais pas prod » (sandbox client, conformité) ne s'exprime pas proprement et justifierait alors des permissions par env explicites — hors MVP.

Option par env `require_second_pair_of_eyes` : le triggerer ne peut pas confirmer (redondant avec la règle de tier prod, utile pour staging).

### 2.5 Table `refresh_tokens`

La rotation avec détection de réutilisation (§2.1, étape 7) impose une persistance des refresh tokens par **famille** :

```sql
refresh_tokens (
  id uuid PK,                          -- = jti du refresh JWT
  user_id FK,
  family_id uuid,                      -- une famille par login ; révoquée en bloc si réutilisation
  parent_id uuid nullable,             -- jeton dont celui-ci est issu (chaîne de rotation)
  token_hash text,                     -- SHA-256 du token (jamais le token en clair)
  used_at timestamptz nullable,        -- posé à la rotation ; un 2e usage = réutilisation détectée
  revoked_at timestamptz nullable,
  expires_at, created_at,
  UNIQUE (token_hash)
)
```

À chaque `/auth/refresh` : le token présenté doit exister, non révoqué, non expiré, `used_at IS NULL`. On le marque `used_at`, on émet un nouveau token (même `family_id`, `parent_id = id`). Si un token déjà `used_at` est représenté → **réutilisation** : révocation de toute la famille + `audit auth.refresh_reuse_detected`. Purge des familles expirées en tâche périodique (§7.5).

---

## 3. Modèle de données

### 3.0 `spaces` — le conteneur racine

Premier niveau de la hiérarchie (breadcrumb `space / stack / env / run`, DESIGN §4) et parent de `stacks`, `variable_sets`, `worker_pools`. Le **RBAC par space** est repoussé en Phase 7 ; au MVP la table existe et le bootstrap crée un space `default` auquel tout est rattaché. Pas de **CRUD multi-space exposé** ni de mapping de groupes Google avant le RBAC (le seed/dev peut insérer d'autres spaces directement — ex. `demo` dans DEV §7 — ce n'est pas l'API publique). Toutes les FK `space_id` pointent sur un space dès Phase 1 — l'entité n'est pas optionnelle, seul son CRUD l'est.

```sql
spaces (
  id uuid PK,
  name text unique,                 -- 'default' au MVP
  description text,
  created_at, updated_at
)
```

### 3.1 `stacks` — le template (repo + code)

```
id uuid PK, space_id FK, name text unique(space), description,
repo_url, repo_auth_kind enum(none|token|deploy_key), repo_secret_encrypted,
webhook_secret_encrypted bytea nullable,   -- §5/§9.6 : secret HMAC du webhook entrant
project_root text default '.',
tool enum(opentofu|terraform), tool_version text,
created_at, updated_at
```

> Ni branche, ni state, ni autodeploy : tout descend dans l'environnement.

> **Webhook entrant & repo partagé** : un repo peut servir plusieurs stacks (`project_root` distincts). Le webhook GitHub/GitLab étant configuré **par repo** avec un secret unique, `webhook_secret_encrypted` est partagé entre ces stacks (même valeur). À la réception (`POST /api/v1/webhooks/github`, §12), l'API résout les stacks dont `repo_url` matche le payload, vérifie le HMAC contre leur secret commun, puis filtre les environnements par branche et `project_root` (§9.6).

### 3.2 `environments` — l'instance exécutable

```
id uuid PK, stack_id FK, name text unique(stack),       -- dev, staging, prod
tier enum(dev|staging|prod),       -- §2.4 : porte les permissions d'apply/destroy
branch text,                       -- branche trackée par CET env
autodeploy bool,                   -- forcé false si protected
protected bool,                    -- §2.4 : force confirmation + 4-eyes (PAS le contrôle d'accès → tier)
require_second_pair_of_eyes bool,
managed_state bool,
allow_mock_apply bool default false,  -- §9.3 : autoriser l'apply d'un run ayant consommé des mocks
head_sha text nullable,               -- §9.6 : tête connue de la branche trackée
head_updated_at timestamptz nullable,
commits_ahead int nullable,           -- nb de commits entre dernier apply et head
affects_project_root bool nullable,   -- les commits d'avance touchent-ils ce project_root ?
locked bool, labels jsonb, position int,
created_at, updated_at
```

Pourquoi pas les workspaces Terraform : isolation faible (code/backend/credentials partagés, suffixe de state), erreurs de ciblage faciles. Un environnement Stackd = state physiquement séparé, variables, protections et pool de workers propres.

### 3.3 `variables` — niveau stack et environnement

```
id uuid PK,
stack_id FK nullable,              -- renseigné pour les vars stack/env
environment_id FK nullable,        -- NULL = commune à la stack ; renseigné = override env
variable_set_id FK nullable,       -- renseigné pour les vars d'un set (§3.4)
kind enum(terraform|environment),  -- terraform → TF_VAR_/tfvars ; environment → env var
name text, value text nullable, value_encrypted bytea nullable,
sensitive bool, hcl bool
-- CHECK : exactement un parent (variable_set_id XOR stack_id)
-- CHECK : environment_id IS NULL si variable_set_id est renseigné
--         (une variable de set n'est jamais scoped à un env : c'est
--          l'ATTACHEMENT du set qui porte le ciblage, §3.4)
-- Unicité : « parent » n'est pas une colonne (stack_id OU variable_set_id) →
--   deux index uniques partiels, pas une contrainte UNIQUE unique :
--     UNIQUE (stack_id, COALESCE(environment_id,'00..0'::uuid), kind, name)
--       WHERE stack_id IS NOT NULL
--     UNIQUE (variable_set_id, kind, name)
--       WHERE variable_set_id IS NOT NULL
--   (COALESCE car NULL ≠ NULL en SQL : sans lui, deux vars stack de même nom
--    avec environment_id NULL ne déclencheraient pas le conflit voulu)
```

### 3.4 `variable_sets` — configuration factorisée

```sql
variable_sets (
  id uuid PK, space_id FK,
  name text unique(space),          -- ex. common-aws, datadog, prod-credentials
  description text,
  auto_attach bool default false,   -- true = attaché à toutes les stacks du space
  created_at, updated_at
)

variable_set_attachments (
  id uuid PK,
  variable_set_id FK,
  target_kind enum('stack','environment'),
  target_id uuid,                   -- stack → tous ses envs ; environment → cet env seul
  priority int default 0,           -- ordonne les sets entre eux à la résolution
  UNIQUE (variable_set_id, target_kind, target_id)
)
```

**Résolution finale d'une variable au claim** (du plus faible au plus fort) :

```
1. variable sets auto_attach            (par priority croissante)
2. variable sets attachés à la stack    (par priority croissante)
3. variable sets attachés à l'env       (par priority croissante)
4. variables de stack (environment_id NULL)
5. variables d'environnement            ← gagne toujours
```

À nom et kind égaux, la couche supérieure écrase. Deux sources hors résolution s'ajoutent au moment du claim : les outputs amont (`dependency:`) et les mocks (`mock`) — voir §9. Le snapshot des **provenances** (`{"TF_VAR_region": "set:common-aws", "TF_VAR_cidr": "env", "TF_VAR_vpc_id": "dependency:network/prod", "TF_VAR_nlb_dns": "mock"}`) est figé dans `runs.variable_provenance` pour l'audit et le badge UI (DESIGN.md §5.2 : "héritée de…", "écrasée ici", `MOCK`). Suppression d'un set attaché → 409 avec la liste des attachements (détachement explicite requis).

### 3.5 `runs`

```
id uuid PK, environment_id FK,
type enum(tracked|proposed|destroy), state enum (§4),
commit_sha, commit_message, commit_author,
triggered_by enum(manual|webhook|dependency|api), trigger_user_id nullable,  -- 'api' réservé : pas
                                     -- de tokens applicatifs/PAT au MVP (auth = Google humain + worker),
                                     -- la valeur est posée quand le déclenchement programmatique arrive (Phase 7)
confirmed_by_user_id nullable,       -- qui a approuvé l'apply (cœur de l'audit)
parent_run_id nullable, run_group_id nullable, worker_id nullable,
plan_summary jsonb,                  -- {add, change, destroy, resources}
check_results jsonb,                 -- résultats des hooks after_plan (§8)
resolved_inputs jsonb,               -- outputs amont injectés (non sensibles)
used_mocks bool default false,       -- §9.3 : au moins un mock consommé
variable_provenance jsonb,           -- §3.4 : provenance de chaque variable résolue
claimed_at, confirmed_at, finished_at, error nullable, created_at
```

Concurrence — 1 run actif par environnement :

```sql
CREATE UNIQUE INDEX one_active_run_per_env ON runs (environment_id)
WHERE state IN ('preparing','planning','checking','unconfirmed','confirmed','applying');
```

### 3.6 `run_events`

```
id, run_id FK, from_state, to_state,
actor enum(system|user|worker), actor_id nullable, payload jsonb, created_at
```

### 3.7 `workers` et `worker_pools`

```
worker_pools: id, space_id, name, labels jsonb, token_hash, created_at
workers: id, pool_id, name, status(idle|busy|offline), labels jsonb,
         version, last_heartbeat_at, registered_at
```

`offline` si heartbeat > 60 s ; run en cours sur worker offline → `failed (worker_lost)` après 120 s. Ciblage par labels (pool prod dédié recommandé).

### 3.8 Dépendances, outputs et mocks

```sql
env_dependencies (
  id uuid PK,
  upstream_env_id FK, downstream_env_id FK,
  trigger_policy enum('on_output_change','always','never'),
  UNIQUE (upstream_env_id, downstream_env_id),
  CHECK (upstream_env_id <> downstream_env_id)
)

output_references (
  id uuid PK, dependency_id FK,
  output_name text,                 -- output Terraform amont
  input_name text,                  -- variable avale (sans TF_VAR_)
  mock_value jsonb nullable,        -- §9.3 : valeur factice pour le bootstrap
  UNIQUE (dependency_id, input_name)
)

env_outputs (
  id uuid PK, environment_id FK, run_id FK,
  name text, value jsonb,           -- NULL si sensitive
  value_hash text, sensitive bool,
  UNIQUE (environment_id, name)
)
```

Anti-cycle : DFS à la création → 422. Helper `POST /stacks/{id}/dependencies/link-by-name` : arêtes en masse entre environnements homonymes de deux stacks.

### 3.9 State managé et hooks

```
state_versions: id, environment_id FK, serial, lineage, size_bytes,
                s3_key, created_by_run_id nullable, created_at
state_locks:    environment_id PK, lock_id, info jsonb, locked_at

hooks (plateforme, §8): id, target_kind enum('stack','environment'), target_id,
       stage enum(before_init|after_init|before_plan|after_plan|before_apply|after_apply),
       name text, command text, on_failure enum('fail','warn'),
       position int, created_at, updated_at

run_logs (étage chaud, §5.2): run_id FK, phase text, section text nullable,
       seq int, lines jsonb, created_at — PK (run_id, phase, seq)
```

### 3.10 `cloud_integrations` (OIDC workload, §10)

```
id uuid PK, environment_id FK unique,
provider enum('aws'),                -- gcp/azure en Phase 7
plan_role_arn text, apply_role_arn text,
region text nullable, session_duration int default 3600,
created_at, updated_at
```

### 3.11 `oidc_signing_keys` (issuer workload, §10)

La rotation JWKS avec chevauchement (§10.1) impose de persister les clés avec leur `kid` : l'ancienne reste publiée tant que des tokens en vol l'utilisent.

```sql
oidc_signing_keys (
  id uuid PK,
  kid text unique,                   -- exposé dans le JWKS et l'en-tête JWT
  algorithm text default 'RS256',
  public_jwk jsonb,                  -- publié sur /oidc/jwks
  private_key_encrypted bytea,       -- AES-256-GCM (§1) OU référence KMS (clé jamais en clair au repos)
  status enum('active','retiring','retired'),  -- 1 seule 'active' à la fois (signe) ; 'retiring' encore dans le JWKS
  created_at, retired_at nullable
)
```

Rotation : nouvelle clé `active`, l'ancienne passe `retiring` (toujours dans le JWKS, ne signe plus), puis `retired` (hors JWKS) une fois le TTL max d'un token écoulé. Option KMS : `private_key_encrypted` devient une référence d'ARN, la signature passe par `kms:Sign` (la clé privée ne quitte jamais KMS) — recommandé en prod (cf. risque §13 / PLAN §5).

---

## 4. State machine des runs

### 4.1 Diagramme

```
                  ┌──────────┐
   trigger ──────▶│  queued  │── env locké / run actif : attend
                  └────┬─────┘
                  ┌────▼─────┐
                  │preparing │ claim, clone, setup tool, hooks before/after_init, init
                  └────┬─────┘
                  ┌────▼─────┐
                  │ planning │ hooks before_plan, plan -out + show -json
                  └────┬─────┘
                  ┌────▼─────┐
                  │ checking │ hooks after_plan (tfsec, infracost, scripts)
                  └────┬─────┘   fail → failed ; warn → confirmation forcée
         ┌────────────┼────────────────────┐
  plan   │            │ changements         │ type=proposed
  vide   │            ▼                     ▼
         │     ┌─────────────┐       ┌──────────┐
         │     │ unconfirmed │       │ finished │ (plan-only)
         │     └────┬───┬────┘       └──────────┘
         │  confirm │   │ discard ──▶ discarded
         │ (can_app-│
         │  ly: tier│
         │  + rôle) │
         │     ┌────▼─────┐
         │     │confirmed │ (reprise par le même worker si possible)
         │     └────┬─────┘
         │     ┌────▼─────┐
         │     │ applying │ hooks before_apply, apply, output -json, hooks after_apply
         │     └────┬─────┘
         ▼          ▼
      ┌──────────────────┐
      │     finished     │──▶ hooks scheduler : capture outputs, cascade, audit
      └──────────────────┘
```

Terminaux : `finished`, `failed`, `discarded`, `canceled`. `canceled` : user sur `queued`/`unconfirmed`, ou signal au worker via heartbeat → SIGINT.

### 4.2 Règles de transition

| Transition | Acteur | Conditions |
|---|---|---|
| `queued → preparing` | worker (claim) | pas de run actif sur l'env, env non locké, labels compatibles |
| `planning → checking` | worker | plan OK **et ≥ 1 hook after_plan**. Sans hook after_plan, `checking` est sauté : les transitions `planning → unconfirmed / confirmed / finished` existent, avec exactement les mêmes conditions que leurs équivalentes depuis `checking` |
| `checking → unconfirmed` | worker | checks OK ou warn ; diff non vide. Un warn **force** la confirmation même si autodeploy |
| `checking → confirmed` | system | checks tous OK, diff non vide, `autodeploy=true`, env non protégé, `used_mocks=false` |
| `planning/checking → finished` | worker | diff vide (outputs capturés après refresh) |
| `unconfirmed → confirmed` | user | `can_apply(user, env)` = role∈{approver,admin} ET `max_apply_tier >= env.tier` (§2.4) ; ≠ triggerer si tier=prod ou 4-eyes ; pour un run `destroy` : `can_destroy` requis ; **bloqué si `used_mocks` et `allow_mock_apply=false`** |
| `confirmed → applying` | worker | reprise du workspace (TTL 24 h), sinon re-plan |
| `applying → finished` | worker | apply exit 0 + outputs uploadés |
| `* → failed` | worker/system | exit ≠ 0, hook `fail`, timeouts (prepare 10 / plan 30 / apply 60 min) |

Fonction unique `transition(run, to_state, actor, payload)` : légalité, update atomique gardé sur `from_state`, `run_event`, audit event si action humaine ou terminale, publication WS, hooks scheduler. L'UPDATE gardé exploite `RETURNING` (PG18 : ancien + nouveau tuple) pour produire le `run_event` `from→to` sans relecture, dans la même transaction.

---

## 5. Système de logs des jobs

### 5.1 Ingestion (worker → API)

```
POST /worker/v1/jobs/{id}/logs
  { "phase": "planning", "section": "hook:infracost" | null,
    "seq": 42, "lines": [ {"t": "...", "msg": "..."} ] }
```

- `seq` strictement croissant par phase → idempotence des retries.
- `section` distingue les hooks du flux terraform (sections dédiées dans la visionneuse).
- Buffer agent : 1 s / 32 Ko. **Masquage avant envoi de TOUTES les valeurs sensibles du run** — `sensitive_env` *et* les `tfvars` marquées `sensitive` (§3.3) — remplacées par `***`. L'agent construit la table de masquage à partir du payload de claim, pas seulement de `sensitive_env`.
- **Fuite résiduelle via `plan.json`** : un hook `after_plan` (infracost, jq, script) lit `plan.json`, qui contient les valeurs des variables. Terraform marque `sensitive` les valeurs qu'il connaît comme telles, mais un hook qui dump le JSON brut peut ré-imprimer une valeur sensible dans son stdout (donc dans les logs). Mitigations : (a) le masquage par valeur ci-dessus s'applique aussi au stdout des hooks ; (b) un secret court ou transformé (base64, sous-chaîne) échappe au masquage par valeur — limite **documentée**, à ne pas mettre des secrets exploitables en `tfvars` non sensibles. Les outils de check (tfsec/checkov/infracost) n'impriment pas les valeurs par défaut.
- Séquences ANSI conservées (rendu couleur côté front).

### 5.2 Stockage à deux étages

| Étage | Où | Quand | Usage |
|---|---|---|---|
| Chaud | table `run_logs` | pendant le run + 7 j | live + récent |
| Froid | S3 `logs/{run_id}/{phase}.log.gz` | archivage async fin de run | historique (1 an puis lifecycle) |

`GET /runs/{id}/logs?phase=&after_seq=` sert l'un ou l'autre de façon transparente. `GET /runs/{id}/logs/download?phase=all`.

### 5.3 Distribution live

WebSocket unique multiplexé : `{"sub": "run:<id>"}` → `log_chunk {phase, section, seq, lines}` + `run_event`. Reconnexion → GET REST `after_seq` pour combler, puis flux.

**Fan-out multi-réplicas (sans broker).** Un client WS est connecté à **un** réplica API ; l'événement (transition ou chunk de log) peut être produit sur **un autre** réplica. Le pont est **Postgres `LISTEN/NOTIFY`** :

- `transition()` (§4.2) et l'ingestion de logs (§5.1) émettent un `NOTIFY` sur un canal par entité (`run_<id>`, `env_<id>`) **dans la même transaction** que l'écriture.
- La charge utile `NOTIFY` ne porte qu'un **signal léger** — `{kind, run_id, phase, max_seq}`, jamais le contenu (plafond 8 Ko de `NOTIFY`, et les lignes de log peuvent être volumineuses).
- À réception, chaque réplica relit la source (`run_logs` après `after_seq`, ou l'état du run) et pousse aux WS locaux abonnés. Le front voit la même séquence qu'à la reconnexion REST — un seul chemin de lecture, idempotent par `seq`.

Au MVP mono-réplica, `LISTEN/NOTIFY` reste le mécanisme (aucune branche « in-process » à maintenir) ; il scale tel quel jusqu'à plusieurs réplicas avant d'exiger un vrai bus (interface `EventBus` abstraite, comme `JobQueue`).

### 5.4 Visionneuse (front)

Spécifiée dans **DESIGN.md §5.3** : virtualisation, sections par phase et par hook repliables, follow-tail, recherche, ancres `#L1234`, rendu ANSI, timestamps toggle, download.

---

## 6. Audit — "qui a apply quoi"

### 6.1 Table `audit_events` (append-only)

```
id uuid PK (v7), actor_kind enum(user|worker|system|webhook), actor_id nullable,
actor_email text nullable,          -- dénormalisé : lisible même si user supprimé
action text,                        -- taxonomie §6.2
target_kind text, target_id uuid,
context jsonb,                      -- stack_name, env_name, run_id, commit, plan_summary...
ip, user_agent nullable, created_at
```

**Immutabilité réelle, pas seulement « pas d'API »** : l'absence d'endpoint d'update/delete ne protège pas d'un bug ou d'une compromission applicative. Le rôle DB applicatif n'a que `INSERT` et `SELECT` sur `audit_events` (`REVOKE UPDATE, DELETE`), un trigger `BEFORE UPDATE OR DELETE` lève une exception, et la purge de rétention passe par un rôle distinct, séparé du rôle applicatif et lui-même audité. Rétention 2 ans, purge admin explicite (auditée). Index : `(created_at)`, `(actor_id, created_at)`, `(target_kind, target_id, created_at)`, `(action, created_at)`.

### 6.2 Taxonomie (MVP)

```
auth.login / auth.logout / auth.domain_denied / auth.refresh_reuse_detected
stack.* / environment.* (created|updated|deleted)
variable.* (created|updated|deleted)          # context: nom, sensitive — JAMAIS la valeur
variable_set.* (created|updated|deleted|attached|detached)
hook.* (created|updated|deleted)
run.triggered / run.confirmed ⭐ / run.discarded / run.canceled
run.applied ⭐ / run.apply_failed / run.destroy_triggered
run.check_failed / run.check_warned           # résultats des hooks after_plan
state.force_unlocked / state.version_downloaded / state.deleted
dependency.created / dependency.deleted / dependency.mock_consumed
cloud_integration.created / cloud_integration.updated / cloud_integration.deleted
worker_pool.created / worker_pool.token_rotated / worker_pool.deleted
worker.diagnostics_requested                  # bundle de debug lecture seule (cf. §observabilité)
hook.* (created|updated|deleted)
user.role_changed / user.apply_tier_changed / user.destroy_permission_changed / user.disabled
```

`run.confirmed` + `run.applied` = réponse à "qui a apply quoi" : identité Google du confirmeur, env, commit, résumé du plan, rôle IAM assumé (si OIDC), lien vers les logs.

### 6.3 Double écriture assumée

`run_events` = mécanique fine de la state machine. `audit_events` = journal métier dénormalisé. L'audit event est écrit **dans la même transaction DB** que l'action — pas de bus, pas de perte.

### 6.4 API et UI

```
GET /api/v1/audit?actor=&action=&target_kind=&target_id=&stack=&environment=&from=&to=
GET /api/v1/audit/export?format=csv          (admin)
```

Page /audit (journal global filtrable), onglet Activity par env (derniers applies : qui/quand/commit/résumé/logs), vue par utilisateur.

---

## 7. Protocole Worker

### 7.1 Enregistrement et heartbeat

```
POST /worker/v1/register   (Bearer pool_token) → worker_id + worker_token
POST /worker/v1/heartbeat  (20 s) → { "commands": [{"type":"cancel_job", ...}] }
```

Heartbeat = canal de commande descendant. Aucune connexion entrante.

### 7.2 Claim (long-poll)

```
POST /worker/v1/jobs/claim?wait=25
→ 204 si rien
→ 200 {
    "job_id": "...",
    "phase": "plan" | "apply",   # type d'exécution du job — ne pas confondre avec les
                                 # phases fines du run (§4) ou des logs (§5) : un job
                                 # "plan" couvre preparing+planning+checking
    "environment": { "id", "name": "prod", "stack_name": "core-network",
      "repo_url", "commit_sha", "project_root", "tool", "tool_version" },
    "repo_credentials": { "kind": "token", "token": "<déchiffré, TTL mémoire>" },
    "env": { ... },                        # résolution sets→stack→env (§3.4)
    "sensitive_env": { ... },              # jamais loggé
    "tfvars_json": { ... },
    "hooks": {                             # §8 : fusion plateforme + .stackd.yml
      "after_plan": [ {"name": "infracost", "command": "...", "on_failure": "warn",
                       "source": "platform"} ]
    },
    "backend": { "type": "http", "address": ".../state/v1/<env_id>",
                 "lock_address", "unlock_address",
                 "username": "env", "password": "<state_token scoped+TTL>" },
    "cloud_credentials": {                 # §10, si cloud_integration configurée
      "provider": "aws",
      "oidc_token": "<JWT workload signé>",
      "role_arn": "arn:aws:iam::123:role/stackd-prod-plan",   # rôle de LA phase
      "region": "eu-west-1"
    },
    "resolved_inputs": { "TF_VAR_vpc_id": "vpc-0abc..." },
    "mock_inputs": { "TF_VAR_nlb_dns": "mock.example.internal" }   # §9.3
  }
```

Claim atomique :

```sql
WITH next AS (
  SELECT r.id FROM runs r
  JOIN environments e ON e.id = r.environment_id
  WHERE r.state IN ('queued','confirmed')
    AND e.labels <@ :worker_labels
    AND NOT EXISTS (...)            -- pas d'autre run actif sur l'env
  ORDER BY (r.state = 'confirmed' AND r.worker_id = :wid) DESC,   -- affinité :
           r.created_at                                            -- l'apply préfère
  FOR UPDATE SKIP LOCKED LIMIT 1                                   -- le worker du plan
)
UPDATE runs SET state=..., worker_id=:wid, claimed_at=now()
FROM next WHERE runs.id = next.id RETURNING runs.*;
```

**La vraie garde de concurrence est l'index unique `one_active_run_per_env` (§3.5), pas `SKIP LOCKED`.** Deux workers qui claim deux runs `queued` **distincts** du même env sélectionnent des lignes différentes : `SKIP LOCKED` ne les sérialise pas entre elles, et le `NOT EXISTS (run actif)` est vrai pour les deux (aucun actif encore). Les deux `UPDATE → preparing` partent ; le second viole l'index unique partiel. Deux protections, à appliquer **toutes les deux** :

1. **Sérialiser par env** : le `SELECT ... FOR UPDATE` verrouille aussi la ligne `environments` correspondante (`JOIN environments e ... FOR UPDATE OF e SKIP LOCKED`), de sorte qu'un seul claim par env progresse à la fois ; les autres sautent l'env verrouillé et prennent un autre run.
2. **Filet de sécurité** : la violation de `one_active_run_per_env` est **attrapée** (SQLSTATE `23505`) et traitée comme « rien à claimer » → `204`, le worker re-poll. C'est la garantie de correction ; le verrou ci-dessus n'est qu'une optimisation pour éviter le travail jeté.

Affinité d'apply : un run `confirmed` est réservé à son worker d'origine pendant **60 s** (`AND (r.worker_id = :wid OR r.confirmed_at < now() - interval '60 seconds')` sur les runs confirmed). Passé ce délai (worker mort ou saturé), n'importe quel worker compatible le prend et fait un **re-plan automatique** avant l'apply (workspace absent → §4.2).

### 7.3 Événements et artifacts

```
POST /worker/v1/jobs/{id}/events
  { "event": "phase_started"|"phase_finished"|"job_failed",
    "phase": "...", "exit_code": 0,
    "result": { "has_changes": true, "summary": {...},
                "checks": [{"name":"infracost","status":"warn","detail":"..."}] } }

PUT /worker/v1/jobs/{id}/artifacts/plan.tfplan | plan.json | outputs.json
```

### 7.4 Déroulé d'un job côté agent (pseudo-code)

```python
job = claim()
ws = Workspace(job.job_id)
ws.git_clone(job.environment.repo_url, job.environment.commit_sha, depth=1)
tf = ensure_tool(job.environment.tool, job.environment.tool_version)  # vérifie le checksum
                                       # SHA-256 (et la signature cosign/GPG si dispo) du binaire
                                       # téléchargé contre une liste épinglée — refus sinon (supply-chain)

if job.cloud_credentials:                    # §10 OIDC workload
    token_file = ws.write_secret("oidc_token", job.cloud_credentials.oidc_token)
    extra_env = { "AWS_WEB_IDENTITY_TOKEN_FILE": token_file,
                  "AWS_ROLE_ARN": job.cloud_credentials.role_arn,
                  "AWS_ROLE_SESSION_NAME": f"stackd-{job.job_id}" }

hooks = merge_hooks(job.hooks, ws.load_stackd_yml())   # plateforme d'abord, §8

if job.phase == "plan":
    write_backend_override(ws, job.backend)
    write_tfvars(ws, job.tfvars_json, job.mock_inputs)
    run_hooks(hooks.before_init); run(tf, "init", "-input=false"); run_hooks(hooks.after_init)
    run_hooks(hooks.before_plan)
    code = run(tf, "plan", "-out=plan.tfplan", "-detailed-exitcode")
    # 0 = no changes, 2 = changes, 1 = erreur
    plan_json = run(tf, "show", "-json", "plan.tfplan")
    upload_artifacts(plan_json, "plan.tfplan")
    checks = run_hooks(hooks.after_plan, expose="plan.json")   # phase checking
    report(phase_finished, has_changes=(code == 2), summary=..., checks=checks)
    if code == 2: keep_workspace(ttl="24h")   # l'apply (auto-confirmé OU confirmé
                                              # manuellement) réutilise ce workspace

elif job.phase == "apply":
    ws = restore_workspace(job.job_id)       # ou re-plan si absent
    run_hooks(hooks.before_apply)
    run(tf, "apply", "-input=false", "plan.tfplan")
    outputs = run(tf, "output", "-json")
    upload_artifacts(outputs, "outputs.json")
    run_hooks(hooks.after_apply)
    report(phase_finished)

ws.cleanup()
```

Runner `docker` : chaque commande (terraform ET hooks) tourne dans `stackd/runner:<tool>-<version>` (image incluant les outils de check courants : tfsec, checkov, infracost, jq).

### 7.5 Tâches périodiques (scheduler interne, multi-réplicas)

Le module scheduler (PLAN §2.1) porte des tâches de fond qui doivent s'exécuter **une seule fois** même avec plusieurs réplicas API :

| Tâche | Fréquence | Effet |
|---|---|---|
| Détection `worker_lost` | 30 s | heartbeat > 60 s → `offline` ; run actif sur worker offline depuis > 120 s → `failed (worker_lost)` (§3.7) |
| Polling staleness Git | 15 min | `git ls-remote` par (repo, branche), dédup par repo → maj `head_sha` (§9.6) |
| Archivage logs froids | fin de run | `run_logs` → S3 gz, purge chaud > 7 j (§5.2) |
| Purge refresh tokens / audit | quotidien | familles expirées (§2.5), audit > 2 ans (§6.1, purge auditée) |

**Garantie d'exécution unique** : chaque tâche prend un **PG advisory lock** dédié (`pg_try_advisory_lock(<task_key>)`) avant de tourner ; un réplica qui n'obtient pas le lock saute son tick. Pas de leader élu permanent (pas de dépendance externe), pas de double exécution. Les tâches sont **idempotentes** de toute façon (relancer un calcul de staleness ou une archive ne casse rien) — l'advisory lock évite surtout le travail redondant et les courses sur les transitions `worker_lost`.

---

## 8. Hooks & checks (custom flows)

### 8.1 Deux sources, une fusion

| Source | Déclaration | Modifiable par | Usage |
|---|---|---|---|
| **Plateforme** | UI/API, niveau stack ou environnement (table `hooks`) | writer+ (audité) | gouvernance imposée : **non contournable par une PR** |
| **Repo** | fichier `.stackd.yml` à la racine du `project_root`, versionné | quiconque pousse du code | logique propre au projet (génération de fichiers, terragrunt, etc.) |

Ordre d'exécution à chaque stage : hooks plateforme stack → hooks plateforme env → hooks repo. Les checks de sécurité critiques vont côté plateforme.

### 8.2 Format `.stackd.yml`

```yaml
version: 1
hooks:
  before_plan:
    - name: generate-locals
      command: ./scripts/gen-locals.sh
  after_plan:
    - name: infracost
      command: infracost breakdown --path plan.json --format table
      on_failure: warn          # fail | warn (défaut: fail)
    - name: no-destroy-prod
      command: jq -e '[.resource_changes[] | select(.change.actions | index("delete"))] | length == 0' plan.json
      on_failure: fail
```

### 8.3 Sémantique d'exécution

- Chaque hook : une commande shell dans le workspace (cwd = project_root), env vars du run injectées (sauf `sensitive_env` pour les hooks **repo** — opt-in par env, même logique que les proposed runs).
- **Credentials cloud (§10) non exportés aux hooks repo.** Les variables `AWS_WEB_IDENTITY_TOKEN_FILE` / `AWS_ROLE_ARN` ne sont injectées que dans l'environnement des invocations **terraform**, jamais dans celui des hooks **repo** par défaut (mêmes raisons que `sensitive_env` : un `.stackd.yml` poussé par PR ne doit pas pouvoir assumer le rôle d'apply prod et exfiltrer). Opt-in par env si un hook a légitimement besoin du cloud. Les hooks **plateforme** (non contournables) y ont accès.
- **Hooks repo aux stages `*_apply` sur tier=prod** : interdits par défaut. Sur un env `tier=prod`, seuls les hooks **plateforme** s'exécutent en `before_apply`/`after_apply` ; un hook repo à ces stages est ignoré avec un warning visible (il tournerait avec le rôle d'écriture prod). Les stages `*_init`/`*_plan` repo restent autorisés (rôle plan = ReadOnly).
- `plan.json` disponible en lecture pour les stages `after_plan` et suivants.
- Timeout par hook : 10 min (configurable). Logs dans une section dédiée de la visionneuse.
- **`on_failure: fail`** → run `failed`, audit `run.check_failed`.
- **`on_failure: warn`** → le run continue mais passe obligatoirement par `unconfirmed` (même avec autodeploy), badge warning + détail sur la page run, audit `run.check_warned`. Un humain assume.
- Résultats agrégés dans `runs.check_results` et affichés en barre de statut des checks.

---

## 9. Dépendances, propagation des outputs et mocks

### 9.1 Capture

À `applying → finished`, parse de `outputs.json` :
- non sensible → upsert `env_outputs` + `value_hash = sha256(canonical_json)`
- sensible → `value=NULL, sensitive=true`. Jamais stocké ni propagé ; une `output_reference` qui pointe dessus → erreur de config visible, pas de null silencieux.

### 9.2 Cascade — hook `on_finished(run)`

```
1. arêtes sortantes de run.environment
2. policy: never → skip ; always → trigger ;
   on_output_change → trigger si value_hash ≠ resolved_inputs du dernier
   run finished de l'env aval
3. run créé : type=tracked, triggered_by=dependency, parent_run_id, run_group_id
4. multi-parents : aval déclenché quand TOUS ses parents du run group sont finished
5. parent failed → branche stoppée, run group = partial_failure
6. env aval protégé → s'arrête en unconfirmed (jamais contourné)
```

Résolution des inputs au claim (valeurs fraîches), snapshot figé dans `resolved_inputs`.

### 9.3 Mock outputs (bootstrap, inspiré Terragrunt)

**Problème** : comment planifier `app/dev` si `network/dev` n'a jamais été appliqué ? Sans mécanisme, la cascade a un problème d'œuf et de poule à la création.

**Mécanisme** :

1. Chaque `output_reference` peut définir une `mock_value` (JSON : string, number, list, map).
2. À la résolution des inputs au claim, pour chaque référence :
   - l'output amont **existe** dans `env_outputs` → valeur réelle (le mock est ignoré, même sur une PR)
   - l'output **n'existe pas** ET `mock_value` définie → mock injecté, la référence est listée dans `mock_inputs` du payload
   - l'output n'existe pas ET pas de mock → run `failed` immédiat avec erreur explicite (`missing_upstream_output`)
3. Le run est marqué `used_mocks=true` + audit `dependency.mock_consumed` (quelles références).
4. **Garde-fous** :
   - badge "MOCKED" très visible sur la page run + liste des valeurs mockées
   - `unconfirmed → confirmed` **refusé** si `used_mocks` et `environment.allow_mock_apply=false` (défaut) : un plan mocké sert à valider la config, pas à être appliqué
   - jamais d'autodeploy d'un run mocké
   - les proposed runs (PR) utilisent librement les mocks (plan-only par nature)

**Bonnes valeurs de mock** : plausibles pour le type attendu par le provider (`vpc-mock00000000`, `subnet-mock...`) — documenté, avec exemples par type de ressource AWS courant.

### 9.4 Run groups

`POST /environments/{id}/runs?with_downstream=true` : sous-graphe (BFS), tri topologique (Kahn), run group, racine lancée, niveaux suivants via cascade. UI : graphe coloré par état (cf. DESIGN.md §5.4).

### 9.5 Patterns multi-région (recettes de référence)

Le modèle n'a pas de dimension "région" native : la région est de la configuration, l'environnement est l'unité. Deux recettes couvrent les cas réels :

**A. Déploiement identique dans N régions**

```
stack core-network (code unique, branche main)
├── env prod-eu-west-1   ← variable sets: [tier-prod, region-eu-west-1]
├── env prod-us-east-1   ← variable sets: [tier-prod, region-us-east-1]
└── env dev-eu-west-1    ← variable sets: [tier-dev,  region-eu-west-1]
```

- Tout ce qui diffère entre régions vit dans les sets `region-*` (provider region, AZs, CIDRs, AMIs). Tout ce qui diffère entre tiers vit dans les sets `tier-*`.
- Un push sur la branche trackée déclenche un run par env (iso-prod multi-région).
- La `cloud_integration` étant par env, chaque région peut assumer un rôle IAM (voire un compte AWS) différent.

**B. Primary région A → secondary "similaire mais pas identique" en région B, avec outputs de A**

Choix de forme, dans l'ordre de préférence :
1. **Même stack, différence par variables** : un flag `TF_VAR_is_primary` (+ `count`/`for_each` dans le code) si la différence est conditionnelle. Maximise la réutilisation.
2. **Stack séparée** (autre `project_root` du même repo ou autre repo) si la différence est structurelle. Évite les conditionnels envahissants.

Flux d'outputs : une arête `env_dependencies` ordinaire —

```
network/prod-eu-primary ──▶ network/prod-us-secondary
  output_references:
    global_cluster_arn → TF_VAR_global_cluster_arn   (mock: "arn:aws:rds::mock")
    kms_replica_key_id → TF_VAR_kms_key_id           (mock: "mrk-mock0000")
  trigger_policy: on_output_change
```

- **Une dépendance entre deux environnements de la même stack est valide** : la contrainte n'interdit que l'auto-référence (`upstream <> downstream`). Le pattern primary/secondary intra-stack est donc natif.
- Bootstrap : les mocks permettent de planifier la région B avant le premier apply de A (§9.3) ; l'apply de B reste bloqué tant qu'il consomme des mocks.
- Env B `protected` → la cascade s'arrête en `unconfirmed` : la promotion d'un changement primary vers la région secondaire passe par un humain.
- Le helper `link-by-name` ne s'applique pas (noms d'envs différents) : les arêtes se créent explicitement — une relation cross-région doit être un choix, pas un automatisme.

### 9.6 Retard Git (staleness) — "appliqué ≠ tête de branche"

> À distinguer du **drift d'infrastructure** (state vs réalité cloud, Phase 7) : ici on compare le **dernier commit appliqué** au **HEAD de la branche trackée**. Cas type : apply lundi 9h00 sur `abc1234`, PR mergée à 9h15 → l'environnement est en retard d'un commit.

**Calcul** :

```
last_applied_sha = commit_sha du dernier run finished (type tracked, avec apply ou no-change)
env.head_sha     = tête connue de env.branch
stale            = head_sha présent ET head_sha ≠ last_applied_sha
```

**Mise à jour de `head_sha`** (du plus précis au fallback) :
1. **Webhook push** (Phase 5) : mise à jour immédiate de tous les envs trackant la branche — même si le webhook ne déclenche pas de run (filtrage `project_root`), il met toujours `head_sha` à jour.
2. **Polling de secours** : `git ls-remote` par (repo, branche) toutes les 15 min (tâche périodique, dédupliquée par repo) — couvre les webhooks absents ou perdus.
3. **Manuel** : `POST /api/v1/environments/{id}/refresh-head`.

**Deux niveaux de précision** :
- *Niveau 1 — branche en avance* : comparaison de SHA, toujours disponible. `commits_ahead` via l'API compare du provider Git si dispo, sinon affichage binaire "en retard".
- *Niveau 2 — te concerne* : `affects_project_root` = au moins un commit d'avance modifie des fichiers sous `project_root` (API compare GitHub/GitLab). Si indisponible : NULL, l'UI reste au niveau 1.

**Effets** :
- Chip `↑N` sur la cellule d'env (DESIGN.md §5.1) et l'en-tête d'env ; variante atténuée si `affects_project_root = false` ("la branche a avancé, mais pas ce dossier").
- **Run `unconfirmed` obsolète** : si `head_sha` avance pendant qu'un plan attend confirmation, bandeau sur la page run — "plan calculé sur `abc1234`, la branche a avancé de N commits" — avec action *Re-plan* (discard + nouveau run sur la tête). La confirmation reste **possible** (appliquer un commit précis est légitime) mais l'obsolescence est impossible à manquer.
- Aucune action automatique : la staleness est une information, jamais un trigger (c'est le rôle des webhooks).
- `GET /api/v1/environments/{id}` expose `head_sha`, `commits_ahead`, `affects_project_root`, `stale`.

---

## 10. Credentials cloud dynamiques — OIDC workload identity

### 10.1 La plateforme comme issuer OIDC

```
GET /.well-known/openid-configuration     → issuer, jwks_uri, alg RS256
GET /oidc/jwks                            → clés publiques (kid, rotation)
```

- Paire de clés RS256, stockée chiffrée (ou KMS), rotation avec chevauchement (l'ancienne clé reste dans le JWKS le temps des tokens en vol).
- L'issuer doit être joignable en HTTPS par AWS STS (URL publique ou via le module Terraform fourni qui crée l'Identity Provider avec le thumbprint).

### 10.2 Token workload (signé au claim, par phase)

```json
{
  "iss": "https://stackd.example.com",
  "sub": "run:prod:core-network:apply",       // tier:stack:phase — base des trust policies
  "aud": "sts.amazonaws.com",
  "environment": "prod", "tier": "prod", "stack": "core-network",
  "environment_id": "...", "run_id": "...", "phase": "apply",
  "triggered_by": "dependency",
  "exp": <now + min(session_duration, durée max de la phase)>
}
```

> Le segment de tier dans le `sub` (`run:<tier>:...`) est ce qui matérialise le **double verrou** de §2.4 : Stackd refuse la confirmation côté API *et* la trust policy AWS refuse l'AssumeRole si le tier ne correspond pas. Un rôle d'écriture prod n'est assumable que par un token de tier prod.

### 10.3 Côté AWS (fourni : module Terraform d'exemple)

```hcl
# Trust policy du rôle apply de prod — refuse tout le reste
condition {
  test     = "StringLike"
  variable = "stackd.example.com:sub"
  values   = ["run:prod:*:apply"]
}
```

Pattern recommandé : `plan_role_arn` = rôle ReadOnly (+ accès modules S3), `apply_role_arn` = rôle d'écriture scoped. Un plan de PR ne peut **physiquement** pas modifier l'infra.

**Le wildcard porte uniquement sur le segment `stack`** (`run:prod:*:apply` = « n'importe quelle stack, mais tier prod ET phase apply »). `tier` et `phase` sont toujours fixes dans la condition : c'est eux qui matérialisent le double verrou (§2.4). Un rôle dont la trust policy laisserait le tier ou la phase en wildcard (`run:*:*:*`) annulerait la garde — à proscrire (cf. §13).

### 10.4 Côté worker

Le worker n'échange rien lui-même dans le cas simple : il écrit le token dans un fichier et exporte `AWS_WEB_IDENTITY_TOKEN_FILE` + `AWS_ROLE_ARN` + `AWS_ROLE_SESSION_NAME=stackd-{run_id}` — le SDK AWS des providers fait l'AssumeRoleWithWebIdentity nativement. Le session name = run_id → **CloudTrail trace chaque action AWS jusqu'au run** (et donc jusqu'à l'humain qui a confirmé : boucle d'audit complète).

Fallback : pas de `cloud_integration` → variables statiques classiques (variable set `aws-credentials`), comportement actuel.

### 10.5 Priorité et coexistence

Si une `cloud_integration` existe ET que des variables `AWS_*` statiques sont résolues → les variables OIDC gagnent, warning de config affiché (source de confusion classique).

---

## 11. State managé — S3 derrière le backend HTTP

### 11.1 Architecture

```
terraform ──(backend "http")──▶ API Stackd ──(boto3, SSE-KMS)──▶ S3
                                    └─▶ Postgres : state_versions, state_locks, audit
```

S3 pour les bytes, HTTP pour l'interface : tokens scoped par run sans credentials AWS distribués, locking visible, audit, refus de serial régressif.

### 11.2 Endpoints

| Méthode | Endpoint | Comportement |
|---|---|---|
| `GET` | `/state/v1/{env_id}` | 200 + dernier state, 404 sinon |
| `POST` | `/state/v1/{env_id}?ID=<lock_id>` | vérifie lock, refuse serial régressif (409), upload S3, `state_version` liée au run, audit |
| `LOCK` | `/state/v1/{env_id}/lock` | 200 ou **423** + holder |
| `UNLOCK` | `/state/v1/{env_id}/lock` | vérifie lock_id |
| `DELETE` | `/state/v1/{env_id}` | admin, soft-delete, audité |

Auth : Basic, password = JWT scoped `{env_id, run_id, scope, exp}`. Scope `ro` pour les proposed runs.

### 11.3 Layout S3

```
s3://stackd-<org>/
  states/{environment_id}/{version_uuid}.tfstate    # SSE-KMS, bucket versioning ON
  logs/{run_id}/{phase}.log.gz
  artifacts/{run_id}/plan.tfplan | plan.json | outputs.json
```

`managed_state: false` → rien d'injecté, le repo garde son bloc `backend "s3"` (compat existant CarCutter).

---

## 12. API REST (surface principale)

```
# Auth
GET  /api/v1/auth/google/start | /callback ; POST /auth/refresh | /logout ; GET /me

# OIDC issuer (workload)
GET  /.well-known/openid-configuration ; GET /oidc/jwks

# Stacks & environnements
GET|POST /api/v1/stacks ; GET|PATCH|DELETE /api/v1/stacks/{id}
POST /api/v1/stacks/{id}/check-repo
GET|POST /api/v1/stacks/{id}/environments ; GET|PATCH|DELETE /api/v1/environments/{id}
POST /api/v1/environments/{id}/refresh-head        # staleness Git, §9.6
GET|POST|PATCH|DELETE /api/v1/stacks/{id}/variables[...]
GET|POST|PATCH|DELETE /api/v1/environments/{id}/variables[...]

# Variable sets
GET|POST /api/v1/variable-sets ; GET|PATCH|DELETE /api/v1/variable-sets/{id}
GET|POST|PATCH|DELETE /api/v1/variable-sets/{id}/variables[...]
GET|POST|DELETE /api/v1/variable-sets/{id}/attachments
GET /api/v1/environments/{id}/resolved-variables    # vue fusionnée + provenance

# Hooks (plateforme)
GET|POST|PATCH|DELETE /api/v1/stacks/{id}/hooks[...] | /api/v1/environments/{id}/hooks[...]

# Cloud integrations (OIDC)
GET|PUT|DELETE /api/v1/environments/{id}/cloud-integration
POST /api/v1/environments/{id}/cloud-integration/test    # AssumeRole de vérification

# Runs
POST /api/v1/environments/{id}/runs            { type?, with_downstream? }
GET  /api/v1/environments/{id}/runs ; GET /api/v1/runs/{id}
POST /api/v1/runs/{id}/confirm | /discard | /cancel
GET  /api/v1/runs/{id}/logs ?phase=&after_seq= ; GET /runs/{id}/logs/download
GET  /api/v1/runs/{id}/plan ; GET /api/v1/runs/{id}/checks
WS   /api/v1/ws                                sub: run:{id}, environment:{id}

# Dépendances
GET|POST /api/v1/environments/{id}/dependencies          # références avec mock_value
POST /api/v1/stacks/{id}/dependencies/link-by-name
DELETE /api/v1/dependencies/{id}
GET /api/v1/environments/{id}/outputs ; GET /api/v1/graph ; GET /api/v1/run-groups/{id}

# Audit
GET /api/v1/audit ; GET /api/v1/audit/export

# Users & permissions (admin)
GET   /api/v1/users
PATCH /api/v1/users/{id}     # role, max_apply_tier, can_destroy, disabled (audité)

# Workers & file d'exécution
GET|POST|DELETE /api/v1/worker-pools ; GET /api/v1/workers
GET /api/v1/queue          # runs en cours + en attente, avec raison de blocage calculée
                           # (active_run|env_locked|no_compatible_worker|apply_affinity_hold)
POST|GET /api/v1/workers/{id}/diagnostics   # admin : bundle de debug lecture seule (via heartbeat)

# Observabilité & onboarding
GET /api/v1/health         # DB, workers (online + heartbeat), runs actifs/en attente, erreurs récentes
GET /api/v1/logs           # admin : buffer JSON structuré, filtres level/event/worker_id/run_id/q
POST /api/v1/auth/me/onboarded              # marque le walkthrough vu (persisté côté serveur)

# State
GET /api/v1/environments/{id}/state/versions[...]
DELETE /api/v1/environments/{id}/state/lock              # force-unlock (admin, audité)

# Webhooks
POST /api/v1/webhooks/github                             # HMAC

# Worker API (agents — détail §7)
POST /worker/v1/register | /heartbeat | /jobs/claim
POST /worker/v1/jobs/{id}/events | /jobs/{id}/logs
PUT  /worker/v1/jobs/{id}/artifacts/{name}
POST /worker/v1/commands/{id}/result        # résultat d'une commande descendante (diagnostics…)

# State backend HTTP (Terraform — détail §11)
GET|POST|DELETE /state/v1/{env_id} ; LOCK|UNLOCK /state/v1/{env_id}/lock
```

---

## 13. Sécurité — synthèse

| Surface | Mesure |
|---|---|
| Auth humains | Google OIDC + PKCE, JWKS/nonce, restriction `hd`, refresh rotatif |
| Credentials cloud | **OIDC workload par défaut** : tokens signés par run/phase, trust policies sur claims, session name = run_id (CloudTrail ↔ audit Stackd). Statique = fallback |
| Clé de signature OIDC | KMS ou volume chiffré (`oidc_signing_keys` §3.11), rotation avec chevauchement, TTL tokens courts. Wildcard `sub` autorisé **uniquement** sur le segment stack ; jamais sur `tier` ni `phase` (§10.3) |
| Secrets statiques | AES-256-GCM, write-only, déchiffrés au claim, masqués dans les logs |
| Hooks | hooks plateforme non contournables par PR ; hooks repo sans `sensitive_env` **ni credentials cloud** par défaut ; hooks repo interdits aux stages `*_apply` sur tier=prod ; masquage des valeurs sensibles dans leur stdout ; timeout ; exécution dans le conteneur du run (§8.3) |
| Mocks | apply interdit par défaut (`allow_mock_apply=false`), badge, audit `mock_consumed` |
| Workers | tokens révocables, aucun port entrant, labels (pool prod isolé) ; binaires d'outil vérifiés par checksum/signature (§7.4, supply-chain) |
| State | S3 SSE-KMS via API uniquement ; tokens scoped, RO pour PR ; locking audité |
| Permissions d'apply | tier par env × plafond `max_apply_tier` par user (§2.4) ; `can_destroy` distinct ; double verrou avec la trust policy OIDC sur le tier |
| Envs protégés | autodeploy interdit, 4-eyes (auto si tier=prod), jamais contournés (cascade incluse) ; le *droit* d'apply vient du tier, pas de `protected` |
| Proposed runs | plan-only, state RO, secrets non injectés par défaut, mocks autorisés |
| Webhooks | HMAC SHA-256 (secret par repo, `stacks.webhook_secret_encrypted` §3.1), anti-replay 5 min |
| Sessions | access JWT 15 min en header Bearer ; refresh httpOnly `SameSite=Strict` + CSRF double-submit, rotation avec détection de réutilisation → révocation de famille (§2.5) |
| Chiffrement au repos | AES-256-GCM, nonce 96 bits aléatoire par valeur, jamais réutilisé (§1) |
| Rate limiting | login, `/auth/refresh`, `/webhooks/*`, `/worker/v1/register`, claim : quotas par IP/identité (anti-bruteforce et anti-abus). MVP : middleware simple ; durcissement Phase 7 |
| Audit | append-only **au niveau DB** (rôle INSERT-only + trigger anti-update/delete §6.1), dénormalisé, transactionnel, 2 ans |

---

## 14. Tests — stratégie minimale

- **Unitaires API** : transitions (`can_apply` par tier dont apply-partout-sauf-prod, `can_destroy`, 4-eyes auto sur tier prod **et sa portée triggerer-humain-seulement**, blocage mock-apply, warn → confirmation forcée), résolution variables 5 couches + provenance, fusion des hooks (ordre plateforme→repo, exclusion des hooks repo `*_apply` sur prod), détection de cycle, cascade multi-parents, résolution mocks (réel > mock > erreur), signature/claims des tokens workload (segment de tier dans le `sub`, wildcard limité au segment stack), validation id_token Google, **rotation refresh + détection de réutilisation → révocation de famille**.
- **Intégration** : Postgres testcontainers ; **claim concurrent → un seul gagne, le perdant attrape `23505` et re-poll** ; backend HTTP avec vrai `tofu` + Garage ; idempotence logs ; **masquage des valeurs sensibles (`tfvars` + env) dans le stdout d'un hook** ; hook `after_plan` qui lit plan.json ; **fan-out WS via `LISTEN/NOTIFY`** ; **exécution unique d'une tâche périodique sous advisory lock avec 2 réplicas** ; AssumeRoleWithWebIdentity contre un mock STS (moto).
- **E2E** : compose éphémère + repo fixture + provider `local_file` → bootstrap d'une cascade 2 stacks **avec mocks** (plan mocké → apply amont → cascade réelle), assertions sur le state final ET les audit events (triggered → checked → confirmed → applied, mock_consumed).
- **Agent** : exit codes, masquage des secrets, reprise de workspace, annulation, écriture du token OIDC et export des env vars.
