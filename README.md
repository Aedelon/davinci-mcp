<p align="center">
  <img src="https://raw.githubusercontent.com/Aedelon/davinci-mcp/main/docs/assets/banner.png" alt="davinci-mcp: the safe, verified MCP server for DaVinci Resolve" width="100%">
</p>

<p align="center">
  <a href="https://github.com/Aedelon/davinci-mcp/actions/workflows/ci.yml"><img src="https://github.com/Aedelon/davinci-mcp/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://pypi.org/project/davinci-mcp/"><img src="https://img.shields.io/pypi/v/davinci-mcp?color=e9762b" alt="PyPI"></a>
  <img src="https://img.shields.io/badge/python-3.13%2B-3776ab" alt="Python 3.13+">
  <img src="https://img.shields.io/badge/license-Apache%202.0-blue" alt="Apache 2.0">
  <img src="https://img.shields.io/badge/DaVinci%20Resolve-21.1-d9381e" alt="DaVinci Resolve 21.1">
  <img src="https://img.shields.io/badge/MCP-2026--07--28-7a8a99" alt="MCP spec 2026-07-28">
  <a href="https://ko-fi.com/aedelon"><img src="https://img.shields.io/badge/Ko--fi-support-ff5e5b?logo=ko-fi&logoColor=white" alt="Support on Ko-fi"></a>
</p>

<p align="center"><b>Let an AI agent edit, grade and deliver in DaVinci Resolve, with every change checked.</b></p>

---

davinci-mcp is an [MCP](https://modelcontextprotocol.io) server for **DaVinci Resolve**. It gives AI agents such as Claude access to the whole Resolve scripting API: 299 typed actions across 17 domains. Every change is **verified against Resolve itself** before it is reported as done.

It is built for **editors and colorists** who want an assistant that does the clicking, and for **developers** who want a dependable bridge between an agent and Resolve.

## See it work

A real Claude agent, given four plain messages, cut this 52-second action trailer from the open movie *Tears of Steel*. It imported the footage, laid out 33 shots on the beat, added the title cards, graded teal and orange, and rendered it. Nothing in the run was staged; the video below is sped up 4×.

<p align="center">
  <a href="https://www.youtube.com/watch?v=iT-ptK_Gx-g"><img src="https://raw.githubusercontent.com/Aedelon/davinci-mcp/main/docs/assets/demo-preview.gif" alt="A Claude agent editing a trailer in DaVinci Resolve through davinci-mcp" width="100%"></a>
  <br><sub><a href="https://www.youtube.com/watch?v=iT-ptK_Gx-g">▶ Watch the full demo on YouTube (39 s)</a></sub>
</p>

<p align="center">
  <a href="https://www.youtube.com/watch?v=epndWG8TqFM"><img src="https://raw.githubusercontent.com/Aedelon/davinci-mcp/main/docs/assets/trailer-poster.jpg" alt="The trailer the agent cut" width="70%"></a>
  <br><sub><a href="https://www.youtube.com/watch?v=epndWG8TqFM">▶ Watch the trailer the agent cut on YouTube (52 s)</a></sub>
</p>

## Why davinci-mcp

**Verified, not hopeful**
- Every write is read back from Resolve, and its receipt says `verified: true` or `false`. An agent can't claim a change that didn't happen.
- `dry_run` previews any write without applying it.
- Deletes need consent: the first call returns a plan and a `confirm_token`, and nothing is removed until the same call comes back with the token.
- Destructive edits take a safety snapshot of the timeline first.
- Every call is written to a JSONL audit log.

**Reliable**
- In the benchmark below, 20 of 20 jobs passed. The community MCP server passed 13 of 20.
- Near-miss parameters are repaired instead of refused (`fade_out` → `FadeOut`, a bare clip name → a selector), and every rewrite is reported.
- Errors are actionable: they suggest the right field ("did you mean"), name the right tool, and say which render setting Resolve rejected.

**Economical**
- `batch_run` chains a whole sequence server-side in one call, for example 18 steps from timeline to render.
- Clients defer the tools, so the first turn costs about 32k tokens of context.
- Replies and schemas are compact. Against the community server: **31 % cheaper** and **60 % fewer calls**.

**Exhaustive and extensible**
- 17 domains: project, media, timeline, item, color, Fusion, gallery, audio, render, interchange, AI tools, UI, app and more.
- One-call workflows: `conform_from_edl`, `deliver`, `build_vertical_reel`, `apply_look`, `title_overlay`.
- Your own [recipes](https://github.com/Aedelon/davinci-mcp/blob/main/docs/recipes.md): YAML templates, plus Python when you opt in.

**Safe by default**
- File access is limited to the folders you allow.
- The server's own state, its configuration and your Claude configuration files are always off-limits.
- Symlinks are never followed when importing a folder.

## Benchmarks

Headless Claude agents (Sonnet) ran four post-production jobs on a live Resolve Studio 21.1, five runs each. An agent that never saw the server's code wrote the jobs:
- **dailies**: log and organise a day of footage;
- **promo**: build a promo with a music bed;
- **look**: grade with versions and a still;
- **spot QC**: duplicate, export interchange files and render a range.

Each check reads Resolve directly, never through the server under test. Every agent ran under the same isolated harness. [Full method and history →](https://github.com/Aedelon/davinci-mcp/blob/main/eval/RESULTS.md)

<p align="center">
  <img src="https://raw.githubusercontent.com/Aedelon/davinci-mcp/main/docs/assets/bench-success.svg" alt="Jobs completed: davinci-mcp 100 %, community MCP 65 %, Resolve built-in MCP 100 %" width="49%">
  <img src="https://raw.githubusercontent.com/Aedelon/davinci-mcp/main/docs/assets/bench-cost.svg" alt="Cost for the 4 jobs: davinci-mcp $0.62, community MCP $0.99, Resolve built-in MCP $0.35" width="49%">
</p>

| Server | Jobs passed | Tool calls | Cost (4 jobs) |
|---|---|---|---|
| **davinci-mcp** | **20 / 20** | 35 | $0.62 |
| Community MCP ([samuelgursky/davinci-resolve-mcp](https://github.com/samuelgursky/davinci-resolve-mcp)) | 13 / 20 | 90 | $0.99 |
| Resolve's built-in MCP | 20 / 20 | 17 | $0.35 |

**About the built-in server.** Resolve 21.1 ships its own MCP server, which is cheaper because it hands the agent one tool, `run_script_unsafe`, to run arbitrary Python inside Resolve. That makes for short runs, but nothing is validated, read back, audited, snapshotted or confirmed before a delete. In our scale test (160 clips, 4 timelines) it once put a marker at the wrong frame, then reported the job done. davinci-mcp trades a little cost for changes you can trust.

## Talk to it like an editor

```text
Import the clips in ~/Footage/day03, sort interviews and B-roll into bins, and colour the circled takes green.
```
```text
Build a 9:16 reel from these three shots, add subtitles from the audio, and render it for Instagram.
```
```text
Conform this EDL against the media in ~/Rushes and deliver a ProRes master and an H.264 proxy.
```
```text
Apply the show LUT to every shot of the timeline except the end card, and grab a still of the first shot.
```
```text
Add a red marker at the start of every shot named "broll_" in Edit_03.
```
```text
Duplicate the approved cut as "QC", export it as FCPXML and OTIO, and render 01:00:02:00 to 01:00:07:24.
```

## How it works

```mermaid
flowchart LR
    A["AI agent<br/>Claude, Cursor…"] -->|MCP call| V["Validate<br/>+ repair"]
    V --> C["Consent<br/>deletes only"]
    C --> N["Snapshot<br/>destructive only"]
    N --> X["Call Resolve<br/>one thread"]
    X <-->|scripting API| D[("DaVinci<br/>Resolve")]
    X --> R["Read back<br/>→ receipt"]
    R --> L["Audit log"]
    R -->|"verified: true / false"| A
```

1. **Validate.** The agent calls a domain tool such as `timeline_write` with `{"op": {"action": ..., ...}}`, and the parameters are checked against a typed schema. Almost-right parameters are repaired, and each repair is reported.
2. **Consent and snapshot.** Deletes stop at a plan until they are confirmed, and destructive edits take a snapshot first.
3. **Call Resolve.** The call runs on a single executor thread, the only place Resolve is ever called from.
4. **Read back.** The server reads the result back from Resolve and returns a receipt with `verified`. Long renders become jobs that the agent can wait on.


## Install

<details open>
<summary><b>Requirements</b></summary>

- **DaVinci Resolve Studio 21.1 or later.** In *Preferences → General → External scripting using*, choose **Local**.
- **The free edition** is reachable through the in-app bridge: run `davinci-mcp install --bridge`, then start the bridge from *Workspace → Scripts*.
- **Python 3.13+** and [uv](https://docs.astral.sh/uv/).
</details>

```bash
uv tool install davinci-mcp
```

```bash
davinci-mcp doctor
```

`doctor` checks that Resolve is reachable and reports its version, edition and channel.

<details>
<summary><b>Claude Code</b></summary>

```bash
davinci-mcp install --target claude-code
```

Or add it by hand:

```bash
claude mcp add davinci-mcp -- davinci-mcp serve
```
</details>

<details>
<summary><b>Claude Desktop</b></summary>

```bash
davinci-mcp install --target claude-desktop
```

This adds the server to `claude_desktop_config.json`. Restart Claude Desktop to load it. To do it by hand:

```json
{
  "mcpServers": {
    "davinci-mcp": { "command": "davinci-mcp", "args": ["serve"] }
  }
}
```

If Claude Desktop can't find the command, use its absolute path (`which davinci-mcp`).
</details>

<details>
<summary><b>Cursor</b></summary>

In `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "davinci-mcp": { "command": "davinci-mcp", "args": ["serve"] }
  }
}
```
</details>

<details>
<summary><b>VS Code</b></summary>

In `.vscode/mcp.json`:

```json
{
  "servers": {
    "davinci-mcp": { "type": "stdio", "command": "davinci-mcp", "args": ["serve"] }
  }
}
```
</details>

<details>
<summary><b>Any other MCP client</b></summary>

Run `davinci-mcp serve` over stdio. Use `davinci-mcp serve --transport http` for Streamable HTTP; it binds to loopback and needs a bearer token for any other host. The server also exposes a `davinci://status` resource.
</details>

## Configure

The optional file `~/.config/davinci-mcp/config.toml` holds the settings. Any key can be overridden with a `DAVINCI_MCP_<SECTION>__<KEY>` environment variable.

```toml
[safety]
allowed_roots = ["~/Movies", "~/Projects"]  # files outside these are refused (default: your home)
auto_snapshot = "destructive"               # "off" | "destructive" | "all"

[recipes]
allow_python = false                        # Python recipes run arbitrary code: opt in
```

## FAQ

<details>
<summary><b>The agent says it cannot reach Resolve</b></summary>

Check three things:
- Resolve is running;
- in Studio, *External scripting using* is set to **Local**;
- no dialog is open in Resolve, such as an update prompt or a project manager.

Then run `davinci-mcp doctor`.
</details>

<details>
<summary><b>Does it work with the free edition?</b></summary>

Yes, through the in-app bridge (`davinci-mcp install --bridge`). Resolve 21.1 removed external scripting from the free edition, so the bridge runs inside Resolve instead.
</details>

<details>
<summary><b>Can it delete my work?</b></summary>

Not without consent. Every delete returns a plan first and needs a matching `confirm_token`. Destructive edits snapshot the timeline, and every call is logged.
</details>

<details>
<summary><b>Will it touch files outside my projects?</b></summary>

Only inside `allowed_roots`. The server's own state, its configuration and your Claude configuration files are always refused, whatever you allow.
</details>

<details>
<summary><b>Why is the built-in Resolve MCP cheaper?</b></summary>

It runs one unchecked Python script per job. davinci-mcp makes typed, verified calls, so it uses a few more steps. `batch_run` and the one-call workflows keep that overhead small.
</details>

## Contributing

Issues and pull requests are welcome. [docs/domain-guide.md](https://github.com/Aedelon/davinci-mcp/blob/main/docs/domain-guide.md) shows how to add an action: a parameter model, a handler, a read-back check and a test. The rest is generated from there.

```bash
uv sync
uv run pytest
uv run ruff check . && uv run pyright
```

## Credits

- Demo footage: *Tears of Steel*, © Blender Foundation | [mango.blender.org](https://mango.blender.org), CC-BY 3.0.
- Demo music: *Five Armies* by Kevin MacLeod ([incompetech.com](https://incompetech.com)), CC-BY 4.0.

davinci-mcp is an independent project. It is not affiliated with, endorsed by or sponsored by Blackmagic Design. DaVinci Resolve is a trademark of Blackmagic Design Pty. Ltd.

## Support

davinci-mcp is free and open source. If it saves you time in the edit, you can buy me a coffee:

<a href="https://ko-fi.com/aedelon"><img src="https://ko-fi.com/img/githubbutton_sm.svg" alt="Support davinci-mcp on Ko-fi"></a>

Made by Delanoe Pirard (Aedelon) · Apache 2.0
