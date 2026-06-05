"""HTML dashboard generator for chemunited-sim recorded data."""

from __future__ import annotations

import html
import json
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

from .pyvis_export import NoSnapshotsError, SnapshotReadError

_PLOTLY_CDN = "https://cdn.plot.ly/plotly-2.35.2.min.js"

_PA_TO_BAR = 1e-5
_M3S_TO_MLMIN = 6e7
_FLAT_ABS_TOL = 1e-12
_FLAT_REL_TOL = 1e-6


@dataclass(frozen=True)
class TraceSpec:
    """Serializable chart trace with scanability metadata."""

    name: str
    x: list[float]
    y: list[float]
    minimum: float | None
    maximum: float | None
    value_range: float
    max_abs: float
    flat: bool
    default_visible: bool

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "x": self.x,
            "y": self.y,
            "min": self.minimum,
            "max": self.maximum,
            "range": self.value_range,
            "maxAbs": self.max_abs,
            "flat": self.flat,
            "defaultVisible": self.default_visible,
            "points": len(self.x),
        }


class _RawTrace(TypedDict):
    name: str
    x: list[float]
    y: list[float]
    minimum: float | None
    maximum: float | None
    range: float
    max_abs: float
    flat: bool


@dataclass(frozen=True)
class ChartSpec:
    """Serializable chart definition consumed by the client-side controller."""

    chart_id: str
    tab: str
    title: str
    x_label: str
    y_label: str
    traces: list[TraceSpec]

    @property
    def all_flat(self) -> bool:
        return bool(self.traces) and all(trace.flat for trace in self.traces)

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.chart_id,
            "tab": self.tab,
            "title": self.title,
            "xLabel": self.x_label,
            "yLabel": self.y_label,
            "allFlat": self.all_flat,
            "traces": [trace.to_payload() for trace in self.traces],
        }


def render_dashboard_html(db_path: str | os.PathLike) -> str:
    """Read all recorded time series from *db_path* and return dashboard HTML."""
    path = Path(db_path)
    uri = f"file:{path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise SnapshotReadError(f"Could not open simulation DB: {path}") from exc

    try:
        meta = _read_meta(conn)
        node_pressures = _read_node_pressures(conn)
        edge_flows = _read_edge_flows(conn)
        inv_states = _read_inventory_states(conn)
        species_moles = _read_species_moles(conn)
        cell_temps = _read_cell_temperatures(conn)
    except NoSnapshotsError:
        raise
    except sqlite3.Error as exc:
        raise SnapshotReadError(f"Could not read simulation DB: {path}") from exc
    finally:
        conn.close()

    return _build_html(
        path.stem,
        meta,
        node_pressures,
        edge_flows,
        inv_states,
        species_moles,
        cell_temps,
    )


# ---------------------------------------------------------------------------
# DB readers
# ---------------------------------------------------------------------------


def _read_meta(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM meta")
    }


def _read_node_pressures(conn: sqlite3.Connection) -> dict[str, list]:
    """Returns {node_id: [(time, pressure_bar), ...]}."""
    series: dict[str, list] = defaultdict(list)
    for row in conn.execute(
        "SELECT time, node_id, pressure FROM node_pressure ORDER BY time"
    ):
        series[row["node_id"]].append(
            (float(row["time"]), float(row["pressure"]) * _PA_TO_BAR)
        )
    return dict(series)


def _read_edge_flows(conn: sqlite3.Connection) -> dict[str, list]:
    """Returns {edge_id: [(time, flow_nls), ...]}."""
    series: dict[str, list] = defaultdict(list)
    for row in conn.execute(
        "SELECT time, edge_id, flow_rate FROM edge_flow ORDER BY time"
    ):
        series[row["edge_id"]].append(
            (float(row["time"]), float(row["flow_rate"]) * _M3S_TO_MLMIN)
        )
    return dict(series)


def _read_inventory_states(conn: sqlite3.Connection) -> dict[str, list]:
    """Returns {node_id: [(time, pressure_bar, temperature_K), ...]}."""
    series: dict[str, list] = defaultdict(list)
    if not _table_exists(conn, "inventory_state"):
        return {}
    for row in conn.execute(
        "SELECT time, node_id, pressure, temperature FROM inventory_state ORDER BY time"
    ):
        series[row["node_id"]].append(
            (
                float(row["time"]),
                float(row["pressure"]) * _PA_TO_BAR,
                float(row["temperature"]),
            )
        )
    return dict(series)


def _read_species_moles(conn: sqlite3.Connection) -> dict[str, dict[str, list]]:
    """Returns {'node_id / phase': {species_id: [(time, moles), ...]}}."""
    series: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    if not _table_exists(conn, "inventory_content"):
        return {}
    for row in conn.execute("""
        SELECT time, node_id, phase, species_id, moles
        FROM inventory_content
        WHERE species_id != '__carrier__'
        ORDER BY time
        """):
        key = f"{row['node_id']} / {row['phase']}"
        series[key][row["species_id"]].append((float(row["time"]), float(row["moles"])))
    return {k: dict(v) for k, v in series.items()}


def _read_cell_temperatures(conn: sqlite3.Connection) -> dict[str, list]:
    """Returns {edge_id: [(time, avg_temp_K), ...]}."""
    series: dict[str, list] = defaultdict(list)
    if not _table_exists(conn, "cell_state"):
        return {}
    for row in conn.execute("""
        SELECT time, edge_id, AVG(temperature) AS avg_temp
        FROM cell_state
        GROUP BY time, edge_id
        ORDER BY time
        """):
        if row["avg_temp"] is not None:
            series[row["edge_id"]].append((float(row["time"]), float(row["avg_temp"])))
    return dict(series)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# HTML builder
# ---------------------------------------------------------------------------


def _build_html(
    stem: str,
    meta: dict[str, str],
    node_pressures: dict[str, list],
    edge_flows: dict[str, list],
    inv_states: dict[str, list],
    species_moles: dict[str, dict[str, list]],
    cell_temps: dict[str, list],
) -> str:
    charts = _build_charts(
        node_pressures=node_pressures,
        edge_flows=edge_flows,
        inv_states=inv_states,
        species_moles=species_moles,
        cell_temps=cell_temps,
    )
    summary = _build_summary(
        node_pressures=node_pressures,
        edge_flows=edge_flows,
        inv_states=inv_states,
        species_moles=species_moles,
        cell_temps=cell_temps,
    )
    payload = {
        "stem": stem,
        "summary": summary,
        "tabs": ["pressures", "flows", "inventories", "species", "temperatures"],
        "charts": [chart.to_payload() for chart in charts],
        "emptyStates": {
            "pressures": "No node pressure traces were recorded.",
            "flows": "No edge flow traces were recorded.",
            "inventories": "No inventory state traces were recorded.",
            "species": "No tracked inventory species were recorded.",
            "temperatures": "No edge cell temperature traces were recorded.",
        },
    }

    safe_stem = html.escape(stem)
    meta_rows = _render_meta_rows(meta)
    metric_cards = _render_metric_cards(summary["metrics"])
    payload_json = _json_script_payload(payload)
    t_min = summary["timeStart"]
    t_max = summary["timeEnd"]

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dashboard - {safe_stem}</title>
<script src="{_PLOTLY_CDN}"></script>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
      "Segoe UI", sans-serif;
    background: #f6f7f9;
    color: #20242c;
  }}
  header {{
    background: #ffffff;
    border-bottom: 1px solid #dde3ea;
    padding: 20px 28px;
  }}
  .header-inner {{
    max-width: 1480px;
    margin: 0 auto;
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 24px;
  }}
  .kicker {{
    color: #526070;
    font-size: 0.78rem;
    font-weight: 700;
    letter-spacing: 0;
    text-transform: uppercase;
  }}
  h1 {{
    margin: 4px 0 0;
    font-size: 1.7rem;
    line-height: 1.15;
    font-weight: 700;
  }}
  .time-range {{
    color: #526070;
    font-size: 0.92rem;
    white-space: nowrap;
  }}
  main {{
    max-width: 1480px;
    margin: 0 auto;
    padding: 22px 24px 36px;
  }}
  .metrics {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 12px;
    margin-bottom: 18px;
  }}
  .metric-card, .panel, .chart-card {{
    background: #ffffff;
    border: 1px solid #dde3ea;
    border-radius: 8px;
    box-shadow: 0 1px 2px rgba(20, 25, 35, 0.04);
  }}
  .metric-card {{ padding: 12px 14px; min-height: 72px; }}
  .metric-label {{
    color: #657181;
    font-size: 0.76rem;
    font-weight: 700;
    letter-spacing: 0;
    text-transform: uppercase;
  }}
  .metric-value {{ margin-top: 6px; font-size: 1.12rem; font-weight: 700; }}
  .metric-unit {{ color: #657181; font-size: 0.82rem; font-weight: 500; }}
  .tabs {{
    display: flex;
    gap: 6px;
    border-bottom: 1px solid #d6dde6;
    margin: 8px 0 18px;
    overflow-x: auto;
  }}
  .tab-button {{
    appearance: none;
    border: 0;
    border-bottom: 3px solid transparent;
    background: transparent;
    color: #526070;
    cursor: pointer;
    font: inherit;
    font-size: 0.92rem;
    font-weight: 700;
    padding: 12px 14px 10px;
    white-space: nowrap;
  }}
  .tab-button.active {{ color: #0b5cad; border-bottom-color: #0b5cad; }}
  .tab-panel {{ display: none; }}
  .tab-panel.active {{ display: block; }}
  .panel {{ padding: 18px; margin-bottom: 16px; }}
  .panel h2, .chart-card h2 {{
    margin: 0;
    font-size: 1rem;
    line-height: 1.25;
  }}
  .panel-subtitle, .chart-meta {{
    margin-top: 5px;
    color: #657181;
    font-size: 0.84rem;
  }}
  .overview-grid {{
    display: grid;
    grid-template-columns: minmax(260px, 0.9fr) minmax(280px, 1.1fr);
    gap: 16px;
  }}
  table {{ border-collapse: collapse; width: 100%; font-size: 0.86rem; }}
  td {{ border-bottom: 1px solid #edf0f3; padding: 7px 8px; vertical-align: top; }}
  td:first-child {{ color: #526070; font-weight: 700; width: 170px; }}
  .toolbar {{
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
    margin-bottom: 14px;
  }}
  .search-input {{
    min-width: 260px;
    flex: 1;
    border: 1px solid #cbd4df;
    border-radius: 6px;
    color: #20242c;
    font: inherit;
    font-size: 0.9rem;
    padding: 9px 11px;
  }}
  .checkbox-label {{
    align-items: center;
    color: #3d4856;
    display: inline-flex;
    gap: 7px;
    font-size: 0.88rem;
    font-weight: 600;
  }}
  .control-button {{
    border: 1px solid #cbd4df;
    border-radius: 6px;
    background: #ffffff;
    color: #20242c;
    cursor: pointer;
    font: inherit;
    font-size: 0.86rem;
    font-weight: 700;
    padding: 8px 11px;
  }}
  .chart-card {{ padding: 16px; margin-bottom: 16px; }}
  .chart-head {{
    align-items: flex-start;
    display: flex;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 8px;
  }}
  .plotly-chart {{ width: 100%; height: 380px; }}
  .trace-list {{
    border-top: 1px solid #edf0f3;
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
    gap: 6px 12px;
    margin-top: 10px;
    max-height: 160px;
    overflow: auto;
    padding-top: 10px;
  }}
  .trace-item {{
    align-items: center;
    color: #3d4856;
    display: flex;
    gap: 7px;
    min-width: 0;
    font-size: 0.82rem;
  }}
  .trace-name {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .trace-pill {{
    background: #eef2f6;
    border-radius: 999px;
    color: #657181;
    font-size: 0.7rem;
    font-weight: 700;
    padding: 2px 6px;
  }}
  .empty-state {{
    background: #ffffff;
    border: 1px dashed #cbd4df;
    border-radius: 8px;
    color: #657181;
    padding: 28px;
    text-align: center;
  }}
  .dataset-list {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 10px;
    margin-top: 12px;
  }}
  .dataset-card {{
    border: 1px solid #edf0f3;
    border-radius: 8px;
    padding: 12px;
  }}
  .dataset-title {{ font-weight: 700; }}
  .dataset-meta {{ color: #657181; font-size: 0.82rem; margin-top: 4px; }}
  @media (max-width: 820px) {{
    header {{ padding: 18px 18px; }}
    main {{ padding: 18px 14px 28px; }}
    .header-inner {{ align-items: flex-start; flex-direction: column; }}
    .time-range {{ white-space: normal; }}
    .overview-grid {{ grid-template-columns: 1fr; }}
    .plotly-chart {{ height: 330px; }}
    .search-input {{ min-width: 100%; }}
  }}
</style>
</head>
<body>
<header>
  <div class="header-inner">
    <div>
      <div class="kicker">chemunited-sim</div>
      <h1>Dashboard - {safe_stem}</h1>
    </div>
    <div class="time-range">
      t: {_format_number(t_min)} s - {_format_number(t_max)} s
    </div>
  </div>
</header>
<main>
  <section class="metrics" aria-label="Simulation summary">
    {metric_cards}
  </section>
  <nav class="tabs" aria-label="Dashboard sections">
    <button class="tab-button active" data-tab="overview">Overview</button>
    <button class="tab-button" data-tab="pressures">Pressures</button>
    <button class="tab-button" data-tab="flows">Flows</button>
    <button class="tab-button" data-tab="inventories">Inventories</button>
    <button class="tab-button" data-tab="species">Species</button>
    <button class="tab-button" data-tab="temperatures">Temperatures</button>
  </nav>

  <section class="tab-panel active" data-panel="overview">
    <div class="overview-grid">
      <div class="panel">
        <h2>Simulation Metadata</h2>
        <div class="panel-subtitle">Values stored in the recorder meta table.</div>
        <table><tbody>{meta_rows}</tbody></table>
      </div>
      <div class="panel">
        <h2>Recorded Datasets</h2>
        <div class="panel-subtitle">
          Trace counts and default visibility by section.
        </div>
        <div id="dataset-summary" class="dataset-list"></div>
      </div>
    </div>
  </section>

  {_render_chart_tab("pressures", "Search pressure traces")}
  {_render_chart_tab("flows", "Search flow traces")}
  {_render_chart_tab("inventories", "Search inventory traces")}
  {_render_chart_tab("species", "Search species traces")}
  {_render_chart_tab("temperatures", "Search temperature traces")}
</main>
<script id="dashboard-data" type="application/json">{payload_json}</script>
<script>
(() => {{
  const payload = JSON.parse(document.getElementById("dashboard-data").textContent);
  const tabs = ["pressures", "flows", "inventories", "species", "temperatures"];
  const colorway = [
    "#0b5cad", "#b33f62", "#2f855a", "#b7791f", "#5a4fcf",
    "#0f766e", "#c2410c", "#4a5568", "#7c3aed", "#0369a1"
  ];
  const state = {{
    activeTab: "overview",
    query: Object.fromEntries(tabs.map((tab) => [tab, ""])),
    showFlat: Object.fromEntries(tabs.map((tab) => [tab, false])),
    visibility: {{}}
  }};

  function traceKey(chart, trace) {{
    return `${{chart.id}}::${{trace.name}}`;
  }}

  function defaultVisible(chart, trace) {{
    if (chart.allFlat) return true;
    return Boolean(trace.defaultVisible);
  }}

  function isVisible(chart, trace) {{
    const key = traceKey(chart, trace);
    if (!(key in state.visibility)) {{
      state.visibility[key] = defaultVisible(chart, trace);
    }}
    return state.visibility[key];
  }}

  function setTab(tab) {{
    state.activeTab = tab;
    document.querySelectorAll(".tab-button").forEach((button) => {{
      button.classList.toggle("active", button.dataset.tab === tab);
    }});
    document.querySelectorAll(".tab-panel").forEach((panel) => {{
      panel.classList.toggle("active", panel.dataset.panel === tab);
    }});
    if (tab !== "overview") renderTab(tab);
  }}

  function formatNumber(value) {{
    if (value === null || value === undefined || Number.isNaN(value)) return "n/a";
    const absValue = Math.abs(value);
    if (absValue !== 0 && (absValue < 0.001 || absValue >= 100000)) {{
      return Number(value).toExponential(3);
    }}
    return Number(value).toLocaleString(undefined, {{ maximumSignificantDigits: 5 }});
  }}

  function chartSubtitle(chart, traces) {{
    const visibleCount = traces.filter((trace) => isVisible(chart, trace)).length;
    const flatCount = chart.traces.filter((trace) => trace.flat).length;
    return `${{visibleCount}} visible of ${{chart.traces.length}} traces` +
      (flatCount ? `, ${{flatCount}} flat` : "");
  }}

  function filteredTraces(chart, tab) {{
    const query = state.query[tab].trim().toLowerCase();
    return chart.traces.filter((trace) => {{
      const matches = !query || trace.name.toLowerCase().includes(query);
      const allowedFlat = state.showFlat[tab] || !trace.flat || chart.allFlat;
      return matches && allowedFlat;
    }});
  }}

  function renderTab(tab) {{
    const container = document.getElementById(`charts-${{tab}}`);
    const charts = payload.charts.filter((chart) => chart.tab === tab);
    container.innerHTML = "";
    if (!charts.length) {{
      container.appendChild(emptyState(payload.emptyStates[tab]));
      return;
    }}
    charts.forEach((chart) => renderChart(container, chart, tab));
  }}

  function renderChart(container, chart, tab) {{
    const card = document.createElement("article");
    card.className = "chart-card";
    const plotId = `plot-${{chart.id}}`;
    const traces = filteredTraces(chart, tab);

    card.innerHTML = `
      <div class="chart-head">
        <div>
          <h2>${{escapeHtml(chart.title)}}</h2>
          <div class="chart-meta">${{escapeHtml(chartSubtitle(chart, traces))}}</div>
        </div>
        <button class="control-button" data-reset-chart="${{chart.id}}">
          Reset traces
        </button>
      </div>
      <div id="${{plotId}}" class="plotly-chart"></div>
      <div class="trace-list" data-trace-list="${{chart.id}}"></div>
    `;
    container.appendChild(card);

    card.querySelector("[data-reset-chart]").addEventListener("click", () => {{
      chart.traces.forEach((trace) => {{
        state.visibility[traceKey(chart, trace)] = defaultVisible(chart, trace);
      }});
      renderTab(tab);
    }});

    renderTraceList(card.querySelector("[data-trace-list]"), chart, traces, tab);
    renderPlot(plotId, chart, traces);
  }}

  function renderTraceList(container, chart, traces, tab) {{
    if (!traces.length) {{
      container.innerHTML =
        `<div class="empty-state">No traces match this filter.</div>`;
      return;
    }}
    traces.forEach((trace) => {{
      const label = document.createElement("label");
      label.className = "trace-item";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = isVisible(chart, trace);
      checkbox.addEventListener("change", () => {{
        state.visibility[traceKey(chart, trace)] = checkbox.checked;
        renderTab(tab);
      }});
      const name = document.createElement("span");
      name.className = "trace-name";
      name.textContent = trace.name;
      label.appendChild(checkbox);
      label.appendChild(name);
      if (trace.flat) {{
        const pill = document.createElement("span");
        pill.className = "trace-pill";
        pill.textContent = "flat";
        label.appendChild(pill);
      }}
      container.appendChild(label);
    }});
  }}

  function renderPlot(plotId, chart, traces) {{
    const visible = traces.filter((trace) => isVisible(chart, trace));
    const plotTraces = visible.map((trace) => ({{
      x: trace.x,
      y: trace.y,
      mode: "lines",
      name: trace.name,
      type: "scatter",
      line: {{ width: trace.flat ? 1.4 : 2.2 }},
      hovertemplate:
        "%{{fullData.name}}<br>t=%{{x:.6g}} s" +
        "<br>value=%{{y:.6g}}<extra></extra>"
    }}));
    const layout = {{
      autosize: true,
      colorway,
      hovermode: "x unified",
      margin: {{ t: 12, r: 18, b: 54, l: 68 }},
      paper_bgcolor: "#ffffff",
      plot_bgcolor: "#ffffff",
      xaxis: {{
        title: {{ text: chart.xLabel, standoff: 8 }},
        gridcolor: "#e9edf2",
        zerolinecolor: "#cbd4df"
      }},
      yaxis: {{
        title: {{ text: chart.yLabel, standoff: 10 }},
        gridcolor: "#e9edf2",
        zerolinecolor: "#cbd4df"
      }},
      legend: {{
        orientation: "h",
        y: -0.24,
        x: 0,
        font: {{ size: 11 }}
      }}
    }};
    const config = {{
      displaylogo: false,
      responsive: true,
      scrollZoom: true,
      modeBarButtonsToRemove: ["lasso2d", "select2d"]
    }};
    Plotly.react(plotId, plotTraces, layout, config);
  }}

  function emptyState(text) {{
    const node = document.createElement("div");
    node.className = "empty-state";
    node.textContent = text;
    return node;
  }}

  function renderDatasetSummary() {{
    const container = document.getElementById("dataset-summary");
    container.innerHTML = "";
    const labels = {{
      pressures: "Pressures",
      flows: "Flows",
      inventories: "Inventories",
      species: "Species",
      temperatures: "Temperatures"
    }};
    tabs.forEach((tab) => {{
      const charts = payload.charts.filter((chart) => chart.tab === tab);
      const traceCount = charts.reduce((sum, chart) => sum + chart.traces.length, 0);
      const defaultCount = charts.reduce(
        (sum, chart) => sum + chart.traces
          .filter((trace) => defaultVisible(chart, trace)).length,
        0
      );
      const flatCount = charts.reduce(
        (sum, chart) => sum + chart.traces.filter((trace) => trace.flat).length,
        0
      );
      const card = document.createElement("div");
      card.className = "dataset-card";
      card.innerHTML = `
        <div class="dataset-title">${{labels[tab]}}</div>
        <div class="dataset-meta">
          ${{charts.length}} chart(s), ${{traceCount}} trace(s)
        </div>
        <div class="dataset-meta">
          ${{defaultCount}} shown by default, ${{flatCount}} flat
        </div>
      `;
      container.appendChild(card);
    }});
  }}

  function escapeHtml(value) {{
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }}

  document.querySelectorAll(".tab-button").forEach((button) => {{
    button.addEventListener("click", () => setTab(button.dataset.tab));
  }});
  tabs.forEach((tab) => {{
    const input = document.querySelector(`[data-search="${{tab}}"]`);
    const flatToggle = document.querySelector(`[data-show-flat="${{tab}}"]`);
    const reset = document.querySelector(`[data-reset-tab="${{tab}}"]`);
    input.addEventListener("input", () => {{
      state.query[tab] = input.value;
      renderTab(tab);
    }});
    flatToggle.addEventListener("change", () => {{
      state.showFlat[tab] = flatToggle.checked;
      renderTab(tab);
    }});
    reset.addEventListener("click", () => {{
      state.query[tab] = "";
      state.showFlat[tab] = false;
      input.value = "";
      flatToggle.checked = false;
      payload.charts
        .filter((chart) => chart.tab === tab)
        .forEach((chart) => chart.traces.forEach((trace) => {{
          state.visibility[traceKey(chart, trace)] = defaultVisible(chart, trace);
        }}));
      renderTab(tab);
    }});
  }});

  renderDatasetSummary();
}})();
</script>
</body>
</html>"""


def _build_charts(
    *,
    node_pressures: dict[str, list],
    edge_flows: dict[str, list],
    inv_states: dict[str, list],
    species_moles: dict[str, dict[str, list]],
    cell_temps: dict[str, list],
) -> list[ChartSpec]:
    charts: list[ChartSpec] = []
    charts.append(
        _make_line_chart(
            chart_id="node_pressures",
            tab="pressures",
            title="Node Pressures",
            series={
                nid: ([t for t, _ in pts], [p for _, p in pts])
                for nid, pts in node_pressures.items()
            },
            x_label="Simulation time (s)",
            y_label="Pressure (bar)",
        )
    )
    charts.append(
        _make_line_chart(
            chart_id="edge_flows",
            tab="flows",
            title="Edge Flow Rates",
            series={
                eid: ([t for t, _ in pts], [f for _, f in pts])
                for eid, pts in edge_flows.items()
            },
            x_label="Simulation time (s)",
            y_label="Flow rate (mL/min)",
        )
    )
    charts.append(
        _make_line_chart(
            chart_id="inv_temperatures",
            tab="inventories",
            title="Inventory Temperatures",
            series={
                nid: ([t for t, _, _ in pts], [temp for _, _, temp in pts])
                for nid, pts in inv_states.items()
            },
            x_label="Simulation time (s)",
            y_label="Temperature (K)",
        )
    )
    charts.append(
        _make_line_chart(
            chart_id="inv_pressures",
            tab="inventories",
            title="Inventory Pressures",
            series={
                nid: ([t for t, _, _ in pts], [pressure for _, pressure, _ in pts])
                for nid, pts in inv_states.items()
            },
            x_label="Simulation time (s)",
            y_label="Pressure (bar)",
        )
    )
    for i, (group_key, species_dict) in enumerate(sorted(species_moles.items())):
        charts.append(
            _make_line_chart(
                chart_id=f"species_{i}",
                tab="species",
                title=f"Species Moles - {group_key}",
                series={
                    sid: ([t for t, _ in pts], [moles for _, moles in pts])
                    for sid, pts in species_dict.items()
                },
                x_label="Simulation time (s)",
                y_label="Moles (mol)",
            )
        )
    charts.append(
        _make_line_chart(
            chart_id="cell_temps",
            tab="temperatures",
            title="Edge Average Cell Temperature",
            series={
                eid: ([t for t, _ in pts], [temp for _, temp in pts])
                for eid, pts in cell_temps.items()
            },
            x_label="Simulation time (s)",
            y_label="Temperature (K)",
        )
    )
    return [chart for chart in charts if chart.traces]


def _make_line_chart(
    *,
    chart_id: str,
    tab: str,
    title: str,
    series: dict[str, tuple[list, list]],
    x_label: str,
    y_label: str,
) -> ChartSpec:
    raw_traces: list[_RawTrace] = []
    for name, (xs, ys) in sorted(series.items()):
        y_values = [float(value) for value in ys]
        x_values = [_round_time(float(value)) for value in xs]
        if y_values:
            minimum = min(y_values)
            maximum = max(y_values)
            value_range = maximum - minimum
            max_abs = max(abs(value) for value in y_values)
        else:
            minimum = None
            maximum = None
            value_range = 0.0
            max_abs = 0.0
        flat = value_range <= max(_FLAT_ABS_TOL, _FLAT_REL_TOL * max_abs)
        raw_traces.append(
            {
                "name": name,
                "x": x_values,
                "y": y_values,
                "minimum": minimum,
                "maximum": maximum,
                "range": value_range,
                "max_abs": max_abs,
                "flat": flat,
            }
        )

    all_flat = bool(raw_traces) and all(trace["flat"] for trace in raw_traces)
    traces = [
        TraceSpec(
            name=trace["name"],
            x=trace["x"],
            y=trace["y"],
            minimum=trace["minimum"],
            maximum=trace["maximum"],
            value_range=trace["range"],
            max_abs=trace["max_abs"],
            flat=trace["flat"],
            default_visible=all_flat or not trace["flat"],
        )
        for trace in raw_traces
    ]
    return ChartSpec(
        chart_id=chart_id,
        tab=tab,
        title=title,
        x_label=x_label,
        y_label=y_label,
        traces=traces,
    )


def _build_summary(
    *,
    node_pressures: dict[str, list],
    edge_flows: dict[str, list],
    inv_states: dict[str, list],
    species_moles: dict[str, dict[str, list]],
    cell_temps: dict[str, list],
) -> dict[str, Any]:
    all_times = sorted(
        {
            t
            for points in _all_point_lists(
                node_pressures, edge_flows, inv_states, species_moles, cell_temps
            )
            for t in _times_from_points(points)
        }
    )
    t_min = min(all_times, default=0.0)
    t_max = max(all_times, default=0.0)
    pressure_values = [p for pts in node_pressures.values() for _, p in pts]
    pressure_values.extend(p for pts in inv_states.values() for _, p, _ in pts)
    flow_values = [flow for pts in edge_flows.values() for _, flow in pts]
    temp_values = [temp for pts in cell_temps.values() for _, temp in pts]
    temp_values.extend(temp for pts in inv_states.values() for _, _, temp in pts)
    species_ids = {
        species_id
        for species_dict in species_moles.values()
        for species_id in species_dict
    }

    metrics = [
        _metric("Duration", t_max - t_min, "s"),
        _metric("Samples", len(all_times), ""),
        _metric("Nodes", len(node_pressures), ""),
        _metric("Edges", len(edge_flows), ""),
        _metric("Inventories", len(inv_states), ""),
        _metric("Species", len(species_ids), ""),
        _metric(
            "Max Pressure",
            max(pressure_values) if pressure_values else None,
            "bar",
        ),
        _metric(
            "Max Abs Flow",
            max((abs(flow) for flow in flow_values), default=None),
            "mL/min",
        ),
        _metric("Temperature Range", _range_text(temp_values), "K", preformatted=True),
    ]
    return {
        "timeStart": _round_time(t_min),
        "timeEnd": _round_time(t_max),
        "sampleCount": len(all_times),
        "metrics": metrics,
    }


def _all_point_lists(
    node_pressures: dict[str, list],
    edge_flows: dict[str, list],
    inv_states: dict[str, list],
    species_moles: dict[str, dict[str, list]],
    cell_temps: dict[str, list],
) -> list[list]:
    point_lists: list[list] = []
    point_lists.extend(node_pressures.values())
    point_lists.extend(edge_flows.values())
    point_lists.extend(inv_states.values())
    point_lists.extend(cell_temps.values())
    for species_dict in species_moles.values():
        point_lists.extend(species_dict.values())
    return point_lists


def _times_from_points(points: list) -> list[float]:
    return [float(point[0]) for point in points]


def _metric(
    label: str,
    value: float | int | str | None,
    unit: str,
    *,
    preformatted: bool = False,
) -> dict[str, str]:
    if value is None:
        text = "n/a"
    elif preformatted:
        text = str(value)
    elif isinstance(value, int):
        text = f"{value:,}"
    else:
        text = _format_number(float(value))
    return {"label": label, "value": text, "unit": unit}


def _range_text(values: list[float]) -> str | None:
    if not values:
        return None
    return f"{_format_number(min(values))} - {_format_number(max(values))}"


def _render_metric_cards(metrics: list[dict[str, str]]) -> str:
    return "\n".join(
        (
            '<div class="metric-card">'
            f'<div class="metric-label">{html.escape(metric["label"])}</div>'
            f'<div class="metric-value">{html.escape(metric["value"])}'
            f' <span class="metric-unit">{html.escape(metric["unit"])}</span></div>'
            "</div>"
        )
        for metric in metrics
    )


def _render_meta_rows(meta: dict[str, str]) -> str:
    if not meta:
        return '<tr><td colspan="2">No metadata recorded.</td></tr>'
    return "".join(
        f"<tr><td>{html.escape(str(k))}</td><td>{html.escape(str(v))}</td></tr>"
        for k, v in sorted(meta.items())
    )


def _render_chart_tab(tab: str, search_placeholder: str) -> str:
    safe_tab = html.escape(tab)
    safe_placeholder = html.escape(search_placeholder)
    return f"""
  <section class="tab-panel" data-panel="{safe_tab}">
    <div class="toolbar">
      <input
        class="search-input"
        data-search="{safe_tab}"
        type="search"
        placeholder="{safe_placeholder}"
      >
      <label class="checkbox-label">
        <input data-show-flat="{safe_tab}" type="checkbox">
        Show flat traces
      </label>
      <button class="control-button" data-reset-tab="{safe_tab}">Reset tab</button>
    </div>
    <div id="charts-{safe_tab}"></div>
  </section>"""


def _json_script_payload(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (
        data.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _format_number(value: float) -> str:
    abs_value = abs(value)
    if abs_value != 0.0 and (abs_value < 0.001 or abs_value >= 100_000):
        return f"{value:.4g}"
    return f"{value:.6g}"


def _round_time(value: float) -> float:
    return round(value, 10)
