# Changelog

## Unreleased

- **Config:** removed `[telemetry] otlp_endpoint`. It was accepted but never used. A `config.toml` that still sets it now fails validation: delete the `[telemetry]` section.

## 1.0.3 (2026-11-01)

- **Tool descriptions:** every domain tool opens with when to use it; `dry_run`, `snapshot` and `confirm_token` are described in the compact schemas; `job_*`, `recipe_list`, `snapshot_list`, `resolve_status` and `search_api` explain their parameters and their siblings.
- **Annotations:** `*_write` tools and `job_cancel` are now `destructiveHint: true`, since they can overwrite current values (a grade, a preset) or stop a render.

## 1.0.2 (2026-11-01)

- **Docs:** the free edition is supported only up to 21.0.x. Free 21.1 hides in-app Python scripts; in-app Lua scripts get no API, and the Console's Lua, which has the API, has no file or network access (tested on 21.1.2). The 1.0.0 notes claimed otherwise.

## 1.0.1 (2026-11-01)

- Listed in the official MCP Registry as `io.github.Aedelon/davinci-mcp` (`server.json`, published by the release workflow).

## 1.0.0 (2026-11-01)

First public release.

### Coverage

- **Domains:** one tool per (domain, kind) over the Resolve 21.1 scripting API. The domains are project, media, timeline, item, color, fusion, gallery, audio, render, interchange, AI, UI and app.
- **Free edition:** reached through the in-app bridge.

### Safety

- **Writes** are verified by reading Resolve back (`verified`).
- **Deletes** need two calls: a plan, then the same call with its `confirm_token`.
- **Snapshots** are taken before destructive edits, by default.
- **Audit:** every call is written to a JSONL log.
- **Files:**
  - access is limited to `allowed_roots`;
  - the server's own state and the client configuration files are always refused;
  - symlinks are never followed when importing a folder.

### Fewer round trips

- **`batch_run`:** ad-hoc steps chained server-side, with one confirmation for the whole batch.
- **`workflow_write`:** single-call jobs (`conform_from_edl`, `deliver`, `build_vertical_reel`, `apply_look`, `title_overlay`).
- **Recipes:** YAML recipes, plus Python recipes (opt-in).
- **Parameter repair:** almost-right parameters are repaired, and each rewrite is reported in `notes`.
- **Error hints** suggest the right field name ("did you mean"), and redirect an action called on the wrong tool.

### Lower token cost

- **Deferred tools:** no tool is marked `anthropic/alwaysLoad`, so clients can defer the whole server. Measured first-turn context: 32k tokens, against 89–102k before.
- **`describe_action`:** compact schemas, several actions in one call, or the action list of one tool.
- **Compact replies:** `batch_run` and `recipe_run` return compact steps; `list_render_formats` lists codecs only when a format is given.

### Tooling

- **`eval/`:** live benchmark harness with three suites (v1, blind v2, scale), isolated agents and a guard against tools outside the server under test.
- **CLI:** `davinci-mcp install | doctor | serve`.
