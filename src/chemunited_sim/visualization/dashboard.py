"""HTML dashboard generator for chemunited-sim recorded data."""

from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from pathlib import Path

from .pyvis_export import NoSnapshotsError, SnapshotReadError

_PLOTLY_CDN = "https://cdn.plot.ly/plotly-2.35.2.min.js"

_PA_TO_BAR = 1e-5
_M3S_TO_NLS = 1e9


def render_dashboard_html(db_path: str | os.PathLike) -> str:
    """Read all recorded time series from *db_path* and return a standalone HTML dashboard."""
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

    return _build_html(path.stem, meta, node_pressures, edge_flows, inv_states, species_moles, cell_temps)


# ---------------------------------------------------------------------------
# DB readers
# ---------------------------------------------------------------------------


def _read_meta(conn: sqlite3.Connection) -> dict[str, str]:
    return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM meta")}


def _read_node_pressures(conn: sqlite3.Connection) -> dict[str, list]:
    """Returns {node_id: [(time, pressure_bar), ...]}"""
    series: dict[str, list] = defaultdict(list)
    for row in conn.execute(
        "SELECT time, node_id, pressure FROM node_pressure ORDER BY time"
    ):
        series[row["node_id"]].append((float(row["time"]), float(row["pressure"]) * _PA_TO_BAR))
    return dict(series)


def _read_edge_flows(conn: sqlite3.Connection) -> dict[str, list]:
    """Returns {edge_id: [(time, flow_nls), ...]}"""
    series: dict[str, list] = defaultdict(list)
    for row in conn.execute(
        "SELECT time, edge_id, flow_rate FROM edge_flow ORDER BY time"
    ):
        series[row["edge_id"]].append((float(row["time"]), float(row["flow_rate"]) * _M3S_TO_NLS))
    return dict(series)


def _read_inventory_states(conn: sqlite3.Connection) -> dict[str, list]:
    """Returns {node_id: [(time, pressure_bar, temperature_K), ...]}"""
    series: dict[str, list] = defaultdict(list)
    for row in conn.execute(
        "SELECT time, node_id, pressure, temperature FROM inventory_state ORDER BY time"
    ):
        series[row["node_id"]].append(
            (float(row["time"]), float(row["pressure"]) * _PA_TO_BAR, float(row["temperature"]))
        )
    return dict(series)


def _read_species_moles(conn: sqlite3.Connection) -> dict[str, dict[str, list]]:
    """Returns {(node_id, phase): {species_id: [(time, moles), ...]}}"""
    series: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for row in conn.execute(
        """
        SELECT time, node_id, phase, species_id, moles
        FROM inventory_content
        WHERE species_id != '__carrier__'
        ORDER BY time
        """
    ):
        key = f"{row['node_id']} / {row['phase']}"
        series[key][row["species_id"]].append((float(row["time"]), float(row["moles"])))
    return {k: dict(v) for k, v in series.items()}


def _read_cell_temperatures(conn: sqlite3.Connection) -> dict[str, list]:
    """Returns {edge_id: [(time, avg_temp_K), ...]}"""
    series: dict[str, list] = defaultdict(list)
    for row in conn.execute(
        """
        SELECT time, edge_id, AVG(temperature) AS avg_temp
        FROM cell_state
        GROUP BY time, edge_id
        ORDER BY time
        """
    ):
        if row["avg_temp"] is not None:
            series[row["edge_id"]].append((float(row["time"]), float(row["avg_temp"])))
    return dict(series)


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
    all_times = [t for pts in node_pressures.values() for t, _ in pts]
    t_min = min(all_times, default=0.0)
    t_max = max(all_times, default=0.0)

    charts_html = []

    charts_html += _make_line_chart(
        chart_id="node_pressures",
        title="Node Pressures",
        series={nid: ([t for t, _ in pts], [p for _, p in pts]) for nid, pts in node_pressures.items()},
        x_label="Simulation time (s)",
        y_label="Pressure (bar)",
    )

    charts_html += _make_line_chart(
        chart_id="edge_flows",
        title="Edge Flow Rates",
        series={eid: ([t for t, _ in pts], [f for _, f in pts]) for eid, pts in edge_flows.items()},
        x_label="Simulation time (s)",
        y_label="Flow rate (nL/s)",
    )

    if inv_states:
        charts_html += _make_line_chart(
            chart_id="inv_temperatures",
            title="Inventory Temperatures",
            series={nid: ([t for t, _, _ in pts], [T for _, _, T in pts]) for nid, pts in inv_states.items()},
            x_label="Simulation time (s)",
            y_label="Temperature (K)",
        )
        charts_html += _make_line_chart(
            chart_id="inv_pressures",
            title="Inventory Pressures",
            series={nid: ([t for t, _, _ in pts], [p for _, p, _ in pts]) for nid, pts in inv_states.items()},
            x_label="Simulation time (s)",
            y_label="Pressure (bar)",
        )

    for i, (group_key, species_dict) in enumerate(sorted(species_moles.items())):
        safe_id = f"species_{i}"
        charts_html += _make_line_chart(
            chart_id=safe_id,
            title=f"Species Moles — {group_key}",
            series={sid: ([t for t, _ in pts], [m for _, m in pts]) for sid, pts in species_dict.items()},
            x_label="Simulation time (s)",
            y_label="Moles (mol)",
        )

    if cell_temps:
        charts_html += _make_line_chart(
            chart_id="cell_temps",
            title="Edge Average Cell Temperature",
            series={eid: ([t for t, _ in pts], [T for _, T in pts]) for eid, pts in cell_temps.items()},
            x_label="Simulation time (s)",
            y_label="Temperature (K)",
        )

    meta_rows = "".join(
        f"<tr><td>{k}</td><td>{v}</td></tr>"
        for k, v in sorted(meta.items())
    )

    charts_block = "\n".join(charts_html)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dashboard — {stem}</title>
<script src="{_PLOTLY_CDN}"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: Arial, sans-serif; background: #f5f5f5; color: #333; }}
  header {{
    background: #08519C; color: #fff;
    padding: 18px 32px;
    display: flex; align-items: center; gap: 16px;
  }}
  header h1 {{ font-size: 1.4rem; font-weight: 600; }}
  header span {{ font-size: 0.9rem; opacity: 0.8; }}
  .content {{ max-width: 1400px; margin: 24px auto; padding: 0 24px; }}
  .meta-card {{
    background: #fff; border-radius: 8px;
    box-shadow: 0 1px 4px rgba(0,0,0,.12);
    padding: 20px 24px; margin-bottom: 24px;
  }}
  .meta-card h2 {{ font-size: 1rem; margin-bottom: 12px; color: #08519C; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 0.85rem; }}
  td {{ padding: 5px 12px; border-bottom: 1px solid #eee; }}
  td:first-child {{ font-weight: 600; width: 180px; color: #555; }}
  .chart-card {{
    background: #fff; border-radius: 8px;
    box-shadow: 0 1px 4px rgba(0,0,0,.12);
    padding: 16px 20px; margin-bottom: 24px;
  }}
  .chart-card h2 {{ font-size: 1rem; margin-bottom: 10px; color: #08519C; }}
  .plotly-chart {{ width: 100%; height: 340px; }}
</style>
</head>
<body>
<header>
  <h1>chemunited-sim — {stem}</h1>
  <span>t: {t_min:.4g} s → {t_max:.4g} s</span>
</header>
<div class="content">
  <div class="meta-card">
    <h2>Simulation Metadata</h2>
    <table><tbody>{meta_rows}</tbody></table>
  </div>
  {charts_block}
</div>
</body>
</html>"""


def _make_line_chart(
    *,
    chart_id: str,
    title: str,
    series: dict[str, tuple[list, list]],
    x_label: str,
    y_label: str,
) -> list[str]:
    if not series:
        return []

    traces = []
    for name, (xs, ys) in sorted(series.items()):
        traces.append({"x": xs, "y": ys, "mode": "lines", "name": name, "type": "scatter"})

    layout = {
        "xaxis": {"title": x_label},
        "yaxis": {"title": y_label},
        "legend": {"orientation": "h", "y": -0.2},
        "margin": {"t": 10, "b": 60, "l": 60, "r": 20},
        "hovermode": "x unified",
    }

    traces_json = json.dumps(traces)
    layout_json = json.dumps(layout)

    return [
        f'<div class="chart-card">',
        f'  <h2>{title}</h2>',
        f'  <div id="{chart_id}" class="plotly-chart"></div>',
        f'  <script>Plotly.newPlot("{chart_id}", {traces_json}, {layout_json}, {{responsive: true}});</script>',
        f'</div>',
    ]
