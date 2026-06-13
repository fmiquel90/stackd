# DESIGN.md — Décisions de design du front

> Compagnon de PLAN.md et SPECS.md. Ce document fige les décisions de design de l'interface : direction visuelle, tokens, navigation, patterns des écrans clés. Objectif : que chaque écran construit (par un humain ou par Claude Code) soit cohérent sans re-débattre des choix.

---

## 1. Sujet, audience, intention

- **Sujet** : une salle de contrôle pour l'infrastructure. On y vient pour répondre vite à trois questions : *qu'est-ce qui tourne ?*, *qu'est-ce qui attend une décision humaine ?*, *qui a fait quoi ?*
- **Audience** : ingénieurs cloud/DevOps/SRE. Utilisateurs experts, quotidiens, au clavier, souvent sur grand écran, parfois en astreinte sur un écran quelconque. Zéro besoin de séduction marketing dans l'app.
- **Intention émotionnelle** : **confiance opérationnelle**. L'interface d'un outil qui applique des changements destructifs sur de la prod doit être calme, dense, lisible, sans ambiguïté. Pas de gamification, pas de décoration, pas d'animation gratuite.
- **Anti-références** : le look "dashboard SaaS générique" (cards arrondies flottantes, dégradés, illustrations 3D) et le look "terminal hacker" (noir pur + vert acide). Les deux sont des défauts, pas des choix.

---

## 2. Direction visuelle

### 2.1 Concept : « blueprint opérationnel »

L'esthétique s'inspire des **dessins techniques et des schémas d'ingénierie** : fonds bleu-ardoise profonds, traits fins, étiquettes en monospace, hiérarchie portée par la typographie et les filets plutôt que par des boîtes ombrées. C'est l'univers naturel du sujet (on dessine de l'infrastructure) et il vieillit bien.

- **Dark-first** : le thème sombre est le thème de référence (logs, astreinte, habitudes du métier). Un thème clair existe dès le départ (même système de tokens, jamais "ajouté plus tard").
- **Densité élevée assumée** : tables compactes, line-height serré sur les données, marges généreuses uniquement autour des zones de décision. La densité est un service rendu à un expert, pas un défaut.
- **Plat et délimité par des filets** : séparations par bordures 1px et variations de fond subtiles. Pas d'ombres portées, pas de glassmorphism. Radius faible (4 px) — l'outil est anguleux comme un schéma.

### 2.2 Élément signature : le **rail de phases**

LA chose mémorable de l'interface : un **rail vertical** présent sur chaque page de run, qui matérialise la state machine (queued → preparing → planning → checking → unconfirmed → applying → finished ; `checking` n'apparaît que si des checks existent, `confirmed` est transitoire et s'affiche comme le départ du segment applying). Chaque segment :

- code couleur sémantique (cf. §3.2), segment actif animé d'une pulsation discrète (la seule animation ambiante de l'app) ;
- cliquable : navigue directement vers la section de logs de cette phase ;
- porte ses métadonnées en mono (durée, exit code, nb de checks) ;
- réutilisé en miniature **horizontale** partout où un run est listé (tableau des stacks, audit, run groups) — une signature visuelle qu'on apprend une fois et qu'on relit partout.

Le rail EST la pédagogie du produit : il rend la state machine visible au lieu de l'expliquer.

---

## 3. Tokens de design

### 3.1 Palette (thème sombre, référence)

| Token | Hex | Usage |
|---|---|---|
| `bg-base` | `#0D1117` → ajusté `#0E141B` | fond app (ardoise bleutée, jamais noir pur) |
| `bg-surface` | `#151C24` | panneaux, tables |
| `bg-raised` | `#1C2530` | éléments interactifs, hover, en-têtes collants |
| `border` | `#2A3441` | filets 1px (le matériau structurel principal) |
| `text-primary` | `#E6EDF3` | texte courant |
| `text-secondary` | `#8B98A5` | labels, méta |
| `accent` | `#D9A23B` (ambre signal) | actions de décision humaine : Confirm, focus, liens actifs |

L'**ambre** comme accent unique est un choix : c'est la couleur de la décision (confirmer un apply), elle se détache du fond bleu sans crier, et elle laisse tout le spectre vert/rouge disponible pour la sémantique d'état. Le bouton le plus important de l'app (Confirm sur prod) est ambre — pas vert, car vert dirait "tout va bien" alors qu'il dit "à toi de juger".

Thème clair : mêmes tokens inversés (`bg-base #F7F9FB`, ardoise pour le texte, ambre assombri `#A87A1F` pour le contraste AA).

### 3.2 Couleurs d'état (le langage sémantique, identique dans les deux thèmes)

| État | Couleur | Hex (dark) | Où |
|---|---|---|---|
| `queued` / neutre | gris | `#6E7B8B` | badges, rail, nœuds de graphe |
| `running` (preparing/planning/checking/applying) | bleu | `#4C8DFF` | + pulsation sur l'élément actif |
| `unconfirmed` (attend un humain) | ambre | `#D9A23B` | la même couleur que l'accent : *attendre un humain EST l'action* |
| `finished` | vert | `#3FB950` | |
| `failed` | rouge | `#F85149` | |
| `discarded` / `canceled` | gris barré | `#6E7B8B` | |

`mocked` n'est pas un état mais un **modificateur** qui se superpose à l'état courant (un run mocké est aussi `unconfirmed`, `planning`...) — rendu par un badge violet `#A371F7` distinct, volontairement hors du spectre opérationnel ("ceci n'est pas réel") : badge MOCKED, valeurs mockées.

Règle absolue : ces couleurs ne servent **que** la sémantique d'état. Jamais de bleu décoratif, jamais de vert sur un bouton non lié à un succès. C'est ce qui rend l'app lisible en un coup d'œil à 3 h du matin.

### 3.3 Typographie

| Rôle | Fonte | Usage |
|---|---|---|
| UI / corps | **IBM Plex Sans** | navigation, formulaires, prose |
| Données / structure | **JetBrains Mono** | ⭐ tout ce qui est *donnée* : IDs, SHA, noms de ressources, valeurs de variables, durées, labels d'eyebrow, en-têtes de colonnes, logs |
| Display | IBM Plex Sans SemiBold, tracking -1% | titres de pages, login, états vides |

**Règle "la donnée est mono"** : c'est le deuxième marqueur identitaire après le rail. Un `commit abc1234`, un `vpc-0f3a...`, un `+3 ~1 −0` sont toujours en mono — l'œil distingue instantanément ce qui vient du système de ce qui vient de l'interface. Échelle : 13 px base UI (densité), 12 px tables et logs, 16/20/24 px titres. Pas de graisse au-delà de SemiBold.

### 3.4 Espacement, radius, élévation

- Grille 4 px. Padding cellules de table : 6×10 px (dense), formulaires : 12 px.
- Radius : 4 px partout, 2 px sur les badges. Pas de pilules sauf badges d'état.
- Élévation : aucune ombre ; la hiérarchie = fond (`base < surface < raised`) + filets.
- Iconographie : Lucide, 16 px, stroke 1.5, toujours accompagnée d'un libellé (jamais d'icône seule sur une action destructive).

---

## 4. Architecture de navigation

```
┌──────────┬─────────────────────────────────────────┐
│ Stackd   │ topbar : breadcrumb · santé · user       │
│──────────├─────────────────────────────────────────┤
│ ▣ Stacks │                                          │
│ ⎇ Graph  │              contenu                     │
│ ▤ Audit  │   (largeur max 1440px, tables full-width)│
│ ▥ Workers│                                          │
│ ⚙ Sets   │                                          │
└──────────┴─────────────────────────────────────────┘
```

- **Nav latérale labellisée (≈ 208 px, icône + libellé texte)** : Stacks, Graph, Audit, Workers, Variable Sets, Settings, avec le wordmark `Stackd` en tête. Item actif = texte ambre + fond `raised`. Le produit a peu de sections : pas de méga-menu.
  - **Décision (révisée) : les libellés sont toujours visibles, pas seulement au survol.** Une nav icônes-seules force le *rappel* (deviner ce qu'une icône signifie) ; une nav labellisée joue sur la *reconnaissance* — plus rapide, moins ambiguë, et meilleure pour l'accessibilité (le libellé n'est pas porté par le seul `aria-label`). Pour un outil expert utilisé quotidiennement, la clarté prime sur les 150 px gagnés. L'ancienne piste « 56 px icônes + tooltip, extensible au hover » est abandonnée : le tooltip cache l'information derrière une interaction.
- **Breadcrumb structurel** : `space / stack / environment / run #142` — toujours présent, chaque segment cliquable. C'est la colonne vertébrale de l'orientation, vu la hiérarchie à 4 niveaux.
- **⌘K command palette** dès la v1 : aller à une stack/env, déclencher un run, copier un ID. Pour une audience experte, c'est la navigation principale réelle ; la nav visuelle est le fallback.
- Routing : URLs propres et partageables (`/stacks/core-network/prod/runs/142?phase=plan&line=87`) — un lien collé dans Slack pendant un incident doit arriver exactement au bon endroit.

---

## 5. Écrans clés — décisions

### 5.1 `/stacks` — la vue d'ensemble (home)

- **Table dense**, pas de cards. Une ligne = une stack ; colonnes = environnements (dev, staging, prod, dans l'ordre `position`).
- Chaque cellule env = mini-rail horizontal du dernier run + horodatage relatif + **chip de retard Git `↑3`** (mono, gris-bleu neutre — c'est une information, pas une décision : l'ambre reste réservé) quand la branche a avancé depuis le dernier apply ; version atténuée si les commits ne touchent pas le `project_root`. Tooltip = liste des commits, clic = trigger d'un run. Cellule cliquable → l'env.
- Ligne d'en-tête collante, tri, filtre par texte et par état ("montre-moi tout ce qui est failed ou unconfirmed").
- **Zone "Attention requise" épinglée en haut** : tous les runs `unconfirmed` et `failed` du space, tous environnements confondus. C'est la réponse à "qu'est-ce qui attend une décision ?" sans chercher.

### 5.2 `/runs/{id}` — l'écran le plus important

```
┌──────────────────────────────────────────────────────────┐
│ core-network / prod / run #142        [Confirm] [Discard]│ ← barre sticky
│ abc1234 "add NAT gateway" · J.Dupont · via cascade       │
├────────┬─────────────────────────────────────────────────┤
│ RAIL   │  [Plan ±]  [Checks 2✓ 1⚠]  [Logs]  [Inputs]     │
│ de     │ ┌─────────────────────────────────────────────┐ │
│ phases │ │            contenu de l'onglet              │ │
│ (§2.2) │ │                                             │ │
└────────┴─┴─────────────────────────────────────────────┴─┘
```

- **Barre d'action sticky** en haut, pas en bas : la décision est toujours visible, avec le contexte (qui a déclenché, tier de l'env). Bouton Confirm **ambre**, désactivé avec la raison en clair ("tier prod requis — votre plafond est staging", "rôle approver requis", "permission destroy requise", "run mocké — apply désactivé", "vous avez déclenché ce run prod"). Le triggerer et le confirmeur sont affichés avec leur tier.
- **Friction proportionnelle au risque.** Un apply non-prod habilité = un clic. Mais confirmer un apply sur **`tier=prod`** ou **tout run `destroy`** exige une **confirmation explicite** dans une popover ancrée au bouton : saisie du **nom de l'environnement** (pattern GitHub, cohérent avec les suppressions §5.7) + récapitulatif `+a ~c −d` du plan, les destructions en évidence. Cohérence avec le principe : supprimer une stack demande déjà de taper son nom — appliquer une destruction sur prod ne peut pas être moins exigeant qu'éditer de la config. La popover ne contourne aucune règle d'accès (`can_apply` reste évalué côté API) : c'est un garde-fou anti-erreur-de-clic, pas un contrôle de permission.
- **Bandeau d'obsolescence** : si la branche trackée avance pendant qu'un plan attend confirmation, bandeau au-dessus du contenu — "Plan calculé sur `abc1234` · la branche a avancé de 2 commits" — avec action *Re-plan*. Confirmer reste possible (appliquer un commit précis est légitime), mais jamais sans le savoir.
- En-tête : triggerer ET confirmeur affichés (avatars Google) — l'audit est dans l'interface, pas caché dans une page.
- Onglet **Plan** : résumé `+3 ~1 −0` en mono géant, puis liste des ressources groupées par action (create/update/delete), chaque ressource dépliable en diff attribut par attribut (ajouté vert / modifié ambre / supprimé rouge, valeurs en mono). **Les destructions sont toujours dépliées par défaut** — on ne cache jamais un delete.
- Onglet **Checks** : un bloc par hook after_plan (nom, statut, durée, sortie), warn = ambre avec la mention "confirmation manuelle requise".
- Onglet **Inputs** : variables résolues avec leur **provenance en badge mono** (`set:common-aws`, `stack`, `env`, `dependency`, `MOCK` en violet). Valeurs sensibles : `•••` sans bouton "révéler".

### 5.3 Visionneuse de logs (dans la page run)

- Plein écran disponible (`f`), fond `bg-base`, JetBrains Mono 12 px, line-height 1.5.
- Sections repliables par phase **et par hook** (chevron + durée + exit code dans l'en-tête de section).
- Numéros de ligne en gouttière, clic = ancre partageable, surlignage de la ligne ciblée à l'arrivée.
- Follow-tail actif par défaut sur un run en cours ; le moindre scroll vers le haut le suspend (bouton flottant "↓ Reprendre le suivi" avec compteur de nouvelles lignes).
- Rendu ANSI complet (palette mappée sur nos tokens, pas les couleurs terminal brutes). Recherche `⌘F` interne avec compteur d'occurrences. Toggle timestamps. Virtualisation obligatoire (react-virtuoso).

### 5.4 `/graph` et run groups

- react-flow, layout dagre gauche→droite, **filtre par nom d'environnement en tête** (voir le graphe "prod" sans le bruit de dev).
- Nœud = carte minimale : stack/env + mini-rail du dernier run. Arête étiquetée du nombre d'output references ; arête pointillée violette si des mocks sont en jeu.
- Vue run group : même graphe, les nœuds se colorent en temps réel pendant la cascade. Pas de confettis à la fin — un état `finished` vert suffit.
- **Alternative accessible obligatoire** : un graphe react-flow n'est pas navigable au lecteur d'écran ni au clavier seul. Toggle **« vue liste »** systématique = table d'adjacence (`env amont → env aval · N references · MOCK?`) triable, focusable, avec le même filtre. Le graphe est l'affichage par défaut ; la liste est l'équivalent fonctionnel complet, pas un pis-aller. Les nœuds du graphe restent atteignables au `Tab` (ordre topologique) avec annonce `aria-label` de l'état du dernier run.

### 5.5 `/queue` — la file d'exécution

La réponse visuelle à "pourquoi mon run ne part pas ?".

- Deux groupes : **En cours** (runs claimés, avec worker, phase active et durée) et **En attente** (`queued` + `confirmed` non claimés), triés par ancienneté.
- Chaque run en attente affiche sa **raison de blocage calculée par l'API**, en clair et en mono : `run #141 actif sur cet environnement`, `environnement verrouillé`, `aucun worker compatible avec {pool: prod}`, `réservation d'affinité apply (worker-aws-1, 38s restantes)`.
- Filtre par stack/env/pool ; lien direct vers le run bloquant et vers `/workers`.
- Un compteur de file dans la nav latérale (badge sur l'icône) quand des runs attendent depuis > 2 min — signal discret, pas de toast.

### 5.6 `/audit`

- Table chronologique dense : horodatage mono, avatar + email, action en badge, cible en breadcrumb cliquable, contexte résumé.
- Les actions ⭐ (`run.confirmed`, `run.applied`) ont une rangée légèrement contrastée.
- Filtres combinables en barre supérieure (acteur, action, stack, env, période) reflétés dans l'URL → une investigation se partage par lien. Export CSV discret à droite.

### 5.7 Formulaires (wizard stack, variables, hooks)

- Une colonne, max 560 px, labels au-dessus, aide en `text-secondary` sous le champ.
- Tout nom technique saisi (stack, env, variable) s'affiche en mono dès la frappe.
- Les actions destructives exigent la saisie du nom de la cible (pattern GitHub) — pas de simple modal "Êtes-vous sûr ?".
- Wizard : étapes numérotées car c'est une vraie séquence ; chaque étape validable indépendamment (le check-repo se lance à l'étape 1, pas à la fin).

---

## 6. Patterns temps réel

- Un seul WebSocket multiplexé ; TanStack Query reste la source de vérité (les events WS **invalident** les queries, ils ne patchent pas le cache à la main — sauf les logs, streamés directement).
- Changement d'état d'un run visible partout où il apparaît (rail, badges) sans refresh, via abonnement aux topics des entités affichées.
- Indicateur de connexion discret dans la topbar ; en cas de coupure : bandeau fin "Reconnexion…" + rattrapage `after_seq`, jamais de modal bloquante.
- Pas de toast pour les événements de fond (un run qui se termine se voit sur son rail). Les toasts sont réservés aux retours d'actions de l'utilisateur courant.

---

## 7. Accessibilité, états, qualité

- Contraste AA minimum sur les deux thèmes (vérifié sur les couleurs d'état sur leurs fonds réels).
- **La couleur n'est jamais le seul porteur d'état** : chaque badge a son libellé texte, le rail a des icônes par segment (✓ ✕ ⏸ ▶). Daltonisme = cas nominal dans ce métier.
- Navigation clavier complète : `j/k` dans les tables, `f` plein écran logs, `⌘K` palette, focus visible ambre 2 px. `prefers-reduced-motion` : la pulsation du rail devient un changement d'opacité statique.
- **Skip-link** « Aller au contenu » en premier focusable (nav latérale + breadcrumb = beaucoup de tab stops avant le contenu sur une app dense).
- **Régions live polies, jamais de vol de focus** : les changements d'état temps réel (rail, badges, bandeau « Reconnexion… ») s'annoncent en `aria-live="polite"` et ne déplacent jamais le focus de l'utilisateur (il peut être en train de lire des logs ou de remplir un formulaire). Les toasts d'action utilisent `aria-live` et ne capturent pas le focus.
- **Chiffres tabulaires** : `font-variant-numeric: tabular-nums` sur toute donnée alignée en colonne (durées, `+a ~c −d`, compteurs, horodatages) — JetBrains Mono est déjà chasse fixe, mais on force les tabular-nums pour que les colonnes ne dansent pas au streaming.
- **Boutons d'action async** : `Confirm`/`Discard`/`Trigger`/`Re-plan` passent en état `loading` (spinner + disabled) pendant l'appel, pour éviter la double soumission — distinct du `disabled` « permission manquante » qui, lui, porte la raison.
- **Parcours mobile d'astreinte** : sur les deux écrans soignés mobile (lire un run/logs, confirmer/discarder), les cibles tactiles respectent **44 px** min ; ailleurs la densité desktop prime.
- États vides = invitations à agir, dans le vocabulaire du produit : "Aucune stack. Connectez un repo pour commencer." + bouton. Pas d'illustrations décoratives.
- Erreurs : factuelles, en français technique clair, avec l'action de sortie ("Le webhook a été rejeté : signature HMAC invalide. Vérifiez le secret dans Settings → Webhooks."). Jamais d'excuses, jamais de vague.
- Skeletons fidèles à la forme finale pour les tables ; spinners réservés aux actions ponctuelles.
- Responsive : optimisé desktop (métier), mais consultation mobile soignée pour deux parcours précis — **lire un run/ses logs et confirmer/discarder** (astreinte). Le reste peut dégrader.

---

## 8. Implémentation

- **Tailwind v4 + tokens CSS custom properties** (`--color-bg-base`, `--color-state-running`, ...) : les deux thèmes = deux jeux de variables, les composants n'utilisent que les tokens. Aucune couleur en dur dans un composant.
- **shadcn/ui comme base mécanique** (Dialog, Popover, Command, DropdownMenu) **re-skinné intégralement** sur nos tokens : on garde l'accessibilité Radix, on remplace l'esthétique par défaut (qui est précisément le look générique qu'on refuse).
- Composants maison (le cœur identitaire) : `<PhaseRail>` (+ variante `mini`), `<StateBadge>`, `<LogViewer>`, `<PlanDiff>`, `<ProvenanceBadge>`, `<RunActionBar>`, `<EnvCell>`.
- Storybook (ou Ladle) dès la Phase 0 pour ces composants : c'est le contrat visuel que Claude Code doit respecter en construisant les pages.
- Fontes self-hosted (IBM Plex Sans, JetBrains Mono) — l'app peut tourner en réseau privé sans CDN externe. `font-display: swap`, preload des seules graisses critiques (Sans 400/600, Mono 400), métriques de fallback (`size-adjust`/`ascent-override`) pour éviter le CLS au chargement.
- **Préférence de thème sans browser storage** (invariant produit : ni localStorage ni sessionStorage). Conséquence : le thème suit `prefers-color-scheme` par défaut ; un override manuel vit **en mémoire** (perdu au reload) et, si une persistance par utilisateur est souhaitée, elle est **stockée côté serveur** sur le profil (`GET /me`), jamais dans le navigateur. À acter au moment du build de la topbar (toggle thème).
- Graphes : react-flow + dagre. Logs : react-virtuoso + anser (ANSI). Dates : `Intl` natif, format relatif < 24 h puis absolu ISO local.

---

## 9. Ce qu'on s'interdit (rappel)

- Ombres portées, dégradés, glassmorphism, illustrations décoratives, emojis dans l'UI.
- Couleurs d'état utilisées pour autre chose que l'état ; accent ambre utilisé pour autre chose que la décision/le focus.
- Icône seule sur une action destructive ; confirmation sans saisie du nom pour les suppressions **et pour tout apply `tier=prod` ou `destroy`** (§5.2).
- Toasts pour les événements de fond ; animations non sollicitées hors pulsation du rail.
- Masquer ou replier une destruction de ressource dans un plan.
- Tout texte système (ID, SHA, valeur, durée) dans la fonte UI : la donnée est mono, toujours.
