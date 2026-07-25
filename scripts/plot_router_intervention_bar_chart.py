#!/usr/bin/env python3
"""Render a single-panel bar chart for coding-agent metrics.

This script intentionally keeps the editable data near the top so that
future measured Token / Time values can be pasted in directly.
"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape


# Update these lists with your final measurements later.
AGENTS = ["Claude Code", "Codex", "Cursor", "OpenCode"]
DSR_VALUES = [0.0, 0.0, 0.0, 0.0]

# Temporary arithmetic-sequence estimates for plotting.
# Replace them with the final Token counts when available.
TOKEN_VALUES = [35_000_000, 50_000_000, 65_000_000, 80_000_000]

# Replace them with the final running time (hours) when available.
TIME_VALUES = [3.0, 5.0, 7.0, 9.0]


COLORS = {
    "DSR": "#9BAEEA",
    "Tokens": "#F5CAA0",
    "Time": "#9CD6C7",
}
TEXT = "#222222"
MUTED = "#666666"
GRID = "#D9D9D9"
BORDER = "#4B4B4B"
RED = "#E24A4A"
FONT_STACK = "'Times New Roman', Georgia, serif"

WIDTH = 1700
HEIGHT = 980
LEFT = 130
RIGHT = 70
TOP = 120
BOTTOM = 120
DISPLAY_MAX = 100.0


def esc(text: str) -> str:
    return escape(text)


def text(
    x: float,
    y: float,
    content: str,
    size: int,
    *,
    anchor: str = "start",
    weight: str = "400",
    fill: str = TEXT,
) -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" '
        f'font-family="{FONT_STACK}" font-size="{size}" font-weight="{weight}" '
        f'fill="{fill}">{esc(content)}</text>'
    )


def rect(
    x: float,
    y: float,
    w: float,
    h: float,
    *,
    fill: str,
    stroke: str = BORDER,
    stroke_width: float = 1.2,
    rx: float = 0.0,
) -> str:
    round_attr = f' rx="{rx:.1f}" ry="{rx:.1f}"' if rx else ""
    return (
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}"'
        f'{round_attr} fill="{fill}" stroke="{stroke}" stroke-width="{stroke_width:.1f}" />'
    )


def line(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    *,
    stroke: str = BORDER,
    stroke_width: float = 1.2,
    dash: str | None = None,
) -> str:
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
        f'stroke="{stroke}" stroke-width="{stroke_width:.1f}"{dash_attr} />'
    )


def map_y(value: float, maximum: float, plot_top: float, plot_bottom: float) -> float:
    ratio = 0.0 if maximum <= 0 else max(0.0, min(1.0, value / maximum))
    return plot_bottom - ratio * (plot_bottom - plot_top)


def fmt_token(value: float) -> str:
    return f"{value / 1_000_000:.1f}M"


def fmt_time(value: float) -> str:
    return f"{value:.1f}h"


def scaled_height_value(metric: str, value: float) -> float:
    """Map each metric to a display scale so all three series remain visible."""
    if metric == "DSR":
        return value
    if metric == "Tokens":
        max_tokens = max(TOKEN_VALUES) or 1.0
        return value / max_tokens * DISPLAY_MAX
    max_time = max(TIME_VALUES) or 1.0
    return value / max_time * DISPLAY_MAX


def build_legend() -> list[str]:
    legend_x = WIDTH - 420
    legend_y = 56
    parts = [rect(legend_x, legend_y, 340, 126, fill="white", stroke="#BBBBBB", stroke_width=1.2, rx=10)]
    parts.append(text(legend_x + 20, legend_y + 28, "Metrics", 24, weight="700"))
    items = [
        ("DSR", "DSR (%, L1-L4 all 0.0%)"),
        ("Tokens", "Token cost (tokens)"),
        ("Time", "Running time (hours)"),
    ]
    for idx, (key, label) in enumerate(items):
        row_y = legend_y + 54 + idx * 20
        parts.append(rect(legend_x + 22, row_y - 11, 18, 12, fill=COLORS[key], stroke=BORDER, stroke_width=1.0))
        parts.append(text(legend_x + 52, row_y, label, 15))
    parts.append(text(legend_x + 20, legend_y + 116, "Bars use per-metric scaling for visibility.", 14, fill=MUTED))
    return parts


def main() -> None:
    output_path = (
        Path(__file__).resolve().parents[1]
        / "artifacts"
        / "plots"
        / "single_panel_agent_metrics.svg"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    plot_left = LEFT
    plot_right = WIDTH - RIGHT
    plot_top = TOP
    plot_bottom = HEIGHT - BOTTOM
    plot_width = plot_right - plot_left
    plot_height = plot_bottom - plot_top

    max_value = DISPLAY_MAX
    y_ticks = [0, 25, 50, 75, 100]

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">',
        '<rect width="100%" height="100%" fill="white" />',
        text(WIDTH / 2, 54, "Coding-Agent Metrics", 34, anchor="middle", weight="700"),
        text(
            WIDTH / 2,
            86,
            "Single-panel grouped bars. DSR denotes the shared L1-L4 outcome and is shown as 0.0% for all agents.",
            18,
            anchor="middle",
            fill=MUTED,
        ),
        line(plot_left, plot_top, plot_left, plot_bottom, stroke=BORDER, stroke_width=1.6),
        line(plot_left, plot_bottom, plot_right, plot_bottom, stroke=BORDER, stroke_width=1.6),
        text(48, (plot_top + plot_bottom) / 2, "Value", 28, anchor="middle", weight="700"),
        text(WIDTH / 2, HEIGHT - 34, "Coding Agent", 28, anchor="middle", weight="700"),
    ]

    for tick in y_ticks:
        y = map_y(tick, max_value, plot_top, plot_bottom)
        parts.append(line(plot_left, y, plot_right, y, stroke=GRID, stroke_width=1.0, dash="8,10"))
        tick_label = f"{tick:.0f}"
        parts.append(text(plot_left - 16, y + 5, tick_label, 15, anchor="end", fill=MUTED))

    group_gap = 82
    bar_width = 42
    intra_gap = 14
    group_width = 3 * bar_width + 2 * intra_gap
    total_groups_width = 4 * group_width + 3 * group_gap
    start_x = plot_left + (plot_width - total_groups_width) / 2

    metric_keys = ["DSR", "Tokens", "Time"]
    metric_values = {
        "DSR": DSR_VALUES,
        "Tokens": TOKEN_VALUES,
        "Time": TIME_VALUES,
    }

    for agent_idx, agent in enumerate(AGENTS):
        group_left = start_x + agent_idx * (group_width + group_gap)
        group_center = group_left + group_width / 2
        for metric_idx, key in enumerate(metric_keys):
            bar_left = group_left + metric_idx * (bar_width + intra_gap)
            bar_center = bar_left + bar_width / 2
            value = metric_values[key][agent_idx]
            if key == "DSR":
                cross_y = plot_bottom - 24
                parts.append(line(bar_left + 4, cross_y - 10, bar_left + bar_width - 4, cross_y + 10, stroke=RED, stroke_width=3.2))
                parts.append(line(bar_left + bar_width - 4, cross_y - 10, bar_left + 4, cross_y + 10, stroke=RED, stroke_width=3.2))
                parts.append(text(bar_center, cross_y - 16, "0.0%", 16, anchor="middle", fill=RED, weight="700"))
                continue

            display_value = scaled_height_value(key, value)
            top_y = map_y(display_value, max_value, plot_top, plot_bottom)
            parts.append(rect(bar_left, top_y, bar_width, plot_bottom - top_y, fill=COLORS[key], stroke=BORDER, stroke_width=1.1, rx=3))
            value_label = fmt_token(value) if key == "Tokens" else fmt_time(value)
            label_y = max(top_y - 10, plot_top + 18)
            parts.append(text(bar_center, label_y, value_label, 16, anchor="middle", weight="700"))

        parts.append(text(group_center, plot_bottom + 38, agent, 18, anchor="middle"))

    parts.extend(build_legend())
    parts.append("</svg>")
    output_path.write_text("\n".join(parts), encoding="utf-8")
    print(f"Saved chart to {output_path}")


if __name__ == "__main__":
    main()
