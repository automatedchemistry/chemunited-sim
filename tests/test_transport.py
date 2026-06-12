"""Tests for transport pocket routing."""

from __future__ import annotations

import math
from collections import deque

import pytest
from chemunited_core.common.enums import PhaseKind
from chemunited_core.components.enums import InternalEdgeRole

from chemunited_sim.adapter.models import HydraulicEdge, HydraulicGraph, HydraulicNode
from chemunited_sim.hydraulics.models import HydraulicState
from chemunited_sim.transport.engine import advance, inject
from chemunited_sim.transport.models import Pocket, TransportState


def _node(node_id: str, *, is_hub: bool = False) -> HydraulicNode:
    return HydraulicNode(
        node_id=node_id,
        boundary=None,
        is_hub=is_hub,
        component=None,
    )


def _edge(
    edge_id: str,
    origin: str,
    destination: str,
    *,
    role: InternalEdgeRole = InternalEdgeRole.TRANSPORT,
) -> HydraulicEdge:
    return HydraulicEdge(
        edge_id=edge_id,
        origin_node_id=origin,
        destination_node_id=destination,
        length=1.0,
        diameter=1.0e-3 if role == InternalEdgeRole.TRANSPORT else 0.0,
        role=role,
        resistance_override=None,
        component=None,
        is_external=role == InternalEdgeRole.TRANSPORT,
    )


def _hot_pocket(volume: float = 1.0e-6) -> Pocket:
    return Pocket(
        phase_kind=PhaseKind.LIQUID,
        volume=volume,
        species_moles={"tracer": 2.0},
        temperature=315.0,
        pressure=123_456.0,
    )


def _bpr_like_graph() -> HydraulicGraph:
    graph = HydraulicGraph()
    for node_id in ("src", "bpr.1", "bpr.2", "sink"):
        graph.nodes[node_id] = _node(node_id)
    graph.edges["upstream"] = _edge("upstream", "src", "bpr.1")
    graph.edges["bpr.1.2"] = _edge(
        "bpr.1.2",
        "bpr.1",
        "bpr.2",
        role=InternalEdgeRole.JUNCTION,
    )
    graph.edges["downstream"] = _edge("downstream", "bpr.2", "sink")
    return graph


def test_non_hub_pass_through_routes_pocket_to_downstream_transport() -> None:
    graph = _bpr_like_graph()
    pocket = _hot_pocket()
    state = TransportState(
        edge_queues={
            "upstream": deque([pocket]),
            "downstream": deque(),
        }
    )
    hyd_state = HydraulicState(
        pressures={},
        flows={
            "upstream": 1.0e-5,
            "bpr.1.2": 1.0e-5,
            "downstream": 1.0e-5,
        },
    )

    result = advance(graph, hyd_state, state, dt=0.1)

    assert result.arrivals == {}
    assert list(result.next_state.edge_queues["upstream"]) == []
    routed = list(result.next_state.edge_queues["downstream"])
    assert len(routed) == 1
    assert routed[0].phase_kind == PhaseKind.LIQUID
    assert routed[0].volume == pytest.approx(pocket.volume)
    assert routed[0].species_moles == pytest.approx(pocket.species_moles)
    assert routed[0].temperature == pytest.approx(315.0)
    assert routed[0].pressure == pytest.approx(123_456.0)


def test_reverse_non_hub_pass_through_injects_at_destination_end() -> None:
    graph = _bpr_like_graph()
    hot = _hot_pocket()
    cold = Pocket(
        phase_kind=PhaseKind.LIQUID,
        volume=2.0e-6,
        species_moles={"carrier": 1.0},
        temperature=298.15,
        pressure=101_325.0,
    )
    state = TransportState(
        edge_queues={
            "upstream": deque([cold]),
            "downstream": deque([hot]),
        }
    )
    hyd_state = HydraulicState(
        pressures={},
        flows={
            "upstream": -1.0e-5,
            "bpr.1.2": -1.0e-5,
            "downstream": -1.0e-5,
        },
    )

    result = advance(graph, hyd_state, state, dt=0.1)

    upstream = list(result.next_state.edge_queues["upstream"])
    assert len(upstream) == 2
    assert upstream[0].temperature == pytest.approx(315.0)
    assert upstream[0].species_moles == pytest.approx(hot.species_moles)
    assert upstream[1].temperature == pytest.approx(298.15)
    assert upstream[1].volume == pytest.approx(1.0e-6)
    assert list(result.next_state.edge_queues["downstream"]) == []


def test_dead_end_without_outgoing_transport_still_discards_pocket() -> None:
    graph = HydraulicGraph()
    graph.nodes["src"] = _node("src")
    graph.nodes["dead"] = _node("dead")
    graph.edges["upstream"] = _edge("upstream", "src", "dead")
    state = TransportState(edge_queues={"upstream": deque([_hot_pocket()])})
    hyd_state = HydraulicState(pressures={}, flows={"upstream": 1.0e-5})

    result = advance(graph, hyd_state, state, dt=0.1)

    assert result.arrivals == {}
    assert list(result.next_state.edge_queues["upstream"]) == []


def test_inject_pads_underfilled_transport_edge_with_air_carrier() -> None:
    graph = HydraulicGraph()
    graph.nodes["src"] = _node("src")
    graph.nodes["dst"] = _node("dst")
    graph.edges["edge"] = _edge("edge", "src", "dst")
    state = TransportState(edge_queues={"edge": deque()})
    hyd_state = HydraulicState(
        pressures={"src": 150_000.0},
        flows={"edge": 1.0e-6},
    )

    result = inject(state, {}, hyd_state, graph)

    queue = list(result.edge_queues["edge"])
    assert len(queue) == 1
    assert queue[0].phase_kind == PhaseKind.GAS
    assert queue[0].species_moles["air"] > 0.0
    assert queue[0].pressure == pytest.approx(150_000.0)
    expected_volume = math.pi * (graph.edges["edge"].diameter / 2.0) ** 2
    assert queue[0].volume == pytest.approx(expected_volume)
