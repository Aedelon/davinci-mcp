# davinci-mcp

An MCP server that lets AI agents drive **DaVinci Resolve**. It is exhaustive, typed and safe.

- **Exhaustive.** It covers the Resolve 21.1 scripting API: project, media pool, timelines, items, color, Fusion, gallery, audio, render, interchange, AI tools and UI.
- **Typed.** Each action has a validated parameter schema and a typed result.
- **Safe.** Every write is verified by reading Resolve back. Deletes need an explicit confirmation, and destructive edits take a snapshot first. Every call is written to an audit log. File access is limited to allowed folders.
- **Economical.** Tools are deferred by the client. One `batch_run` call chains several steps, and almost-right parameters are repaired instead of refused.

## Requirements

- **DaVinci Resolve Studio 21.1+**, with *Preferences → General → External scripting using* set to **Local**.
- The free edition also works, through the in-app bridge (`davinci-mcp install --bridge`, then *Workspace → Scripts*).
- **Python 3.13+** and [uv](https://docs.astral.sh/uv/).

## Install

```bash
uv tool install davinci-mcp
```

Register it in Claude Code and Claude Desktop:

```bash
davinci-mcp install
```

Check the connection to Resolve:

```bash
davinci-mcp doctor
```

`davinci-mcp serve` runs the server over stdio, the default. `--transport http` serves it on loopback instead; set a bearer token for any other host.

## How agents use it

| Tool | Purpose |
|---|---|
| `<domain>_read` / `_write` / `_delete` | One tool per domain and kind. The call shape is `{"op": {"action": ..., ...params}}`. |
| `describe_action` | Compact schema of one or several actions (`actions=["color_write.set_cdl", ...]`), or the action list of one tool. |
| `batch_run` | Several calls in one round trip. Each step reads earlier outputs through `{{ steps.<id>.… }}` templates. |
| `workflow_write` | One-call jobs: `conform_from_edl`, `deliver`, `build_vertical_reel`, `apply_look`, `title_overlay`. |
| `recipe_list` / `recipe_run` | Reusable recipes in YAML, or in Python (opt-in). See [docs/recipes.md](docs/recipes.md). |
| `resolve_status`, `job_*`, `snapshot_list`, `search_api` | Connection state, long jobs, safety snapshots, API lookup. |

**Writes.** Each write returns a receipt with `verified=true|false`, computed by reading Resolve back. `dry_run=true` previews a write without applying it.

**Deletes.** A `*_delete` call first returns a plan and a `confirm_token`. Repeat the same call with that token to apply it.

## Configuration

Settings live in `~/.config/davinci-mcp/config.toml`. Any setting can be overridden with a `DAVINCI_MCP_<SECTION>__<KEY>` environment variable.

```toml
[safety]
allowed_roots = ["~/Movies", "~/Projects"]  # file access outside these is refused
auto_snapshot = "destructive"               # "off" | "destructive" | "all"

[recipes]
allow_python = false                        # Python recipes run arbitrary code
```

The server's own state, its configuration and the Claude configuration files are always off-limits to tools.

## Benchmarks

[eval/](eval/) runs headless Claude agents on real jobs in a live Resolve, with checks that do not go through the server under test. Result of [benchmark v7/v8](eval/RESULTS.md): 4 jobs written blind, 5 runs each, sum of medians. The community and built-in servers were measured in v7, davinci-mcp in v8, all with the same harness.

| Server | Success | Calls | Cost |
|---|---|---|---|
| **davinci-mcp** | **20/20** | 35 | $0.62 |
| community MCP | 13/20 | 90 | $0.99 |
| Resolve's built-in MCP (`run_script_unsafe`) | 20/20 | 17 | $0.35 |

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run pyright
```

[ARCHITECTURE.md](ARCHITECTURE.md) describes the design, and [docs/domain-guide.md](docs/domain-guide.md) explains how to add actions.

## License

Apache 2.0, © Delanoe Pirard / Aedelon.
