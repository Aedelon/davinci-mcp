# Changelog

## 1.0.0 (unreleased)

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
