"""Platform setup: gas-liquid pressurised flow-through reactor."""

from chemunited_core.compounds import VolumeContentBase


def build_draw(platform) -> None:
    # ── Chemistry ──────────────────────────────────────────────────────────────
    platform.add_compound(
        name="reagent_a", molecular_weight=120.0, cp_liquid=150.0, density_liquid=1050.0
    )
    platform.add_compound(
        name="product_b", molecular_weight=120.0, cp_liquid=150.0, density_liquid=1020.0
    )
    platform.add_compound(
        name="solvent", molecular_weight=78.0, cp_liquid=130.0, density_liquid=880.0
    )
    platform.add_compound(name="nitrogen", molecular_weight=28.0, cp_gas=29.0)

    # ── Components ─────────────────────────────────────────────────────────────
    platform.add_component(name="gassupply", figure="Source", setpoint="3 bar")
    platform.add_component(
        name="liquidpump", figure="SyringePump", flow_rate="3 ml/min"
    )
    platform.add_component(name="productsink", figure="Sink", setpoint="1 bar")
    platform.add_component(name="wastesink", figure="Sink", setpoint="1 bar")
    platform.add_component(
        name="gastube", figure="Loop", length="10 cm", diameter="1.5 mm"
    )
    platform.add_component(
        name="liquidtube", figure="Loop", length="10 cm", diameter="1.5 mm"
    )
    platform.add_component(
        name="reactortube", figure="FlowReactor", length="5 cm", diameter="2 mm"
    )
    platform.add_component(
        name="outlettube", figure="Loop", length="20 cm", diameter="1.5 mm"
    )
    platform.add_component(
        name="collecttube", figure="Loop", length="10 cm", diameter="1.5 mm"
    )
    platform.add_component(
        name="wastetube", figure="Loop", length="10 cm", diameter="1.5 mm"
    )
    platform.add_component(name="tmixer", figure="Distributor", number_ports=3)
    platform.add_component(
        name="reactor",
        figure="GlassBottle",
        capacity="10 ml",
        top_access=1,
        bottom_access=1,
    )
    platform.add_component(name="bpr", figure="BackPressureRegulator", setpoint="2 bar")
    platform.add_component(name="divertvalve", figure="SolenoidValve2Way")

    platform["reactor"].internal_inventory.liq_content = VolumeContentBase(
        phase_kind="LIQUID",
        volume=3.0e-6,
        initial_species={"reagent_a": 1.0e-4, "solvent": 3.4e-2},
        initial_pressure=202650.0,
        initial_temperature=298.15,
    )

    platform["reactor"].internal_inventory.gas_content = VolumeContentBase(
        phase_kind="GAS",
        volume=7.0e-6,
        initial_species={"nitrogen": 4.3e-1},
        initial_pressure=202650.0,
        initial_temperature=298.15,
    )

    # ── Connections ────────────────────────────────────────────────────────────
    platform.add_connection(
        "gassupply", "gastube", 1, 1, length="2 cm", diameter="1.5 mm"
    )
    platform.add_connection("gastube", "tmixer", 2, 1, length="2 cm", diameter="1.5 mm")
    platform.add_connection(
        "liquidpump", "liquidtube", 1, 1, length="2 cm", diameter="1.5 mm"
    )
    platform.add_connection(
        "liquidtube", "tmixer", 2, 2, length="2 cm", diameter="1.5 mm"
    )
    platform.add_connection(
        "tmixer", "reactortube", 3, 1, length="2 cm", diameter="2 mm"
    )
    platform.add_connection(
        "reactortube", "reactor", 2, 2, length="2 cm", diameter="2 mm"
    )
    platform.add_connection("reactor", "bpr", 1, 1, length="2 cm", diameter="1.5 mm")
    platform.add_connection("bpr", "outlettube", 2, 1, length="2 cm", diameter="1.5 mm")
    platform.add_connection(
        "outlettube", "divertvalve", 2, 0, length="2 cm", diameter="1.5 mm"
    )
    platform.add_connection(
        "divertvalve", "collecttube", 1, 1, length="2 cm", diameter="1.5 mm"
    )
    platform.add_connection(
        "collecttube", "productsink", 2, 1, length="2 cm", diameter="1.5 mm"
    )
    platform.add_connection(
        "divertvalve", "wastetube", 2, 1, length="2 cm", diameter="1.5 mm"
    )
    platform.add_connection(
        "wastetube", "wastesink", 2, 1, length="2 cm", diameter="1.5 mm"
    )

    # ── Reactions ──────────────────────────────────────────────────────────────
    platform.add_reaction(
        "reactor", "FirstOrderDecay",
        reactant="reagent_a",
        product="product_b",
        rate_constant=0.05,
        phase="LIQUID",
        delta_temperature_per_mol_converted=2.0,
    )
