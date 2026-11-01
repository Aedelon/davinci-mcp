# Copyright Delanoe Pirard / Aedelon. Apache 2.0.
"""Render the README benchmark charts (SVG) from the committed eval results."""

from __future__ import annotations

import json
from pathlib import Path
from statistics import median
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "eval" / "results"
OUT = ROOT / "docs" / "assets"

SERVERS = (
    ("davinci-mcp", "davinci-mcp", "#e9762b"),
    ("davinci-resolve", "Community MCP", "#7a8a99"),
    ("resolve-native", "Resolve built-in MCP", "#b9a58c"),
)


def _rows() -> list[dict[str, Any]]:
    v7 = json.loads((RESULTS / "2026-10-26_bench10_v7_results.json").read_text())
    v8 = json.loads((RESULTS / "2026-10-26_bench11_v8_results.json").read_text())
    # davinci-mcp from v8, the two others from v7: same harness, same blind suite.
    return [r for r in v7 if r["server"] != "davinci-mcp"] + v8


def _totals(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for server, *_ in SERVERS:
        mine = [r for r in rows if r["server"] == server]
        scenarios = sorted({str(r["scenario"]) for r in mine})
        cells = [[r for r in mine if r["scenario"] == s] for s in scenarios]
        out[server] = {
            "success": 100 * sum(r["score"] == 1 for r in mine) / len(mine),
            "cost": sum(median(float(r["cost_usd"]) for r in c) for c in cells),
            "calls": sum(median(float(r["tool_calls"]) for r in c) for c in cells),
        }
    return out


def _chart(title: str, unit: str, values: dict[str, float], fmt: str, path: Path) -> None:
    w, bar_h, gap, left = 760, 34, 18, 210
    h = 70 + len(SERVERS) * (bar_h + gap)
    peak = max(values.values()) or 1
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}" font-family="Avenir Next, Inter, Helvetica, Arial, sans-serif">',
        '<rect width="100%" height="100%" rx="14" fill="#fbf6ef"/>',
        f'<text x="24" y="38" font-size="20" font-weight="700" fill="#3a2418">{title}</text>',
        f'<text x="{w - 24}" y="38" font-size="13" fill="#8a6f5a" text-anchor="end">{unit}</text>',
    ]
    for i, (server, label, color) in enumerate(SERVERS):
        y = 62 + i * (bar_h + gap)
        length = (w - left - 110) * values[server] / peak
        weight = "700" if server == "davinci-mcp" else "500"
        parts += [
            f'<text x="24" y="{y + 22}" font-size="15" font-weight="{weight}" '
            f'fill="#3a2418">{label}</text>',
            f'<rect x="{left}" y="{y}" width="{length:.1f}" height="{bar_h}" '
            f'rx="7" fill="{color}"/>',
            f'<text x="{left + length + 10:.1f}" y="{y + 22}" font-size="15" '
            f'font-weight="{weight}" fill="#3a2418">{fmt.format(values[server])}</text>',
        ]
    parts.append("</svg>")
    path.write_text("\n".join(parts) + "\n")


def main() -> None:
    totals = _totals(_rows())
    OUT.mkdir(parents=True, exist_ok=True)
    pick = {k: {s: totals[s][k] for s in totals} for k in ("success", "cost", "calls")}
    _chart(
        "Jobs completed",
        "success rate, 20 runs each · higher is better",
        pick["success"],
        "{:.0f} %",
        OUT / "bench-success.svg",
    )
    _chart(
        "Cost for the 4 jobs",
        "USD, sum of medians · lower is better",
        pick["cost"],
        "${:.2f}",
        OUT / "bench-cost.svg",
    )
    _chart(
        "Tool calls for the 4 jobs",
        "sum of medians · lower is better",
        pick["calls"],
        "{:.0f}",
        OUT / "bench-calls.svg",
    )
    print(json.dumps(totals, indent=1))


if __name__ == "__main__":
    main()
