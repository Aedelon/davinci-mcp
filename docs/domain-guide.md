# Writing a davinci-mcp domain

Read this, then `src/davinci_mcp/domains/timeline.py` (the reference template), before writing code.

## Project facts

- Python 3.14 via `uv run …`, ruff + pyright **strict** (config in `pyproject.toml`), pytest (asyncio auto).
- Every file starts with `# Copyright Delanoe Pirard / Aedelon. Apache 2.0.` + module docstring, `from __future__ import annotations`, full type hints, English comments.
- No new dependencies. External binaries allowed: `ffmpeg`, `ffprobe` (Homebrew), macOS `screencapture`, `sips`, `osascript` — always `subprocess.run([...], stdin=subprocess.DEVNULL, check=False, capture_output=True)`, never a shell string.
- If `import davinci_mcp` fails: `chflags nohidden .venv/lib/python3.14/site-packages/*.pth` (iCloud hides .pth files; Python 3.14 skips them).
- **Never call the real DaVinci Resolve** (it is shared, single-threaded, and holds the user's projects). Tests use fakes only.

## Files you own

- `src/davinci_mcp/domains/<module>.py` exporting `ACTIONS: list[ActionSpec]`.
- `tests/test_domain_<module>.py` and optionally `tests/fakes/<module>.py`.
- Do **not** edit anything else (`domains/__init__.py`, `refs.py`, `pipeline.py`, `tests/fakes/fake_resolve.py`, `tests/conftest.py`, `tests/helpers.py`…). Registration in `domains/__init__.py` is done by the orchestrator. If you need a shared helper, keep a private one in your module and mention it in your report.

## The action model

```python
@action(domain="media", kind=Kind.READ, params=ListClipsParams, result=ClipList)
def list_clips(s: ResolveSession, p: ListClipsParams) -> ClipList:
    """One-line summary, ≤ 90 chars (it becomes the tool description line)."""
```

- Handlers run on the single resolve-api thread with a `ResolveSession` (`s.resolve`, `s.project_manager`, `s.project`, `s.timeline`, `s.caps`). They are synchronous and must return the `result` model.
- `Kind.READ` → tool `<domain>_read` (auto-approvable). `Kind.WRITE` → `<domain>_write`. `Kind.DESTRUCTIVE` → `<domain>_delete` (deletes, overwrites, irreversible switches). Clients call `{"op": {"action": "<name>", ...params}}`.
- **Writes** must pass `verify=` (read the state back → `Verification(ok, observed, note)`) or `verify_reason="why no read-back is possible"`.
- **Destructive** actions must pass `plan=` returning `PlanDraft(summary, effects, targets, fingerprint)`; the fingerprint is a short hash of the state that will be destroyed, so a changed state invalidates the consent token.
- **Long operations** (render, transcription, analysis…): `@action(..., kind=Kind.WRITE, long_running=True)` and the handler returns `JobRequest(kind, label, launch, poll, cancel=None, interpret=None)` (`davinci_mcp.actions.spec`). `launch(s)` starts it once and returns truthy; `poll(s)` returns a status mapping. The default interpreter reads `CompletionPercentage` / `Error` / `done` (JobStatus strings are localized — never compare them). Resolve 21.1.1 pairs launches with `Project.Get*Status()` (e.g. `GetTranscribeAudioStatus`). No `result=` is needed (it is `JobInfo`).
- Other spec options: `since=(21, 1, 0)` for 21.1+ APIs (the gate reads the live version), `studio_only=True` (Studio-only calls on the free edition open a modal that breaks later calls), `page="color"` for page-bound calls (PageGuard switches and restores), `snapshot="timeline"|"project"` declares WHAT to copy for risky edits (taken automatically only for destructive actions under the default `safety.auto_snapshot = "destructive"`; clients can force or skip it per call with `snapshot: true|false`; timeline copies target the action's `timeline` param and are filed in the "davinci-mcp snapshots" bin), `idempotent=True` when replay is harmless, `timeout_s=` for slow calls.
- Raise `DavinciError(ErrorCode.X, message, hint=..., retryable=False, details=...)` for every failure: `NOT_FOUND`, `AMBIGUOUS_REFERENCE`, `WRONG_STATE`, `API_CALL_FAILED` (Resolve returned False/None), `CAPABILITY_UNAVAILABLE`, `EXTRAS_MISSING`, `PATH_NOT_ALLOWED`, `INVALID_PARAMS`.
- Param models: `model_config = ConfigDict(extra="forbid")`, `Field(description=...)` on anything non-obvious, sensible defaults. Result models are plain pydantic models (no `extra` restriction needed). Keep schemas lean: the whole server's tool list must stay small.
- Read results may include `images: list[ImageFile]` (`davinci_mcp.images`) to attach pictures.

## Shared helpers (import, don't copy)

- `davinci_mcp.refs`: `TimelineRef`, `ItemRef` (id | track+position | name), `ClipRef` (id | name | file_path), `FolderRef` (id | path "Master/A/B"), `TrackSel`, `resolve_timeline`, `resolve_item`, `resolve_clip`, `resolve_folder`, `iter_timelines`, `iter_items`, `walk_folders`, `all_clips`, `summarize_timeline`, `summarize_item`, `proxies(value)` (typed list from a Resolve list return), `const(s, "NAME")` (live `resolve.NAME` constant — Resolve silently rejects the string names, always resolve constants), `TrackType`.
- `davinci_mcp.domains._runtime.runtime()`: `.settings`, `.allowed_roots`, `.cache`, `.snapshots`.
- `davinci_mcp.safety.paths.check_path(path, runtime().allowed_roots, must_exist=...)` for **every** filesystem path coming from a client.
- `davinci_mcp.safety.pages.restore_page_after(s.resolve)` for getters with hidden page switches.
- `davinci_mcp.api._generated.resolve_21_1`: pydantic models for every stub TypedDict (`ProjectSettings`, `TimelineItemProperties`, `RenderSettings`, `AutoCaptionSettings`, …) and `Literal` aliases (`MarkerColor`, `ClipColor`, `TrackType`, constant-name enums like `CompositeMode`). Reuse them for params/results instead of redefining.
- Exact signatures: `/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/DaVinciResolveScript.pyi` (official 21.1.1 stub) and `src/davinci_mcp/api/_generated/api_surface_21_1.json`. Live method lists per class: `research/api-introspection/live-dir-21.1.1.10.json`. Known API traps: `research/audit-existant.md` §6.2 — read the sections for your domain and encode the traps (guards, warnings, read-backs).

## API rules that bite

- Presence of a method = `s.caps.has(obj, "Name")` (uses `dir()`); **never `hasattr`** (always True on Resolve proxies). Use `s.caps.require(obj, "Name", since=(21,1,0))` before calling optional methods.
- `getattr(proxy, "Unknown")` returns None; calling it raises TypeError.
- All indices exposed to clients are **1-based** (tracks, timelines, items, nodes, takes).
- Resolve often returns True without applying a change: that is why writes verify.
- Prefer the 21.1 plural APIs (`GetSettings`, `GetProperties`, `SetProperties`) and fall back to the deprecated singular ones when `caps.has` says the plural form is missing.

## Tests

```python
from tests.fakes.fake_resolve import make_resolve
from tests.helpers import call, confirm, mcp_client


async def test_list(settings):
    resolve = make_resolve(timelines=2)
    async with mcp_client(resolve, settings, ACTIONS) as client:
        err, out = await call(client, "media_read", {"op": {"action": "list_clips"}})
        assert not err
```

- Fakes: `tests/fakes/fake_resolve.py` mirrors live `dir()` (unknown methods return None). Add behaviour for the methods you need in **your own** `tests/fakes/<module>.py` (subclass the fake classes, or attach callables on instances with `setattr`). Read `fake_resolve.py` first to see what already exists.
- Cover every action: success, verification failure (fake returns True without effect), NOT_FOUND / validation errors, destructive plan → `confirm(...)`, long-running start + `job_wait`.
- Validation for your files (must all pass): `uv run ruff check <files> && uv run ruff format --check <files> && uv run pyright <your src files> && uv run pytest <your tests> -q`.

## Report (≤ 8 lines)

Files, action list per tool (read/write/delete), API traps encoded, validation results, anything left out and why, helpers worth promoting to `refs.py`.
