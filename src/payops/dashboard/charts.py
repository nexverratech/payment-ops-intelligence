"""Plotly chart builders. Pure functions: rows in, figure out (no Streamlit, no HTTP),
so they can be tested on their own.

Design rules followed:
* Each provider keeps the SAME colour everywhere, whatever is filtered
  (colour follows the entity, never its rank).
* The four provider colours were checked for colour-blind separation.
  Two have low contrast on white, so every chart has a legend or labels,
  hover details, and a table view next to it.
* One y-axis per chart; thin 2px lines; quiet grid.
* Deployments are dotted vertical lines; provider notices are shaded spans.
"""

from __future__ import annotations

from collections.abc import Sequence

import plotly.graph_objects as go

PROVIDER_COLORS = {
    "NOVAPAY": "#2a78d6",    # blue
    "KESTREL": "#eb6834",    # orange
    "BLUERIVER": "#1baf7a",  # aqua
    "ORBIT": "#eda100",      # yellow
}
OTHER_COLOR = "#8a8984"       # any provider not listed above
CURRENT_COLOR = "#2a78d6"
PREVIOUS_COLOR = "#b9b8b2"    # neutral grey for "the period before"
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
GRID = "#e6e5e1"
SURFACE = "#fcfcfb"
EVENT_LINE = "#52514e"
NOTICE_FILL = "rgba(82, 81, 78, 0.08)"
# A failure rate built on very few payments is mostly noise (2 failures out of 12 = 17%).
# Such points are left out of trend lines; the table view still shows them.
MIN_POINT_ATTEMPTS = 30


def provider_color(code: str) -> str:
    return PROVIDER_COLORS.get(code, OTHER_COLOR)


def failure_rate_trend(
    rows: Sequence[dict], events: Sequence[dict] = (), title: str = "Failure rate", min_attempts: int = MIN_POINT_ATTEMPTS
) -> go.Figure:
    """Failure rate over time, one line per provider (or one line if rows have no provider),
    with deployments and provider notices marked. Points with fewer than `min_attempts`
    payments are left as gaps."""
    fig = go.Figure()
    series: dict[str, list[dict]] = {}
    for r in rows:
        series.setdefault(r.get("provider_code") or "ALL", []).append(r)
    for code in sorted(series):
        pts = sorted(series[code], key=lambda r: r["bucket"])
        fig.add_trace(go.Scatter(
            x=[p["bucket"] for p in pts],
            y=[(p["failure_rate"] or 0) * 100 if p["attempts"] >= min_attempts else None for p in pts],
            customdata=[[p["failures"], p["attempts"]] for p in pts],
            name="All providers" if code == "ALL" else code,
            mode="lines",
            connectgaps=False,
            line={"width": 2, "color": TEXT_SECONDARY if code == "ALL" else provider_color(code)},
            hovertemplate="%{fullData.name}: %{y:.2f}% (%{customdata[0]:,} of %{customdata[1]:,})<extra></extra>",
        ))
    _mark_events(fig, events)
    _style(fig, title, y_title="Failure rate (%)")
    fig.update_layout(hovermode="x unified")
    return fig


def provider_excess_bar(rows: Sequence[dict], key: str = "provider_code") -> go.Figure:
    """Extra failures per provider compared with the previous period (negative = fewer)."""
    rows = sorted(rows, key=lambda r: r["excess_failures"])
    fig = go.Figure(go.Bar(
        x=[r["excess_failures"] for r in rows],
        y=[r[key] for r in rows],
        orientation="h",
        marker={"color": [provider_color(r[key]) for r in rows]},
        text=[f"{r['excess_failures']:+,.0f}" for r in rows],
        textposition="outside",
        textfont={"color": TEXT_PRIMARY},
        customdata=[[_pct(r.get("rate_baseline")), _pct(r.get("rate_current")), r.get("contribution_share") or 0] for r in rows],
        hovertemplate="%{y}: %{customdata[0]} → %{customdata[1]}<br>excess %{x:,.1f} failures, "
                      "share %{customdata[2]:.0%}<extra></extra>",
    ))
    _style(fig, "Extra failures vs previous period", x_title="Excess failures (actual − expected)")
    fig.update_layout(showlegend=False, bargap=0.35)
    fig.add_vline(x=0, line={"color": TEXT_SECONDARY, "width": 1})
    return fig


def reasons_bar(rows: Sequence[dict]) -> go.Figure:
    """Failures per reason: this period vs the previous one (grouped horizontal bars)."""
    rows = sorted(rows, key=lambda r: r["failures"])
    labels = [r["failure_reason"] for r in rows]
    fig = go.Figure([
        go.Bar(y=labels, x=[r["failures_previous"] for r in rows], name="Previous period", orientation="h",
               marker={"color": PREVIOUS_COLOR}, hovertemplate="%{y}: %{x:,} (previous)<extra></extra>"),
        go.Bar(y=labels, x=[r["failures"] for r in rows], name="Selected period", orientation="h",
               marker={"color": CURRENT_COLOR}, text=[f"{r['failures']:,.0f}" for r in rows], textposition="outside",
               textfont={"color": TEXT_PRIMARY}, hovertemplate="%{y}: %{x:,} (selected)<extra></extra>"),
    ])
    _style(fig, "Failures by reason", x_title="Failed payments")
    fig.update_layout(barmode="group", bargap=0.3, bargroupgap=0.08)
    return fig


def volume_trend(rows: Sequence[dict], title: str = "Payment attempts") -> go.Figure:
    """Attempts over time (all selected providers combined)."""
    totals: dict[str, int] = {}
    for r in rows:
        totals[r["bucket"]] = totals.get(r["bucket"], 0) + r["attempts"]
    xs = sorted(totals)
    fig = go.Figure(go.Scatter(x=xs, y=[totals[x] for x in xs], mode="lines", name="Attempts",
                               line={"width": 2, "color": CURRENT_COLOR},
                               hovertemplate="%{x}<br>%{y:,} attempts<extra></extra>"))
    _style(fig, title, y_title="Attempts")
    fig.update_layout(showlegend=False)
    return fig


# ---- helpers -------------------------------------------------------------------

def _mark_events(fig: go.Figure, events: Sequence[dict]) -> None:
    deployments = sorted((e for e in events if e.get("kind") == "deployment"), key=lambda e: e["deployed_at"])
    level = {e["deployment_id"]: i % 3 for i, e in enumerate(deployments)}  # stagger labels on 3 rows
    for e in events:
        if e.get("kind") == "deployment":
            fig.add_shape(type="line", x0=e["deployed_at"], x1=e["deployed_at"], y0=0, y1=1, yref="paper",
                          line={"color": EVENT_LINE, "width": 1, "dash": "dot"})
            fig.add_annotation(x=e["deployed_at"], y=1, yref="paper", text=e["deployment_id"], showarrow=False,
                               yanchor="bottom", yshift=13 * level[e["deployment_id"]],
                               font={"size": 10, "color": TEXT_SECONDARY},
                               hovertext=f"{e.get('service', '')} {e.get('version', '')}: {e.get('change_summary', '')}")
        elif e.get("kind") == "provider_notice":
            end = e.get("reported_end_at") or e["reported_start_at"]
            fig.add_shape(type="rect", x0=e["reported_start_at"], x1=end, y0=0, y1=1, yref="paper",
                          fillcolor=NOTICE_FILL, line={"width": 0}, layer="below")
            fig.add_annotation(x=e["reported_start_at"], y=0.98, yref="paper", text=e["incident_id"], showarrow=False,
                               xanchor="left", yanchor="top", font={"size": 10, "color": TEXT_SECONDARY},
                               hovertext=f"{e.get('provider_code', '')}: {e.get('title', '')}")


def _style(fig: go.Figure, title: str, x_title: str | None = None, y_title: str | None = None) -> None:
    fig.update_layout(
        title={"text": title, "font": {"size": 15, "color": TEXT_PRIMARY}, "x": 0, "xanchor": "left"},
        paper_bgcolor=SURFACE, plot_bgcolor=SURFACE,
        font={"color": TEXT_SECONDARY, "size": 12},
        margin={"l": 10, "r": 30, "t": 90, "b": 10},
        # Legend below the plot, so it never collides with event labels along the top.
        legend={"orientation": "h", "yanchor": "top", "y": -0.12, "xanchor": "left", "x": 0},
        hoverlabel={"bgcolor": "white", "font": {"color": TEXT_PRIMARY}},
    )
    fig.update_xaxes(title=x_title, showgrid=False, linecolor=GRID, ticks="outside", tickcolor=GRID)
    fig.update_yaxes(title=y_title, gridcolor=GRID, zeroline=False, linecolor=GRID)


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.2f}%"
