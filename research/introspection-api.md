# Introspection de l'API live — DaVinci Resolve Studio 21.0.4.5 (macOS)

Relevé du 2026-10-26 avec [tools/introspect/introspect_api.py](../tools/introspect/introspect_api.py). Le script crée un projet jetable (`zz_davinci_mcp_introspect`) avec un clip ProRes de test généré par ffmpeg, relève `dir()` de chaque type d'objet, puis supprime ce projet et recharge le projet courant. Projet courant restauré et projet jetable supprimé : **vérifié**.

Données brutes : [api-introspection/](api-introspection/) (`live-dir-*`, `readme-methods-*`, `diff-*`, `dumps-*`, `fusion-actions-*`).

## 1. API Resolve : le README est quasi exhaustif

| Objet | Méthodes live | Documentées | Non documentées |
|---|---|---|---|
| Resolve | 35 | 33 | `SetHighPriority` |
| ProjectManager | ~46 | — | `GetProjectLastModifiedTime` |
| Project | — | — | (aucune ; `DeleteRenderJobByIndex` est listée « Unsupported » et absente) |
| MediaStorage, MediaPool, Folder (16), MediaPoolItem (44), Timeline (61), TimelineItem (88), Graph (12), Gallery (9), GalleryStillAlbum (7), ColorGroup (6) | | | aucune |
| GalleryStill | 0 méthode | | objet opaque |

- **397 méthodes** côté Resolve (hors `Print`, présent partout). Le README en couvre la totalité, sauf 2 méthodes. Confiance : HIGH.
- Conséquence : la promesse « documenté + non documenté » ne rapporte presque rien côté Resolve. `dir()` ne donne **ni signatures ni types** : la vraie zone cachée, ce sont les **arguments, les clés de dictionnaires et les valeurs énumérées**.

## 2. La vraie surface cachée : clés, réglages, énumérations

| Dump | Taille | Intérêt |
|---|---|---|
| `Project.GetSetting()` | 158 clés | gestion des couleurs (ACES, color science), audio, galerie… |
| `Timeline.GetSetting()` | 158 clés | réglages par timeline |
| `MediaPoolItem.GetClipProperty()` | 263 clés | métadonnées de clip |
| `TimelineItem.GetProperty()` | 26 clés | transform, crop, composite, retime, scaling |
| `GetRenderFormats` / `GetRenderCodecs` | 23 formats, 326 codecs | validation des paramètres de rendu |
| `GetRenderPresetList` | 38 presets | |
| `Fusion.GetRegSummary()` | 1 270 entrées | tous les outils et modificateurs Fusion enregistrés |

→ À générer automatiquement : des **schémas typés (enum/Literal)** pour chaque setting, propriété et format, avec un diff par version. C'est là qu'on bat une couverture basée sur le README.

## 3. Fusion : surface large et non documentée dans le README

- `Fusion` : 141 membres. `FusionComposition` : 92. `FusionTool` : 38. `FusionInput` : 26. Un outil `Background` expose 117 inputs.
- Points notables : `comp.Transcribe`, `StartUndo` / `EndUndo` / `Undo` / `Redo` / `GetUndoStack` (vrai undo transactionnel côté Fusion), `Lock` / `Unlock`, `SaveSettings` / `LoadSettings` (fichiers `.setting`), `UserControls`, `AddModifier`, `GetKeyFrames`.
- **ActionManager : 886 actions** (`fusion-actions-*.json`), dont 547 `AddTool_*`, plus Viewer, Comp, Player, Frame, Layout… Elles sont **limitées au périmètre Fusion** : aucune action des pages Edit, Color, Fairlight ou Deliver. Confiance : HIGH (catégories relevées).
  → Ça ne remplace pas l'automatisation de l'interface par l'OS pour les autres pages, mais ça couvre l'interface de la page Fusion sans aucun clic.
- `UIManager` (fenêtres UI natives Fusion) et `HotkeyManager` (`GetKeys`) : potentiellement utiles pour lire les raccourcis réels de l'utilisateur.

## 4. Autres points d'entrée découverts dans `Developer/`

| Dossier | Ce que c'est | Intérêt pour davinci-mcp |
|---|---|---|
| `Workflow Integrations/` | Plugins Electron 36 dans Resolve, API JS avec promesses (20.1+) | Autre canal **dans l'application** pour le pont. À vérifier : disponibilité sur la version gratuite |
| `OGraf HTML Templates/` | Titres HTML/CSS/JS (standard EBU OGraf) rendus par CEF via Fusion | **Titres animés générés en HTML par le LLM**, idéal pour les Reels et sous-titres stylés |
| `Fusion Fuse/`, `Fusion Templates/`, `DaVinciCTL/`, `LUT/`, `OpenFX/`, `CodecPlugin/` | Exemples et SDK | Génération et installation de Fuses, DCTL, templates |

## 5. Environnement constaté

- Resolve Studio 21.0.4.5, scripting externe actif. Resolve tournait en `-nogui`, sans doute lancé par le MCP existant.
- Pythons disponibles : 3.10 à 3.13 (python.org). `fusionscript` fonctionne avec **3.13** : vérifié. Le MCP existant utilise 3.11.
- ffmpeg et ffprobe sont disponibles via Homebrew.

## 6. Conséquences pour le cadrage

1. Requalifier « non documenté » : la cible est la **surface de paramètres** (clés, enums, formats) et **Fusion**, pas les méthodes Resolve.
2. Le générateur de stubs et de mocks doit enregistrer les **valeurs retournées** (dumps), pas seulement `dir()`.
3. Le diff de version doit porter sur **méthodes + clés + enums + RegSummary Fusion + actions**.
4. Nouvelles features candidates : un undo Fusion transactionnel, des titres OGraf générés, un pont via Workflow Integration (à évaluer).
