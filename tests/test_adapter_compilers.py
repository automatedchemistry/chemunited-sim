"""Tests for adapter graph compilation details."""

from __future__ import annotations

import pytest
from chemunited_core.common.enums import ConnectionType
from chemunited_core.connections import EdgeData, EdgeMode
from chemunited_core.figure_registry import COMPONENTS

from chemunited_sim.adapter import compile_graph


def test_solenoid_valve_2_way_common_port_compiles_as_hub():
    defn = COMPONENTS["SolenoidValve2Way"]
    valve = defn.data_class.from_mode(defn.mode_class(name="divertvalve"))

    graph = compile_graph([valve], [])

    assert sorted(
        node_id for node_id in graph.nodes if node_id.startswith("divertvalve.")
    ) == ["divertvalve.0", "divertvalve.1", "divertvalve.2"]
    assert graph.nodes["divertvalve.0"].is_hub is True
    assert graph.nodes["divertvalve.1"].is_hub is False
    assert graph.nodes["divertvalve.2"].is_hub is False


def test_heat_connection_compiles_as_heat_link_not_hydraulic_edge():
    controller_defn = COMPONENTS["TemperatureControl"]
    vessel_defn = COMPONENTS["GlassBottle"]
    controller = controller_defn.data_class.from_mode(
        controller_defn.mode_class(name="chiller")
    )
    vessel = vessel_defn.data_class.from_mode(
        vessel_defn.mode_class(name="reactor", heat_exchange=True)
    )
    edge = EdgeData.from_mode(
        EdgeMode(
            name="thermal_link",
            origin="chiller",
            origin_port=1,
            destination="reactor",
            destination_port=2,
            classification=ConnectionType.HEAT,
        )
    )

    graph = compile_graph([controller, vessel], [edge])

    assert len(graph.heat_links) == 1
    link = graph.heat_links[0]
    assert link.provider_component == "chiller"
    assert link.provider_port == 1
    assert link.target_component == "reactor"
    assert link.target_port == 2
    assert "thermal_link" not in graph.edges
    assert "chiller.1" not in graph.nodes


def test_heat_connection_rejects_non_heat_endpoint():
    controller_defn = COMPONENTS["TemperatureControl"]
    vessel_defn = COMPONENTS["GlassBottle"]
    controller = controller_defn.data_class.from_mode(
        controller_defn.mode_class(name="chiller")
    )
    vessel = vessel_defn.data_class.from_mode(
        vessel_defn.mode_class(name="reactor", heat_exchange=True)
    )
    edge = EdgeData.from_mode(
        EdgeMode(
            name="bad_thermal_link",
            origin="chiller",
            origin_port=1,
            destination="reactor",
            destination_port=1,
            classification=ConnectionType.HEAT,
        )
    )

    with pytest.raises(ValueError, match="not a HEAT port"):
        compile_graph([controller, vessel], [edge])
