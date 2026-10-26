# Evaluation: davinci-mcp vs the existing community MCP

> 2026-10-26, DaVinci Resolve Studio 21.1.1.10 (macOS), headless `claude -p` agents,
> model **Sonnet** on both sides, one MCP server per run (`--strict-mcp-config`).
> Harness: [run_eval.py](run_eval.py), scenarios and independent checks: [scenarios.py](scenarios.py).
> Baseline: samuelgursky/davinci-resolve-mcp v4.14.4 (37 compound tools).

## Protocol

For every (server, scenario), the harness:
1. creates a throwaway project `zz_eval_<scenario>`;
2. generates the media (ffmpeg, plus macOS `say` for speech);
3. runs one agent with the same prompt, restricted to the MCP tools under test;
4. grades the final Resolve state through the scripting API, **never through the server under test**;
5. deletes the project.

No other project was changed in any run, and every throwaway project was deleted.

| Scenario | Graded criteria |
|---|---|
| Reel vertical sous-titré | timeline 1080×1920, 3 clips in order, subtitles from audio, H.264 render at 1080×1920 |
| Conformation / livraison | EDL import as timeline, 4 events, linked to media, ProRes 422 HQ 1080p master, H.264 720p proxy |
| Étalonnage de série | LUT on node 1 of 4 clips, color group with 4 clips, still in the gallery |
| Motion design Fusion | Fusion comp, Text+ "HITSCROLL", animated Size, Merge into the output |

## Results

| Scenario | Server | Score | Tool calls | Tool errors | Wall (s) | Input tokens | Cost ($) |
|---|---|---|---|---|---|---|---|
| Reel | existing | 100 % | 20 | 1 | 55.6 | 998 k | 0.42 |
| Reel | davinci-mcp v1 | 100 % | 26 | 3 | 126.4 | 2 069 k | 0.66 |
| Reel | **davinci-mcp v2** | **100 %** | **16** | **0** | **54.9** | 1 174 k | 0.58 |
| Conform | existing | 100 % | 25 | 0 | 89.5 | 1 092 k | 0.36 |
| Conform | davinci-mcp v1 | 80 % | 23 | 1 | 99.6 | 1 639 k | 0.38 |
| Conform | **davinci-mcp v2** | **100 %** | **22** | **0** | **72.2** | 1 499 k | 0.37 |
| Grade | existing | 100 % | 26 | 1 | 56.5 | 923 k | 0.32 |
| Grade | davinci-mcp v1 | 50 % | 14 | 4 | 54.3 | 1 012 k | 0.30 |
| Grade | **davinci-mcp v2** | **100 %** | **14** | **0** | **49.2** | **593 k** | **0.18** |
| Fusion | existing | 100 % | 15 | 0 | 38.5 | 473 k | 0.21 |
| Fusion | davinci-mcp v1 | 100 % | 20 | 3 | 46.5 | 1 208 k | 0.26 |
| Fusion | **davinci-mcp v2** | **100 %** | **12** | **0** | 49.8 | 578 k | **0.16** |

**Totals, davinci-mcp v2 vs the existing server:**
- success 4/4 vs 4/4;
- tool calls **64 vs 86 (−26 %)**;
- tool errors **0 vs 2**;
- wall time 226 s vs 240 s;
- cost $1.29 vs $1.31;
- input tokens 3.84 M vs 3.49 M (**+10 %**).

## What v1 → v2 fixed (all found by these runs)

- **Render outputs.** The job now reports the files really produced and fails when none exists. The "H.264 Master" preset writes `.mov`, so agents looking for `.mp4` concluded the render had failed. `render_timeline` also gained a `format`/`codec` override.
- **LUTs.** `SetLUT` rejects absolute paths outside Resolve's LUT root. LUT files are now installed into `LUT/MCP` and the list is refreshed (LUT file types only, size-capped).
- **EDL import.** It now reports linked/offline events with the Start TC hint. The baseline server already did this; ours silently produced offline events.
- **References.** A bare id string is accepted (`"item": "<uuid>"`), as are plain import paths.
- **Fusion.** Inputs resolve by exact id before display name, because Text+ has two inputs displayed as "Size". `GetKeyFrames()` returns `{index: time}`. `add_tool` no longer fails when the name is already right.

## Caveats

- **One run per cell.** Agent runs vary; treat differences under ~20 % as noise. A credible benchmark needs ≥ 5 runs per cell and a median.
- The baseline was run once, before our fixes, so it is not equally tuned to these prompts.
- The agents inherit the user's Claude Code environment (hooks, CLAUDE.md, skills) on both sides. One davinci-mcp v1 run loaded a skill, which inflated its tokens.
- Input tokens are dominated by per-turn context, mostly cache reads. The baseline's tools are deferred behind ToolSearch, while ours (~13 k tokens) are loaded upfront; this explains part of the token gap on long scenarios.
- Not measured: the native Blackmagic `ResolveMCP` (run-script approach).
