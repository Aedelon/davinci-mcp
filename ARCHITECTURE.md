# davinci-mcp — Architecture du socle

> Conception du 2026-10-26. Référence : [CADRAGE.md](CADRAGE.md), [RECHERCHE.md](RECHERCHE.md).
> Stack vérifiée : Python 3.14, FastMCP 4.1.3, `fusionscript` de Resolve Studio 21.1.1 chargé depuis Python 3.14.

## 1. Vue d'ensemble

```
 Client MCP ──stdio / Streamable HTTP──▶ FastMCP (server.py)
                                           │  tools générés depuis le registre
                                           ▼
                                   ActionRegistry ── ActionSpec (déclaratif)
                                           │  pipeline d'exécution
            ┌──────────────┬───────────────┼───────────────┬──────────────┐
            ▼              ▼               ▼               ▼              ▼
        Consent        Snapshots       PageGuard        Receipts      AuditLog
            └──────────────┴───────┬───────┴───────────────┴──────────────┘
                                   ▼
                        ResolveExecutor (1 thread)
                                   ▼
                        ConnectionManager ── Capabilities
              ┌────────────────────┼─────────────────────┐
              ▼                    ▼                     ▼
       ExternalBackend      PythonBridgeBackend    LuaBridgeBackend
       (fusionscript)       (gratuit ≤ 21.0)       (gratuit ≥ 21.1)
```

Invariant central : **tout appel à l'API Resolve passe par `ResolveExecutor`**. Il n'y a jamais d'appel depuis le thread asyncio, ni depuis les threads de FastMCP (`run_in_thread=True` par défaut), ni depuis un job. L'existant contourne son propre verrou : c'est exactement ce qu'on interdit.

## 2. Arborescence

```
src/davinci_mcp/
  __init__.py
  __main__.py            # CLI : serve | doctor | install | gen-schemas
  config.py              # Settings (TOML + env DAVINCI_MCP_*)
  errors.py              # ErrorCode, DavinciError → ToolResult isError
  executor.py            # ResolveExecutor (thread unique, timeouts)
  connection/
    backend.py           # Protocol Backend
    external.py          # DaVinciResolveScript, découverte des chemins, lancement
    bridge.py            # client du pont (protocole commun Python et Lua)
    manager.py           # choix du backend, test de vie, reconnexion
  capabilities.py        # version, édition, canal, méthodes par classe (dir())
  refs.py                # références d'objets : uid prioritaire, nom/index en repli
  cache.py               # cache TTL + compteur de génération
  api/
    stub_parser.py       # parse DaVinciResolveScript.pyi
    _generated/          # modèles Pydantic générés (versionnés par build)
  actions/
    spec.py              # ActionSpec, Kind, décorateur @action
    registry.py          # registre, génération des tools
    pipeline.py          # consentement → snapshot → page → appel → reçu → audit
    core.py              # actions socle : status, capabilities, jobs, search_api
  safety/
    consent.py           # dry-run, plan, jeton HMAC
    snapshots.py         # duplication de timeline, export .drp
    paths.py             # racines autorisées
    pages.py             # PageGuard : sauvegarde et restauration de la page
  receipts.py            # Receipt, vérification par relecture
  jobs.py                # JobManager (lancement + Get*Status), persistance
  audit.py               # journal JSONL des écritures
  server.py              # construction de FastMCP depuis le registre
tools/
  introspect/introspect_api.py
  gen_schemas.py         # .pyi → api/_generated/
tests/
  fakes/                 # faux objets Resolve générés depuis l'introspection
```

## 3. Connexion

### 3.1 Backends

```python
class Backend(Protocol):
    channel: Literal["external", "bridge-python", "bridge-lua"]

    def connect(self) -> ResolveApp: ...  # renvoie l'objet `resolve` (ou un proxy)
    def close(self) -> None: ...
```

- **ExternalBackend** : charge `DaVinciResolveScript` avec les chemins par OS (variables d'environnement, ou valeurs par défaut du README 21.1). Appelle `scriptapp("Resolve")`.
- **BridgeBackend** (même client pour Python et Lua) : JSON-RPC sur TCP en loopback. Le pont, dans Resolve, tient une table de handles ; le client renvoie des `RemoteProxy` dont `__getattr__` fait des appels RPC. **Le code métier est identique quel que soit le backend** (duck typing). Sécurité : jeton par machine (fichier 0600), HMAC + nonce + horodatage, limite de taille avant authentification, arrêt après inactivité.
- Le pont est conçu dès le socle (protocole et client), mais son implémentation côté Resolve est livrée après le socle. Le pont Lua demande un test sur une install gratuite 21.1.

### 3.2 ConnectionManager

- **Ordre** : external (si Studio) → bridge-python → bridge-lua. Le canal choisi est indiqué dans chaque réponse (`meta.channel`).
- **Test de vie** : `GetProjectManager().GetCurrentDatabase()`. S'il renvoie `None`, l'instance est coincée (piège documenté : tout le reste semble sain). On ne revalide **pas** à chaque appel, contrairement à l'existant (`GetVersion()` à chaque appel) : on teste à la connexion, puis seulement quand une erreur de transport arrive.
- **Reconnexion** : sur erreur de transport, une reconnexion puis **un seul** nouvel essai, uniquement pour les actions `READ` ou `idempotent`. Les écritures ne sont jamais rejouées automatiquement : on renvoie `CONNECTION_LOST` avec la consigne de relire l'état.
- **Lancement** : seulement si **aucun** processus Resolve n'existe (jamais de deuxième instance). Option `-nogui`. Le mode headless est détecté par l'argv du processus (l'API ne permet pas de le savoir).
- Tout sous-processus est lancé avec `stdin=DEVNULL` (piège ffmpeg sous stdio).

### 3.3 Capabilities

Calculées à la connexion et mises en cache par connexion :
- `version` (tuple), `edition` (`IsStudio()` en 21.1+, sinon `GetProductName()`), `channel`, `headless` ;
- `methods[class]` = `dir()` de chaque type d'objet atteignable. **Jamais `hasattr`** : il renvoie toujours True.
- `extras` : inconnus a priori. On les déduit des échecs (une chaîne « Required package… » ou un `False`) et on les mémorise.

Une action déclare `requires=("Timeline.AddTransition",)`. Si la méthode est absente, l'action renvoie `CAPABILITY_UNAVAILABLE` avec la version minimale. **La liste des tools reste identique** (spec 2026-07-28) : on ne masque rien, on refuse avec une erreur explicite.

## 4. ResolveExecutor

```python
class ResolveExecutor:
    async def run[T](self, fn: Callable[[ResolveSession], T], *, timeout: float) -> T: ...
```

- Un `ThreadPoolExecutor(max_workers=1)` dédié, nommé `resolve-api`. L'objet `resolve` n'existe que dans ce thread.
- Un timeout par appel. En cas de dépassement, l'appel n'est **pas** interruptible : on marque l'instance comme « suspecte », les appels suivants attendent avec un délai borné et le test de vie tranche.
- Garde d'affinité : `ResolveSession` vérifie `threading.current_thread()` dans ses accesseurs. Un appel hors thread lève `INTERNAL`, ce qui est détecté en test.
- Les jobs (rendu, transcription…) **sondent** leur statut via l'exécuteur, comme toute autre lecture.

## 5. Registre d'actions : une seule source

```python
@action(
    domain="timeline",
    name="add_marker",
    kind=Kind.WRITE,
    params=AddMarkerParams,
    result=MarkerInfo,
    requires=("Timeline.AddMarker",),
    since=(20, 0),
    idempotent=False,
    snapshot=None,
    page=None,
    long_running=False,
)
def add_marker(s: ResolveSession, p: AddMarkerParams) -> Change[MarkerInfo]: ...
```

- **Kind** : `READ` / `WRITE` / `DESTRUCTIVE`. Le kind détermine les annotations MCP (`readOnlyHint`, `destructiveHint`, `idempotentHint`). Elles sont **générées**, jamais écrites à la main.
- **Exposition** : par domaine, **un tool par kind présent** (`timeline_read`, `timeline_write`, `timeline_delete`). Le paramètre `action` est un **discriminant** d'une union Pydantic (`oneOf`), ce qui donne des schémas stricts et des lectures auto-approuvables. Les noms sont triés pour garantir un ordre déterministe.
- **Forme d'appel implémentée** : `{"op": {"action": "add_marker", ...params}, "dry_run"?: bool, "confirm_token"?: str}`. L'union discriminée est placée **sous `op`** : le niveau racine reste un objet simple, parce que l'API Claude refuse un `oneOf` en racine du schéma d'entrée (confiance MEDIUM). Chaque action renvoie un résultat validé par l'`outputSchema` côté client.
- **Pivots** (`meta: {"anthropic/alwaysLoad": true}`) : `resolve_status`, `search_api`. Autres tools fixes : `job_status`, `job_wait`, `job_cancel`, `job_list`, `snapshot_list`.
- **Domaine modèle** : [src/davinci_mcp/domains/timeline.py](src/davinci_mcp/domains/timeline.py) (lister, ajouter et supprimer des markers) sert de gabarit aux auteurs de domaines. Un module de domaine expose `ACTIONS` et est enregistré dans `domains/__init__.py`.
- La doc de référence (MkDocs), les tests de contrat et le budget de tokens sont **générés** depuis le registre. Le budget est vérifié en CI.
- Une action `WRITE` ou `DESTRUCTIVE` **doit** fournir `verify` (relecture), ou `verify=None` avec une justification. Le registre refuse sinon, au chargement.

## 6. Pipeline d'exécution

Pour chaque appel :

1. Validation Pydantic (paramètres inconnus refusés, avec la liste des noms valides).
2. Capability gate.
3. **Consentement** (DESTRUCTIVE) : sans `confirm_token`, on renvoie un `Plan` (effets, cibles, empreinte de l'état) et un jeton `HMAC(secret, action + params + empreinte)` valable 5 min. Avec `dry_run=true`, on renvoie toujours le plan, sans rien appliquer. Si l'empreinte de l'état a changé entre le plan et la confirmation, on renvoie `CONFIRMATION_STALE`. Si le client gère l'elicitation, on la propose ; le jeton reste la garantie.
4. **Snapshot** si l'action le demande (`timeline` = `DuplicateTimeline` ; `project` = `ExportProject` en `.drp`). On refuse le snapshot de projet sur « Untitled Project » (`SaveProject` bloque en headless).
5. **PageGuard** si l'action exige une page (ex. `ExportLUT`, `AddVersion` → `color`). On restaure la page d'origine ensuite. Les getters à effet de bord connus (`GetCurrentRenderMode` → Deliver) sont marqués `page_side_effect`.
6. Appel via l'exécuteur.
7. **Reçu** : relecture, `before` / `after` / `verified`. Un `True` de l'API contredit par la relecture donne `verified=false` + `VERIFICATION_FAILED` (avertissement, pas une exception).
8. Invalidation du cache (génération + 1), puis entrée dans le journal d'audit (JSONL : action, paramètres, cible, résultat, snapshot, durée).

## 7. Erreurs

```python
class ErrorCode(StrEnum):
    RESOLVE_NOT_RUNNING
    NOT_CONNECTED
    CONNECTION_LOST
    INSTANCE_STUCK
    STUDIO_REQUIRED
    CAPABILITY_UNAVAILABLE
    EXTRAS_MISSING
    INVALID_PARAMS
    NOT_FOUND
    AMBIGUOUS_REFERENCE
    WRONG_STATE
    API_CALL_FAILED
    VERIFICATION_FAILED
    CONFIRMATION_REQUIRED
    CONFIRMATION_STALE
    CONFIRMATION_INVALID
    PATH_NOT_ALLOWED
    TIMEOUT
    INTERNAL
```

Chaque erreur porte `{code, message, hint, retryable, next_call?, details}` et est renvoyée en `ToolResult(is_error=True, structured_content=...)`. `retryable` a une valeur par défaut selon le code (ex. `CONNECTION_LOST` → True, `STUDIO_REQUIRED` → False).

## 8. Références d'objets

```python
Ref = TimelineRef | ItemRef | ClipRef | FolderRef | ...  # union discriminée sur "kind"


class ItemRef(BaseModel):
    kind: Literal["timeline_item"]
    id: str | None = None  # GetUniqueId(), prioritaire
    timeline: TimelineRef | None = None
    track: TrackSel | None = None  # repli : (type, index 1-based) + item_index 1-based
    name: str | None = None  # repli : nom (AMBIGUOUS_REFERENCE si plusieurs)
```

- **Tous les index sont en base 1** (comme l'API Resolve), documentés comme tels. L'existant mélange base 0 et base 1.
- Index `uid → objet` construit à la demande pour chaque timeline ou Media Pool, et invalidé par génération. On évite ainsi les parcours O(N) à chaque appel.
- Chaque résultat renvoie les `id` des objets touchés, pour les appels suivants.

## 9. Générateur de schémas depuis le `.pyi`

`tools/gen_schemas.py` (lancé à la main et en CI quand le stub change) :
- `TypedDict` (classe ou forme fonctionnelle `TypedDict('X', {...})`) → `BaseModel` avec `extra="forbid"` et des champs optionnels si `total=False`. Les clés avec espaces (`ClipProperties`) passent par des alias. Les commentaires et docstrings deviennent des descriptions.
- `Literal` → `Literal` ; les alias `float` décrits comme « One of the resolve.* constants » → enum de **noms de constantes**, résolus vers la valeur live **au moment de l'appel** (piège : les chaînes sont rejetées en silence).
- Classes → `API_SURFACE = {class: {method: MethodSig(params, returns, doc)}}`, utilisé pour `search_api`, la capability gate et les wrappers.
- Sortie : `api/_generated/resolve_<version>.py` + `api_surface_<version>.json`, fusionnés avec l'introspection live (méthodes dépréciées encore actives, Fusion).

## 10. Jobs

- `JobManager` : un job = `{id, kind, started, poll: Callable, status, progress, result}`. Il est persisté dans `~/.local/state/davinci-mcp/jobs.json`.
- Modèle 21.1.1 : on lance (`TranscribeAudio`…) puis on sonde `Project.Get*Status()`. `JobStatus` est localisé : on se fie à `CompletionPercentage`, à `Error` et à l'existence du fichier.
- Exposition : tools `task=True` (extension MCP Tasks, fonctions async) **et** tools de repli `job_status`, `job_wait`, `job_cancel`. Progress notifications via `ctx.report_progress`.

## 11. Configuration

`~/.config/davinci-mcp/config.toml`, surchargé par des variables `DAVINCI_MCP_*` :

```toml
[connection]
auto_launch = true
headless = false
call_timeout_s = 30
[safety]
allowed_roots = ["~/Movies", "~/Documents/Resolve"]
snapshot_dir = "~/.local/state/davinci-mcp/snapshots"
snapshot_retention = 20
enable_code_execution = false
[http]
host = "127.0.0.1"
port = 8765
token_env = "DAVINCI_MCP_TOKEN"
[telemetry]
otlp_endpoint = ""          # opt-in
```

## 12. Ordre d'implémentation du socle

1. `errors`, `config`, `executor` + tests.
2. `connection` (external) + `capabilities` + test de vie + smoke test live.
3. `api/stub_parser` + `gen_schemas` → modèles générés depuis le stub 21.1.1.
4. `actions` (spec, registry, pipeline) + `receipts` + `audit` + `refs` + `cache`.
5. `safety` (consent, snapshots, pages, paths) + `jobs`.
6. `server.py` + actions socle (`resolve_status`, `resolve_capabilities`, `search_api`, `job_*`) + test de bout en bout via un client FastMCP.
7. Pont : protocole + client, puis scripts Python et Lua côté Resolve (après le socle).

Les domaines (projet, media, timeline, couleur, Fusion, Fairlight, rendu, IA, UI, interchange, vision) se branchent ensuite en parallèle, un agent `implementer` par domaine, sans toucher au socle.
