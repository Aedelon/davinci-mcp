# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Run the reference scenarios with a headless Claude agent against one or more MCP servers.

For every (server, scenario): the harness creates a throwaway project "zz_eval_<key>",
generates the media, runs ``claude -p`` with ONLY that MCP server, grades the final
Resolve state with independent checks, then deletes the project. Results go to
``<work>/results.json`` and a Markdown table on stdout.

Usage:
    uv run python eval/run_eval.py --work DIR [--servers davinci-mcp davinci-resolve]
        [--scenarios reel conform grade fusion] [--model sonnet] [--timeout 1500]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from scenarios import SCENARIOS, Scenario

SCRIPT_API = Path(
    "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
)
sys.path.append(str(SCRIPT_API / "Modules"))
import DaVinciResolveScript as dvr  # noqa: E402

SYSTEM_RULES = (
    "You operate DaVinci Resolve only through the MCP tools you have. Work ONLY inside the "
    "currently open project; never open, create, rename or delete any other project and "
    "never touch other projects or databases. When a tool asks for confirmation of a "
    "destructive step that is part of the task, you may confirm it. When you are done, "
    "reply with a one-line summary."
)


def server_configs(work: Path) -> dict[str, dict[str, Any]]:
    claude = json.loads((Path.home() / ".claude.json").read_text())
    existing = claude["mcpServers"]["davinci-resolve"]
    ours_exe = Path.home() / ".local/share/venvs/davinci-mcp/bin/davinci-mcp"
    return {
        "davinci-mcp": {
            "command": str(ours_exe),
            "args": ["serve"],
            "env": {
                "DAVINCI_MCP_SAFETY__ALLOWED_ROOTS": f"[{work}]",
                "XDG_STATE_HOME": str(work / "state"),
            },
        },
        "davinci-resolve": existing,
    }


def resolve_handle() -> Any:
    r = dvr.scriptapp("Resolve")
    if r is None:
        raise SystemExit("Resolve Studio is not reachable (external scripting must be Local).")
    return r


def fresh_project(r: Any, name: str) -> Any:
    pm = r.GetProjectManager()
    current = pm.GetCurrentProject()
    if current is not None and current.GetName() not in {"Untitled Project", name}:
        pm.SaveProject()
    if name in (pm.GetProjectListInCurrentFolder() or []):
        pm.CloseProject(pm.GetCurrentProject())
        pm.DeleteProject(name)
    project = pm.CreateProject(name)
    if project is None:
        raise SystemExit(f"Could not create {name} (a modal may be open in Resolve).")
    r.OpenPage("edit")
    return project


def delete_project(r: Any, name: str, home: str | None) -> bool:
    """DeleteProject is flaky on a recently-current project: switch away, then retry."""
    pm = r.GetProjectManager()
    for _ in range(4):
        if name not in (pm.GetProjectListInCurrentFolder() or []):
            return True
        current = pm.GetCurrentProject()
        if current is not None and current.GetName() == name:
            pm.CloseProject(current)
        if home and home != name:
            pm.LoadProject(home)
        if pm.DeleteProject(name):
            return True
        time.sleep(2)
    return False


def run_agent(
    server: str, config: dict[str, Any], prompt: str, model: str, timeout: int, log: Path
) -> dict[str, Any]:
    cfg = log.with_suffix(".mcp.json")
    cfg.write_text(json.dumps({"mcpServers": {server: config}}))
    cmd = [
        "claude",
        "-p",
        prompt,
        "--mcp-config",
        str(cfg),
        "--strict-mcp-config",
        "--allowedTools",
        f"mcp__{server}",
        "--disallowedTools",
        "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch,Agent",
        "--append-system-prompt",
        SYSTEM_RULES,
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        model,
        "--no-session-persistence",
    ]
    started = time.monotonic()
    with log.open("w") as fh:
        try:
            subprocess.run(
                cmd,
                stdout=fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
                cwd=log.parent,
            )
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
    wall = time.monotonic() - started
    tool_calls = errors = 0
    result: dict[str, Any] = {}
    for line in log.read_text().splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    tool_calls += 1
        if ev.get("type") == "user":
            for block in ev.get("message", {}).get("content", []) or []:
                if isinstance(block, dict) and block.get("is_error"):
                    errors += 1
        if ev.get("type") == "result":
            result = ev
    usage = result.get("usage", {})
    return {
        "wall_s": round(wall, 1),
        "timed_out": timed_out,
        "turns": result.get("num_turns"),
        "tool_calls": tool_calls,
        "tool_errors": errors,
        "cost_usd": result.get("total_cost_usd"),
        "input_tokens": (usage.get("input_tokens") or 0)
        + (usage.get("cache_read_input_tokens") or 0)
        + (usage.get("cache_creation_input_tokens") or 0),
        "output_tokens": usage.get("output_tokens"),
        "final": str(result.get("result", ""))[:300],
    }


def run_one(
    r: Any, server: str, config: dict[str, Any], sc: Scenario, args: argparse.Namespace
) -> dict[str, Any]:
    work = args.work / f"{server}__{sc.key}"
    shutil.rmtree(work, ignore_errors=True)
    (work / "out").mkdir(parents=True)
    pm = r.GetProjectManager()
    before = set(pm.GetProjectListInCurrentFolder() or [])
    project = fresh_project(r, f"zz_eval_{sc.key}")
    sc.setup(r, project, work)
    pm.SaveProject()
    config = json.loads(json.dumps(config).replace(str(args.work), str(work)))
    metrics = run_agent(
        server, config, sc.prompt(work), args.model, args.timeout, work / "agent.jsonl"
    )
    r = resolve_handle()
    pm = r.GetProjectManager()
    project = pm.GetCurrentProject()
    if project is None or project.GetName() != f"zz_eval_{sc.key}":
        project = pm.LoadProject(f"zz_eval_{sc.key}")
    checks = sc.check(r, project, work) if project is not None else {"project_lost": False}
    after = set(pm.GetProjectListInCurrentFolder() or [])
    deleted = delete_project(r, f"zz_eval_{sc.key}", args.home)
    return {
        "server": server,
        "scenario": sc.key,
        "checks": checks,
        "score": sum(checks.values()) / max(1, len(checks)),
        "other_projects_changed": sorted((before ^ after) - {f"zz_eval_{sc.key}"}),
        "eval_project_deleted": deleted,
        **metrics,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--servers", nargs="+", default=["davinci-mcp", "davinci-resolve"])
    parser.add_argument("--scenarios", nargs="+", default=[s.key for s in SCENARIOS])
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--timeout", type=int, default=1500)
    args = parser.parse_args()
    args.work = args.work.resolve()
    args.work.mkdir(parents=True, exist_ok=True)
    configs = server_configs(args.work)
    r = resolve_handle()
    pm = r.GetProjectManager()
    home = pm.GetCurrentProject().GetName() if pm.GetCurrentProject() else None
    args.home = home
    results_path = args.work / "results.json"
    results: list[dict[str, Any]] = (
        json.loads(results_path.read_text()) if results_path.exists() else []
    )
    done = {(x["server"], x["scenario"]) for x in results}
    try:
        for sc in [s for s in SCENARIOS if s.key in args.scenarios]:
            for server in args.servers:
                if (server, sc.key) in done:
                    continue
                print(f"== {server} / {sc.key}", flush=True)
                res = run_one(resolve_handle(), server, configs[server], sc, args)
                results.append(res)
                results_path.write_text(json.dumps(results, indent=2))
                print(json.dumps(res, indent=1), flush=True)
    finally:
        r = resolve_handle()
        pm = r.GetProjectManager()
        if home and home != "Untitled Project":
            pm.LoadProject(home)
    print(
        "\n| scenario | server | score | checks | tool calls | errors | wall s | in tok | out tok | $ |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|")
    for x in results:
        ok = ", ".join(k for k, v in x["checks"].items() if not v) or "all"
        print(
            f"| {x['scenario']} | {x['server']} | {x['score']:.0%} | missing: {ok} | "
            f"{x['tool_calls']} | {x['tool_errors']} | {x['wall_s']} | {x['input_tokens']} | "
            f"{x['output_tokens']} | {x['cost_usd']} |"
        )
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    raise SystemExit(main())
