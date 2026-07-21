"""Tests for adapter graph compilation details."""

from __future__ import annotations

import pytest
from chemunited_core.common.constant import ATMOSPHERE_PRESSURE_PA
from chemunited_core.common.enums import ConnectionType
from chemunited_core.components.enums import BoundaryConditionKind
from chemunited_core.connections import EdgeData, EdgeMode
from chemunited_core.figure_registry import COMPONENTS

from chemunited_sim.adapter import compile_graph, resync_component


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


def test_autosampler_vial_movement_contact_compiles_as_hydraulic_junction():
    gantry_defn = COMPONENTS["Gantry3D"]
    vial_defn = COMPONENTS["Vial"]
    gantry = gantry_defn.data_class.from_mode(
        gantry_defn.mode_class(
            name="as", position_x="1", position_y="A", position_z="DOWN"
        )
    )
    vial = vial_defn.data_class.from_mode(
        vial_defn.mode_class(name="tray", column=3, row=2, pressure_access=False)
    )
    contact = EdgeData.from_mode(
        EdgeMode(
            name="tray_1_as_2",
            origin="tray",
            origin_port=1,
            destination="as",
            destination_port=2,
            classification=ConnectionType.MOVEMENT,
        )
    )

    graph = compile_graph([gantry, vial], [contact])

    assert "as.1" in graph.nodes
    assert "as.2" in graph.nodes
    assert "tray.1" in graph.nodes
    assert "tray.A1" in graph.nodes
    assert graph.nodes["as.1"].boundary is None
    assert graph.nodes["tray.1"].boundary is not None
    assert graph.edges["as.1.2"].resistance_override is None
    assert graph.edges["as.1.3"].resistance_override is not None
    assert graph.edges["tray.1.A1"].origin_node_id == "tray.1"
    assert graph.edges["tray_1_as_2"].role.name == "JUNCTION"
    assert graph.edges["tray_1_as_2"].length == 0.0
    assert graph.edges["tray_1_as_2"].diameter == 0.0
    assert "tray.A1" in graph.inventory_nodes


def test_resync_component_updates_gantry_head_boundary():
    gantry_defn = COMPONENTS["Gantry3D"]
    gantry = gantry_defn.data_class.from_mode(
        gantry_defn.mode_class(name="as", position_z="UP")
    )
    graph = compile_graph([gantry], [])

    boundary = graph.nodes["as.1"].boundary
    assert boundary is not None
    assert boundary.kind == BoundaryConditionKind.PRESSURE
    assert boundary.value == ATMOSPHERE_PRESSURE_PA

    gantry.apply("set_z_position", position="DOWN")
    resync_component(graph, gantry)

    assert graph.nodes["as.1"].boundary is None

    gantry.apply("set_z_position", position="UP")
    resync_component(graph, gantry)

    boundary = graph.nodes["as.1"].boundary
    assert boundary is not None
    assert boundary.kind == BoundaryConditionKind.PRESSURE
    assert boundary.value == ATMOSPHERE_PRESSURE_PA


def test_hplc_pump_compiles_forced_flow_onto_its_junction_edge():
    pump_defn = COMPONENTS["HPLCPump"]
    pump = pump_defn.data_class.from_mode(pump_defn.mode_class(name="hplcpump"))
    pump.apply("infuse", rate="1 ml/min")

    graph = compile_graph([pump], [])

    edge = graph.edges["hplcpump.1.2"]
    assert edge.resistance_override is None
    assert edge.forced_flow == pytest.approx(
        pump.internal_edges[(1, 2)].forced_flow_override
    )
    assert edge.forced_flow > 0.0


def test_resync_component_propagates_pump_forced_flow():
    """Regression test for the HPLCPump backward-flow bug's second cause:
    resync_component() must propagate forced_flow (not just
    resistance_override), or the very next solve after a command sees a
    stale/None forced_flow on an already-open edge.
    """
    pump_defn = COMPONENTS["HPLCPump"]
    pump = pump_defn.data_class.from_mode(pump_defn.mode_class(name="hplcpump"))
    graph = compile_graph([pump], [])

    edge = graph.edges["hplcpump.1.2"]
    assert edge.resistance_override is not None
    assert edge.forced_flow is None

    pump.apply("infuse", rate="2 ml/min")
    resync_component(graph, pump)

    assert edge.resistance_override is None
    assert edge.forced_flow == pytest.approx(
        pump.internal_edges[(1, 2)].forced_flow_override
    )
    assert edge.forced_flow > 0.0

    pump.apply("stop")
    resync_component(graph, pump)

    assert edge.resistance_override is not None
    assert edge.forced_flow is None


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
