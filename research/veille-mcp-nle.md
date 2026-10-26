# Veille technique — MCP NLE / VFX / audio et design MCP (pour `davinci-mcp`)

> Date de la veille : 2026-10-26. Méthode : recherche web (WebSearch/WebFetch), API GitHub (`gh api`, étoiles et `pushed_at` relevés le 2026-10-26), registres PyPI / npm / registre MCP officiel. Aucun code tiers installé ni exécuté.
> Copyright Delanoe Pirard / Aedelon — Apache 2.0.

Légende des niveaux de confiance :
- **HIGH** : vérifié sur la source primaire (dépôt, doc officielle, spec, registre).
- **MEDIUM** : une seule source secondaire (blog, presse, README marketing) ou chiffre auto-déclaré.
- **LOW** : extrait de moteur de recherche non ouvert ou sources contradictoires.

---

## 0. Ce qu'il faut retenir (et qui change le cadrage)

1. **Resolve Studio 21.1 contient un serveur MCP natif** (`ResolveMCP`, stdio), sorti le 2026-09-08. Il expose quelques tools génériques, dont `run_script` (Python sandboxé, objet `resolve` injecté) et `run_script_unsafe`. C'est le concurrent n°1 : `davinci-mcp` doit apporter ce que des scripts générés à la volée ne donnent pas (tools typés, sûreté, vision, workflows, édition gratuite). Sources : [aspect.inc docs](https://aspect.inc/docs/mcp/davinci-resolve), [voyager.so](https://voyager.so/blog/davinci-resolve-mcp), [CineD](https://www.cined.com/davinci-resolve-21-1-released-ai-assistant-integration-via-mcp-individual-hdr-trims-and-python-scripting-moves-to-studio/). Confiance **MEDIUM** (liste de tools tirée de guides tiers, pas de la doc Blackmagic).
2. **Édition gratuite 21.1 : Python a disparu**, y compris dans *Workspace > Scripts*. **Lua y tourne encore**, mais dans un bac à sable (`io`, `os.execute`, `require`, `bmd.readfile/writefile` bloqués, `UIManager` nul, `print` muet), d'après un test publié sur 21.1.0.17. Le pont interne prévu au cadrage doit donc être **en Lua** pour le gratuit ≥ 21.1. Sources : [xere.my](https://xere.my/journal/davinci-resolve-21-1-free-python-scripting-removed-lua-benchmark/), [Puget Systems](https://www.pugetsystems.com/blog/2026/09/10/how-davinci-resolve-free-v21-1-scripting-changes-affect-puget-bench/). Confiance **MEDIUM** : les sources se contredisent sur le transport possible (voir §4.4).
3. **FastMCP 2.x est obsolète** : la version courante est **4.1.3** (2026-10-20) ; la 4.0 (2026-08-31) supporte la spec **2026-07-28**. Le dernier backport 2.x date du 2026-04-13 (2.14.7). Sources : [PyPI fastmcp](https://pypi.org/project/fastmcp/), [gofastmcp.com/updates](https://gofastmcp.com/updates). Confiance **HIGH**.
4. **La spec MCP courante est 2026-07-28** : protocole sans état (plus d'`initialize`), `server/discover`, `subscriptions/listen` à la place de `resources/subscribe`, pattern MRTR pour l'elicitation, Tasks déplacés dans une extension officielle. **Sampling, Roots et Logging sont dépréciés.** Plusieurs points du cadrage §7–8 sont à revoir (sampling, logging MCP, MCP roots). Source : [changelog spec 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/changelog.md). Confiance **HIGH**.
5. **`tools/list` ne doit plus varier selon la connexion** (spec 2026-07-28). Le pattern « activer un domaine pour cette session » (hoyt-harness) n'est plus conforme. Pour un grand catalogue, il reste trois options : la recherche d'outils côté client (Claude Code la fait par défaut), une passerelle `search`/`describe`/`execute` côté serveur, ou la recherche intégrée à FastMCP. Source : [spec tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools.md). Confiance **HIGH**.

---

## 1. Autres MCP DaVinci Resolve

Point de référence, exclu de l'analyse détaillée : **samuelgursky/davinci-resolve-mcp**, 3 769 ★, push 2026-10-23, npm `davinci-resolve-mcp` 4.15.3 (2026-10-23) ([GitHub](https://github.com/samuelgursky/davinci-resolve-mcp), [npm](https://www.npmjs.com/package/davinci-resolve-mcp)). **HIGH**

### 1.1 Serveur natif Blackmagic (Studio 21.1+)

| Élément | Constat | Source | Confiance |
|---|---|---|---|
| Disponibilité | Studio 21.1+, *File > Setup AI Assistants*. Exige *External scripting = Local* et *Automatic scripted actions = Allow safe* | [aspect.inc](https://aspect.inc/docs/mcp/davinci-resolve), [grainzilla](https://grainzilla.com/how-to-let-claude-drive-davinci-resolve-21-1-through-its-built-in-mcp-server/) | MEDIUM |
| Binaire / transport | `ResolveMCP` en stdio, sans arguments. macOS : `…/DaVinci Resolve.app/Contents/Applications/ResolveMCP` ; Windows : `ResolveMCP.exe` ; Linux : `/opt/resolve/bin/ResolveMCP`. Bundle `DaVinciResolve.mcpb` pour Claude Desktop. Pas de mode réseau | [aspect.inc](https://aspect.inc/docs/mcp/davinci-resolve), [voyager.so](https://voyager.so/blog/davinci-resolve-mcp) | MEDIUM |
| Tools | `get_resolve_status`, `get_whats_new`, `search_scripting_api`, `get_scripting_api`, `get_scripting_docs`, `launch_resolve`, `run_script` (sans accès fichiers, réseau ni processus), `run_script_unsafe` (accès système). Un forum parle de « 14 tools » : la liste est peut-être incomplète | [aspect.inc](https://aspect.inc/docs/mcp/davinci-resolve), [voyager.so](https://voyager.so/blog/davinci-resolve-mcp) | MEDIUM |
| Approche | « Doc + exécution de code » : l'agent cherche dans la doc puis écrit du Python contre l'API | [Y.M.Cinema](https://ymcinema.com/2026/09/17/davinci-resolve-21-1-mcp-ai-assistants-control-edit/), [grainzilla](https://grainzilla.com/how-to-let-claude-drive-davinci-resolve-21-1-through-its-built-in-mcp-server/) | MEDIUM |
| Doc officielle | Manuel 21.1, section « Using DaVinci Resolve with AI Assistants », p. 4318 | [Y.M.Cinema](https://ymcinema.com/2026/09/17/davinci-resolve-21-1-mcp-ai-assistants-control-edit/) | MEDIUM |
| API 21.1 | « 20 nouvelles API de scripting » plus une console intégrée (Studio) ; `AddTransition()` apparaît | [CineD](https://www.cined.com/davinci-resolve-21-1-released-ai-assistant-integration-via-mcp-individual-hdr-trims-and-python-scripting-moves-to-studio/), [pilotcut](https://www.pilotcut.com/blog/davinci-resolve-studio-21-1-mcp-api-gaps-vs-pilotcut) | MEDIUM |
| API 21.1.1 | API **asynchrones** pour Quick Export, transcription, détection de plans, Smart Reframe et création de sous-titres. Signatures et mode de complétion non publiés dans le résumé. Option « toutes les couches de nœuds » pour `ApplyGradeFromDRX` | [Digital Production](https://digitalproduction.com/2026/10/06/davinci-resolve-21-1-1/) | MEDIUM |
| Lacunes connues de l'API | Pas de setter trim/slip/slide/roll/move, pas de lame (blade), pas d'écriture de vitesse fiable, pas d'édition du texte des sous-titres, pas d'insert/overwrite/fit-to-fill natifs. Certains `True` sont perdus à la sauvegarde. Miniatures dépendantes de la page Color | [pilotcut](https://www.pilotcut.com/blog/davinci-resolve-studio-21-1-mcp-api-gaps-vs-pilotcut) (post marketing) | LOW–MEDIUM |

### 1.2 Serveurs communautaires

Étoiles et date de dernier push relevées via `gh api` le 2026-10-26 (**HIGH** pour ces deux colonnes). Les fonctionnalités et nombres de tools viennent des README (**MEDIUM**, auto-déclarés).

| Projet | ★ / push | Release | Approche | Features originales |
|---|---|---|---|---|
| [hiteshK03/davinci-resolve-mcp](https://github.com/hiteshK03/davinci-resolve-mcp) | 147 / 2026-03-28 | — | Pont Python *dans* Resolve (Workspace > Scripts) + HTTP localhost. 162 tools annoncés | Remplacements IA locaux des fonctions Studio : Demucs (isolation de voix), rembg (détourage), faster-whisper. Même README que MDizzleZA |
| [apvlv/davinci-resolve-mcp](https://github.com/apvlv/davinci-resolve-mcp) | 79 / 2026-10-22 | — | API externe, resources + `execute_python` / `execute_lua` | Petit périmètre, Fusion |
| [Poechant/davinci-resolve-cli](https://github.com/Poechant/davinci-resolve-cli) (`dvr`, PyPI) | 53 / 2026-05-22 | v0.3.0 (2026-06-03) | CLI d'abord, `dvr mcp` = 20 tools générés depuis les verbes CLI | `render submit/status/wait/cancel` avec `jobId` ; `--dry-run` sur toute mutation ; `dvr doctor` en JSON ; plugin **Workflow Integration** (JS) pour `cut`/`move`, inopérant faute d'API razor et réservé à Studio ; SKILL.md livré dans le wheel |
| [wassermanproductions/unofficial-davinci-mcp](https://github.com/wassermanproductions/unofficial-davinci-mcp) | 37 / 2026-07-20 | — | Deux niveaux auto-détectés : live (Studio) ou interchange (FCPXML 1.9 / EDL / CSV de markers / `.cube`) | `resolve_capabilities` ; moteurs déterministes (grille de beats, color match Lab → LUT 33 pts avec ΔE, loudness EBU R128) ; aperçu avant toute mutation |
| [hoyt-harness/davinci-mcp-professional](https://github.com/hoyt-harness/davinci-mcp-professional) | 25 / 2026-10-10 | v5.0.1 (2026-09-01) | **Noyau de 6 tools + 9 domaines** activés via `activate_domain` → `notifications/tools/list_changed` (289 tools) | Économie de contexte. Pattern non conforme à la spec 2026-07-28 (liste par connexion) |
| [lordhoell/davinci-resolve-mcp](https://github.com/lordhoell/davinci-resolve-mcp) | 23 / 2026-04-03 | — | FastMCP, 440+ tools, **registre d'objets** (les tools renvoient des ID JSON réutilisables) | Skill Claude Code livré avec |
| [Tooflex/davinci-resolve-mcp](https://github.com/Tooflex/davinci-resolve-mcp) | 21 / 2026-07-25 | — | 32 tools + 6 resources en lecture seule | — |
| [guycochran/resolve-mcp-server](https://github.com/guycochran/resolve-mcp-server) | 13 / 2026-09-16 | — | 77 tools, 16 resources, 5 prompts ; **aucun tool d'exécution de code** | Recherche IntelliSearch / transcriptions (21.1+), Slate ID, vision cloud optionnelle (Moondream) ; token d'accès |
| [emircbngl/davinci-resolve-mcp-free](https://github.com/emircbngl/davinci-resolve-mcp-free) (« resolve-pilot ») | 11 / 2026-10-15 | — | **Routage automatique par canal** : API Studio → pont Lua live → snippets Lua à coller ; plus FCPXML et UI macOS (Accessibility) | Bridge par **fichiers** (`~/.resolve-pilot/bridge`) qui s'arrête seul après 1 h ; `run_lua` marqué destructif ; outils vision (`grab_frames`, `timeline_frames`, `screenshot_resolve`) ; `checkpoint` (.drp) ; plan → édition → export ; `grade_clips` vérifié sur les frames |
| [jenkinsm13/resolve-mcp](https://github.com/jenkinsm13/resolve-mcp) (PyPI `resolve-mcp`) | 7 / 2026-04-18 | PyPI | 295+ tools (API v20.3), `uvx resolve-mcp` | Serveur séparé `resolve-assistant` (Gemini regarde les rushes et construit la timeline) |
| [CiprianSpiridon/davinci-resolve-mcp](https://github.com/CiprianSpiridon/davinci-resolve-mcp) | 6 / 2026-08-14 | — | 334 tools : 316 live (22 modules, connexion paresseuse) + **18 tools hors ligne** | Lecture/écriture des formats Resolve hors ligne (DRX XML, « zstd FieldsBlob »), SQLite local ; `execute_resolve_code` en échappatoire |
| [2sem/davinci-resolve-lite-mcp](https://github.com/2sem/davinci-resolve-lite-mcp) (registre MCP, PyPI) | 6 / 2026-09-16 | 0.18.0 | **Le serveur MCP tourne dans Resolve** (script de menu) en HTTP `127.0.0.1:8765/mcp`, zéro dépendance, 163 tools | Morte sur Lite 21.1+ (Python bloqué) |
| [saadk408/davinci-resolve-lua-mcp](https://github.com/saadk408/davinci-resolve-lua-mcp) (registre MCP) | 5 / 2026-10-21 | releases | Script **Lua** lancé via Workspace > Scripts sur le gratuit 21.1 + serveur TS, bundle `.mcpb` | 15 tools + `run_lua` ; recherche dans la doc de scripting livrée par Blackmagic ; rendu avec suivi du statut |
| [ET-sec/resolve-console-bridge](https://github.com/ET-sec/resolve-console-bridge) | 2 / 2026-09-19 | — | Pont Lua collé dans la **Console** du gratuit 21.1, loopback **signé** (token, horodatage, nonce à usage unique) ; branché sur samuelgursky ≥ 4.7.9 | Modèle de sécurité explicite (anti-rejeu) ; « testé sur 21.1.0.14 » |
| [sandraschi/davinci-resolve-mcp](https://github.com/sandraschi/davinci-resolve-mcp) | 3 / 2026-10-26 | — | FastMCP 3.x, **9 tools « portmanteau »** par défaut (`RESOLVE_TOOL_MODE`) | Sampling (désormais déprécié) |
| [DigitalWorkflowCompany/resolve-mcp](https://github.com/DigitalWorkflowCompany/resolve-mcp) | 2 / 2026-06-06 | — | 88 tools + 20 resources, Resolve 21, macOS | Tools IA 21 (transcription avec locuteurs, classification audio, motion deblur) ; `execute_python` / `execute_lua` |
| [MDizzleZA/davinci-resolve-mcp-free](https://github.com/MDizzleZA/davinci-resolve-mcp-free) | 1 / 2026-08-16 | — | Pont interne HTTP (même base que hiteshK03), « 17 tools groupés, jobs async » | — |
| Autres listés : [kerwilgil](https://github.com/kerwilgil/davinci-resolve-mcp) (162 tools, Windows), [yinglj](https://github.com/yinglj/davinci-resolve-mcp), [continuumpraxis-ux/davinci-bridge](https://github.com/continuumpraxis-ux/davinci-bridge) (agents cloud → Studio 21.1), [YGSWGJR/resolve-mcp-plugin](https://github.com/YGSWGJR/resolve-mcp-plugin), [EddieRivers](https://github.com/EddieRivers/davinci-resolve-mcp-free), [SpaceWasTaken](https://github.com/SpaceWasTaken/Davinci-Claude-MCP) (forks de samuelgursky), `@higgsfield_org/davinci-resolve-mcp` (npm, couleur), `davinci-resolve-ai-bridge-mcp` (npm 1.13.0) | ≤ 5 ★ | | | |

Registres :
- **Registre MCP officiel** : seuls `io.github.2sem/davinci-resolve-lite-mcp` et `io.github.saadk408/davinci-resolve-lua-mcp` y figurent ([API registre](https://registry.modelcontextprotocol.io/v0/servers?search=davinci)). La place « référence » y est libre. **HIGH**
- **Glama** liste plusieurs serveurs (dont hnethery, Positronikal → hoyt-harness) ([glama](https://glama.ai/mcp/servers/y5vtdfkv6k)). **LOW** (non ouvert en détail)
- Smithery : aucun résultat pertinent trouvé. **LOW**

### 1.3 Synthèse

- **Trois architectures** coexistent : (a) API externe (Studio), (b) pont interne + IPC (HTTP, TCP, fichiers ; Python avant 21.1, Lua après), (c) interchange hors ligne (FCPXML/EDL/DRX/LUT). Les meilleurs projets combinent (a), (b) et (c) avec un **routage automatique** et disent quel canal ils ont utilisé (emircbngl, wasserman).
- **Course au nombre de tools** (162 à 440+) sans gestion du contexte, sauf hoyt-harness (domaines) et sandraschi (portmanteau).
- Ce qui manque presque partout : `outputSchema`, annotations, Tasks, et des tests contre un vrai Resolve.

---

## 2. MCP pour d'autres logiciels de montage, VFX et audio

| Logiciel | Projet (★ / push) | Connexion | Design des tools | Idées notables | Conf. |
|---|---|---|---|---|---|
| **Blender** | [ahujasid/mcp-for-blender](https://github.com/ahujasid/mcp-for-blender) (32 356 / 2026-10-24) | Addon = serveur TCP dans Blender (`localhost:9876`, sans auth), serveur MCP stdio relais, JSON sur socket | ~20 tools + `execute_blender_code` | `BLENDER_MCP_SAFE_MODE=1` (analyse du script avant exécution) ; **MCP Apps** : viewport inline et sélecteurs d'assets en vignettes ; installeur qui détecte les clients | HIGH |
| Blender (officiel) | Blender Lab MCP ([projects.blender.org/lab/blender_mcp](https://projects.blender.org/lab/blender_mcp)), v1.0.3 (2026-09-11), Blender 5.1+ | Addon + socket, même port 9876 | Exécution de code généré, « sans garde-fous » de l'aveu de l'éditeur | Connecteur Claude officiel (avril 2026). Le fork [bpy-dev/blender-mcp](https://github.com/bpy-dev/blender-mcp) ajoute un **mode headless/CLI** (bpy standalone, timeouts, plafonds de sortie), doc d'API runtime, **benchmarks de tokens** et BlenderBench | MEDIUM ([strayspark](https://www.strayspark.studio/blog/official-blender-mcp-server-comparison-2026)) |
| **Premiere Pro** | [hetpatel-11/Adobe_Premiere_Pro_MCP](https://github.com/hetpatel-11/Adobe_Premiere_Pro_MCP) (756 / 2026-10-24) | Panneau **CEP** (production) ; UXP expérimental | **283 tools derrière `search_tools` / `get_tool_schema` / `invoke_tool`** + petit set toujours chargé ; 13 resources, 10 prompts | `verify_premiere_connection` d'abord ; `--doctor` ; plugins Codex/Claude Code ; `.mcpb` par release | HIGH |
| Premiere Pro | [leancoderkavy/premiere-pro-mcp](https://github.com/leancoderkavy/premiere-pro-mcp) (438 / 2026-10-24) | UXP + CEP/ExtendScript + QE DOM | 441 tools cœur + 96 conditionnés par capacité UXP ; 29 workflows guidés | Contexte projet versionné ; planification sûre des éditions ; reçus d'import avec revérification ; nom dans le registre `io.github.leancoderkavy/premiere-pro` | MEDIUM |
| Premiere Pro | [ayushozha/AdobePremiereProMCP](https://github.com/ayushozha/AdobePremiereProMCP) (128 / 2026-10-05) | Go (orchestrateur) → gRPC → TS → **WebSocket loopback authentifié par token** → CEP | Recettes (transitions, MOGRT, livraisons) | **Dry-run + copie de séquence + relecture exacte** ; snapshots de timeline ; skills en resource `config://workflow-skills` | MEDIUM |
| **After Effects** | [Dakkshin/after-effects-mcp](https://github.com/Dakkshin/after-effects-mcp) (798 / 2026-04-01) | Panneau ExtendScript (`mcp-bridge-auto.jsx`) qui interroge un dossier de commandes | Tools fins + `batchSetLayerProperties` | Simple, bridge par fichiers | HIGH |
| After Effects | [JUNKDOGE-JOE](https://github.com/JUNKDOGE-JOE/after-effects-mcp) (87), [ishu86](https://github.com/ishu86/after-effects-mcp) (43), [Engine-Room-Games](https://github.com/Engine-Room-Games/after-effects-mcp) (35) | CEP + chat embarqué | 70+ tools | Recettes de motion design | LOW |
| **Final Cut Pro** | [dreliq9/fcp-mcp](https://github.com/dreliq9/fcp-mcp) (12 / 2026-10-12) | FCPXML (moteur) + **AppleScript** (contrôle live) + Compressor | **Profils** (`workflow` par défaut = 34 tools, 3 prompts, 3 resources) | **Éditions FCPXML transactionnelles** (commit atomique validé), timecode en arithmétique rationnelle, presets d'annotations de sûreté (`tool_metadata.py`), erreurs stables + diagnostics structurés, événements d'observabilité | HIGH |
| **Avid Media Composer** | [leancoderkavy/avid-media-composer-mcp](https://github.com/leancoderkavy/avid-media-composer-mcp) (8 / 2026-10-25) | Analyse hors ligne (AVB/AAF/ALE/EDL) + Extension SDK + adaptateur natif Windows | Catalogue de 167 actions conditionnées par capacité | **Plan d'édition composé → classification du risque → approbation liée à un token SHA-256** ; refus d'un repli silencieux (« fail closed ») ; HTTP bearer ; index média par hash | MEDIUM |
| **Nuke** | [kleer001/nuke-mcp](https://github.com/kleer001/nuke-mcp) (14 / 2026-03-26) | Panneau Nuke, socket port 54321 | 57 tools, colonne **annotations** (readOnly/destructive) | **Confirmation des tools destructifs imposée dans le code**, pas seulement dans les consignes ; tools gatés NukeX / Nuke 17+ ; `update_project_memory` | HIGH |
| Nuke | [dughogan/nuke_mcp](https://github.com/dughogan/nuke_mcp) (39) | Socket | — | Référence d'architecture saluée par kleer001 | LOW |
| **Houdini** | [capoomgit/houdini-mcp](https://github.com/capoomgit/houdini-mcp) (307 / 2026-06-11) | Plugin = serveur TCP `localhost:9876` + pont stdio uv | Nœuds + exec de code | Clone du pattern Blender | HIGH |
| Houdini, Nuke, Photoshop… | org [dcc-mcp](https://github.com/dcc-mcp) (houdini 18 ★, push 2026-10-25) | `dcc-mcp-core` commun, **passerelle auto avec élection « premier arrivé »** (port 9765) | **Chargement progressif de skills** (`discover → load → unload`) | Endpoint Prometheus `/metrics`, persistance des jobs, moteur de workflows, refus d'exécuter HOM sur un worker HTTP | MEDIUM |
| **Ableton Live** | [ahujasid/ableton-mcp](https://github.com/ahujasid/ableton-mcp) (3 247 / 2026-09-22) | MIDI Remote Script = serveur TCP, JSON | Tools fins | **Elicitation pour le consentement** (avec repli en message de chat si le client ne la supporte pas) ; `--list-targets` avant installation | HIGH |
| **REAPER** | [shiehn/total-reaper-mcp](https://github.com/shiehn/total-reaper-mcp) (87), [bonfire-systems](https://github.com/bonfire-systems/reaper-mcp) (166), [xDarkzx](https://github.com/xDarkzx/Reaper-MCP) (108) | Pont **Lua par fichiers** (JSON dans un dossier, polling), socket non implémenté | « 100 % ReaScript » (600+), **profils** choisis côté serveur | Un seul script de pont pour tous les profils | HIGH |
| **CapCut** | [JmsLdrn/capcut-mcp](https://github.com/JmsLdrn/capcut-mcp) (12), [mrbuslov/capcut-ai-editor](https://github.com/mrbuslov/capcut-ai-editor) (123) | Édition directe des **drafts JSON** propriétaires | Tools + `capcut_raw_patch` (deep-merge) | Clonage depuis un template, sauvegardes, validation défensive | MEDIUM |
| **OBS** | `obs-studio-mcp` ([PyPI](https://pypi.org/project/obs-studio-mcp/)), `obs-mcp` (148 tools), `@firfi/obs-mcp` | obs-websocket v5 (`127.0.0.1:4455`, mot de passe en variable d'env) | Couverture du protocole + helpers | **`dry_run=true` sur toute mutation** ; toolsets opt-in | MEDIUM |
| **ffmpeg** | [video-creator/ffmpeg-mcp](https://github.com/video-creator/ffmpeg-mcp) (153), [egoist/ffmpeg-mcp](https://github.com/egoist/ffmpeg-mcp) (122), [misbahsy/video-audio-mcp](https://github.com/misbahsy/video-audio-mcp) (89) | CLI ffmpeg | Tools de transcodage, découpe, probe | Peu d'idées neuves ; utile comme brique interne | HIGH |
| **Unreal** (hors NLE, mais pattern clé) | [ChiR24/Unreal_mcp](https://github.com/ChiR24/Unreal_mcp) « EngineWire » (979 / 2026-10-25) | Plugin C++ natif, Streamable HTTP `:3000` + proxy stdio, **token de capacité** en en-tête | **Un seul tool `unreal`** : `search` → `describe` → `execute`, 400+ capacités | Chaque erreur porte un `nextCall` exécutable ; paramètres stricts (`UNDECLARED_PARAMETER`) ; **62 capacités destructives exigent un `consentGrant`** obtenu via `describe` ; « reçu » de ce qui a changé ; captures d'écran | HIGH |
| **Photoshop** (pattern) | [alisaitteke/photoshop-mcp](https://github.com/alisaitteke/photoshop-mcp) (736 / 2026-10-24) | UXP | 118 tools + recettes | **Chaque recette multi-étapes = une seule étape d'undo** ; erreurs structurées ; couche de prompts | HIGH |
| Kdenlive | [AMMIROSOH/Kdenlive-mcp](https://github.com/AMMIROSOH/Kdenlive-mcp) (12), [crom-org/kdenlive-mcp-plus](https://github.com/crom-org/kdenlive-mcp-plus) | Headless MLT / D-Bus | — | Édition headless du projet | LOW |

### 2.1 Patterns transverses

| Pattern | Qui | Intérêt pour Resolve |
|---|---|---|
| Plugin interne + socket loopback | Blender, Houdini, Ableton, Nuke, Unreal | = pont interne. Ajouter un token (Unreal, ET-sec, ayushozha) : Blender n'a **aucune auth** |
| Bridge par fichiers (polling) | REAPER, After Effects, resolve-pilot | Le seul canal possible si le Lua sandboxé de Resolve gratuit 21.1 n'a pas de socket (à vérifier) |
| Passerelle search/describe/execute | Unreal, Premiere (hetpatel), FastMCP `search_tools` | Catalogue exhaustif sans saturer le contexte, conforme à la spec 2026-07-28 |
| Consentement lié à un token | Unreal (`consentGrant`), Avid (SHA-256 du plan) | Sûreté plus forte que `destructiveHint`, qui n'est qu'un indice que le client peut ignorer |
| Transactions + relecture | fcp-mcp, ayushozha, Avid | Combler les « `True` mensongers » de l'API Resolve |
| Undo atomique par recette | Photoshop | Resolve n'expose pas d'undo par API : snapshot `.drp` ou timeline dupliquée |
| Vision | Blender (viewport, MCP Apps), Unreal, resolve-pilot | Export de frame et planches contact, déjà au cadrage |
| Hors ligne / interchange | fcp-mcp, Avid, wasserman, CiprianSpiridon | Fonctionne sans Studio ni Resolve lancé |
| Profils de tools | fcp-mcp, REAPER, sandraschi | À écarter (cadrage : pas de profils) au profit de la recherche d'outils |

---

## 3. Bonnes pratiques de design MCP (2025-2026)

### 3.1 Spec MCP courante : 2026-07-28 (HIGH)

Sources : [versioning](https://modelcontextprotocol.io/specification/versioning), [changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog.md), [deprecated](https://modelcontextprotocol.io/specification/2026-07-28/deprecated.md), [tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools.md), [MRTR](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr.md).

| Sujet | État 2026-07-28 | Impact `davinci-mcp` |
|---|---|---|
| Sessions | **Sans état** : plus d'`initialize` ni de `Mcp-Session-Id` ; version et capacités client dans `_meta` à chaque requête ; `server/discover` obligatoire | L'état (handles d'objets Resolve, jobs) passe par des **handles explicites** dans les arguments. Le cadrage le prévoit déjà (registre d'IDs). Documenter leur durée de vie dans la description du tool créateur |
| `tools/list` | Ne doit **pas** varier par connexion ; ordre **déterministe** recommandé (cache de prompt) ; `ttlMs` et `cacheScope` requis | Pas d'activation de domaines par session. Une seule variation légitime : les capacités détectées (version, édition), communes à tous les clients, avec `list_changed` si Resolve change |
| Resources | `resources/subscribe` remplacé par **`subscriptions/listen`** (flux unique, opt-in par type) | Les abonnements `resolve://…` du cadrage passent par ce mécanisme (via FastMCP 4) |
| Elicitation | Modes form et URL, via **MRTR** : `InputRequiredResult` + `requestState` (opaque, à protéger par HMAC/AEAD, TTL, lié à la requête) | Confirmation des opérations destructives : retour `input_required`, puis nouvel appel. Ne fonctionne que si le client déclare `elicitation` (Claude Code déclare `form` et `url` en 2026-07-28 ([doc](https://code.claude.com/docs/en/mcp))) ; sinon repli sur le paramètre `confirm` ou un token |
| Sampling, Roots, Logging | **Dépréciés** (retrait possible ≥ 2027-07-28) | Retirer sampling et logging MCP du cadrage. Racines autorisées = **configuration** ; logs = stderr + OpenTelemetry (déjà prévu) |
| Tasks | Extension officielle **`io.modelcontextprotocol/tasks`** : `CreateTaskResult` (`taskId`, `ttlMs`, `pollIntervalMs`), `tasks/get`, `tasks/update`, `tasks/cancel`, états `working / input_required / completed / failed / cancelled` ; `notifications/tasks` | Rendus, transcriptions, détection de plans, Quick Export (async en 21.1.1). **Repli obligatoire** : tools `job_status` / `job_cancel` pour les clients sans l'extension ([Tasks](https://modelcontextprotocol.io/extensions/tasks/overview.md)) |
| Schémas | `inputSchema` / `outputSchema` en JSON Schema 2020-12 complet ; `structuredContent` = n'importe quelle valeur JSON ; texte sérialisé en plus « SHOULD » | Conforme au cadrage (Pydantic + résumé texte) |
| Erreurs | Erreurs d'exécution en `isError: true` avec un message actionnable ; erreurs protocole en JSON-RPC | Format d'erreur unique avec `hint` / `nextCall` (pattern Unreal) |
| Annotations | `title`, `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`, **non fiables** côté client | Claude Code exécute en parallèle les tools `readOnlyHint` et rejoue les `idempotentHint` après une coupure ([doc Claude Code](https://code.claude.com/docs/en/mcp)) : bien les poser change le comportement |
| Extensions | `extensions` dans les capacités ; officielles : **MCP Apps** (`io.modelcontextprotocol/ui`), Tasks, **Skills over MCP**, auth | MCP Apps pour un viewer de frames ou une timeline ; Skills over MCP pour distribuer les guides de workflow |
| Divers | Propagation OpenTelemetry via `_meta` (`traceparent`) ; noms de tools de 1 à 128 caractères, `[A-Za-z0-9_.-]` | Traces opt-in cohérentes avec le cadrage |

### 3.2 Recommandations Anthropic

| Source | Recommandations clés |
|---|---|
| [Writing effective tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents) (2025-09-11) — HIGH | Peu de tools à fort impact plutôt qu'un tool par endpoint ; namespacing par préfixe ; paramètre `response_format` (concis/détaillé ; 72 vs 206 tokens dans l'exemple) ; noms lisibles plus IDs ; pagination, filtrage et troncature avec des défauts sensés ; erreurs actionnables ; descriptions traitées comme des prompts ; **évaluation** sur tâches réalistes (précision, tool calls, tokens, erreurs) |
| [Code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp) (2025-11-04) — HIGH | Divulgation progressive ; `search_tools` avec niveau de détail (nom / description / schéma) ; filtrer les données dans le code ; 150 k → 2 k tokens (−98,7 %) dans leur exemple ; skills réutilisables |
| [Advanced tool use](https://www.anthropic.com/engineering/advanced-tool-use) (2025-11-24) — HIGH | **Tool Search** (`defer_loading`, regex ou BM25) : −85 % de tokens, Opus 4.5 79,5 % → 88,1 % sur leurs évals MCP ; à activer au-delà de ~10 k tokens de définitions ou de 10 tools ; garder 3 à 5 tools toujours chargés. **Programmatic Tool Calling** : −37 % de tokens. **`input_examples`** : 72 % → 90 % sur les paramètres complexes |
| [Claude Code — MCP](https://code.claude.com/docs/en/mcp) — HIGH | Recherche d'outils **active par défaut** (`ENABLE_TOOL_SEARCH` : vide/`true`/`auto`/`auto:N`/`false` ; `auto` = 10 % du contexte) ; les **server instructions** guident la recherche (4 096 caractères max ; description d'un tool chargé via la recherche : 16 384) ; `_meta["anthropic/alwaysLoad"]` par tool ; `_meta["anthropic/maxResultSizeChars"]` (au-delà de 50 000 caractères, le résultat est déporté dans un fichier) ; `MAX_MCP_OUTPUT_TOKENS` = 25 000 par défaut, avertissement à 10 000 ; `_meta["anthropic/requiresUserInteraction"]` pour forcer une approbation à chaque appel ; prompts exposés en `/server:prompt` ; resources en `@server:uri` ; appels longs passés en arrière-plan après 2 min (`CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS`) |

**Conséquence** : la règle « aucune limitation du nombre de tools » du cadrage est compatible avec Claude Code, qui diffère les définitions. Elle coûte cher sur les clients sans recherche d'outils (Claude Desktop selon la version, Cursor). Il faut donc une **passerelle optionnelle côté serveur** (cf. §5) pour ces clients, sans profils.

### 3.3 FastMCP vs SDK officiel

| Point | FastMCP | SDK officiel `mcp` | Source / conf. |
|---|---|---|---|
| Version courante | **4.1.3** (2026-10-20) ; 4.0.0 le 2026-08-31 ; 3.0.0 le 2026-02-18 ; 2.14.7 (dernier 2.x) le 2026-04-13 | **2.3.0** (2026-10-02) | [PyPI fastmcp](https://pypi.org/project/fastmcp/), [PyPI mcp](https://pypi.org/project/mcp/) — HIGH |
| Spec 2026-07-28 | Oui depuis 4.0, négociation des anciens clients par connexion | Oui (2.x) | [updates](https://gofastmcp.com/updates) — HIGH (FastMCP), MEDIUM (SDK, déduit de la date) |
| Middleware | `on_call_tool`, `on_list_tools`, `on_discover`… ; intégrés : Logging/StructuredLogging, Timing, ResponseCaching, RateLimiting, ErrorHandling/Retry, ResponseLimiting | Minimal | [middleware](https://gofastmcp.com/servers/middleware) — HIGH |
| Composition / transforms | 3.0 : architecture providers + transforms (renommage, namespace, filtrage, versioning, sécurisation) ; tool transformation depuis 2.8 ; mount/proxy depuis 2.0 | Non | [updates](https://gofastmcp.com/updates) — HIGH |
| Recherche d'outils côté serveur | `RegexSearchTransform`, `BM25SearchTransform` → tools synthétiques `search_tools` + `call_tool`, option `always_visible`, `max_results=5` | Non | [tool-search](https://gofastmcp.com/servers/transforms/tool-search) — HIGH |
| Code Mode | Depuis 3.1 : l'agent cherche, inspecte, puis écrit du Python qui chaîne les appels dans un sandbox (extra `code-mode`) | Non | [updates](https://gofastmcp.com/updates) — MEDIUM |
| Tasks | Paquet `fastmcp-tasks` (4.0) ; tâches de fond depuis 2.14 | — | HIGH |
| MCP Apps | `FastMCPApp` (3.2), extra `apps` | — | HIGH |
| Breaking 4.0 | Sampling et roots initiés par le serveur **retirés** ; DI `Depends` / `CallArgument` ; `add_extension()` | — | HIGH |

**Recommandation** : viser **FastMCP ≥ 4.1** (pas 2.x). La 2.x ne parle pas la spec 2026-07-28 et n'a ni transforms, ni recherche intégrée, ni extension Tasks. Mettre à jour CADRAGE.md §3.

---

## 4. Ressources communautaires sur l'API de scripting Resolve

### 4.1 Documentation

| Ressource | Contenu | Fraîcheur | Conf. |
|---|---|---|---|
| `README.txt` officiel livré avec Resolve (`Developer/Scripting/`) et, en 21.1, tools natifs `get_scripting_api` / `get_scripting_docs` | Référence officielle | Suit la version installée | HIGH (existence) / MEDIUM (tools 21.1) |
| [deric.github.io/DaVinciResolve-API-Docs](https://deric.github.io/DaVinciResolve-API-Docs/) ([repo](https://github.com/deric/DaVinciResolve-API-Docs), 71 ★, dernier push 2020-10-19) | Mise en forme du README (objets Resolve → TimelineItem, propriétés, headless `-nogui`) | **Périmée** (ère 16.x/17) | HIGH |
| [ExtremRaym — Resolve Scripting Doc](https://www.extremraym.com/cloud/resolve-scripting-doc/) ([annonce](https://www.extremraym.com/en/resolve-api-doc-release/), 2024-10-24) | README converti en HTML navigable par un parseur Python | Version couverte non précisée, ~v19 | MEDIUM |
| Forum BMD « Creating Scripts for DaVinci Resolve » ([fil](https://forum.blackmagicdesign.com/viewtopic.php?p=941931)) | Q&R, méthodes de sélection manquantes (`GetSelectedTimelineItems`…) | Continu | LOW |
| [Manuel 21.1, section AI Assistants](https://ymcinema.com/2026/09/17/davinci-resolve-21-1-mcp-ai-assistants-control-edit/) (p. 4318) | Usage MCP natif | 2026-09 | MEDIUM |

### 4.2 Stubs et wrappers typés

| Paquet | Version / date | Remarque | Conf. |
|---|---|---|---|
| [`fusionscript-stubs`](https://pypi.org/project/fusionscript-stubs/) ([czukowski](https://github.com/czukowski/fusionscript-stubs)) | **21.0.0** (2026-07-11) | `.pyi` versionnés comme Resolve ; usage sous `TYPE_CHECKING`. **Meilleure base** pour le typage strict du cadrage, à compléter par l'introspection | HIGH |
| [`pybmd`](https://pypi.org/project/pybmd/) ([WheheoHu/pybmd](https://github.com/WheheoHu/pybmd), 37 ★) | 2026.3.0 (2026-09-18) | Wrapper objet de l'API Resolve | HIGH |
| [EmberLightVFX/BMD-Fusion-Scripting-Stubs](https://github.com/EmberLightVFX/BMD-Fusion-Scripting-Stubs) (15 ★, 2025-11-11) | — | Génère des stubs pour **tous les objets Fusion** : utile pour le domaine Fusion | HIGH |
| [GravityCY/ResolveLuaLS](https://github.com/GravityCY/ResolveLuaLS) (2026-04) | — | Définitions de types **Lua** (LuaLS) pour l'API et les Fuses : utile pour le pont Lua | MEDIUM |
| [brunocbreis/monet-resolve](https://github.com/brunocbreis/monet-resolve) (2026-10-22) | — | « L'API plus les fonctions qui lui manquent » : à auditer | LOW |
| [`pydavinci`](https://pypi.org/project/pydavinci/) | 0.2.3 (2022) | Abandonné | HIGH |
| `resolve-stubs`, `davinci-resolve-stubs`, `types-davinci-resolve` | — | **N'existent pas sur PyPI** (vérifié) | HIGH |

### 4.3 Méthodes non documentées et lacunes

- Pas de source fiable recensant des méthodes non documentées : l'introspection `dir()` prévue au cadrage reste la bonne voie. **MEDIUM**
- Lacunes d'écriture signalées (trim, lame, vitesse, texte des sous-titres, modes d'édition), plus des retours `True` perdus à la sauvegarde ([pilotcut](https://www.pilotcut.com/blog/davinci-resolve-studio-21-1-mcp-api-gaps-vs-pilotcut)). Les **Workflow Integrations** (plugin JS) ne comblent pas razor/move et sont réservées à Studio ([Poechant](https://github.com/Poechant/davinci-resolve-cli)). **MEDIUM**
- Les noms de tools et le comportement diffèrent entre 21.0.x et 21.1.x ; un projet ouvert en 21.1 ne s'ouvre plus en 20.3 ([pilotcut](https://www.pilotcut.com/blog/davinci-resolve-studio-21-1-mcp-api-gaps-vs-pilotcut), [Digital Production](https://digitalproduction.com/2026/10/06/davinci-resolve-21-1-1/)). **MEDIUM**

### 4.4 Scripting dans la version gratuite 21.1

| Point | Constat | Source | Conf. |
|---|---|---|---|
| Python | Retiré du gratuit : « We have moved the ability to script in Python to the Studio version » ; motif : usage de l'API Python pour débloquer des fonctions Studio | [Puget](https://www.pugetsystems.com/blog/2026/09/10/how-davinci-resolve-free-v21-1-scripting-changes-affect-puget-bench/) | HIGH (citation des notes de version) |
| Scripting externe | Studio uniquement (déjà le cas avant 21.1) | [wasserman](https://github.com/wassermanproductions/unofficial-davinci-mcp), [hoyt-harness](https://github.com/hoyt-harness/davinci-mcp-professional) | HIGH |
| Lua via Workspace > Scripts | **Fonctionne** sur 21.1.0.17 : `AddRenderJob`, `StartRendering`, `SetRenderSettings`, `AppendToTimeline`, `ImportFolderFromFile`, `GetFusionCompByIndex`, `Get/SetSetting`, `dofile` | [xere.my](https://xere.my/journal/davinci-resolve-21-1-free-python-scripting-removed-lua-benchmark/) | MEDIUM |
| Bac à sable Lua | `io`, `os.execute`, `package`/`require`, `bmd.readfile/writefile` bloqués ; `UIManager` nul ; `print` muet ; **sockets non testés** | [xere.my](https://xere.my/journal/davinci-resolve-21-1-free-python-scripting-removed-lua-benchmark/) | MEDIUM |
| **Contradiction** | ET-sec dit faire tourner un pont Lua **par socket loopback signé** depuis la **Console** du gratuit 21.1.0.14 ; resolve-pilot utilise un dossier de fichiers ; saadk408 un script Lua de menu. Or xere.my rapporte `io` bloqué et `print` muet. Hypothèses : la Console n'a pas le même bac à sable que le menu Scripts, ou les builds diffèrent | [ET-sec](https://github.com/ET-sec/resolve-console-bridge), [emircbngl](https://github.com/emircbngl/davinci-resolve-mcp-free), [saadk408](https://github.com/saadk408/davinci-resolve-lua-mcp) | **LOW** : à vérifier soi-même sur 21.1.1 |
| 20 nouvelles API + console | Studio seulement | [cutsio](https://cutsio.com/blog/davinci-resolve-21-1-free-vs-studio) | MEDIUM |
| Lite (App Store) | Sandboxé ; Python bloqué en 21.1+ | [2sem](https://github.com/2sem/davinci-resolve-lite-mcp), [emircbngl](https://github.com/emircbngl/davinci-resolve-mcp-free) | MEDIUM |

---

## 5. Idées à intégrer dans `davinci-mcp` (classées par valeur)

### Valeur critique (structurent l'architecture)

1. **Passer à FastMCP ≥ 4.1 et à la spec 2026-07-28.** Handles explicites, `server/discover`, `subscriptions/listen`, MRTR. Retirer sampling, roots et logging MCP du cadrage. *Sources : §3.1, §3.3.*
2. **Se positionner face au MCP natif Studio 21.1.** Proposer au minimum un **mode d'exécution de code équivalent** (`run_python` / `run_lua`, désactivé par défaut) **plus** ce que le natif n'a pas : tools typés avec `outputSchema`, relecture post-écriture, snapshots, vision, workflows, interchange, édition gratuite. Envisager un **mode « compagnon »** : détecter `ResolveMCP` et réutiliser ses tools de doc (`search_scripting_api`) plutôt que de les dupliquer. *§1.1.*
3. **Pont gratuit 21.1+ en Lua**, avec un transport déterminé **expérimentalement** (socket, fichiers ou Console, cf. contradiction §4.4) et un routage automatique : API Studio → pont Lua → snippet Lua à coller → interchange. Chaque résultat indique le canal utilisé (pattern resolve-pilot / wasserman). *§1.2, §4.4.*
4. **Catalogue exhaustif sans profils, mais avec passerelle.** Exposer tous les tools (Claude Code les diffère déjà) avec un **ordre déterministe**, des **server instructions** soignées (< 4 096 caractères, mots-clés en tête) et `anthropic/alwaysLoad` sur 3 à 5 tools pivots (`resolve_status`, `get_context`, `search_api`). En option, un `BM25SearchTransform` (`always_visible`) pour les clients sans recherche d'outils. Ne **pas** activer de domaines par session (non conforme). *§3.1–3.3, Unreal, hetpatel.*

### Valeur haute (différenciation qualité et sûreté)

5. **Écritures vérifiées** : après chaque mutation, relire l'état et renvoyer un **reçu** (avant/après, `verified: true|false`). Cela neutralise les retours `True` trompeurs de l'API. *fcp-mcp, ayushozha, Unreal, pilotcut.*
6. **Confirmation forte des opérations destructives** : `destructiveHint` + `dry_run` (déjà prévus) **plus** un **token de consentement** (plan → hash → appel avec `confirm_token`) ou une elicitation MRTR quand le client la supporte, avec repli. Imposer la confirmation **dans le code** (Nuke). `anthropic/requiresUserInteraction` pour `run_*_unsafe`. *Avid, Unreal, kleer001, doc Claude Code.*
7. **Jobs = extension Tasks + repli.** Rendus, Quick Export, transcription, détection de plans et Smart Reframe (async 21.1.1) en `CreateTaskResult`, avec `job_status` / `job_wait` / `job_cancel` pour les autres clients. Persister les jobs (dcc-mcp). Un appel de plus de 2 min passe en arrière-plan côté Claude Code : préférer les tâches. *§3.1, Poechant.*
8. **Erreurs actionnables normalisées** : `{code, message, hint, nextCall}` en `isError: true`, refus des paramètres inconnus avec la liste des noms valides. *Unreal, Anthropic.*
9. **Annotations précises partout** (`readOnlyHint` → parallélisme, `idempotentHint` → rejeu sûr côté Claude Code), avec des presets centralisés comme `tool_metadata.py` de fcp-mcp.
10. **`response_format` concis/détaillé + `fields`** sur les tools de lecture, plus `anthropic/maxResultSizeChars` sur les rares tools volumineux (dump de timeline, graphe Fusion). *Anthropic, doc Claude Code.*

### Valeur moyenne (fonctionnalités qui font la différence)

11. **Vision** : `export_frame`, planche contact, avant/après d'étalonnage. En option, un viewer **MCP Apps** (frames, mini-timeline) via `FastMCPApp`, avec repli texte et image. *Blender (viewport inline), resolve-pilot.*
12. **Interchange hors ligne** comme canal de premier rang : écriture FCPXML/EDL/OTIO, CSV de markers, `.cube`, lecture DRX. Utile en gratuit, sans Resolve lancé, et pour les opérations absentes de l'API (insert, trim) : générer une timeline puis l'importer. *wasserman, fcp-mcp, CiprianSpiridon, Avid.*
13. **Moteurs déterministes** que le LLM ne doit pas deviner : grille de beats, color match Lab → LUT avec ΔE, loudness R128, détection silence/noir/freeze via ffmpeg. *wasserman, resolve-pilot.*
14. **Undo par recette** : chaque workflow haut niveau ouvre un checkpoint (timeline dupliquée ou `.drp`) et propose `rollback(checkpoint_id)`. *Photoshop, resolve-pilot `checkpoint`.*
15. **Doctor / capabilities** : `davinci-mcp doctor --json` et tool `resolve_capabilities` (édition, version, canal, Python/Lua, ffmpeg, extensions client). *Poechant, wasserman, hetpatel `--doctor`.*
16. **Skills over MCP / SKILL.md** livrés avec le paquet (guides de workflow Reel, livraison, étalonnage) ; ne pas surcharger les descriptions. *Poechant, lordhoell, extension Skills.*

### Valeur basse (finitions)

17. **Sécurité du pont** : loopback seulement, token par machine (fichier 0600), horodatage et nonce anti-rejeu, arrêt automatique après inactivité. *ET-sec, resolve-pilot, Unreal.* (Le cadrage couvre déjà l'HTTP ; l'étendre au pont interne.)
18. **Typage** : partir de `fusionscript-stubs` 21.0 + stubs Fusion d'EmberLightVFX + LuaLS pour le pont, puis compléter par l'introspection. *§4.2.*
19. **Bench public** contre samuelgursky et le natif `ResolveMCP` (tokens de schéma, tokens par tâche, taux de succès), sur le modèle BlenderBench. *bpy-dev.*
20. **Publication** au registre MCP officiel (catégorie quasi vide, §1.2), bundle `.mcpb` par release, installeur qui détecte les clients (`--list-targets`). *ahujasid, hetpatel, saadk408.*

---

## Annexe — Points à vérifier soi-même (non tranchés)

- Liste exacte des tools de `ResolveMCP` (manuel 21.1 p. 4318, ou lancer `ResolveMCP` et appeler `tools/list` sur une machine Studio).
- Signatures des API async de 21.1.1 (README.txt de scripting livré avec 21.1.1).
- Bac à sable Lua du gratuit 21.1.x : sockets disponibles ? différences entre Console et menu Scripts ? `bmd.*` ?
- Support réel de l'extension Tasks et de MCP Apps dans Claude Code et Claude Desktop : la [matrice des clients](https://modelcontextprotocol.io/extensions/client-matrix.md) n'a pas pu être lue (cases perdues à la conversion).
- Nombre réel de tools des projets communautaires (auto-déclaré).
