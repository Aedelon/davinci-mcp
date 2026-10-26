# Resolve Studio 21.1.1 — introspection, MCP natif et nouveautés

Relevé du 2026-10-26 sur DaVinci Resolve Studio **21.1.1.10** (macOS). Données brutes :
- [api-introspection/](api-introspection/) (`*-21.1.1.10.json`) ;
- [resolve-mcp-native/handshake.json](resolve-mcp-native/handshake.json).

Le projet jetable a été supprimé. Le projet courant était un « Untitled Project » transitoire : le script le referme proprement au lieu de tenter de le recharger.

## 1. Le MCP natif `ResolveMCP` (vérifié en direct)

- Binaire : `DaVinci Resolve.app/Contents/Applications/ResolveMCP`, en stdio. Bundle `Contents/Resources/DaVinciResolve.mcpb`.
- Protocole annoncé : **2024-11-05** (ancien). Capacités : tools, resources (liste vide), logging. **Pas de prompts.**
- **14 tools, ~10,9 Ko de schémas (~2,7 k tokens), aucun `outputSchema`** :

| Tool | Rôle | Annotations |
|---|---|---|
| `launch_resolve`, `get_resolve_status` | cycle de vie | lecture |
| `get_whats_new(since)` | changelog | lecture |
| `get_scripting_api(api, types, outline, as_file)`, `search_scripting_api(pattern, api)`, `get_scripting_docs(document, section, as_file)` | doc et stubs `.pyi` | lecture |
| `run_script(script, timeout)` | Python 3.14 en bac à sable (pas de `os`, `sys`, `pathlib`, réseau ni processus) ; `resolve` et `project` injectés, retour via `result` | destructif |
| `run_script_unsafe(script, timeout)` | idem, avec accès complet au système | destructif, openWorld |
| `list_luts`, `list_dctls`, `update_dctl` (compilé avant écriture), `delete_dctl`, `delete_lut`, `generate_lut(path, size, transform)` | LUT et DCTL, écriture limitée à `LUT/MCP` | mixtes |

**Lecture** : c'est un MCP « doc + exécution de code ». Il n'a aucun tool métier typé, aucune vision, aucune sûreté (ni snapshot ni reçu), aucun prompt, aucun job, et il ne fonctionne qu'avec Studio. C'est la confirmation du positionnement de `davinci-mcp`. En mode compagnon, ses tools de doc et de LUT/DCTL sont réutilisables. Confiance : HIGH.

## 2. Diff de l'API live 21.0.4 → 21.1.1

**447 méthodes côté Resolve** (contre 397), soit **+50** :

| Objet | Ajouts |
|---|---|
| Resolve | `IsStudio`, `GetCurrentProject`, `GetCurrentTimeline`, `GetMediaPool`, `GetGallery`, `ValidateDCTL`, `EncryptDCTL`, presets clavier (`Get/Load/Import/Export/Delete…KeyboardPreset`) |
| Project | `GetSettings` / `SetSettings`, presets de réglages projet (7 méthodes), `UpdateRenderPreset`, `SetQuickExportEnabledForRenderPreset`, `GetAudioRenderFormats` / `Codecs`, **statuts asynchrones** : `GetTranscribeAudioStatus`, `GetDetectSceneCutsStatus`, `GetSmartReframeStatus`, `GetCreateSubtitlesFromAudioStatus`, `GetAnalyzeForSlateStatus`, `GetRenderWithQuickExportStatus` |
| MediaStorage | clonage : `StartCloneMedia`, `StopCloneMedia`, `GetCloneStatus`, `SetCloneToolSettings` |
| MediaPool | `CreateMulticamClip` |
| MediaPoolItem | `GetTranscription`, `SetAudioMapping` |
| Timeline | `AutoAlignClips`, `NormalizeAudioLevel`, `GetNormalizeAudioModes`, `Get/SetOutputBlanking`, `Get/SetSettings` |
| TimelineItem | **`AddTransition`**, **`Get/SetSpeed`**, `Get/SetFades`, `GetType`, `Get/SetProperties`, multicam (`FlattenMulticam`, `PerformMulticamSmartSwitch`), output blanking, `SetSourceAudioChannelMapping` |
| Fusion | 1 270 → **1 303** outils enregistrés ; `GetActivationInfo` remplace `GetRLMLicenseInfo` |
| TimelineItem.GetProperty | +6 clés `*Enabled` (Composite, Cropping, DynamicZoom, LensCorrection, RetimeAndScaling, Transform) |

**Modèle asynchrone 21.1.1** : la méthode de lancement (`TranscribeAudio`, `DetectSceneCuts`, `SmartReframe`, `CreateSubtitlesFromAudio`, `RenderWithQuickExport`…) renvoie un booléen. On suit ensuite l'avancement avec `Project.Get*Status()`, qui renvoie des `TypedDict` avec `JobStatus`. Cela se traduit directement en **MCP Tasks** (lancement → tâche → sondage du statut).

## 3. Le stub officiel `DaVinciResolveScript.pyi` (2 848 lignes)

- 16 classes, **~50 `TypedDict`** (dont `ProjectSettings` ~320 lignes, `TimelineSettings`, `TimelineItemProperties`, `RenderSettings`, `ClipProperties`, `AutoCaptionSettings`, `MulticamOptions`, les statuts de jobs…), des `Literal` (couleurs de clip, de marker, de flag, types de piste) et des alias de constantes `resolve.*` documentés.
- Il **remplace le README.txt** (devenu `README.md`) comme référence. Les noms surchargés historiques (`GetSetting`, `SetSetting`, `GetProperty`, `SetProperty`, `GetClips`, `GetSubFolders`…) sont **encore présents en live mais absents du stub** : ils sont dépréciés (« Overloaded function signatures are deprecated »).
- `Fusion` et `FusionComp` restent des classes vides dans le stub : Fusion n'a toujours pas de doc typée officielle.

→ **Source idéale pour générer nos schémas Pydantic.** On parse le `.pyi` avec `ast` (TypedDict → modèles, Literal → enums, docstrings → descriptions), puis on complète avec l'introspection live : Fusion, clés réellement présentes, méthodes dépréciées encore actives.

## 4. Environnement d'exécution 21.1

- **Python 3.14 intégré** (`ResolvePython`) : `import DaVinciResolveScript` fonctionne sans variables d'environnement. Pas de `pip`.
- **LuaJIT 5.1** en ligne de commande : `Contents/Libraries/Fusion/fuscript`.
- Python 2 n'est plus supporté. Les Pythons externes ≥ 3.6 restent acceptés avec les variables d'environnement : vérifié avec 3.13.
- Scripting réseau : port 1144 + ports dynamiques 49152-65535 (Studio). Le mode `-nogui` est supporté officiellement.
- Fonctions IA qui exigent des téléchargements « Extras » (IntelliSearch, Slate ID, Speech Generator, modèles de transcription étendus) : sinon l'appel renvoie `False`. → Le tool de capacités doit le détecter et l'expliquer.

## 5. Conséquences pour le cadrage

1. **Python cible : 3.14** (aligné sur `ResolvePython`), en restant compatible 3.13 pour les interpréteurs externes.
2. **Générateur de schémas** depuis le `.pyi` officiel : c'est la brique centrale du socle.
3. **Jobs** : modèle lancement + `Get*Status`, encapsulé dans MCP Tasks.
4. **Méthodes dépréciées** : on appelle les nouvelles (`GetSettings`, `GetProperties`…) et on se replie sur les anciennes selon la version détectée.
5. **Benchmark** : le natif sert de base de comparaison (~2,7 k tokens de schémas, 0 tool métier).
