# davinci-mcp — Document de cadrage

> Issu de 64 questions de cadrage (2026-10-26), amendé après la phase de recherche ([RECHERCHE.md](RECHERCHE.md) §4). Copyright Delanoe Pirard / Aedelon — Apache 2.0.

## 0. Positionnement

Deux concurrents :
- le **MCP natif Blackmagic** (Studio 21.1+), générique : doc + exécution de code ;
- **samuelgursky/davinci-resolve-mcp**, large mais non typé.

`davinci-mcp` se distingue par :
- des tools typés et vérifiés ;
- la sûreté ;
- un budget de tokens maîtrisé ;
- la vision ;
- un canal interchange hors ligne ;
- le support de la version gratuite (pont Lua) ;
- un mode compagnon du MCP natif.

## 1. Vision

| Sujet | Décision |
|---|---|
| Objectif | Projet open source public, référence du marché |
| Rapport à l'existant | Réécriture from scratch, inspirée du MCP Resolve GitHub (audit préalable) |
| Cibles | Monteurs pros, créateurs de contenu, étalonneurs, développeurs/pipeline |
| Nom | `davinci-mcp` |

## 2. Cible Resolve

- **Version** : Resolve 20+.
- **Éditions** : Studio (scripting externe) + gratuite via un pont interne : **Python pour ≤ 21.0**, **Lua pour ≥ 21.1** (Python y est retiré, Lua tourne dans un bac à sable ; transport à valider expérimentalement).
- **OS** : macOS, Windows, Linux.
- **Multi-versions** : détection de capacités à l'exécution ; tools indisponibles masqués ou désactivés.

## 3. Stack

- Python + **uv**, ruff, pytest.
- **FastMCP ≥ 4.1** (standalone ; la 2.x n'est plus maintenue), conforme à la **spec MCP 2026-07-28** (sans état, `server/discover`, `subscriptions/listen`).
- Python **3.14** (aligné sur `ResolvePython`, le Python intégré à Resolve 21.1), compatible 3.13 pour les interpréteurs externes.
- **Schémas générés depuis le stub officiel** `DaVinciResolveScript.pyi` (21.1+ : TypedDict → modèles Pydantic, Literal → enums), complétés par l'introspection live (Fusion, méthodes dépréciées encore actives, clés réelles).
- Transports : **stdio + Streamable HTTP**.
- Config : `~/.config/davinci-mcp/config.toml`, surchargé par variables d'environnement.

## 4. Architecture de connexion

- Serveur MCP **externe** (DaVinciResolveScript), avec repli sur un **pont interne** (Python ≤ 21.0, Lua ≥ 21.1). Routage automatique : API Studio → pont → interchange. Chaque résultat indique le canal utilisé.
- **Mode compagnon** : si `ResolveMCP` (le MCP natif) est présent, on réutilise sa doc et son exécution de code au lieu de les dupliquer.
- Lancement auto de Resolve, désactivable.
- Appels longs → **extension MCP Tasks**, avec repli `job_status` / `job_wait` / `job_cancel` pour les clients sans Tasks, et des progress notifications.
- Reconnexion auto transparente (détection d'objet mort, un nouvel essai).
- **Sérialisation** de tous les appels vers Resolve (API non thread-safe) ; plusieurs clients acceptés en HTTP.

## 5. Design des tools

- Granularité **hybride** : tools par domaine avec paramètre `action` + tools workflow haut niveau.
- **Aucune limitation** du nombre de tools exposés (pas de profils ; la liste ne varie pas selon la session, conformément à la spec). **Ordre déterministe**, instructions serveur soignées, 3 à 5 tools pivots toujours chargés (`alwaysLoad` : statut, contexte, recherche d'API) ; compatible avec la recherche d'outils côté client.
- Retours **JSON structuré** (Pydantic, `outputSchema`, `structuredContent`) + résumé texte court.
- Identifiants : `GetUniqueId()` / registre d'handles en priorité, nom/index acceptés en fallback.
- Opérations par lot natives (listes) partout où c'est utile.
- Pagination + filtres + sélection de champs pour les gros résultats.
- Cache court avec invalidation à chaque écriture.

## 6. Périmètre fonctionnel

### Couverture API
- **100 % de l'API** (397 méthodes live en 21.0.4, le README en couvre la quasi-totalité) **+ toute la surface de paramètres** : clés de settings et de propriétés, enums, formats et codecs, typés depuis l'introspection. **+ Fusion** (1 270 outils enregistrés, 886 actions), et un diff automatique de toute cette surface à chaque version.
- Domaines : **tous** — projet, media storage/pool, timeline, items, markers, couleur, gallery, Fusion, Fairlight, rendu.
- Tool d'exécution de code arbitraire (Python/Lua) : **présent, désactivé par défaut**.

### Au-delà de l'API (dès la v1)
- Automatisation UI par **raccourcis clavier + menus**, backend **macOS d'abord**, interface abstraite pour Windows/Linux.

### Fusion
Graphe de nœuds complet (inputs, keyframes, expressions), macros & templates/Text+, import/export `.setting`, génération/installation de Fuses et DCTL.

### Couleur
Nœuds, LUT, CDL ; versions, stills, albums, PowerGrades ; copie de grade en masse ; analyse visuelle (frames, avant/après).

### Fairlight
API native au maximum + traitement externe ffmpeg (normalisation, mixage, extraction) puis réimport.

### IA Resolve
Toutes les fonctions IA exposées par la version détectée (sous-titres auto, détection de scènes, Magic Mask, Smart Reframe…).

### Vision de l'agent
Export de frame (image MCP), capture d'écran UI, planche contact, métadonnées ffprobe.

### Workflows
Tools haut niveau + **recettes extensibles** : YAML pour le simple, plugins Python pour le complexe.

### Interchange hors ligne
Canal de premier rang : écriture et lecture FCPXML, EDL, OTIO, CSV de markers, `.cube`, DRX. Il sert pour la version gratuite, quand Resolve n'est pas lancé, et pour les opérations absentes de l'API (insert, trim, blade) : on génère la timeline, puis on l'importe.

### Titres OGraf
Génération de titres et sous-titres animés en HTML/CSS/JS (standard EBU OGraf, supporté comme Titles sur la page Edit).

## 7. Fonctions MCP

- **Resources** avec abonnements (`resolve://project`, `resolve://timeline/current`, …).
- Quelques **Prompts** (monter un Reel, préparer une livraison, audit de projet).
- Progress notifications et elicitation (pattern MRTR de la spec 2026-07-28). **Sampling et logging MCP retirés** (dépréciés) : les logs vont en fichier.
- Extension **Tasks** pour les opérations longues.
- Base de connaissances (doc API + guides) via un **tool de recherche**.

## 8. Sécurité et sûreté

- Annotations précises par action (`readOnlyHint`, `destructiveHint`, `idempotentHint`) + paramètre `dry_run` + **jeton de consentement** pour les opérations destructives : le dry-run renvoie un plan et son hash, puis l'appel réel exige `confirm_token`. Si le client sait le faire, on passe par une elicitation ; la confirmation est imposée dans le code.
- **Reçu après chaque écriture** : on relit l'état et on renvoie avant/après avec `verified: true|false`.
- **Snapshot auto** (export `.drp` / duplication de timeline) avant opérations lourdes, configurable.
- Échec en cours de batch : arrêt, rapport détaillé réussi/échoué, pointeur vers le snapshot.
- Accès fichiers limité à des **racines autorisées** déclarées dans la config (MCP roots retiré : déprécié).
- HTTP : écoute `127.0.0.1` par défaut, **token bearer** obligatoire si exposé au réseau.
- Exec de code : désactivé par défaut, avertissement à l'activation.

## 9. Qualité

- Tests : **mocks** (CI) + **intégration réelle** contre Resolve (local).
- Mock **généré par introspection** (enregistrement signatures + réponses réelles).
- Typage **strict** : ruff étendu + pyright/mypy strict, stubs typés de l'API Resolve.
- Couverture **≥ 90 %**.
- Mesure du « mieux optimisé » : benchmarks de latence, tokens consommés (schémas + réponses), taux de réussite agent sur scénarios.

### Scénarios d'évaluation de référence
1. Reel vertical sous-titré (import, montage, sous-titres stylés, rendu 9:16).
2. Conformation / livraison (XML/EDL, relink, rendus multi-formats).
3. Étalonnage de série (LUT d'entrée, match, color groups, stills).
4. Motion design Fusion (titre animé, lower third, macro réutilisable).

## 10. Observabilité

Logs JSON rotatifs, journal d'audit rejouable des actions modifiantes, traces OpenTelemetry **opt-in** (endpoint choisi par l'utilisateur, désactivé par défaut, rien envoyé ailleurs).

## 11. Distribution et documentation

- Installation : `uvx davinci-mcp`, `uv tool install` / pip, commande `davinci-mcp install` (config des clients + installation du script-pont), bundle `.mcpb`.
- Clients documentés : Claude Desktop/Code, Cursor/VS Code, autres (Codex, Gemini CLI…).
- Doc : README + site MkDocs avec référence des tools générée depuis les schémas. **Anglais**.
- Publication : PyPI + registre MCP officiel.
- Dépôt GitHub **privé jusqu'à la v1.0**, puis public ; contributions : **issues seulement**.
- SemVer + changelog automatique (conventional commits), publication PyPI automatisée.
- Maintenance : diff d'introspection automatisé entre versions de Resolve.

## 12. Méthode et planning

1. **Recherche** : audit du MCP GitHub existant, introspection de l'API du Resolve installé, veille des autres MCP NLE (Premiere, After Effects…).
2. **Socle** (à la main, dans la session principale) : connexion/pont, sérialisation, registre d'IDs, cache, jobs, config, sécurité, conventions de tools, mock par introspection.
3. **Domaines en parallèle** : un agent `implementer` par domaine, sur brief fermé, une fois le socle et les conventions figés.
4. **Évaluation** : scénarios de référence + benchmarks contre le MCP existant.
- Le MCP Resolve actuellement installé reste actif jusqu'à la bascule (parité atteinte).

## Points à vérifier

- ~~Version Python supportée~~ : Python 3.14 intégré en 21.1 ; un Python externe ≥ 3.6 reste accepté (3.13 vérifié en 21.0.4 et 21.1.1).
- ~~Tools de `ResolveMCP` et API async 21.1.1~~ : relevés, voir [research/introspection-21.1.md](research/introspection-21.1.md).
- Pont Lua sur la version gratuite 21.1 : transport possible (socket, fichiers ou Console) et limites du bac à sable.
- Fonctions IA réellement exposées par l'API (Magic Mask, Smart Reframe) ou disponibles seulement dans l'interface.
- Support de l'extension Tasks et de MCP Apps par Claude Code et Claude Desktop.
- ~~Licence du MCP existant~~ : MIT (vérifié). On réécrit sans copier de code.
