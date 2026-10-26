# Recipes

A recipe runs several actions **server-side in one MCP call**. Every step goes through the normal pipeline:
- parameter validation;
- read-back verification;
- snapshots per policy;
- audit log.

The run stops at the first failed or unverified step and reports every step's outcome.

```text
recipe_list()                                  → built-in and user recipes, with params schemas
recipe_run(name, params, dry_run?, confirm_token?)
```

## YAML recipes (declarative, always enabled)

Put `*.yaml` files in `~/.config/davinci-mcp/recipes/`. The folder list is configurable with `recipes.dirs` in `config.toml`.

```yaml
name: assemble_timeline          # identifier, unique
description: Import media files and create a timeline with them, in order.
destructive: false               # true → recipe_run first returns a plan + confirm_token
params:
  clips: {type: array, required: true, description: Absolute media paths, in order.}
  name:  {type: string, default: Assembly}
steps:
  - id: import                   # later steps read its output as steps.import
    tool: media_write
    op: {action: import_media, items: "{{ params.clips }}"}
  - id: timeline
    tool: timeline_write
    snapshot: false              # optional, like the tool's snapshot flag
    op:
      action: create_timeline_from_clips
      name: "{{ params.name }}"
      clips: "{{ steps.import.receipt.result.imported[*].id }}"
```

**Templates** only *read* values; nothing is evaluated:
- `"{{ path }}"` alone keeps the value's type (list, number…);
- inside a longer string it is interpolated as text;
- a path starts at `params` or `steps`, uses dotted keys, `[n]` indexes and `[*]` projections.

Each step's output is the tool's structured result:
- `steps.<id>.receipt.result.*` for writes;
- `steps.<id>.result.*` for reads;
- `steps.<id>.job` for jobs. Jobs are waited for unless `wait: false`.

**Consent.** A destructive recipe is confirmed once, as a whole: the plan lists every step, then its inner destructive steps run without asking again. A YAML recipe counts as destructive when it declares `destructive: true` **or** contains any `*_delete` step, so it can never skip confirmation. A Python recipe must declare `destructive=True` to run destructive steps; otherwise those steps fail with `CONFIRMATION_REQUIRED`.

## Python recipes (opt-in)

Python recipes are arbitrary code. They are loaded only when `recipes.allow_python = true` is set in `config.toml`.

```python
# ~/.config/davinci-mcp/recipes/tag_beats.py
from pydantic import BaseModel
from davinci_mcp.recipes.model import PythonRecipe


class Params(BaseModel):
    bpm: float
    bars: int = 8


async def run(ctx, p: Params) -> dict:
    info = await ctx.call("timeline_read", {"action": "timeline_info"})
    fps = float(info["result"]["frame_rate"] or 25)
    frames = [round(i * 60 / p.bpm * fps) for i in range(p.bars * 4)]
    await ctx.call(
        "timeline_write",
        {"action": "add_markers", "markers": [{"frame": f, "color": "Blue"} for f in frames]},
    )
    return {"markers": len(frames)}


RECIPE = PythonRecipe(
    name="tag_beats", description="Beat markers from a BPM.", params=Params, run=run
)
```

`ctx.call(tool, op, snapshot=None, wait=True)` returns the step's structured output. It raises (and the run reports `failed`) when the step errors or is not verified.

## Built-in workflow actions

For the common jobs there are typed, single-call actions in `workflow_write`:
- `conform_from_edl`: import, EDL timeline, Start TC fix, linked check;
- `deliver`: several renders, real output files verified;
- `build_vertical_reel`;
- `apply_look`;
- `title_overlay`.

Recipes can chain them.
