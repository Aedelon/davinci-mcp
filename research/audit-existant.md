# Audit de l'existant — samuelgursky/davinci-resolve-mcp v4.14.4

> Audit en lecture seule de la copie locale `~/Library/Application Support/davinci-resolve-mcp/` (v4.14.4, package npm managé) + dépôt GitHub (dernière release **v4.15.3** du 2026-10-23, 3 769 ⭐, 431 forks, 108 issues dont 3 ouvertes, 4 advisories de sécurité publiées).
> Objectif : préparer une réécriture *from scratch* (pas de copie de code — licence MIT, mais on reprend des **idées** et des **faits mesurés sur l'API**, pas des implémentations).
> Date : 2026-10-26. Méthode : grep/AST statique sur `src/` (aucune exécution de leur code, serveur non lancé, Resolve non touché), lecture des docs, `gh` pour issues/advisories/CI.

Légende de confiance : **HIGH** = vérifié dans le code/doc/GitHub ; **MEDIUM** = une seule source ou déduction raisonnable ; **LOW** = estimation.

---

## 0. Contexte stratégique à connaître avant tout

- **Blackmagic livre son propre MCP dans Resolve Studio 21.1** (sorti le 2026-09-08) — HIGH ([Digital Production](https://digitalproduction.com/2026/09/08/resolve-21-1-adds-mcp-and-finally-gets-presets/), [YMCinema](https://ymcinema.com/2026/09/17/davinci-resolve-21-1-mcp-ai-assistants-control-edit/)). Le projet audité s'aligne dessus : il cite les outils officiels `list_luts`, `generate_lut` (qui **exécute un corps de fonction Python fourni par l'appelant**), `delete_lut`, `search_scripting_api`, `get_scripting_api` (`docs/reference/lut-file-controls.md:10,70`, `docs/reference/typed-api-search.md:9`, `docs/reference/resolve-211-typed-api.md:11`). Notre MCP doit donc se démarquer **à la fois** de ce projet communautaire **et** du MCP officiel (Studio seulement).
- **Resolve 21.1 a basculé le scripting Python sur Studio.** Sur la version gratuite 21.1, le menu Workspace ▸ Scripts ne liste plus les `.py` (issue #203, confirmé sur Fedora, macOS et Windows) — HIGH. **Mais la Console de la version gratuite 21.1 reste un environnement Lua 5.1 avec le global `resolve` injecté, `bmd.readstring/writestring` et LuaJIT `ffi`** (commentaire Windows dans #203) — MEDIUM, une seule mesure contributeur. Un bridge **Lua** reste donc une piste ouverte pour la version gratuite 21.1 ; eux ne l'ont pas construit.
- **Le mainteneur ne valide en direct que sur Studio 19.1.3.7** (et sur une version gratuite 21.0.3). Tout ce qui concerne 21.0.4/21.1 est « mesuré par un contributeur, pas reproduit ici » (`docs/reference/api-coverage.md:20`, #207) — HIGH.
- Le projet sert aussi de vitrine à un produit commercial fermé (« Bradford Post Assistant », README) — HIGH. Ça explique en partie l'inflation de surface fonctionnelle.

---

## 1. Architecture

### 1.1 Volumétrie et modules (HIGH)

| Bloc | Taille | Remarque |
|---|---|---|
| `src/server.py` | **33 950 lignes**, un seul fichier | serveur « compound », 39 `@mcp.tool()`, ~650 noms d'action distincts (`grep 'action == "…"'`) |
| `src/analysis_dashboard.py` | 16 377 lignes | panneau de contrôle web (UI d'analyse) |
| `src/utils/` | ~70 modules | `media_analysis.py` 8 222 l., `api_truth.py` 3 862 l., `fuse_templates.py` 1 970 l., `edit_engine.py` 1 834 l., `destructive_hook.py` 1 606 l. |
| `src/granular/` | 13 modules, **390 outils** | un outil par méthode d'API |
| `resolve-advanced/` (Node) | ~66 k lignes JS + `aaf_probe.py` 1 629 l. | 2e serveur MCP « offline » (18 outils) |
| `install.py` | 108 Ko | installeur universel (12 clients MCP) |
| `CHANGELOG.md` | **977 Ko** | rythme de release extrême (v2.0 → v4.15 en quelques mois, des centaines de releases) |
| Tests (GitHub, pas dans le paquet npm) | 383 fichiers `tests/*.py`, dont 55 `tests/live*` ; ~4 000 tests offline (#278 : 3 916 passés + 90 échecs Windows) | |

`src/server.py` total Python `src/` = **125 808 lignes**. Monolithe : imports de plus de 80 modules utilitaires en tête de fichier (`server.py:46-200`), état global (`resolve`, `dvr_script`, `_resolve_lock`, `_bridge_lock`), enregistrement de providers par effet de bord à l'import (`server.py:993, 1202, 1333-1418`).

### 1.2 SDK MCP et transports (HIGH)

- **Python `mcp` 1.30.0** (`venv/lib/.../mcp-1.30.0.dist-info`), via `mcp.server.fastmcp.FastMCP` (`server.py:256-258`). Épinglé `mcp[cli]>=1.30,<2` dans `requirements.txt` parce que **SDK 2.0 supprime `mcp.server.fastmcp`** (issue #103). PyPI est aujourd'hui en **mcp 2.3.0** → ils sont sur la branche 1.x legacy.
- Couplage à des **attributs privés du SDK** : `mcp._mcp_server.version` (`server.py:278`), `fastmcp._tool_manager._tools`, `tool.fn`, `tool.is_async` (`server.py:~33860`, « verified on mcp 1.27 and 1.30 »), `fastmcp._mcp_server.run(...)` (`utils/mcp_stdio.py:23`, « no public injection point exists yet »). Monkey-patch de `mcp.tool` pour injecter les annotations par défaut (`server.py:375-399`).
- Transports : **stdio** par défaut, avec un wrapper stdio maison qui force LF strict en UTF-8 (`utils/mcp_stdio.py`, correctif Windows) ; **SSE** et **streamable-http** en option (`--transport`, `utils/mcp_transport.py`) : loopback par défaut, bearer token, `TransportSecuritySettings` contre le DNS rebinding.
- Serveur Node « advanced » : `@modelcontextprotocol/sdk ^1.30.0` + `zod ^4` (`resolve-advanced/package.json`).

### 1.3 Connexion à Resolve (HIGH)

`utils/resolve_connection.py:connect_resolve` essaie trois modes dans cet ordre :
1. **Bridge forcé** (`DAVINCI_RESOLVE_BRIDGE=1`) → exclusif, aucune bascule silencieuse vers un autre transport.
2. **Réseau** (`RESOLVE_SCRIPT_HOST` + `RESOLVE_SCRIPT_TIMEOUT`, 5 s par défaut) → `scriptapp("Resolve", host, timeout)`.
3. **Local** `scriptapp("Resolve")`.
4. Si rien ne répond → **repli automatique sur le bridge** s'il écoute.

`import DaVinciResolveScript` au chargement du module (`server.py:906`), avec un correctif Windows (PYTHONHOME, PATH, `os.add_dll_directory`) **avant** l'import, faute de quoi l'import provoque un access violation natif (`server.py:880-903`, issues #26, #158).

**Bridge pour la version gratuite** (`utils/resolve_bridge.py`, `resolve_bridge_ops.py`, `resolve_bridge_client.py`) :
- Un script lancé depuis Workspace ▸ Scripts (≤ 21.0.x) reçoit le vrai objet `resolve` et le ré-expose sur **127.0.0.1** via un `socketserver` en stdlib seule.
- Authentification : **HMAC-SHA256** sur une forme canonique JSON, horodatage + **nonces à usage unique** conservés pendant 2× la tolérance d'horloge (la fenêtre de rejeu est fermée), bornes pré-auth (1 Mo, lecture 5 s, 16 connexions), timeout d'opération 300 s. Configuration dans `~/.config/davinci-resolve-mcp/bridge.json`.
- Côté client, un **proxy transparent** (`BridgeProxy.__getattr__`) : chaque appel de méthode = un aller-retour signé ; les objets Resolve reviennent sous forme de *handles* opaques dans une table bornée et limitée à la session. Latence mesurée ~0,35 ms par appel. Les conteneurs tronqués (`max_items`) sont signalés (`transport.truncations`).
- Opération générique `call` **sans allowlist de méthodes** (assumé : « l'API native n'a aucune auth, refuser serait du théâtre ») ; noms dunder et privés refusés.
- Détection du modèle d'hôte (script en cours de processus ou `fuscript` enfant) pour décider bloquer ou non ; modes `exit` et `reload` (recompilation vérifiée avant l'arrêt).
- **Ne fonctionne pas en headless** (le menu exige une UI) et **ne fonctionne plus sur la version gratuite 21.1** (#203).

**Lancement automatique** (`server.py:995-1080`) : `get_resolve()` est paresseux (au premier appel d'outil), vérifie que le handle est vivant via `GetVersion()` **à chaque appel**, et **ne lance Resolve que s'il ne tourne pas du tout** (un bug passé lançait Studio en plus de la version gratuite à chaque appel). Les erreurs « non connecté » sont **contextuelles** (bridge activé, Resolve lancé mais muet, etc.) avec un champ `remediation` (`_not_connected_error`).

### 1.4 Threads et sérialisation (HIGH sur le mécanisme, MEDIUM sur sa robustesse)

- Tous les outils synchrones sont réinstallés comme coroutines qui exécutent le corps via `anyio.to_thread.run_sync` sous un **`_bridge_lock` global** (`server.py:~33870`, `_install_threaded_tool_dispatch`) : un seul appel Resolve à la fois, et la boucle d'événements stdio n'est plus bloquée (correctif de #88).
- Correctif d'annulation : `checkpoint_if_cancelled()` après le thread pour éviter le crash SDK « Request already responded to » (#272).
- **Trous** : `media_analysis` est `async` et **ne prend pas le verrou** (« assumed not to run concurrently », commentaire `server.py:874-877`) ; les jobs de fond (`utils/background_jobs.py`) tournent dans des threads démons et se coordonnent par un **fichier JSON advisory** (`utils/resolve_busy.py`), avec une fenêtre de course reconnue dans la docstring. `page_lock.py` : RLock + `fcntl` inter-processus (no-op sous Windows).
- Pas de pool, pas de file d'attente typée, pas d'annulation coopérative des opérations longues côté Resolve.

### 1.5 Modes de serveur (HIGH)

| Mode | Outils | Schéma |
|---|---|---|
| Compound (`server.py`) | **39** décorés dans le code (README et docstring disent 37, `api-coverage.md` dit 36 : **les compteurs dérivent**) | `(action: str, params: Optional[Dict[str, Any]])` partout |
| Granular (`--full` ou `resolve_mcp_server.py`) | **390** (docstring : « 377 ») | un paramètre typé par argument d'API (796 au total) |
| Advanced Node | 18 | zod |

Les deux serveurs Python sont des **processus séparés** avec des états séparés (jetons de confirmation non partagés, `utils/confirm_tokens.py`). Le mode granular n'archive pas la timeline avant mutation (README, « Security Posture »).

---

## 2. Design des outils

### 2.1 Granularité et pattern `action` (HIGH)

- Compound : 39 outils × ~650 actions. Les paramètres sont un **`Dict[str, Any]` libre** → le schéma JSON ne dit **rien** des paramètres réels. Toute la documentation des actions tient dans la **docstring en prose** (format `action(arg?, …) -> {clé}`). La validation se fait à l'exécution (`utils/contracts.py:validate`, `_guard_missing_params`), ce qui donne des erreurs tardives et rend le schéma inutile pour l'auto-complétion côté client.
- Adressage des timeline items par **position** : `track_type`, `track_index` (base 1), `item_index` (**base 0**) (`server.py:2750-2766`, docstring de `timeline_item`). C'est fragile (les index bougent après chaque édition) et le mélange base 0/base 1 est un piège. Un ID stable (`timeline_item_id` = `GetUniqueId()`) n'existe que sur certains chemins (Fusion, `_find_timeline_item_by_id`).
- Clips du Media Pool désignés par `GetUniqueId()`, mais résolus par **parcours récursif complet du pool à chaque ID** (`server.py:2931-2939`, `_find_clip`) → O(N×M), coûteux via le bridge (0,35 ms par appel).
- Timelines désignées par index base 1 (qui bouge) ; id et nom ajoutés après coup (#88).

### 2.2 Entrées et sorties (HIGH / MEDIUM)

- Pas d'`outputSchema` utile : retours `Dict[str, Any]`, laissés à l'inférence FastMCP (`structured_output=None`). Résultat : soit un schéma générique, soit un **bug** — `-> Any` sous Python 3.10 a cassé toutes les captures d'image (`Unable to serialize … Image`, #314, corrigé en v4.15.1 par `structured_output=False`). MEDIUM sur la forme exacte émise par 1.30.
- **Enveloppe d'opération** ajoutée à chaque retour sous `_operation` : `status` (`success|partial|blocked|failed`), `verification` (avec `contradiction` distincte : Resolve dit oui, la relecture dit non), `changes`, `warnings`, `execution_id` (`utils/operation_result.py`, README « Operation envelope »). Bonne idée sémantique, mais ce poids s'ajoute à **chaque** réponse.
- Erreurs **renvoyées comme payload réussi** (`{"error": {message, code, category, retryable, reason, remediation, state}}`, `server.py:1759-1800`) et non via `isError: true` → le client voit un succès MCP. Les exceptions non gérées remontent quand même.
- Images : `timeline_frame(action="capture")` renvoie un `Image` FastMCP (frame rendu par Deliver, `max_width` pour borner le coût en contexte).

### 2.3 Annotations MCP (HIGH)

`ToolAnnotations` déduites **du nom de l'outil**, pas de l'action (`server.py:330-372`) : `timeline`, `timeline_item`, `project_manager`… sont tous `destructiveHint=True`, même pour `get_items`. **Conséquence : un client ne peut pas auto-approuver les lectures.** C'est inhérent au pattern compound. `media_analysis` = `EXTERNAL_WRITE`.

### 2.4 Pagination, taille des réponses (HIGH)

- Pas de pagination par curseur sur les listes cœur (items, clips) ; quelques `limit` ad hoc (14 occurrences).
- Réponses énormes rapportées : `execute_tighten` à **226 Ko** (#84, corrigé par `include_details`).

### 2.5 Taille des schémas en tokens (estimation statique par AST, sans exécuter le serveur)

| Surface | Mesure | Estimation `tools/list` |
|---|---|---|
| Compound | 39 outils, **~106 000 caractères** de docstrings (`timeline` seul 18 742 car., `media_analysis` 10 134, `resolve_control` 9 499, `media_pool` 7 746) | **≈ 28-32 k tokens** (MEDIUM) + 15 prompts + 9 ressources |
| Granular | 390 outils, 796 paramètres, 67 000 caractères de docstrings | **≈ 60-75 k tokens** (LOW-MEDIUM : surcoût JSON Schema par outil et par paramètre estimé) |

Leur argument « compound = contexte léger » est **relatif** : ~30 k tokens en permanence, dont l'essentiel est de la prose d'actions non structurée. (Le dump dynamique de `tools/list` a été bloqué par la politique « pas d'exécution de code tiers » ; estimation `chars/4`.)

### 2.6 Primitives MCP au-delà des tools (HIGH)

- **Prompts** : 15 (`server.py:402-830`), des routeurs de workflow (`color_grade_workflow`, `conform_workflow`…).
- **Resources** : 9 statiques en compound (`status://mcp_version`, `status://resolve_connection`, `status://current_project`, `status://current_timeline`, `knowledge://topics`, `analysis://recent_reports`, `capabilities://…`), plus `resolve://…` en granular. **Pas de subscriptions ni de `notifications/resources/updated`.**
- **Pas de progress notifications** : `ctx` est accepté par `media_analysis` mais `report_progress` n'est jamais appelé ; les jobs longs passent par du polling `job_status`.
- **Pas de sampling** ni d'**elicitation** : la vision passe par le protocole maison `host_chat_paths` (le serveur rend des chemins de frames, le modèle hôte les lit puis rappelle `commit_vision`) ; les confirmations passent par des jetons maison au lieu de l'elicitation.

---

## 3. Couverture fonctionnelle

### 3.1 Domaines (outils compound, HIGH)

App et pages (`resolve_control`, `layout_presets`) · projets (`project_manager`, `_folders`, `_cloud`, `_database`, `project_settings`) · rendu (`render`, `render_presets`) · média (`media_storage`, `media_pool`, `folder`, `media_pool_item`, `media_pool_item_markers`) · timeline (`timeline`, `timeline_markers`, `timeline_frame`, `timeline_ai`, `timeline_versioning`) · items (`timeline_item`, `_markers`, `_fusion`, `_color`, `_takes`) · étalonnage (`graph`, `color_group`, `gallery`, `gallery_stills`, `lut`) · Fusion (`fusion_comp`) · extensions (`fuse_plugin`, `dctl`, `script_plugin`) · couches au-delà de l'API (`media_analysis`, `edit_engine`, `knowledge`, `setup`).

### 3.2 Fonctions au-delà de l'API Resolve

| Feature | Ce que c'est | Confiance |
|---|---|---|
| `media_analysis` (8 k lignes, 71 actions) | ffprobe/ffmpeg, Whisper, CLIP/CLAP, détection de 2-pop/clap, découpage en plans, vision via `host_chat_paths` + `commit_vision`, write-back des métadonnées et marqueurs dans le Media Pool, budgets (« caps »), DB d'analyse SQLite | HIGH |
| `edit_engine` | boucles plan → confirm → execute : selects, tighten (silences d'après le transcript), silence ripple (ffmpeg silencedetect), swap par similarité ; produit des timelines **variantes** sans toucher l'original | HIGH |
| `timeline_versioning` + `destructive_hook` | archivage auto de la timeline avant chaque mutation destructive, rollback, journal `brain_edits` (pollue le projet avec des timelines `_archived`, #83) | HIGH |
| `timeline_frame.capture` | **vrai frame rendu** (une seule frame rendue via Deliver puis restauration des réglages, de la page et de la tête de lecture) ; modes `thumbnail`, `still` | HIGH |
| `knowledge` | sert docs, guides et kernels en prose via MCP (recherche, sections, alias) | HIGH |
| `fuse_plugin` / `dctl` / `script_plugin` | templates, installation/suppression marquées MCP, `run_inline` (exécution de code arbitraire Python/Lua) | HIGH |
| `lut` | liste/installation/atténuation de LUT dans `MCP/` sous la racine maître | HIGH |
| `api_truth` | registre requêtable de 123 faits mesurés sur l'API, poussé sous forme de `known_limitation` dans les réponses | HIGH |
| Jetons de confirmation, `inspect_operation` (risque/rayon d'impact), traces d'exécution, rapports d'audit | | HIGH |
| Control panel | UI web locale (loopback + token) d'inspection et d'analyse | HIGH |
| `resolve-advanced` (Node, offline) | lecture/écriture de **.drp/.drt/.drx** sans Resolve : codec DRX (power windows, qualifiers, HSL curves calibrés contre le panneau), auteur de timelines .drt (cuts, retimes, transitions, marqueurs), patch Project.db (sous-titres, styles), conform QC, AAF/prproj, pipeline YAML → SQLite | HIGH (existence) / MEDIUM (fiabilité : #167, #168 de mauvais FrameRate, advisory CWE-88) |
| Headless (`-nogui`) | étude poussée (105 sondes GUI vs headless), instances isolées via `BMD_RESOLVE_*_DIR` | HIGH |
| Automatisation UI | **absente** (seul `osascript … activate` pour passer Resolve au premier plan, `server.py:1932`) | HIGH |

### 3.3 Que valent « 100 % API coverage » et « 93,6 % live tested » ? (HIGH)

- **100 %** = 361 méthodes sur 361 **du README legacy (révision 21.0)** embarqué (`docs/reference/api-coverage.md:9-30`). Une méthode présente sur deux classes compte deux fois. **Le stub typé 21.1 (410 méthodes, 46 TypedDicts, 513 champs) n'est pas couvert à 100 %** : 50 noms sans référence exécutable relevés à `91d03a5` (`docs/reference/resolve-211-typed-api.md`). « Couvert » veut dire « wrappé », pas « fonctionne ».
- **93,6 %** = 338 sur 361 lignes marquées ✅ **ou ⚠️**. Or ⚠️ signifie « appel accepté, a renvoyé `False`, demande un contexte particulier » : **43 lignes ⚠️ contre 295 ✅**. Le vrai taux « résultat attendu observé » est donc ≈ **295/361 = 81,7 %**. Les « 100 % de taux de réussite » sont tautologiques (une méthode non exécutable est retirée du dénominateur).
- Plusieurs ⚠️ cachent des faits majeurs : `ArchiveProject` ne produit **jamais** d'archive et fait crasher 21.1 avec les médias.
- Tests live : manuels, sur la machine du mainteneur, majoritairement Studio 19.1.3 (base de 2024).

---

## 4. Qualité

| Axe | Constat | Confiance |
|---|---|---|
| Tests | ~4 000 tests offline (pytest), `DaVinciResolveScript` **stubé par MagicMock** ; 55 harnais `tests/live*` manuels (Resolve réel, projets jetables, médias synthétiques), **pas en CI** | HIGH |
| CI | `.github/workflows/ci.yml` : Ubuntu uniquement, Python 3.10 et 3.11, ffmpeg, pytest, garde-fous de dérive (comptes d'outils, docs générées, parité API, symétrie lecture/écriture), smoke Node, handshake MCP du serveur advanced. **Rien sous Windows ni macOS** (90 échecs Windows, #278) | HIGH |
| Typage | annotations superficielles (`Dict[str, Any]`, `Optional[...]`) ; **aucun mypy/pyright** ; pas de `pyproject.toml` | HIGH |
| Lint | **pas de ruff/black** ; seulement pyflakes via `tests.test_static_undefined_names` et un test de définitions dupliquées (symptôme d'un fichier de 34 k lignes) | HIGH |
| Tests qui mentent | tests unitaires qui appellent la fonction directement sans passer par `call_tool` → #314 a échappé ; correctif : test via `mcp.call_tool` | HIGH |
| Sécurité | voir ci-dessous | HIGH |

**Sécurité** — 4 advisories GHSA :
- **GHSA-x29h-6rgf-w233 (High, 2026-10-19)** : injection d'arguments → exécution de commande dans `conform.relink_scalefix` (`find` lancé avec `footageDir` non validé, sans `--`). Classe CWE-88, versions 2.58.0 à 4.14.2. CVE demandé (#316).
- **GHSA-gmp7-qjp9-m7gm (Medium)** : suppression de projet (y compris le projet ouvert) et 13 suppressions classées HIGH qui contournaient le safe mode, le dry-run et l'audit.
- **GHSA-vh75-g46q-hgcw (Medium)** : installation/suppression de plugins et `run_inline` (exécution de code arbitraire) classés comme lectures → safe mode, dry-run et audit contournés.
- **GHSA-8f4v-j8rq-hj47 (Low)** : bearer token du transport réseau écrit en clair dans `logs/server.log`.

Leçon : **la classification lecture/écriture/destructif faite par tables parallèles au code dérive.** Plus largement : outils d'exécution de code exposés à un agent exposé au prompt injection ; scan tiers « MCPSafe AIVSS 72/100 » (#37) ; « reachscan » (#21) : le LLM peut déclencher quit/restart/suppression.

Points positifs : loopback partout, tokens en 0600, garde Host/Origin, `stdin=DEVNULL` pour les sous-processus, chemins confinés (`MCP/`) pour les écritures de LUT et DCTL.

---

## 5. Points faibles concrets, bugs et limitations

### 5.1 Dette d'architecture
1. **Monolithe de 34 k lignes** + un dashboard de 16 k lignes ; ~650 branches `if action == …` ; providers et hooks enregistrés à l'import. Testabilité et lisibilité médiocres ; les compteurs de la doc dérivent (36/37/39 outils ; 365/377/390).
2. **Schémas d'entrée vides** (`params: dict`) → aucune validation côté client, pas d'auto-complétion, et le modèle lit 30 k tokens de prose pour deviner les clés. Les erreurs de clés arrivent à l'exécution.
3. **Annotations inexactes par construction** (le niveau outil ne peut pas exprimer lecture vs écriture par action).
4. **Couplage aux internes du SDK 1.x** et blocage sur `mcp<2` (le SDK 2.x existe : 2.0.0 → 2.3.0).
5. Deux serveurs Python + un serveur Node, trois sources de vérité, état non partagé (jetons de confirmation).

### 5.2 Fiabilité et performances
6. **Concurrence imparfaite** : un verrou global, mais `media_analysis` async et les jobs de fond hors verrou ; coordination par fichier JSON advisory.
7. `get_resolve()` appelle `GetVersion()` à **chaque** appel d'outil (aller-retour supplémentaire).
8. Résolution des IDs par parcours complet du pool (`_find_clip`) et par balayage de toutes les pistes (`_find_timeline_item_by_id`) : O(N) par lookup, sans cache.
9. Opérations longues : polling `job_status` au lieu de notifications de progression ; historique de blocages (#88, #272 avec ffmpeg qui lit le flux JSON-RPC par stdin hérité, #160 Whisper en timeout mais reporté comme succès).
10. Effets de bord de `timeline_frame` : laisse le format de rendu en JPG et déclenche un dialogue bloquant « Render Path Inaccessible » (#296) ; laisse Resolve sur la page Deliver (#270).
11. Plaintes CPU (#290, non diagnostiquées).
12. Gros volumes de logs (« 128 MB server.log », `server.py:225-230`).

### 5.3 Portabilité
13. Windows : crash d'import DLL avec Python 3.13 (#158, #26), `wmic` supprimé dans Windows 11 → détection de processus cassée (#210, #150), encodage cp1252 (#153), LF/CRLF stdio (#27), 90 tests rouges (#278). Bundle Mac App Store non détecté (#299).

### 5.4 Ce qui manque (opportunités)
| Manque | Détail |
|---|---|
| **Automatisation UI** | rien pour ce que l'API ne fait pas (patch panel / Source Track Selector, Gallery panel, Magic Mask clicks, Smart Bins, rename de dossier, Render in Place, proxies). Ils le documentent comme « GUI automation = dernier recours » sans l'implémenter |
| **Progress MCP** | `notifications/progress` jamais émis |
| **Resources dynamiques** | pas de subscriptions (état projet/timeline/page/rendu poussé) ; pas de resource templates utiles côté compound |
| **Elicitation** | confirmations par jetons maison au lieu de `elicitation/create` |
| **Sampling** | vision par protocole maison ; pas de sampling (choix défendable : compatibilité clients) |
| **outputSchema / structuredContent typés** | absents |
| **Pagination par curseur** | absente |
| **Bridge Lua pour la version gratuite 21.1** | Console Lua avec `resolve` injecté non exploitée |
| **Validation 21.1 en propre** | dépendance aux contributeurs |
| **Couverture 21.1 typée complète** | ~50 méthodes sans wrapper exécutable |

---

## 6. Bonnes idées à reprendre et pièges de l'API Resolve

> **Règle : on réécrit.** Ci-dessous ce sont des **concepts** et des **faits d'API mesurés** (qui ne sont pas du code protégeable), à revalider sur nos propres builds (Studio 21.1.x). Les citations renvoient au registre `src/utils/api_truth.py` (123 entrées, dont 100 marquées `submit`) et à `docs/reference/api-limitations.md` (généré à partir de ce registre : 42 capacités manquantes, 58 bugs).

### 6.1 Idées d'architecture à reprendre (en mieux)
1. **Registre de vérités d'API versionné** (symbole, réalité, recommandation, tags, build de vérification, sonde live qui ré-dérive le fait et marque `drifted`) **poussé au point d'appel** (`known_limitation` compact). À reprendre, mais en données structurées (YAML/JSON + schéma), pas en dicts Python de 3 800 lignes.
2. **Relecture systématique (« verify by readback »)** + la notion de **contradiction** (succès annoncé, relecture en désaccord) comme signal de premier ordre.
3. **Refus des pièges destructifs** sans `acknowledge_trap` (ex. `CopyGrades`) ; dry-run exempté.
4. **Erreurs structurées** `{code, category, retryable, remediation, state}` avec un `retryable` par défaut selon la catégorie, et `retryable=False` explicite quand réessayer est inutile (bridge absent).
5. **Messages « non connecté » dérivés de la situation** (Resolve lancé ou non, édition, bridge) au lieu d'affirmations génériques.
6. **Ne jamais lancer une seconde instance de Resolve** ; lancement seulement si aucun processus.
7. **Bridge loopback HMAC + nonces + bornes pré-auth**, avec handles d'objets bornés par session.
8. **Capture de frame réellement rendue** (rendu d'une frame + restauration complète de l'état) et `max_width` pour borner le coût en tokens.
9. **Variantes de timeline au lieu de mutations en place** (edit_engine), plan → confirm → execute.
10. **Détection de runtime par l'OS** (argv `-nogui`), puisque l'API ne permet pas de distinguer headless et GUI.
11. Knowledge base servie par MCP ; prompts de workflow.
12. Gardes de dérive en CI (comptes, docs générées, parité API) — utile, mais chez nous les docs doivent être **générées depuis le code** (registre d'actions déclaratif) plutôt que vérifiées après coup.

### 6.2 Gotchas API documentés — liste exhaustive

#### A. Introspection et appels
- **`hasattr()` renvoie toujours True** sur les objets Resolve (le pont fabrique un callable pour n'importe quel nom) ; `getattr` renvoie `None` pour les noms inexistants sur 19.1.3 ; **seul `dir()` est fiable** — et encore, pour les Tools Fusion, `dir()` **omet** `GetAttrs`/`SetAttrs` qui fonctionnent pourtant (et liste `Composition` deux fois). Vérifier une méthode connue avant de croire une absence.
- **Les dicts de paramètres à clés enum doivent utiliser les constantes du handle vivant** : `AutoSyncAudio` (`AUDIO_SYNC_*`), `CreateSubtitlesFromAudio` (`SUBTITLE_*` / `AUTO_CAPTION_*`), famille CloudProject (`CLOUD_SETTING_*` / `CLOUD_SYNC_*`), `Timeline.Export` (`EXPORT_*` — ni la chaîne `'fcpxml'` ni le nom de constante ne marchent). Des clés en chaînes sont **rejetées silencieusement** (`False`, rien n'est écrit).
- Les constantes documentées `resolve.MARKER_*` (pour `AnalyzeForSlate`) **n'existent pas** sur 21.0.2.4.
- **Méthodes IA 21** (`AnalyzeForIntellisearch`, `GenerateSpeech`) renvoient une **chaîne d'erreur** (« Required package … not installed ») au lieu de `False` quand le pack Extras manque ; `AnalyzeForSlate` renvoie un `False` nu. Ne jamais faire `bool()` d'un retour IA. Aucun moyen API de savoir quels Extras sont installés (lecture de `Extras/*/log.dpl1`, non documenté).
- `PerformAudioClassification` fonctionne sans pack ; `ClearAudioClassification` remet `Category` à `'Uncategorized'` et non à `''`.
- `ResetIntellisearchAnalysis` : documenté en 21.0.2, absent de la copie antérieure du README.
- `DisableBackgroundTasksForCurrentResolveSession` : renvoie `None`, **irréversible** pour la session, s'applique à toute l'instance.
- `SetHighPriority` : sur 19.x sans argument, sans getter, **irréversible** ; sur 21.1 il faut un **vrai bool** (l'appel sans argument est un no-op qui renvoie `False` ; `1` ou `'high'` sont refusés), réversible avec `False`, toujours sans getter.
- `Resolve.GetUIManager`, `OpenProjectSettings` et `OpenPreferences` **n'existent pas** : aucun appel n'ouvre ces dialogues. `Fusion().UIManager` existe, avec 15 noms.
- Les rendus sont des méthodes de `Project` (pas d'objet séparé).
- `GetTimelineByName` **n'existe pas** : itérer `GetTimelineByIndex(1..GetTimelineCount())`.
- 21.1 déprécie `GetSetting`/`SetSetting` et `GetProperty`/`SetProperty` au profit des formes plurielles (sauf la forme à 4 arguments de Super Scale) ; les signatures surchargées sont dépréciées ; Python 2 abandonné.
- Les `subprocess` enfants **doivent avoir `stdin=DEVNULL`** sous un serveur stdio (ffmpeg lit le `c` de `"jsonrpc"` comme sa touche de commande → blocage, #272).

#### B. Pages, premier plan, panneaux (effets de bord cachés)
- `GetCurrentClipThumbnailImage` : **page Color uniquement** ET **Resolve au premier plan**, sinon `None` silencieux ; et ce n'est **pas précis à la frame** (vignette du clip, identique pour toutes les frames du clip).
- `GalleryStillAlbum.ExportStills` : `False` nu si le **panneau Gallery** n'est pas ouvert (non lisible et non ouvrable par l'API) ; `GrabStill` réussit quand même. Pour des pixels, utiliser `Project.ExportCurrentFrameAsStill`.
- `TimelineItem.ExportLUT` : `True` **uniquement depuis la page Color**.
- `TimelineItem.AddVersion` et `Graph.ApplyGradeFromDRX` : `False` pour tous les clips depuis la page **Edit** (12/12), OK depuis Color ; les lectures répondent normalement depuis Edit.
- `Timeline.DeleteClips` : exige la **page Edit**, premier essai instable ; **ne supprime pas l'audio lié** (l'API supprime exactement ce qu'on lui passe).
- **`Project.GetCurrentRenderMode` bascule sur la page Deliver** (un getter avec effet de bord !) ; `AddRenderJob` aussi. Lire la page **avant** le premier appel de rendu puis la restaurer.
- **Appels réservés à Studio sur la version gratuite** : `False` + **un modal d'upsell qui fait échouer les appels SUIVANTS sans rapport**. Détecter l'édition avant (`GetProductName()` : `'DaVinci Resolve'` vs `'DaVinci Resolve Studio'`).

#### C. Projets et base de données
- `CreateProject` **remplace** le projet courant ; s'il n'a jamais été sauvé, il disparaît. Il renvoie aussi `None` avec un modal « Save Current Project », même quand le projet courant est nommé (21.0.4.5). Contournement : `LoadProject` d'un projet propre d'abord (`CloseProject` jette les modifications non sauvegardées).
- **`SaveProject` sur « Untitled Project »** : `False` en GUI, **blocage infini en headless** (`Fusion::RemoteApp::WaitPkt`), puis l'instance se dégrade. Garde : `GetName() != 'Untitled Project'`.
- `DeleteProject` : `False` si le projet est (ou était récemment) courant, premier essai instable ; `CloseProject` le libère, `LoadProject` ne suffit pas.
- **`GetCurrentDatabase() is None`** = instance « coincée » après un arrêt sale : tout a l'air sain (`GetProductName`, `GetCurrentPage`…), mais `Create`/`LoadProject` renvoient `False` indéfiniment. **C'est le vrai test de vie.** Aucun remède sauf redémarrer.
- `ArchiveProject` : ne produit **jamais** d'archive par script (`False` instantané sans médias ; avec médias/proxies, création d'un dossier vide puis **SIGSEGV**, y compris sur 21.1.0.14).
- **Presets de réglages projet 21.1 modifiés avant le premier `SaveProject`** → **destruction du projet lui-même** malgré des retours `True` (#207). Exiger `SaveProject() == True` avant.
- `SetSetting('timelinePlaybackFrameRate')` : toujours `False` (étape à faire dans l'UI, ou dupliquer un projet modèle).
- Cloud : pas de liste, pas d'export, pas de gestion des utilisateurs.

#### D. Media Pool et import
- `ImportMedia` atterrit **toujours dans le dossier courant** (pas de paramètre de destination) → `SetCurrentFolder` avant, restauration après.
- `CreateTimelineFromClips` échoue (« Failed to create timeline ») si le **dossier courant** n'est pas celui des clips ; son clipInfo **n'a pas de champ piste**.
- `SetClipProperty('Reel Name')` : `True` mais **non persisté** quand le projet dérive automatiquement les noms de bobine (même chose avec `SetMetadata`).
- `GetClipProperty('Transcription')` : **aperçu tronqué par une ellipse** ; en 21.1, utiliser `MediaPoolItem.GetTranscription()`.
- `SetClipColor` : exactement **16 noms** (Orange, Apricot, Yellow, Lime, Olive, Green, Teal, Navy, Blue, Purple, Violet, Pink, Tan, Beige, Brown, Chocolate) ; les noms de la palette des **marqueurs** (Rose, Fuchsia, Cyan, Red, Lavender, Sky, Mint, Lemon, Sand, Cocoa, Cream) sont refusés par un `False` nu. 5 noms communs aux deux palettes rendent le piège intermittent. Sur generator/title : `True` mais **non persisté**.
- Pas de rename de dossier ; pas de Smart Bins ni de Power Bins ; pas de génération de proxy ni d'optimized media (link/unlink seulement).
- `LinkFullResolutionMedia` renvoie `False` sans fixture adéquate.
- **Import de timeline : fusion des médias du pool par une identité grossière** → deux fichiers différents créés à la même seconde peuvent se **cross-linker silencieusement** (`linked == total` ne voit rien).

#### E. Timeline et édition
- **`AppendToTimeline` clipInfo `endFrame` est EXCLUSIF** (durée = end − start) ; **`Timeline.GetEndFrame()` exclusif** aussi ; `TimelineItem.GetEnd()` aussi (intervalle `[start, end)`).
- **`recordFrame` est TIMELINE-ABSOLU** (compte depuis la frame 0 globale ; une timeline à 01:00:00:00 commence à 86400) → `recordFrame=0` place le clip une heure **avant** la timeline, sans aucune erreur.
- **Recouvrement sur une même piste** : l'item **le plus tôt gagne**, l'append suivant est **ignoré silencieusement** (pas de mode overwrite).
- **Placements d'un chunk d'append en erreur : présents en session, PERDUS à la sauvegarde** (573 → 500). La sauvegarde est la frontière de vérification : save, relire, ré-appender.
- Les retours d'`AppendToTimeline` peuvent avoir un `GetUniqueId` vide ou inexistant (enregistrement dans une plage occupée) → **26 clips perdus** chez un utilisateur. Toujours ré-énumérer avant toute suppression dépendante.
- **Sources à fps mixtes** : conversion par **arrondi inférieur** → 1 frame de trou.
- `GetSourceStartFrame` peut être décalé de 1 → vérifier le placement avec `GetLeftOffset` (exact à 100 %). Sur un item **audio**, il compte en frames du **média** ; **un WAV fige le timelineFrameRate du projet au moment de l'import**.
- Pas de setters de position (trim, slip, slide, roll, move), pas de razor/blade, pas d'insert/overwrite/replace/fit-to-fill, pas de Render in Place.
- **Vitesse et retime** : `Get/SetProperty('Speed'…)` morts (`None`/`False`) avant 21.1 ; 21.1 ajoute `Get/SetSpeed` et `Get/SetFades`. Témoin fiable de la vitesse = étendue de `GetSourceStart/EndFrame` vs durée record (`GetLeftOffset` est **aveugle** à la vitesse, par construction) ; un clip **inversé** a `start > end`. Les retimes ne passent à l'import que par **OTIO LinearTimeWarp** et **EDL M2** ; **pas par xmeml** (et un XML Premiere avec retimes place chaque clip au mauvais endroit de la source, *in / ratio*, sans erreur).
- **`Insert*IntoTimeline` (titres, generators, OFX, comps Fusion) n'ont pas de trackIndex** et atterrissent sur la cible du Source Track Selector ; **verrouiller V1 fait échouer l'insert au lieu de le rediriger** (même chose avec le verrou UI). Contournement mesuré : **timeline imbriquée** (titre seul dans sa timeline, puis append du MediaPoolItem de cette timeline avec trackIndex/recordFrame) ; le texte reste éditable via `GetMediaPoolItem().GetTimeline()` (21.0.4+). **Un compound clip coupe le lien vers le Text+** (`FusionCompCount` passe de 1 à 0).
- `InsertFusionCompositionIntoTimeline` : moyen fiable d'obtenir une comp Fusion sur une timeline vide.
- `DuplicateTimeline` **déplace silencieusement la timeline courante** vers la copie → mémoriser et restaurer avec `SetCurrentTimeline`.
- **Transitions** : ce sont des items de `GetItemListInTrack` (supprimables via `DeleteClips`) ; création native **seulement en 21.1** (`AddTransition`, `None` sans poignées) ; pas de relecture du type, de l'alignement ou de la durée. Discriminant : `GetProperty()` vide + pas de MediaPoolItem, mais generators et sous-titres lisent pareil → il faut la **géométrie** (à cheval sur un cut). Un fondu enchaîné audio a un nom **vide**.
- **Sous-titres** : pas d'édition de texte ni de timings, pas de style par l'API ; le style vit dans `Sm2TiTrack.FieldsBlob` (patch DB avec Resolve **fermé**) ; Word Highlight passe par un aller-retour DRT export → patch → import.
- `ImportTimelineFromFile` : renvoie `None` si le nom de timeline existe déjà ; **pour FCP7 XML, le `<sequence><name>` interne l'emporte sur `timelineName`** et renvoie la timeline EXISTANTE ; DRT ignore les importOptions ; l'OTIO doit avoir la **forme Resolve** (timecode-absolu), sinon `None` ; les **transitions xmeml sont importées mais inertes au rendu** ; l'EDL **perd les fondus BL**.
- **Export** : `EXPORT_FCPXML_1_10` crée un **dossier bundle** (`Info.fcpxml`) au lieu d'un fichier ; EDL vidéo seulement, bobine `AX` ; FCP7 sans `pproTicks`, bords `-1` sous les transitions ; **DRT perd les marqueurs d'items** ; **OTIO perd les marqueurs de timeline**. Fidélité : seul DRT garde tout ; AAF **remet les transforms à zéro** ; FCPXML perd une piste audio ; OTIO perd l'association à la source.
- Pistes audio clonées hors ligne : **muettes** (strip Fairlight absent dans `Sm2Sequence.FieldsBlob`).

#### F. Étalonnage
- **Pas d'édition de nodes** (ajout, suppression, connexion) ni de lecture/écriture des valeurs primaires (lift/gamma/gain, courbes, qualifiers, windows) via l'API ; seulement CDL, DRX, LUT et copie.
- **`SetCDL` en écriture seule** (pas de `GetCDL`) ; `NodeIndex` base 1 ; le nombre de nodes est dans `item.GetNodeGraph().GetNumNodes()` (`TimelineItem.GetNumNodes` déprécié).
- **`SetNodeEnabled` en écriture seule** ; `SetKeyframeInterpolation` en écriture seule.
- **`CopyGrades` REMPLACE tout le grade cible, renvoie `True`, ne crée AUCUNE version** → perte irrécupérable.
- `ApplyGradeFromStill` **n'existe pas** → `Graph.ApplyGradeFromDRX(path, gradeMode)` (0 = sans keyframes, 1 = aligné sur le timecode source, 2 = aligné sur la première frame).
- `ApplyGradeFromDRX` avec la même structure de nodes **garde la mise en page existante** (les positions du .drx sont ignorées) → `ResetAllGrades` d'abord pour réorganiser.
- **`SetLUT` ne résout que la racine LUT maître** (pas le dossier utilisateur, même en chemin absolu) ; installer dans `MCP/` sous la racine maître, puis `RefreshLUTList` ; `SetLUT(1, "")` vide le node.
- `CreateMagicMask` : piste seulement (les clics de l'opérateur sont nécessaires), modes `'F'|'B'|'BI'`.
- `ValidateDCTL` (21.1) sensible à la mise en forme du source (une fonction sur une seule ligne déclenche « no return value »).

#### G. Fusion
- **`SetInput` entre `Comp.Lock()` et `Unlock()`** : stocké et relu correctement, mais **non appliqué au rendu**. Ne verrouiller que le travail structurel.
- Comp créée par l'API : **ignorée au rendu si MediaOut ne descend pas de MediaIn**.
- `Paste()` / `LoadSettings()` avec la table en mémoire échouent à travers le pont → passer par un fichier `.setting` temporaire.
- Positions via `comp.CurrentFrame.FlowView.SetPos` ; `GetPosTable` indexée à partir de 1, format variable.
- `AddModifier` prend l'**ID de registre** (`'StyledTextFollower'`, pas `'Follower'`) ; les entrées `NestControl` (en-têtes de groupe) refusent tout modificateur.
- 19.x : les comps Fusion importées ne se rendent que via le **cache disque indexé par octets** (toute ré-écriture hors ligne = rendu noir).

#### H. Rendu
- `GetRenderCodecs` renvoie `{description: id}` (description = **clé**, l'id accepté est la **valeur**).
- Certains formats n'ont **aucun codec** (`wav` partout ; `braw`/`mts` en 21.0.4.5) → WAV seul impossible via `SetCurrentRenderFormatAndCodec`.
- **`SetRenderSettings` s'applique PAR-DESSUS le preset chargé** (un état « Audio Only » survit) → `LoadRenderPreset` d'un preset vidéo d'abord.
- **`CustomName: ''` ou `None` fait rejeter TOUT le payload** ; TargetDir ne peut pas être vidé ; CustomName n'a pas de relecture directe ; les jobs mis en file sont la **seule relecture** des réglages (`GetRenderJobList`).
- **MarkIn/MarkOut absolus, et bornés silencieusement au début de la timeline** (passer `GetMarkInOut` + `GetStartFrame`).
- `ExportSubtitle` / `SubtitleFormat` **inertes sur 19.x** (renvoient `True`).
- **`JobStatus` est une chaîne localisée** (« Concluso ») → se fier à `CompletionPercentage == 100`, à l'absence d'`Error` et à l'existence du fichier.
- `StartRendering` : retour qui dépend de l'état (21.1).
- **Supprimer ou fermer un projet pendant un rendu bloque tout le pipeline de rendu** (`IsRenderingInProgress` reste `True`, `Quit` refusé).

#### I. Runtime, headless, versions
- Une seule instance de Resolve par machine ; une seconde instance isolée (`BMD_RESOLVE_CONFIG_DIR`, `SUPPORT_DIR`, `LUT_DIR`) **sort silencieusement** → pas de workers parallèles.
- `-fastmode` : processus vivant mais **non scriptable** (port 15000 jamais ouvert).
- **Headless indétectable par l'API** (seul l'argv `-nogui` le trahit) ; écarts headless : `nested_timeline` et `SetClipsLinked` échouent, file multi-jobs et aller-retour de presets divergent.
- Le scripting réseau atteint une instance headless ; `fuscript -p Resolve` découvre les instances.
- **Python** : 3.10 à 3.12 = la plage la plus sûre ; 3.13+ peut faire renvoyer `None` à `scriptapp` sur les builds anciens (#16) ; Windows + 3.13 : access violation de DLL (#158).
- macOS : Resolve ne cherche Python que dans `PYTHON3HOME` puis `/usr/local/bin/python3` ; le préfixe doit contenir `lib/libpython3.X.dylib` **et** un `bin/python3` sans numéro de version ; `launchctl setenv` ne survit pas à un redémarrage (#182, #143).
- **Version gratuite 21.1 : plus de Python in-app** (#203) ; Console Lua disponible.
- Format `.drt` = un `.drp` accepté par `ImportTimelineFromFile` (`project.xml` et `MediaPool/MpFolder.xml` requis, le chemin uuid du SeqContainer doit être conservé) ; le start timecode est dans le blob `MediaExtents` ; le timecode source est dans `MediaStartTime` (en secondes).

---

## 7. Où on peut les battre

| # | Axe | Eux | Nous (cible) | Effort | Confiance que c'est un vrai gain |
|---|---|---|---|---|---|
| 1 | **Schémas typés** | `params: dict` + 30 k tokens de prose | outils par domaine avec `action` en **union discriminée** (oneOf/enum) et **outputSchema** typé ; descriptions courtes, détails via resource/`knowledge` | M | HIGH |
| 2 | **Budget tokens** | ~30 k (compound) / ~70 k (granular) | cible < 8 k tokens pour la liste des tools ; chargement progressif (toolset discovery / meta-tool `describe_action`) | M | HIGH |
| 3 | **Annotations exactes** | par outil, tout « destructif » | séparer `*_read` et `*_write`/`*_destructive` par domaine → auto-approbation des lectures | S | HIGH |
| 4 | **Architecture** | monolithe 34 k l., SDK 1.x privé | package modulaire (un module par domaine, registre d'actions déclaratif qui génère schéma, doc et classification de risque — **une seule source**, ce qui supprime la classe de bug des 3 GHSA) ; SDK MCP officiel 2.x en API publique uniquement | L | HIGH |
| 5 | **Exécuteur Resolve** | verrou global + trous (async, jobs) | un seul thread propriétaire de Resolve + file de commandes (acteur), annulation, timeouts par appel, horloge de santé basée sur `GetCurrentDatabase()` | M | HIGH |
| 6 | **IDs et perfs** | index positionnels, scan O(N) | IDs stables partout (`GetUniqueId`), cache d'index invalidé à la mutation, opérations en lot | M | MEDIUM-HIGH |
| 7 | **Progress / resources / elicitation** | polling, ressources statiques, jetons maison | `notifications/progress`, resources avec subscriptions (projet, timeline, page, file de rendu), elicitation pour les confirmations destructives (repli sur jeton si le client ne la supporte pas) | M | HIGH (côté protocole) / MEDIUM (support client variable) |
| 8 | **Pagination** | aucune | curseurs sur toutes les listes ; troncature explicite | S | HIGH |
| 9 | **Erreurs** | payload `error` en succès MCP | `isError: true` + `structuredContent` d'erreur typé (code, catégorie, retryable, remediation) | S | HIGH |
| 10 | **Vérité d'API** | dicts Python 3,8 k l. | registre de données versionné par build (21.1.x d'abord), sondes live automatisées et rejouables sur **notre** Studio 21.1, poussé dans les réponses | M | HIGH |
| 11 | **Automatisation UI** | absente | couche optionnelle (macOS AX/AppleScript, Windows UIA) pour les trous de l'API : patch panel, Gallery panel, rename de bin, Smart Bins, Render in Place, proxies — avec **vérification par relecture API ou capture** | L | MEDIUM (fragile entre versions) |
| 12 | **Vision** | `host_chat_paths` + rendu d'une frame via Deliver (effets de bord #270, #296) | frames en `ImageContent` direct + resource de frame ; capture sans toucher aux réglages de rendu quand c'est possible (`ExportCurrentFrameAsStill`), état restauré de façon transactionnelle | M | MEDIUM |
| 13 | **Version gratuite 21.1** | morte (#203) | **bridge Lua** via la Console (global `resolve` + LuaJIT ffi) — à prototyper | M-L | LOW-MEDIUM (une seule mesure, et Blackmagic peut le fermer) |
| 14 | **Sécurité** | 4 GHSA, `run_inline`, injection CWE-88 | aucune exécution de code arbitraire par défaut (opt-in explicite), pas de shell, `--` et validation de chemins, allowlists de racines, classification de risque générée depuis le registre, audit log | S-M | HIGH |
| 15 | **Qualité** | pas de ruff/mypy, CI Linux seulement | uv + ruff + pyright strict, CI macOS/Windows/Linux, tests de contrat via `call_tool` (pas d'appel direct), harnais live reproductible | M | HIGH |
| 16 | **Couverture 21.1 typée** | ~50 méthodes non wrappées, validées par des tiers | génération des wrappers depuis `DaVinciResolveScript.pyi` (410 méthodes, 46 TypedDicts) + validation live en propre | M | HIGH |
| 17 | **Différenciation vs MCP officiel** | — | multi-version (19 à 21.1), headless/CI, vérification par relecture, garde-fous, offline .drp/.drt (phase 2) | — | MEDIUM (le périmètre exact du MCP officiel reste à auditer) |

**Recommandation** : ne **pas** reprendre l'empilement « tout faire » (dashboard 16 k l., serveur Node de 66 k l., pipeline YAML). Viser d'abord un cœur live propre (items 1 à 10, 14 à 16), puis l'automatisation UI (11) et l'offline (.drt) en modules séparés.

---

## Sources
- Code local : `~/Library/Application Support/davinci-resolve-mcp/` (v4.14.4) — `src/server.py`, `src/utils/*.py`, `src/granular/*.py`, `docs/reference/api-coverage.md`, `docs/reference/api-limitations.md`, `docs/reference/headless-cli.md`, `docs/reference/headless-capability-matrix.md`, `docs/reference/resolve-211-typed-api.md`, `docs/reference/lut-file-controls.md`, `SECURITY.md`, `README.md`, `AGENTS.md`.
- GitHub : https://github.com/samuelgursky/davinci-resolve-mcp — issues #84, #88, #103, #158, #203, #207, #210, #243, #270, #272, #278, #290, #296, #299, #314, #316 ; advisories GHSA-x29h-6rgf-w233, GHSA-gmp7-qjp9-m7gm, GHSA-vh75-g46q-hgcw, GHSA-8f4v-j8rq-hj47 ; `.github/workflows/ci.yml`, `requirements.txt`.
- [Digital Production — Resolve 21.1 Adds MCP](https://digitalproduction.com/2026/09/08/resolve-21-1-adds-mcp-and-finally-gets-presets/) ; [YMCinema — Resolve 21.1 MCP](https://ymcinema.com/2026/09/17/davinci-resolve-21-1-mcp-ai-assistants-control-edit/) ; [CineD — Python scripting moves to Studio](https://www.cined.com/es/davinci-resolve-21-1-released-ai-assistant-integration-via-mcp-individual-hdr-trims-and-python-scripting-moves-to-studio/).
- PyPI : `pip index versions mcp` → dernière version 2.3.0.
