# davinci-mcp — Synthèse de la phase de recherche

> 2026-10-26. Rapports détaillés :
> - [research/audit-existant.md](research/audit-existant.md) : samuelgursky/davinci-resolve-mcp v4.14.4, avec ~90 pièges de l'API au §6.2.
> - [research/veille-mcp-nle.md](research/veille-mcp-nle.md) : ~18 MCP Resolve, MCP d'autres logiciels (NLE/VFX/audio), spec MCP, 20 idées classées.
> - [research/introspection-api.md](research/introspection-api.md) : l'API réelle de ton Resolve Studio 21.0.4.5.

## 1. Ce qui change la donne

| # | Constat | Confiance | Vérifié par moi |
|---|---|---|---|
| 1 | **Blackmagic livre un MCP natif dans Resolve Studio 21.1** (2026-09-08). D'après des guides tiers, c'est une approche « doc + exécution de code » : `search_scripting_api`, `run_script` sandboxé, `run_script_unsafe`… | Existence : HIGH. Liste des tools : MEDIUM | Existence oui ([Digital Production](https://digitalproduction.com/2026/09/08/resolve-21-1-adds-mcp-and-finally-gets-presets/)). Tools non |
| 2 | **Version gratuite 21.1 : Python retiré**, Lua encore exécutable mais dans un bac à sable. Le pont interne gratuit doit passer en **Lua**, transport à valider expérimentalement | MEDIUM | Non (on est en 21.0.4) |
| 3 | **FastMCP courant = 4.1.3** (2.x n'est plus maintenu) ; SDK officiel `mcp` 2.3.0 | HIGH | Oui (PyPI) |
| 4 | **Spec MCP 2026-07-28** : protocole sans état, Tasks devient une extension. Les features client ne listent plus qu'**Elicitation** : sampling, roots et logging sont dépréciés. `tools/list` ne doit pas varier selon la session | HIGH | Oui ([spec](https://modelcontextprotocol.io/specification/2026-07-28)) |
| 5 | L'API Resolve live colle au README (397 méthodes, 2 non documentées). La surface cachée réelle, ce sont **les clés, les enums et Fusion** : 158 settings projet, 263 propriétés de clip, 326 codecs, 1 270 outils Fusion, 886 actions Fusion | HIGH | Oui (introspection) |

## 2. L'existant (samuelgursky) en bref

- **Forces** : popularité (~3 800 ★), couverture large (~650 actions), installeur multi-clients, pont gratuit (≤ 21.0), ~4 000 tests sur mocks, et surtout une **mine de pièges de l'API** documentés.
- **Faiblesses exploitables** (HIGH) :
  - un monolithe `server.py` de 34 k lignes ;
  - des paramètres `Dict[str, Any]` non typés et ~30 k tokens de schémas ;
  - tout est annoté « destructif », donc aucune lecture n'est auto-approuvée ;
  - les erreurs reviennent comme des succès ;
  - ni pagination, ni progress, ni elicitation ;
  - les items sont adressés par index, en mélangeant base 0 et base 1 ;
  - le verrou global est contourné par les tâches async et les jobs de fond ;
  - la CI tourne sous Linux seulement, sans ruff ni mypy ;
  - **4 advisories de sécurité**, dont une High (injection d'arguments → exécution de commande).
- **Chiffres gonflés** : « 100 % coverage » = 361/361 méthodes du README 21.0. « 93,6 % live tested » descend à ~82 % de résultats réellement conformes, et la validation live est surtout faite sur Studio 19.1.3.

## 3. Positionnement proposé

On a deux concurrents : le **MCP natif** (générique, il écrit du code à la volée, Studio 21.1+ seulement) et **samuelgursky** (large mais lâche). `davinci-mcp` gagne sur :
1. **Des tools typés et vérifiés** : schémas stricts générés depuis l'introspection (enums de settings, codecs, outils Fusion), et un **reçu après chaque écriture** (relecture avant/après, `verified`).
2. **La sûreté** : annotations précises en lecture/écriture, `dry_run`, jeton de consentement pour les opérations destructives, snapshots et rollback par recette, erreurs `isError` actionnables.
3. **Un budget de tokens** inférieur à 8 k pour les schémas, des instructions serveur soignées, quelques tools pivots toujours chargés.
4. **Ce que personne n'a** :
   - la vision (frames, planche contact, avant/après) ;
   - un canal **interchange hors ligne** (FCPXML/EDL/OTIO) pour ce que l'API ne fait pas (insert, trim, blade) ;
   - des titres **OGraf** HTML générés ;
   - l'undo Fusion transactionnel ;
   - un **pont Lua** pour la version gratuite 21.1+.
5. **Mode compagnon** : détecter `ResolveMCP` et réutiliser sa doc et son exécution de code au lieu de les dupliquer.

## 4. Amendements au cadrage à valider

| Cadrage actuel | Proposition | Raison |
|---|---|---|
| FastMCP 2.x | **FastMCP ≥ 4.1** | 2.x n'est plus maintenu, 4.x supporte la spec 2026-07-28 |
| Sampling, logging MCP, MCP roots | **Retirés** : logs en fichier, racines autorisées via la config | Dépréciés dans la spec |
| Jobs asynchrones maison | **Extension Tasks** + repli `job_status` / `job_wait` / `job_cancel` | Standard officiel |
| Pont interne Python (gratuite) | **Pont Python ≤ 21.0 + pont Lua ≥ 21.1** | Python retiré du gratuit en 21.1 |
| « Documenté + non documenté » | Viser **la surface de paramètres + Fusion + actions** | Les méthodes non documentées sont quasi inexistantes |
| Dry-run seul | Dry-run **+ jeton de consentement** (plan → hash → confirm) | Robuste même sans elicitation côté client |
| Pas de limite de tools | Inchangé, mais avec **ordre déterministe + tools pivots `alwaysLoad`** | Conforme à la spec, compatible avec la recherche d'outils de Claude Code |
| — | **Nouveau** : canal interchange hors ligne, titres OGraf, mode compagnon `ResolveMCP` | Différenciation |

## 5. Relevé sur Studio 21.1.1 (fait)

Détails dans [research/introspection-21.1.md](research/introspection-21.1.md) :
- **MCP natif vérifié en direct** : 14 tools (doc, `run_script`, LUT/DCTL), ~2,7 k tokens, protocole 2024-11-05, sans `outputSchema`, sans prompts, resources vides.
- **+50 méthodes** (447 au total) : transitions, vitesse, fondus, multicam, normalisation audio, presets, clonage, statuts de jobs asynchrones.
- **Stub officiel `.pyi`** avec ~50 TypedDict : la source de nos schémas.
- **Python 3.14 intégré** (`ResolvePython`) et LuaJIT (`fuscript`).

Reste à faire : une install **gratuite** 21.1 de test pour le pont Lua.

## 6. Prochaines étapes

1. ~~Valider les amendements du §4~~ : validés et reportés dans [CADRAGE.md](CADRAGE.md) le 2026-10-26.
2. ~~Passer en 21.1.x, relancer l'introspection, appeler `tools/list` sur `ResolveMCP`~~ : fait (21.1.1.10).
3. Concevoir le socle : connexion, pont, exécuteur à thread unique, registre d'IDs, registre d'actions déclaratif (source unique des schémas, des docs et des annotations), erreurs, reçus, snapshots.
