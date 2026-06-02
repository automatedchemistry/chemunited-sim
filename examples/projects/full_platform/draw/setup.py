"""Platform setup: gas-liquid pressurised flow-through reactor."""

from __future__ import annotations

from chemunited_core.common.constant import ATMOSPHERE_PRESSURE_PA
from chemunited_core.common.enums import PhaseKind
from chemunited_core.components import (
    BackPressureRegulatorData,
    BackPressureRegulatorMode,
    FlowSourceData,
    FlowSourceMode,
    JunctionData,
    JunctionMode,
    PlugFlowComponentData,
    PlugFlowMode,
    PressureControlData,
    PressureControlMode,
    ValveComponentData,
    ValveMode,
    VesselComponentData,
    VesselMode,
)
from chemunited_core.compounds import VolumeContentBase
from chemunited_core.utils.internal_quantity import ChemUnitQuantity

from chemunited_sim.adapter import HydraulicNode


def build_draw(platform) -> None:
    # ── Chemistry ──────────────────────────────────────────────────────────────
    platform.add_compound(name="reagent_a", molecular_weight=120.0, cp_liquid=150.0, density_liquid=1050.0)
    platform.add_compound(name="product_b", molecular_weight=120.0, cp_liquid=150.0, density_liquid=1020.0)
    platform.add_compound(name="solvent", molecular_weight=78.0, cp_liquid=130.0, density_liquid=880.0)
    platform.add_compound(name="nitrogen", molecular_weight=28.0, cp_gas=29.0)

    # ── Components ─────────────────────────────────────────────────────────────
    platform.add_component(name="gassupply", figure="PressureControl", setpoint="3 bar")
    platform.add_component(name="liquidpump", figure="FlowSource", flow_rate="3 ml/min")
    platform.add_component(name="productsink", figure="PressureControl", setpoint="1 bar")
    platform.add_component(name="wastesink", figure="PressureControl", setpoint="1 bar")
    platform.add_component(name="gastube", figure="PlugFlow", length="10 cm", diameter="1.5 mm")
    platform.add_component(name="liquidtube", figure="PlugFlow", length="10 cm", diameter="1.5 mm")
    platform.add_component(name="reactortube", figure="PlugFlow", length="5 cm", diameter="2 mm")
    platform.add_component(name="outlettube", figure="PlugFlow", length="20 cm", diameter="1.5 mm")
    platform.add_component(name="collecttube", figure="PlugFlow", length="10 cm", diameter="1.5 mm")
    platform.add_component(name="wastetube", figure="PlugFlow", length="10 cm", diameter="1.5 mm")
    platform.add_component(name="tmixer", figure="Junction", number_ports=3)
    platform.add_component(name="reactor", figure="Vessel", capacity="10 ml", top_access=1, bottom_access=1)
    platform.add_component(name="bpr", figure="BackPressureRegulator", setpoint="2 bar")
    platform.add_component(name="divertvalve", figure="Valve", stator_ports=[(1, 2), (0,)], rotor_ports=[(7, None), (7,)])
    
    platform.add_component_data(PressureControlData.from_mode(
        PressureControlMode(name="gassupply", setpoint=ChemUnitQuantity("3 bar"))
    ))
    platform.add_component_data(FlowSourceData.from_mode(
        FlowSourceMode(name="liquidpump", flow_rate=ChemUnitQuantity("3 ml/min"))
    ))
    platform.add_component_data(PressureControlData.from_mode(
        PressureControlMode(name="productsink", setpoint=ChemUnitQuantity("1 bar"))
    ))
    platform.add_component_data(PressureControlData.from_mode(
        PressureControlMode(name="wastesink", setpoint=ChemUnitQuantity("1 bar"))
    ))
    platform.add_component_data(PlugFlowComponentData.from_mode(
        PlugFlowMode(name="gastube", length=ChemUnitQuantity("10 cm"), diameter=ChemUnitQuantity("1.5 mm"))
    ))
    platform.add_component_data(PlugFlowComponentData.from_mode(
        PlugFlowMode(name="liquidtube", length=ChemUnitQuantity("10 cm"), diameter=ChemUnitQuantity("1.5 mm"))
    ))
    platform.add_component_data(PlugFlowComponentData.from_mode(
        PlugFlowMode(name="reactortube", length=ChemUnitQuantity("5 cm"), diameter=ChemUnitQuantity("2 mm"))
    ))
    platform.add_component_data(PlugFlowComponentData.from_mode(
        PlugFlowMode(name="outlettube", length=ChemUnitQuantity("20 cm"), diameter=ChemUnitQuantity("1.5 mm"))
    ))
    platform.add_component_data(PlugFlowComponentData.from_mode(
        PlugFlowMode(name="collecttube", length=ChemUnitQuantity("10 cm"), diameter=ChemUnitQuantity("1.5 mm"))
    ))
    platform.add_component_data(PlugFlowComponentData.from_mode(
        PlugFlowMode(name="wastetube", length=ChemUnitQuantity("10 cm"), diameter=ChemUnitQuantity("1.5 mm"))
    ))
    platform.add_component_data(JunctionData.from_mode(
        JunctionMode(name="tmixer", number_ports=3)
    ))

    # Reactor vessel with initial liquid charge
    _OPERATING_P = 2.0 * ATMOSPHERE_PRESSURE_PA
    reactor = VesselComponentData.from_mode(
        VesselMode(name="reactor", capacity=ChemUnitQuantity("10 ml"), top_access=1, bottom_access=1)
    )
    reactor.internal_inventory.liq_content = VolumeContentBase(
        phase_kind=PhaseKind.LIQUID,
        volume=3.0e-6,
        initial_species={"reagent_a": 1.0e-4, "solvent": 3.4e-2},
        initial_pressure=_OPERATING_P,
        initial_temperature=298.15,
    )
    reactor.internal_inventory.gas_content = VolumeContentBase(
        phase_kind=PhaseKind.GAS,
        volume=7.0e-6,
        initial_species={"nitrogen": 4.3e-1},
        initial_pressure=_OPERATING_P,
        initial_temperature=298.15,
    )
    platform.add_component_data(reactor)

    platform.add_component_data(BackPressureRegulatorData.from_mode(
        BackPressureRegulatorMode(name="bpr", setpoint=ChemUnitQuantity("2 bar"))
    ))
    platform.add_component_data(ValveComponentData.from_mode(
        ValveMode(name="divertvalve", stator_ports=[(1, 2), (0,)], rotor_ports=[(7, None), (7,)])
    ))

    # ── Connections ────────────────────────────────────────────────────────────
    platform.add_connection("gassupply",  "gastube",     1, 1, length="2 cm", diameter="1.5 mm", name="egsgt")
    platform.add_connection("gastube",    "tmixer",      2, 1, length="2 cm", diameter="1.5 mm", name="egtmixer")
    platform.add_connection("liquidpump", "liquidtube",  1, 1, length="2 cm", diameter="1.5 mm", name="elplt")
    platform.add_connection("liquidtube", "tmixer",      2, 2, length="2 cm", diameter="1.5 mm", name="eltmixer")
    platform.add_connection("tmixer",     "reactortube", 3, 1, length="2 cm", diameter="2 mm",   name="emixerrt")
    platform.add_connection("reactortube","reactor",     2, 2, length="2 cm", diameter="2 mm",   name="ertrx")
    platform.add_connection("reactor",    "bpr",         1, 1, length="2 cm", diameter="1.5 mm", name="erxbpr")
    platform.add_connection("bpr",        "outlettube",  2, 1, length="2 cm", diameter="1.5 mm", name="ebprot")
    platform.add_connection("outlettube", "divertvalve", 2, 0, length="2 cm", diameter="1.5 mm", name="eotvalve")
    platform.add_connection("divertvalve","collecttube", 1, 1, length="2 cm", diameter="1.5 mm", name="evalvect")
    platform.add_connection("collecttube","productsink", 2, 1, length="2 cm", diameter="1.5 mm", name="ectproduct")
    platform.add_connection("divertvalve","wastetube",   2, 1, length="2 cm", diameter="1.5 mm", name="evalvewt")
    platform.add_connection("wastetube",  "wastesink",   2, 1, length="2 cm", diameter="1.5 mm", name="ewtsink")


def build_graph_patch(graph) -> None:
    """Mark the divert valve centre port as a hub so the transport engine routes pockets correctly."""
    old = graph.nodes["divertvalve.0"]
    graph.nodes["divertvalve.0"] = HydraulicNode(
        node_id=old.node_id,
        boundary=old.boundary,
        is_hub=True,
        component=old.component,
    )
