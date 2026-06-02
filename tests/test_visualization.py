"""Tests for pyvis visualization and API endpoint behavior."""

from __future__ import annotations

import queue
import shutil
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest
from chemunited_core.common.enums import PhaseKind
from chemunited_core.components.enums import InternalEdgeRole
from fastapi.testclient import TestClient

from chemunited_sim.adapter.models import HydraulicEdge, HydraulicGraph, HydraulicNode
from chemunited_sim.cli import server
from chemunited_sim.cli.clock import SimClock
from chemunited_sim.cli.loader import ProjectState
from chemunited_sim.cli.server import SimStatus, SimulationState, app
from chemunited_sim.hydraulics.models import HydraulicState
from chemunited_sim.inventory.models import InventoryState
from chemunited_sim.recorder import Recorder
from chemunited_sim.transport.models import Pocket, TransportState
from chemunited_sim.visualization import (
    EdgeCellSnapshot,
    InventorySnapshot,
    NoSnapshotsError,
    VisualizationSnapshot,
    load_latest_snapshot,
    render_pyvis_html,
)
from chemunited_sim.worker import SimConfig


@pytest.fixture
def workspace_tmp(request) -> Path:
    safe_name = request.node.name.replace("/", "_").replace("\\", "_")
    path = Path("examples") / "simulation" / "test_visualization_tmp" / safe_name
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _make_node(node_id: str, component: str = "comp") -> HydraulicNode:
    return HydraulicNode(
        node_id=node_id,
        boundary=None,
        is_hub=False,
        component=component,
    )


def _make_edge(
    edge_id: str,
    origin: str,
    dest: str,
    *,
    flow_component: str = "comp",
) -> HydraulicEdge:
    return HydraulicEdge(
        edge_id=edge_id,
        origin_node_id=origin,
        destination_node_id=dest,
        length=1.0,
        diameter=0.004,
        role=InternalEdgeRole.TRANSPORT,
        resistance_override=None,
        component=flow_component,
        is_external=False,
    )


def _simple_graph() -> HydraulicGraph:
    graph = HydraulicGraph()
    graph.nodes["n0"] = _make_node("n0")
    graph.nodes["n1"] = _make_node("n1")
    graph.edges["e1"] = _make_edge("e1", "n0", "n1")
    return graph


def _transport_state(graph: HydraulicGraph) -> TransportState:
    edge_queues = {}
    for eid, edge in graph.edges.items():
        pocket = Pocket(
            phase_kind=PhaseKind.GAS,
            volume=1.0e-6,
            species_moles={},
            temperature=298.15,
            pressure=101_325.0,
        )
        edge_queues[eid] = deque([pocket])
    return TransportState(edge_queues=edge_queues)


def _inventory_state(node_id: str = "v1.Inventory") -> InventoryState:
    return InventoryState(
        node_id=node_id,
        capacity=1.0e-3,
        pressure=200_000.0,
        temperature=298.15,
        liq_volume=5.0e-4,
        gas_volume=5.0e-4,
        liq_species_moles={"water": 10.0},
        gas_species_moles={},
    )


def _write_recorded_db(db_path: Path, graph: HydraulicGraph) -> None:
    rec = Recorder(db_path=db_path, graph=graph, dt=0.1, record_interval=1.0)
    try:
        rec.record(
            0.0,
            HydraulicState(
                pressures={"n0": 200_000.0, "n1": 101_325.0},
                flows={"e1": 1.0e-6},
            ),
            _transport_state(graph),
            {"v1.Inventory": _inventory_state()},
        )
        rec.record(
            1.0,
            HydraulicState(
                pressures={"n0": 210_000.0, "n1": 101_325.0},
                flows={"e1": -2.0e-6},
            ),
            _transport_state(graph),
            {"v1.Inventory": _inventory_state()},
        )
    finally:
        rec.close()


def test_load_latest_snapshot_reads_latest_time(workspace_tmp):
    graph = _simple_graph()
    db_path = workspace_tmp / "run.db"
    _write_recorded_db(db_path, graph)

    snapshot = load_latest_snapshot(db_path)

    assert snapshot.time == pytest.approx(1.0)
    assert snapshot.pressures["n0"] == pytest.approx(210_000.0)
    assert snapshot.flows["e1"] == pytest.approx(-2.0e-6)
    assert snapshot.inventories["v1.Inventory"].species_moles["liquid"]["water"] == (
        pytest.approx(10.0)
    )


def test_load_latest_snapshot_raises_for_db_without_snapshots(workspace_tmp):
    graph = _simple_graph()
    db_path = workspace_tmp / "empty.db"
    rec = Recorder(db_path=db_path, graph=graph, dt=0.1)
    rec.close()

    with pytest.raises(NoSnapshotsError):
        load_latest_snapshot(db_path)


def test_render_pyvis_html_contains_graph_and_reversed_flow():
    graph = _simple_graph()
    snapshot = load_latest_snapshot_from_rows(
        pressures={"n0": 200_000.0, "n1": 101_325.0},
        flows={"e1": -1.0e-6},
    )

    html = render_pyvis_html(
        graph=graph,
        components=[SimpleNamespace(name="comp")],
        snapshot=snapshot,
    )

    assert "vis-network" in html or "vis.js" in html
    assert "n0" in html
    assert "n1" in html
    assert "e1" in html
    assert '"from": "n1"' in html
    assert '"to": "n0"' in html


def test_render_pyvis_html_scales_flow_pressure_and_temperature():
    graph = _simple_graph()
    graph.nodes["v1.Inventory"] = _make_node("v1.Inventory")
    graph.inventory_nodes["v1.Inventory"] = SimpleNamespace()
    graph.nodes["n2"] = _make_node("n2")
    graph.edges["e2"] = _make_edge("e2", "n1", "n2")

    snapshot = VisualizationSnapshot(
        time=1.0,
        pressures={
            "n0": 100_000.0,
            "n1": 200_000.0,
            "n2": 150_000.0,
            "v1.Inventory": 200_000.0,
        },
        flows={"e1": 1.0e-6, "e2": 4.0e-6},
        inventories={
            "v1.Inventory": InventorySnapshot(
                pressure=200_000.0,
                temperature=350.0,
            )
        },
        edge_cells={
            "e1": EdgeCellSnapshot(cell_count=1, average_temperature=300.0),
            "e2": EdgeCellSnapshot(cell_count=1, average_temperature=350.0),
        },
    )

    html = render_pyvis_html(
        graph=graph,
        components=[SimpleNamespace(name="comp")],
        snapshot=snapshot,
    )

    assert '"background": "#E8F1FF"' in html
    assert '"background": "#08519C"' in html
    assert '"border": "#B2182B"' in html
    assert '"color": "#FFF0F0"' in html
    assert '"color": "#B2182B"' in html
    assert '"width": 4.5' in html
    assert '"width": 8.0' in html


def load_latest_snapshot_from_rows(
    *,
    pressures: dict[str, float],
    flows: dict[str, float],
):
    return VisualizationSnapshot(time=1.0, pressures=pressures, flows=flows)


def _set_server_state(
    workspace_tmp: Path,
    *,
    project: ProjectState | None = None,
    db_path: Path | None = None,
) -> None:
    server._state = SimulationState(
        sim_status=SimStatus.IDLE if project is not None else SimStatus.NO_PROJECT,
        current_t=0.0,
        config=SimConfig(),
        db_path=db_path,
        project=project,
        clock=SimClock(),
        cmd_queue=queue.Queue(),
        db_dir=workspace_tmp,
    )


def _project(workspace_tmp: Path, graph: HydraulicGraph) -> ProjectState:
    component = SimpleNamespace(name="comp")
    return ProjectState(
        project_path=workspace_tmp,
        processes={},
        configs={},
        components={"comp": component},
        graph=graph,
    )


def test_visualization_endpoint_requires_project(workspace_tmp):
    _set_server_state(workspace_tmp, project=None, db_path=None)

    response = TestClient(app).get("/simulation/visualization")

    assert response.status_code == 404


def test_visualization_endpoint_requires_db(workspace_tmp):
    graph = _simple_graph()
    _set_server_state(
        workspace_tmp,
        project=_project(workspace_tmp, graph),
        db_path=None,
    )

    response = TestClient(app).get("/simulation/visualization")

    assert response.status_code == 404


def test_visualization_endpoint_rejects_db_without_snapshots(workspace_tmp):
    graph = _simple_graph()
    db_path = workspace_tmp / "empty.db"
    rec = Recorder(db_path=db_path, graph=graph, dt=0.1)
    rec.close()
    _set_server_state(
        workspace_tmp,
        project=_project(workspace_tmp, graph),
        db_path=db_path,
    )

    response = TestClient(app).get("/simulation/visualization")

    assert response.status_code == 409


def test_visualization_endpoint_returns_inline_html(workspace_tmp):
    graph = _simple_graph()
    db_path = workspace_tmp / "run.db"
    _write_recorded_db(db_path, graph)
    _set_server_state(
        workspace_tmp,
        project=_project(workspace_tmp, graph),
        db_path=db_path,
    )

    response = TestClient(app).get("/simulation/visualization")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "n0" in response.text
    assert "e1" in response.text


def test_visualization_endpoint_rejects_unreadable_db(workspace_tmp):
    graph = _simple_graph()
    db_path = workspace_tmp / "not_sqlite.db"
    db_path.write_text("not sqlite", encoding="utf-8")
    _set_server_state(
        workspace_tmp,
        project=_project(workspace_tmp, graph),
        db_path=db_path,
    )

    response = TestClient(app).get("/simulation/visualization")

    assert response.status_code == 422
