# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Run the reference scenarios with a headless Claude agent against one or more MCP servers.

For every (run, scenario, server): the harness creates a throwaway project
"zz_eval_<key>", generates the media, runs ``claude -p`` with ONLY that MCP server,
grades the final Resolve state with independent checks, then deletes the project.
Servers are interleaved inside each run so drift (Resolve state, API load) hits all of
them alike. Raw rows go to ``<work>/results.json``, medians to ``<work>/summary.json``.

Usage:
    uv run python eval/run_eval.py --work DIR [--runs 5]
        [--servers davinci-mcp davinci-resolve resolve-native]
        [--scenarios reel conform grade fusion] [--model sonnet] [--timeout 1500]
    uv run python eval/run_eval.py --work DIR --summarize-only
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

NATIVE_MCP = Path(
    "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Applications/ResolveMCP"
)
METRICS = ("tool_calls", "tool_errors", "wall_s", "input_tokens", "output_tokens", "cost_usd")

ALLOWED_BUILTINS = frozenset({"Read", "Glob", "Grep", "ToolSearch"})

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
    ours_exe = Path(__file__).resolve().parents[1] / ".venv/bin/davinci-mcp"
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
        # Blackmagic's built-in MCP (Studio 21.1+): docs search + sandboxed run_script.
        "resolve-native": {"command": str(NATIVE_MCP), "args": []},
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


def _results(ev: dict[str, Any], names: dict[str, str], server: str) -> tuple[int, int]:
    """(errored tool results, successful results of tools the agent must not use)."""
    failed = outside = 0
    for block in ev.get("message", {}).get("content", []) or []:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        if block.get("is_error"):
            failed += 1
            continue
        name = names.get(str(block.get("tool_use_id")), "")
        if not name.startswith(f"mcp__{server}__") and name not in ALLOWED_BUILTINS:
            outside += 1
    return failed, outside


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
        # Isolation without --disallowedTools: that list stops Claude Code from deferring MCP
        # tools behind tool search (89k vs 32k first-turn tokens). Ignore the user's settings
        # (their Bash/Write allow rules), and deny anything that would ask for permission.
        "--setting-sources",
        "local",
        "--permission-mode",
        "default",
        "--permission-prompts",
        "none",
        # Default mode still auto-approves read-only shell commands (ls...). Denying Bash
        # alone keeps tool deferral (measured); the full disallow list did not.
        "--disallowedTools",
        "Bash",
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
    started = False
    names: dict[str, str] = {}
    forbidden = 0  # successful calls to tools other than this server's, Read/Glob/Grep, search
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
                    names[block.get("id", "")] = str(block.get("name", ""))
        if ev.get("type") == "user":
            failed, outside = _results(ev, names, server)
            errors += failed
            forbidden += outside
        if ev.get("type") == "result":
            result = ev
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            statuses = {m.get("name"): m.get("status") for m in ev.get("mcp_servers", [])}
            started = statuses.get(server) == "connected"
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
        "server_started": started,
        "forbidden_tool_calls": forbidden,
    }


def run_one(
    r: Any, server: str, config: dict[str, Any], sc: Scenario, args: argparse.Namespace, run: int
) -> dict[str, Any]:
    work = args.work / f"r{run}__{server}__{sc.key}"
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
    # Another Resolve driver (an app or a test suite) switching projects mid-run makes
    # the run meaningless: flag it so main() retries it.
    interference = project is None or not project.GetName().startswith("zz_eval_")
    if project is None or project.GetName() != f"zz_eval_{sc.key}":
        project = pm.LoadProject(f"zz_eval_{sc.key}")
    checks = sc.check(r, project, work) if project is not None else {"project_lost": False}
    after = set(pm.GetProjectListInCurrentFolder() or [])
    deleted = delete_project(r, f"zz_eval_{sc.key}", args.home)
    return {
        "run": run,
        "server": server,
        "scenario": sc.key,
        "checks": checks,
        "score": sum(checks.values()) / max(1, len(checks)),
        "other_projects_changed": sorted((before ^ after) - {f"zz_eval_{sc.key}"}),
        "eval_project_deleted": deleted,
        "interference": interference,
        **metrics,
    }


def _median(values: list[float]) -> float | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Per (scenario, server): success rate, median score and median metrics."""
    cells: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in results:
        if row.get("interference") or row.get("server_started") is False:
            continue  # another driver switched projects / the server never started
        if row.get("forbidden_tool_calls"):
            continue  # the agent bypassed the server under test (shell, file writes...)
        cells.setdefault((row["scenario"], row["server"]), []).append(row)
    out: dict[str, Any] = {"cells": [], "servers": {}}
    for (scenario, server), rows in sorted(cells.items()):
        cell: dict[str, Any] = {
            "scenario": scenario,
            "server": server,
            "runs": len(rows),
            "success_rate": sum(1 for r in rows if r["score"] == 1) / len(rows),
            "median_score": _median([r["score"] for r in rows]),
            "timeouts": sum(1 for r in rows if r.get("timed_out")),
            "foreign_project_changes": sum(1 for r in rows if r.get("other_projects_changed")),
        }
        for m in METRICS:
            cell[f"median_{m}"] = _median([r.get(m) for r in rows])
        out["cells"].append(cell)
    for server in sorted({c["server"] for c in out["cells"]}):
        mine = [c for c in out["cells"] if c["server"] == server]
        out["servers"][server] = {
            "success_rate": sum(c["success_rate"] * c["runs"] for c in mine)
            / sum(c["runs"] for c in mine),
            **{
                f"sum_median_{m}": round(sum(c[f"median_{m}"] or 0 for c in mine), 3)
                for m in METRICS
            },
        }
    return out


def print_summary(summary: dict[str, Any]) -> None:
    print(
        "\n| scenario | server | runs | success | median score | calls | errors | wall s | in tok | $ |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|")
    for c in summary["cells"]:
        print(
            f"| {c['scenario']} | {c['server']} | {c['runs']} | {c['success_rate']:.0%} | "
            f"{c['median_score']:.0%} | {c['median_tool_calls']} | {c['median_tool_errors']} | "
            f"{c['median_wall_s']} | {c['median_input_tokens']} | {c['median_cost_usd']} |"
        )
    print("\n| server | success | Σ median calls | Σ errors | Σ wall s | Σ in tok | Σ $ |")
    print("|---|---|---|---|---|---|---|")
    for server, t in summary["servers"].items():
        print(
            f"| {server} | {t['success_rate']:.0%} | {t['sum_median_tool_calls']} | "
            f"{t['sum_median_tool_errors']} | {t['sum_median_wall_s']} | "
            f"{t['sum_median_input_tokens']} | {t['sum_median_cost_usd']} |"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument(
        "--servers", nargs="+", default=["davinci-mcp", "davinci-resolve", "resolve-native"]
    )
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--summarize-only", action="store_true")
    parser.add_argument(
        "--suite",
        choices=["v1", "v2", "scale"],
        default="v1",
        help="v2: scenarios written blind; scale: 150-clip project",
    )
    parser.add_argument("--scenarios", nargs="+", default=None)
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--timeout", type=int, default=1500)
    args = parser.parse_args()
    suite: tuple[Scenario, ...] = SCENARIOS
    if args.suite == "v2":
        from scenarios_v2 import SCENARIOS as V2  # noqa: PLC0415 - optional suite

        suite = V2
    elif args.suite == "scale":
        from scenarios_scale import SCENARIOS as SCALE  # noqa: PLC0415 - optional suite

        suite = SCALE
    args.scenarios = args.scenarios or [s.key for s in suite]
    args.work = args.work.resolve()
    args.work.mkdir(parents=True, exist_ok=True)
    results_path = args.work / "results.json"
    if args.summarize_only:
        summary = summarize(json.loads(results_path.read_text()))
        (args.work / "summary.json").write_text(json.dumps(summary, indent=2))
        print_summary(summary)
        return 0
    configs = server_configs(args.work)
    r = resolve_handle()
    pm = r.GetProjectManager()
    home = pm.GetCurrentProject().GetName() if pm.GetCurrentProject() else None
    args.home = home
    results: list[dict[str, Any]] = (
        json.loads(results_path.read_text()) if results_path.exists() else []
    )
    done = {(x.get("run", 0), x["server"], x["scenario"]) for x in results}
    try:
        for run in range(args.runs):
            for sc in [s for s in suite if s.key in args.scenarios]:
                for server in args.servers:
                    if (run, server, sc.key) in done:
                        continue
                    print(f"== run {run} / {server} / {sc.key}", flush=True)
                    res = run_one(resolve_handle(), server, configs[server], sc, args, run)
                    if not res["server_started"]:
                        print("   MCP server did not start: retrying once")
                        res = run_one(resolve_handle(), server, configs[server], sc, args, run)
                    if res.get("forbidden_tool_calls"):
                        print("   WARNING: tools outside the server under test were used")
                    if res["interference"]:
                        print("   interference detected (project switched): retrying once")
                        res = run_one(resolve_handle(), server, configs[server], sc, args, run)
                    results.append(res)
                    results_path.write_text(json.dumps(results, indent=2))
                    print(
                        f"   score={res['score']:.0%} calls={res['tool_calls']} "
                        f"errors={res['tool_errors']} wall={res['wall_s']}s",
                        flush=True,
                    )
    finally:
        r = resolve_handle()
        pm = r.GetProjectManager()
        if home and home != "Untitled Project":
            pm.LoadProject(home)
    summary = summarize(results)
    (args.work / "summary.json").write_text(json.dumps(summary, indent=2))
    print_summary(summary)
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    raise SystemExit(main())
