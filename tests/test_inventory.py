"""Tests for inventory emission behavior."""

from __future__ import annotations

import pytest
from chemunited_core.common.enums import PhaseKind
from chemunited_core.components.enums import PortAccess
from chemunited_core.compounds.entity import IDEAL_GAS_CONSTANT

from chemunited_sim.inventory.engine import emit
from chemunited_sim.inventory.models import InventoryState
from chemunited_sim.inventory.port_map import EdgePortAccess


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


def test_emit_gas_deficit_uses_air_carrier_for_full_requested_volume():
    state = _state()
    state.gas_volume = 1.0e-9
    emitted = emit(
        {"vessel.Inventory": state},
        {"edge": 3.0e-9},
        {"edge": EdgePortAccess("vessel.Inventory", PortAccess.TOP)},
    )

    pocket = emitted["edge"]
    expected_air = state.pressure * 2.0e-9 / (IDEAL_GAS_CONSTANT * state.temperature)
    assert pocket.phase_kind == PhaseKind.GAS
    assert pocket.volume == pytest.approx(3.0e-9)
    assert pocket.species_moles["nitrogen"] == pytest.approx(6.0)
    assert pocket.species_moles["air"] == pytest.approx(expected_air)
    assert state.gas_volume == pytest.approx(0.0)
    assert state.gas_species_moles["nitrogen"] == pytest.approx(0.0)


def test_emit_liquid_deficit_uses_speciesless_carrier_for_full_requested_volume():
    state = _state()
    state.liq_volume = 1.0e-9
    emitted = emit(
        {"vessel.Inventory": state},
        {"edge": 4.0e-9},
        {"edge": EdgePortAccess("vessel.Inventory", PortAccess.BOTTOM)},
    )

    pocket = emitted["edge"]
    assert pocket.phase_kind == PhaseKind.LIQUID
    assert pocket.volume == pytest.approx(4.0e-9)
    assert pocket.species_moles == {"solvent": pytest.approx(8.0)}
    assert state.liq_volume == pytest.approx(0.0)
    assert state.liq_species_moles["solvent"] == pytest.approx(0.0)


def test_emit_sufficient_inventory_preserves_existing_fractional_behavior():
    state = _state()
    state.gas_volume = 2.0e-9
    emitted = emit(
        {"vessel.Inventory": state},
        {"edge": 1.0e-9},
        {"edge": EdgePortAccess("vessel.Inventory", PortAccess.TOP)},
    )

    pocket = emitted["edge"]
    assert pocket.phase_kind == PhaseKind.GAS
    assert pocket.volume == pytest.approx(1.0e-9)
    assert pocket.species_moles == {"nitrogen": pytest.approx(3.0)}
    assert state.gas_volume == pytest.approx(1.0e-9)
    assert state.gas_species_moles["nitrogen"] == pytest.approx(3.0)
