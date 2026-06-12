"""Tests for inventory emission behavior."""

from __future__ import annotations

import pytest
from chemunited_core.common.enums import PhaseKind
from chemunited_core.components.enums import PortAccess
from chemunited_core.compounds.entity import IDEAL_GAS_CONSTANT

from chemunited_sim.hydraulics.models import HydraulicState
from chemunited_sim.inventory.engine import assimilate, emit
from chemunited_sim.inventory.models import InventoryState
from chemunited_sim.inventory.port_map import EdgePortAccess
from chemunited_sim.transport.models import Pocket


def _state() -> InventoryState:
    return InventoryState(
        node_id="vessel.Inventory",
        capacity=1.0e-6,
        pressure=120_000.0,
        temperature=300.0,
        liq_volume=5.0e-7,
        gas_volume=5.0e-7,
        liq_species_moles={"solvent": 8.0},
        gas_species_moles={"nitrogen": 6.0},
    )


def test_emit_top_falls_back_to_liquid_when_gas_is_unavailable():
    state = _state()
    state.liq_volume = state.capacity
    state.gas_volume = 0.0
    emitted = emit(
        {"vessel.Inventory": state},
        {"edge": 3.0e-9},
        {"edge": EdgePortAccess("vessel.Inventory", PortAccess.TOP)},
    )

    pocket = emitted["edge"]
    assert pocket.phase_kind == PhaseKind.LIQUID
    assert pocket.volume == pytest.approx(3.0e-9)
    assert pocket.species_moles == {"solvent": pytest.approx(0.024)}
    assert state.gas_volume == pytest.approx(3.0e-9)
    assert state.gas_species_moles["nitrogen"] == pytest.approx(6.0)
    assert state.liq_volume == pytest.approx(state.capacity - 3.0e-9)
    assert state.liq_species_moles["solvent"] == pytest.approx(7.976)
    assert state.liq_volume + state.gas_volume == pytest.approx(state.capacity)


def test_emit_bottom_falls_back_to_gas_when_liquid_is_unavailable():
    state = _state()
    state.liq_volume = 0.0
    state.gas_volume = state.capacity
    emitted = emit(
        {"vessel.Inventory": state},
        {"edge": 4.0e-9},
        {"edge": EdgePortAccess("vessel.Inventory", PortAccess.BOTTOM)},
    )

    pocket = emitted["edge"]
    assert pocket.phase_kind == PhaseKind.GAS
    assert pocket.volume == pytest.approx(4.0e-9)
    assert pocket.species_moles == {"nitrogen": pytest.approx(0.024)}
    assert state.liq_volume == pytest.approx(0.0)
    assert state.liq_species_moles["solvent"] == pytest.approx(8.0)
    assert state.gas_volume == pytest.approx(state.capacity)
    assert state.gas_species_moles["nitrogen"] == pytest.approx(5.976)
    assert state.gas_species_moles["air"] == pytest.approx(
        state.pressure * 4.0e-9 / (IDEAL_GAS_CONSTANT * state.temperature)
    )
    assert state.liq_volume + state.gas_volume == pytest.approx(state.capacity)


def test_emit_sufficient_inventory_preserves_existing_fractional_behavior():
    state = _state()
    state.gas_volume = 2.0e-9
    state.liq_volume = state.capacity - state.gas_volume
    emitted = emit(
        {"vessel.Inventory": state},
        {"edge": 1.0e-9},
        {"edge": EdgePortAccess("vessel.Inventory", PortAccess.TOP)},
    )

    pocket = emitted["edge"]
    assert pocket.phase_kind == PhaseKind.GAS
    assert pocket.volume == pytest.approx(1.0e-9)
    assert pocket.species_moles == {"nitrogen": pytest.approx(3.0)}
    assert state.gas_volume == pytest.approx(2.0e-9)
    assert state.gas_species_moles["nitrogen"] == pytest.approx(3.0)
    assert state.gas_species_moles["air"] == pytest.approx(
        state.pressure * 1.0e-9 / (IDEAL_GAS_CONSTANT * state.temperature)
    )
    assert state.liq_volume + state.gas_volume == pytest.approx(state.capacity)


def test_emit_uses_carrier_only_when_both_phases_are_insufficient():
    state = _state()
    state.gas_volume = 1.0e-9
    state.liq_volume = 2.0e-9
    emitted = emit(
        {"vessel.Inventory": state},
        {"edge": 3.0e-9},
        {"edge": EdgePortAccess("vessel.Inventory", PortAccess.TOP)},
        variable_volume_inventory_ids={"vessel.Inventory"},
    )

    pocket = emitted["edge"]
    expected_air = state.pressure * 2.0e-9 / (IDEAL_GAS_CONSTANT * state.temperature)
    assert pocket.phase_kind == PhaseKind.GAS
    assert pocket.volume == pytest.approx(3.0e-9)
    assert pocket.species_moles["nitrogen"] == pytest.approx(6.0)
    assert pocket.species_moles["air"] == pytest.approx(expected_air)
    assert state.gas_volume == pytest.approx(0.0)
    assert state.liq_volume == pytest.approx(2.0e-9)
    assert state.gas_species_moles["nitrogen"] == pytest.approx(0.0)


def test_normal_vessel_liquid_assimilation_displaces_headspace():
    state = _state()

    assimilate(
        {"vessel.Inventory": state},
        {
            "vessel.Inventory": [
                Pocket(
                    phase_kind=PhaseKind.LIQUID,
                    volume=1.0e-7,
                    species_moles={"solute": 2.0},
                    temperature=300.0,
                    pressure=120_000.0,
                )
            ]
        },
        HydraulicState(pressures={"vessel.Inventory": 120_000.0}, flows={}),
    )

    assert state.liq_volume == pytest.approx(6.0e-7)
    assert state.gas_volume == pytest.approx(4.0e-7)
    assert state.liq_species_moles["solute"] == pytest.approx(2.0)
    assert state.liq_volume + state.gas_volume == pytest.approx(state.capacity)


def test_variable_volume_inventory_can_grow_on_assimilation():
    state = _state()

    assimilate(
        {"vessel.Inventory": state},
        {
            "vessel.Inventory": [
                Pocket(
                    phase_kind=PhaseKind.LIQUID,
                    volume=1.0e-7,
                    species_moles={"solute": 2.0},
                    temperature=300.0,
                    pressure=120_000.0,
                )
            ]
        },
        HydraulicState(pressures={"vessel.Inventory": 120_000.0}, flows={}),
        variable_volume_inventory_ids={"vessel.Inventory"},
    )

    assert state.liq_volume == pytest.approx(6.0e-7)
    assert state.gas_volume == pytest.approx(5.0e-7)
    assert state.liq_volume + state.gas_volume == pytest.approx(state.capacity + 1.0e-7)
