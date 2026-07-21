"""Tests for hydraulic solver numerical behavior."""

from __future__ import annotations

import pytest
from chemunited_core.common.constant import ATMOSPHERE_PRESSURE_PA
from chemunited_core.components.enums import BoundaryConditionKind, InternalEdgeRole
from chemunited_core.components.internals import PortBoundaryCondition

from chemunited_sim.adapter.models import HydraulicEdge, HydraulicGraph, HydraulicNode
from chemunited_sim.hydraulics import solve


def _node(
    node_id: str,
    boundary: PortBoundaryCondition | None = None,
    *,
    is_hub: bool = False,
) -> HydraulicNode:
    return HydraulicNode(
        node_id=node_id,
        boundary=boundary,
        is_hub=is_hub,
        component=node_id.split(".")[0],
    )


def _edge(
    edge_id: str,
    origin: str,
    destination: str,
    *,
    role: InternalEdgeRole = InternalEdgeRole.TRANSPORT,
    forced_flow: float | None = None,
) -> HydraulicEdge:
    return HydraulicEdge(
        edge_id=edge_id,
        origin_node_id=origin,
        destination_node_id=destination,
        length=0.02,
        diameter=0.0015,
        role=role,
        resistance_override=None,
        component=None,
        is_external=role != InternalEdgeRole.JUNCTION,
        forced_flow=forced_flow,
    )


def test_junction_star_preserves_external_flow_balance():
    graph = HydraulicGraph()
    graph.nodes["pressure"] = _node(
        "pressure",
        PortBoundaryCondition(BoundaryConditionKind.PRESSURE, 300_000.0),
    )
    graph.nodes["pump"] = _node(
        "pump",
        PortBoundaryCondition(BoundaryConditionKind.FLOW, 5.0e-8),
    )
    graph.nodes["sink"] = _node(
        "sink",
        PortBoundaryCondition(BoundaryConditionKind.PRESSURE, 101_325.0),
    )
    for node_id in ["j.0", "j.1", "j.2", "j.3"]:
        graph.nodes[node_id] = _node(node_id, is_hub=node_id == "j.0")

    graph.edges["pressure_to_j1"] = _edge("pressure_to_j1", "pressure", "j.1")
    graph.edges["pump_to_j2"] = _edge("pump_to_j2", "pump", "j.2")
    graph.edges["j3_to_sink"] = _edge("j3_to_sink", "j.3", "sink")
    graph.edges["j.1.0"] = _edge(
        "j.1.0",
        "j.1",
        "j.0",
        role=InternalEdgeRole.JUNCTION,
    )
    graph.edges["j.2.0"] = _edge(
        "j.2.0",
        "j.2",
        "j.0",
        role=InternalEdgeRole.JUNCTION,
    )
    graph.edges["j.3.0"] = _edge(
        "j.3.0",
        "j.3",
        "j.0",
        role=InternalEdgeRole.JUNCTION,
    )

    state = solve(graph)

    external_in = state.flows["pressure_to_j1"] + state.flows["pump_to_j2"]
    external_out = state.flows["j3_to_sink"]
    assert (external_in - external_out) * 1e9 == pytest.approx(0.0, abs=1e-3)
    assert state.flows["j.2.0"] * 1e9 == pytest.approx(50.0, abs=1e-3)


def test_forced_flow_edge_holds_exact_flow_against_adverse_pressure_gradient():
    """Regression test for the HPLCPump backward-flow bug.

    A forced-flow edge (an active Pump's own junction edge) must carry
    exactly its commanded flow, correctly signed, even on a single cold
    solve where the surrounding pressure gradient runs the opposite way
    (downstream pinned *higher* than upstream) -- the exact "pump working
    against back-pressure" scenario where the old dp/Q resistance-matching
    iteration could settle on the wrong sign for many ticks.
    """
    target_flow = 1.6666666666666667e-8  # 1 mL/min in m3/s

    graph = HydraulicGraph()
    graph.nodes["upstream"] = _node(
        "upstream",
        PortBoundaryCondition(BoundaryConditionKind.PRESSURE, 101_325.0),
    )
    graph.nodes["downstream"] = _node(
        "downstream",
        PortBoundaryCondition(BoundaryConditionKind.PRESSURE, 500_000.0),
    )
    graph.edges["pump.1.2"] = _edge(
        "pump.1.2",
        "upstream",
        "downstream",
        role=InternalEdgeRole.JUNCTION,
        forced_flow=target_flow,
    )

    state = solve(graph)

    assert state.flows["pump.1.2"] == pytest.approx(target_flow)


def test_forced_flow_edge_decouples_pressure_of_unconnected_sides():
    """A forced-flow edge is the *only* link between two otherwise-isolated
    subnetworks, neither pinned by any other Dirichlet BC. Each side must be
    anchored to atmosphere independently (mirroring a real pump, which
    genuinely decouples upstream/downstream absolute pressure), while the
    edge itself still carries exactly the forced flow.
    """
    target_flow = 5.0e-9

    graph = HydraulicGraph()
    graph.nodes["a.1"] = _node("a.1")
    graph.nodes["a.2"] = _node("a.2")
    graph.edges["pump.1.2"] = _edge(
        "pump.1.2",
        "a.1",
        "a.2",
        role=InternalEdgeRole.JUNCTION,
        forced_flow=target_flow,
    )

    state = solve(graph)

    assert state.pressures["a.1"] == pytest.approx(float(ATMOSPHERE_PRESSURE_PA))
    assert state.pressures["a.2"] == pytest.approx(float(ATMOSPHERE_PRESSURE_PA))
    assert state.flows["pump.1.2"] == pytest.approx(target_flow)
