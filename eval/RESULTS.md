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


## Benchmark v3: 5 runs per cell, 3 servers (2026-10-26)

Same harness, with `--runs 5`, Sonnet everywhere and the servers interleaved inside each run. The contenders: davinci-mcp (with the v2 fixes), the community MCP (samuelgursky v4.14.4) and Blackmagic's built-in **ResolveMCP**, which works by docs search plus a sandboxed `run_script`. Values are **medians over 5 runs**; "success" means every check passed.

| Scenario | Server | Success | Calls | Errors | Wall (s) | Input tok | Cost ($) |
|---|---|---|---|---|---|---|---|
| Reel | davinci-mcp | 5/5 | 15 | 0 | 55.5 | 1 122 k | 0.27 |
| Reel | community | 5/5 | 18 | 0 | 49.6 | 952 k | 0.33 |
| Reel | native | 5/5 | 7 | 0 | 38.8 | 504 k | 0.20 |
| Conform | davinci-mcp | 5/5 | 24 | 1 | 81.4 | 1 354 k | 0.59 |
| Conform | community | 5/5 | 28 | 0 | 89.5 | 1 255 k | 0.41 |
| Conform | native | **2/5** | 5 | 0 | 36.2 | 389 k | 0.15 |
| Grade | davinci-mcp | 5/5 | 14 | 0 | 36.0 | 599 k | 0.18 |
| Grade | community | 5/5 | 32 | 0 | 66.1 | 1 126 k | 0.41 |
| Grade | native | 5/5 | 10 | 1 | 53.6 | 583 k | 0.19 |
| Fusion | davinci-mcp | 5/5 | 11 | 0 | 36.0 | 585 k | 0.16 |
| Fusion | community | 5/5 | 15 | 0 | 42.2 | 473 k | 0.21 |
| Fusion | native | 5/5 | 5 | 0 | 32.9 | 377 k | 0.13 |

**Totals (sum of the medians over the 4 scenarios):**

| Server | Success | Calls | Wall (s) | Input tok | Cost ($) |
|---|---|---|---|---|---|
| davinci-mcp | **100 %** (20/20) | 64 | 209 | 3.66 M | 1.20 |
| community | 100 % (20/20) | 93 | 247 | 3.81 M | 1.36 |
| native ResolveMCP | 85 % (17/20) | **27** | **162** | **1.85 M** | **0.66** |

### Reading

- **Reliability.** davinci-mcp and the community server pass every run. The native server writes one Python script per task: it is the cheapest and the fastest, but it failed the conform scenario in 3 runs out of 5. The EDL imported, but its events were not linked to the media, and nothing in that approach detects or reports it.
- **Against the community server**, davinci-mcp uses 31 % fewer calls, 15 % less time, 4 % fewer input tokens and 12 % less money. It is cheaper on grade and Fusion, and costlier on conform, where linking EDL events takes several attempts.
- **Against the native server**, davinci-mcp costs about twice the tokens: typed tools mean more round trips, where a script does everything in one call. The trade is reliability and verifiable receipts against raw cost.

### Incidents and exclusions

- **10 of the first 20 davinci-mcp runs never started.** Claude Code's 30 s MCP connect timeout expired because the repository lived in iCloud Drive: evicted ("dataless") source files were re-downloaded at each cold start, and `PYTHONDONTWRITEBYTECODE=1` prevented any bytecode cache.
  - Fix: the repo now lives outside synced folders, with a local venv precompiled through `uv sync --compile-bytecode` (cold import ≈ 0.8 s).
  - The 10 runs were re-run (same run indices) and all of them started. The pre-rerun rows are kept in `results/2026-10-26_bench5_before_rerun.json`.
- The harness now reads the MCP status from the agent's init event, and retries then excludes any run whose server did not start.
- One run detected another Resolve driver switching projects. It was retried automatically and excluded from the medians.


## Benchmark v4: composites and recipes (2026-10-26)

davinci-mcp gained five single-call `workflow_write` actions (`conform_from_edl`, `deliver`, `build_vertical_reel`, `apply_look`, `title_overlay`) and the recipe engine. Same prompts and harness as v3, 5 runs per cell. Nothing tells the agents to use the new tools. The other two servers keep their v3 results.

| Scenario | davinci-mcp v3 | **davinci-mcp v4** | community | native |
|---|---|---|---|---|
| Reel | 15 calls · $0.27 | 14 · $0.28 | 18 · $0.33 | 7 · $0.20 |
| Conform | 24 · $0.59 | **8 · $0.17** | 28 · $0.41 | 5 · $0.15 (2/5 pass) |
| Grade | 14 · $0.18 | 14 · $0.18 | 32 · $0.41 | 10 · $0.19 |
| Fusion | 11 · $0.16 | 12 · $0.18 | 15 · $0.21 | 5 · $0.13 |

**Totals (sum of the medians):**

| Server | Success | Calls | Wall (s) | Cost ($) |
|---|---|---|---|---|
| **davinci-mcp v4** | **100 %** (20/20) | 48 | **139** | 0.81 |
| davinci-mcp v3 | 100 % | 64 | 209 | 1.20 |
| community | 100 % | 93 | 247 | 1.36 |
| native ResolveMCP | 85 % | **27** | 162 | **0.66** |

### Reading

- davinci-mcp is now the **fastest** of the three, at 100 % success, and within $0.15 of the native server's cost.
- **Adoption is the limit, not capability.** Agents used `conform_from_edl` + `deliver` in nearly every conform run (−67 % calls, −71 % cost on that scenario). They called `build_vertical_reel` and `title_overlay` only once in 5 runs each, and `apply_look` never: they kept composing primitives. All five composites passed the scenario checks when called directly on live Resolve. Next lever: make them discoverable (server instructions, tool descriptions, `alwaysLoad`).
- **Bias caveat.** The composites were designed after seeing these four scenarios. A fair test of the general claim needs new scenarios the composites were not shaped for.
- Live finding: `deliver` initially rejected the codec name "ProRes 422 HQ" (Resolve's id is `ProRes422HQ`). Codec lookup now ignores case, spaces and dots.


## Benchmark v5: four scenarios written blind (2026-10-26)

The v1–v4 scenarios were known when the composites were designed. For v5, an agent that could read only the scenario format and Blackmagic's scripting docs, never the server code, wrote four new jobs: [scenarios_v2.py](scenarios_v2.py) (`run_eval.py --suite v2`).

| Scenario | Task |
|---|---|
| `dailies` | bins and sub-bins, metadata from a camera-log CSV, clip colors, a clip marker |
| `promo` | 25 fps timeline at 10:00:00:00, music alone on a renamed A2, −8 dB, 50-frame fade out, marker |
| `look` | local version "warm" on every clip with a CDL, still album, still exported as JPEG |
| `spot_qc` | duplicate a timeline, markers, FCPXML + OTIO exports, range render in ProRes 422 LT |

None of them maps directly onto a composite. Each was validated live: a raw-API reference solution passes every check, and the untouched setup fails them.

Results are medians over 3 runs per cell. The run was stopped after 3 complete passes, once the gaps were stable. davinci-mcp also took the blind-written v2 suite, unchanged.

| Server | Success | Calls | Errors | Wall (s) | Input tokens | Cost ($) |
|---|---|---|---|---|---|---|
| davinci-mcp v5 | **100 %** (12/12) | 89 | 8 | 228 | 4.49 M | 1.06 |
| community | 75 % (9/12) | 93 | 0 | 266 | 3.32 M | 1.26 |
| native ResolveMCP | **100 %** (12/12) | **26** | 3 | **163** | **1.57 M** | **0.63** |

### Reading

- **Reliability.** The community server failed `promo` in all 3 runs: the music volume never reached −8 dB.
- **The native server wins on cost.** It writes one Python script through `run_script_unsafe` (2 calls for the whole `look` job), with no validation, read-back, audit, snapshot or delete confirmation.
- **davinci-mcp lost calls to near-miss parameters.** In the transcripts it made 8 errors where the community server made none. Two were bugs:
  - `set_cdl` sent numbers where `SetCDL` wants strings;
  - `set_settings` compared "25" with 25.0 as text.

  The others were almost-right shapes: `"album": "Stills 2"`, `fades: {"fade_out": 50}`, `properties: {"Volume": -8}`.
- **Composites were not used.** On jobs they do not cover, adding them makes no difference.

### Incidents

- Resolve 21.1.1 fails ProRes 422 LT renders downscaled to 720p on the GPU (repeatable, at 7–14 %). `spot_qc` therefore renders 1080p.
- A fresh project defaults to 24 fps; the v2 setup sets 25 fps before building timelines.
- After 21 runs, two progress windows that no benchmark run had opened ("Create Subtitles", "AI Smart Reframe") blocked project creation. The run was resumed from where it stopped.

## Benchmark v6: input repair and batch_run (2026-10-26)

Changes, all driven by the v5 transcripts:
- the `set_cdl` and `set_settings` fixes, both checked live;
- **near-miss repair**: when strict validation fails, the pipeline rewrites the call along the model's own fields and reports each rewrite in `notes`. It turns a bare string into a selector, matches field names loosely (`fade_out` → `FadeOut`, `Volume` → `AudioVolume`), and wraps a value one level down;
- `INVALID_PARAMS` hints show the field names of nested objects;
- render errors name the rejected settings;
- **`batch_run`**, always loaded: ad-hoc steps chained server-side through the recipe engine, with `{{ steps.<id>… }}` templates and one confirmation when a step deletes.

The v2 suite was run on davinci-mcp only, 5 runs per scenario. The other servers keep their v5 numbers.

| Scenario | v5 calls · errors · $ | **v6 calls · errors · $** |
|---|---|---|
| `dailies` | 20 · 1 · 0.21 | **11** · 1 · 0.19 |
| `promo` | 22 · 3 · 0.27 | **17** · **0** · 0.24 |
| `look` | 28 · 3 · 0.29 | **19** · **0** · 0.27 |
| `spot_qc` | 19 · 1 · 0.28 | 20 · 1 · 0.33 |
| **Total** | 89 · 8 · 1.06 | **67 · 2 · 1.02** |

The v6 total ran in 209 s with 4.04 M input tokens; the success rate stayed at 100 % (20/20).

### Reading

- **Calls fell by 25 % and call errors by 75 %, with success unchanged.** Repairs fired in the `look` and `promo` runs.
- **Adoption of `batch_run` is uneven.** Agents used it in every `dailies` run and in `look`, never in `promo` or `spot_qc`.
- **Cost barely moved (−3 %).** Two reasons appear in the transcripts:
  - the `batch_run` reply repeats every step's full receipt;
  - agents called `describe_action` 48 times in 20 runs (23 in `look`, 18 in `spot_qc`).

  The next levers are a compact `batch_run` reply and descriptions that make `describe_action` unnecessary for the common actions.
- The gap with the native server, at 67 calls against 26 and $1.02 against $0.63, is the price of per-step validation, verification and audit.


## Benchmark v7: deferred tools, compact replies, fixed harness (2026-10-26)

**What the v6 transcripts showed.** Most of the cost came from context resent on every turn, not from tool replies (about 5k tokens of replies per run). davinci-mcp started every run at 88.6k first-turn tokens, against 62k for the native server and 40k for the community server, and its agents never called `ToolSearch`. Two causes, both measured with `claude -p`:

| Cause | Fix |
|---|---|
| One tool marked `anthropic/alwaysLoad` makes Claude Code load the whole server up front instead of deferring its tools (101.7k first-turn tokens with it, 48.8k without). | No tool is marked `alwaysLoad` any more; a test guards it. |
| The harness's `--disallowedTools` list (7 built-ins) also disabled deferral, but not for every server: ours was loaded in full (89k), the community server deferred (40k). The v1–v6 cost comparisons were biased against davinci-mcp. | Agents now run with `--setting-sources local --permission-mode default --permission-prompts none --disallowedTools Bash`. The user's Bash/Write allow rules are ignored, anything that would ask for approval is denied, and Bash is denied outright, since default mode auto-approves read-only commands. Only Read, Glob, Grep and ToolSearch remain. A run that still succeeds with any other tool is flagged and excluded; none was. |

With the fixed harness, first-turn context is 32k for davinci-mcp, 32k for the community server and 58k for the native server.

**Also in v7:**
- `batch_run` and `recipe_run` return compact step outputs (status, verified, result, plus warnings, notes and job) with no duplicated YAML outputs; `verbose=true` keeps the full receipts;
- `describe_action` accepts `actions=['tool.action', …]`, several actions in one call;
- the server instructions say to load every needed tool in one search and to send known sequences as one `batch_run`.

All three servers ran the v2 suite, 5 runs per cell (60 runs). The harness change makes every server cheaper, so **v7 numbers compare with each other only, not with v5 or v6**.

| Scenario | davinci-mcp | community | native |
|---|---|---|---|
| `dailies` | 8 calls · $0.13 | 20 · $0.16 | 3 · $0.07 |
| `promo` | 6 · $0.19 | 20 · $0.27 (0/5 pass) | 6 · $0.11 |
| `look` | 11 · $0.16 | 36 · $0.35 (3/5 pass) | 4 · $0.09 |
| `spot_qc` | 11 · $0.20 | 14 · $0.21 | 4 · $0.09 |

**Totals (sum of the medians):**

| Server | Success | Calls | Errors | Wall (s) | Input tokens | Cost ($) |
|---|---|---|---|---|---|---|
| **davinci-mcp v7** | **100 %** (20/20) | 36 | 1 | 132 | 1.35 M | 0.68 |
| community | 65 % (13/20) | 90 | 1 | 184 | 2.83 M | 0.99 |
| native ResolveMCP | **100 %** (20/20) | **17** | 0 | **86** | **1.15 M** | **0.35** |

### Reading

- **Against the community server**, davinci-mcp uses 60 % fewer calls, 28 % less time, 52 % fewer input tokens and 31 % less money, and it never fails.
- **Where the community server failed:**
  - `promo` in all 5 runs: the music volume never reached −8 dB;
  - `look` in 2 runs: the still album or JPEG export was missing;
  - it also wrote stills to `~/Documents/resolve-stills`, outside the job's folder.
- **Calls fell from 67 (v6) to 36** at the same harness level of strictness. On `promo` the agent did nearly everything in two `batch_run` calls; `describe_action` was called in groups.
- **The native server is still about 2× cheaper.** It runs one Python script per job through `run_script_unsafe`: no validation, no read-back, no audit, no snapshot, no delete confirmation. A davinci-mcp turn is lighter (32k against 58k of context), but there are more of them.
- **Remaining levers, from the v7 transcripts:**
  - `describe_action` replies average 17k chars of raw JSON Schema (titles, `anyOf` with null, defaults, `$defs`, result schemas) and stay in context for the rest of the run;
  - `render_read.list_render_formats` replies are about 15k chars, called 2–3 times on `spot_qc` only to find a codec id;
  - `spot_qc` never uses `batch_run`, with 5 separate `timeline_write` calls.
