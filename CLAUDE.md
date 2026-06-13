# CLAUDE.md — Instructions pour Claude Code

> Ce fichier cadre le travail de Claude Code sur **Stackd** (nom de code), une plateforme self-hostable d'orchestration Terraform. Lis-le en entier avant toute tâche, puis le document de référence concerné.

---

## 0. Avant de coder : lire le bon document

Quatre documents font autorité. **Ils priment sur ce fichier en cas de conflit de détail**, et sur tes propres a priori sur "comment on fait d'habitude".

| Document | Quand le consulter |
|---|---|
| **PLAN.md** | découpage en phases, périmètre, ordre d'implémentation, ce qui est hors-scope MVP |
| **SPECS.md** | modèle de données, state machine, protocole worker, API, audit, OIDC, hooks, mocks — **la source de vérité technique** |
| **DESIGN.md** | tout le front : tokens, composants, écrans, règles visuelles |
| **DEV.md** | environnement local, Taskfile, seed, scénario e2e |

Règle : **ne devine pas une structure déjà spécifiée**. Si tu écris un modèle de données, une transition d'état ou un endpoint, ouvre SPECS.md et reprends la définition existante au lieu d'en inventer une variante. Si quelque chose manque ou semble contradictoire, signale-le explicitement plutôt que de combler le trou en silence.

---

## 1. Ce qu'est le produit (en deux phrases)

Une plateforme qui orchestre des runs Terraform/OpenTofu (`plan` → confirmation humaine → `apply`) sur des workers self-hostés en mode pull, avec stacks multi-environnements, dépendances inter-environnements, audit complet et credentials cloud dynamiques par OIDC. **L'API est la seule source de vérité ; les workers sont stateless et jetables.**

---

## 2. Structure du monorepo

```
api/        FastAPI (Python 3.13+) — REST + WS, auth, worker API, state backend, OIDC issuer, audit
worker/     agent (Python pour tout le MVP, réécriture Go post-MVP) — poll, clone, hooks, terraform, OIDC→STS
front/      React + Vite + TypeScript (SPA)
deploy/     docker-compose.dev.yml, Helm/Terraform de déploiement (plus tard)
docs/       PLAN.md, SPECS.md, DESIGN.md, DEV.md
Taskfile.yml  orchestration (voir DEV.md)
```

Modules de l'API (cf. SPECS §2.1) : `auth/ stacks/ environments/ variable_sets/ runs/ workers/ scheduler/ audit/ oidc/ statebackend/ webhooks/ hooks/ ws/`. Un module = un domaine, avec ses routes, schémas Pydantic, et logique. Pas de fourre-tout `utils/` qui grossit sans fin.

---

## 3. Stack technique imposée

**Ne pas substituer ces choix sans accord explicite** (ils sont le résultat de décisions documentées) :

- **API** : FastAPI, Pydantic v2, SQLAlchemy 2 **async**, Alembic (migrations), PostgreSQL 18 (cf. PLAN §6 — `uuidv7()` natif, I/O async). **Gestion des paquets et environnements Python : `uv`** (pas pip, pas Poetry) — `uv sync`, `uv run`, `uv add` ; dépendances dans `pyproject.toml` + `uv.lock` committé. Pas de Redis/broker au MVP — la queue est Postgres via `SELECT ... FOR UPDATE SKIP LOCKED` (SPECS §7.2).
- **Front** : React 19, Vite 7, TypeScript strict, TanStack Query (état serveur), Tailwind v4 (plugin `@tailwindcss/vite`) + tokens CSS, shadcn/ui re-skinné, react-flow + dagre (graphes), react-virtuoso + anser (logs ANSI). Gestionnaire de paquets : **pnpm**. Détails et pistes débloquées : PLAN §6.
- **Worker** : Python + watchfiles (hot reload), runner Docker en prod / local en dev.
- **Objet** : S3 (Garage en dev) pour tfstate, logs archivés, artifacts.
- **IaC cible** : **OpenTofu en premier**, Terraform en option utilisateur (raison : licence BUSL — voir PLAN §5).

---

## 4. Invariants non négociables

Ces règles traversent tout le code. Les violer casse le modèle de sécurité ou d'audit.

1. **L'état d'un run ne change que par `transition(run, to_state, actor, payload)`** (SPECS §4.2). Cette fonction unique vérifie la légalité, fait l'update atomique gardé sur `from_state`, écrit le `run_event`, l'`audit_event` si l'action est humaine ou terminale, publie sur le WS et appelle les hooks. Jamais d'`UPDATE runs SET state=...` ailleurs.
2. **Toute action mutante écrit un `audit_event` dans la MÊME transaction DB** que l'action (SPECS §6.3). Pas de bus d'événements, pas d'écriture "après coup".
3. **Les secrets ne sont jamais loggés ni renvoyés en clair.** Variables `sensitive` : write-only via l'API, AES-256-GCM au repos, déchiffrées seulement à la construction du payload de claim, masquées dans les logs par l'agent (SPECS §13).
4. **Permission d'apply = `can_apply(user, env)`** : `role ∈ {approver, admin}` ET `max_apply_tier >= env.tier` (SPECS §2.4). `destroy` exige `can_destroy` en plus. Ce contrôle ne repose **pas** sur `protected` (qui ne fait que forcer la confirmation + 4-eyes).
5. **Concurrence : un seul run actif par environnement** (index unique partiel, SPECS §3.5). Deux envs d'une même stack peuvent tourner en parallèle.
6. **Un run ayant consommé des mocks (`used_mocks=true`) ne peut pas être appliqué** sauf `environment.allow_mock_apply=true` (SPECS §9.3).
7. **Outputs sensibles : jamais stockés, jamais propagés** dans les cascades (SPECS §9.1).
8. **Pas de browser storage dans le front** (localStorage/sessionStorage) — état en mémoire via TanStack Query / state React.

---

## 5. Conventions de code

**Python (api, worker)**
- Formatage/lint : **ruff** (format + check), via `uv run ruff`. Typage : annotations partout, `mypy` en CI. Toute commande Python passe par `uv run` (jamais d'invocation directe d'un python système ou d'un venv activé à la main).
- Async de bout en bout : routes async, SQLAlchemy async, pas d'I/O bloquant dans le boucle d'event.
- Schémas Pydantic séparés des modèles SQLAlchemy (jamais exposer un modèle ORM directement).
- IDs : UUIDv7. Timestamps : `timestamptz` UTC, suffixe `_at`. Erreurs API : RFC 9457 (problem+json).
- Tests : pytest, testcontainers pour Postgres, moto pour AWS/STS. Pas de mock de la DB — DB réelle en test.

**TypeScript (front)**
- Lint : eslint + le formateur du projet. TS strict, pas de `any` non justifié.
- Composants fonctionnels + hooks. Les events WS **invalident** les queries TanStack (ils ne patchent pas le cache à la main), sauf les logs qui sont streamés (DESIGN §6).
- **Aucune couleur en dur** : uniquement les tokens CSS (`--color-state-running`, etc.). La couleur ne porte jamais seule une information — toujours un libellé/icône en plus (DESIGN §7).
- Les composants identitaires (`PhaseRail`, `StateBadge`, `LogViewer`, `PlanDiff`, `ProvenanceBadge`, `RunActionBar`, `EnvCell`) ont une story Storybook : c'est leur contrat.

**Git / commits**
- Commits conventionnels (`feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`).
- Une migration Alembic par changement de schéma, jamais d'édition d'une migration déjà mergée.
- Messages de commit en anglais, courts et factuels.

---

## 6. Workflow de développement

Tout passe par le **Taskfile** (voir DEV.md, ne pas réintroduire de Makefile) :

```
task dev          # stack locale complète (compose + migrations + seed)
task test         # pytest + vitest
task e2e          # scénario complet de non-régression (le contrat fonctionnel)
task seed         # données de démo idempotentes
task reset        # repart de zéro
```

- En dev, l'auth se fait par **dev login** (3 personas : admin/alice/bob, tiers distincts) — pas besoin de Google. Repos Git en fixtures `file://`, Terraform sans cloud (`local_file`, `random`). Détails : DEV.md.
- Avant de marquer une tâche finie : `task test` passe, et si la tâche touche au cœur (runs, permissions, cascade), `task e2e` passe aussi.
- Le module `dev_auth` est **supprimé du build de prod** — ne jamais le rendre atteignable quand `STACKD_ENV=production`.

---

## 7. Pièges spécifiques à ce projet

- **`phase` est surchargé** : dans le payload de claim, `phase ∈ {plan, apply}` est le *type de job* ; dans la state machine et les logs, les phases sont fines (preparing/planning/checking/...). Ne pas confondre (SPECS §7.2).
- **Le state vit dans S3 mais Terraform parle au backend HTTP de l'API**, pas à S3 directement (SPECS §11). Ne propose pas de pointer Terraform sur un backend `s3` natif pour les envs `managed_state=true`.
- **Les hooks ont deux sources** : plateforme (DB, non contournable) et repo (`.stackd.yml`). Les checks de sécurité vont côté plateforme (SPECS §8).
- **`tier` est linéaire** (dev < staging < prod) : qui peut prod peut tout. Ne pas bâtir un système de policies générique par-dessus — c'est explicitement repoussé en Phase 7.
- **Mocks** : valeur réelle > mock > erreur explicite. Un mock ne sert qu'au bootstrap d'une cascade et bloque l'apply par défaut (SPECS §9.3).

---

## 8. Ce qu'il ne faut PAS faire

- Inventer un champ, un état ou un endpoint quand SPECS en définit déjà un — relis d'abord.
- Ajouter une dépendance lourde (broker, ORM alternatif, framework front) sans accord.
- Élargir le périmètre MVP : pas d'OPA, pas de SAML, pas de multi-IaC, pas de module registry (PLAN §1.2).
- Mettre du code métier dans les migrations, ou modifier l'état d'un run hors de `transition()`.
- Logguer un secret, renvoyer une variable sensible en clair, ou contourner `can_apply`.
- Réintroduire Make, du browser storage, ou des couleurs en dur dans le front.
- Terminer une tâche sans test, ou avec `task e2e` cassé sur une tâche qui touche au cœur.

---

## 9. Quand tu n'es pas sûr

Dis-le. Un "cette partie n'est pas spécifiée dans SPECS §X, je propose Y, confirme ?" vaut mieux qu'un choix silencieux qui devra être défait. Le projet a été conçu avec une cohérence inter-documents forte — la préserver compte plus que d'avancer vite sur une hypothèse.
