"""Inventory assimilation and emission engine for chemunited-sim.

``assimilate`` absorbs arriving pockets into vessel phase inventories and
updates temperature (volume-weighted blend, instantaneous equilibrium).

``emit`` draws replacement pockets from vessel inventories for each departing
transport edge, using the port-access mapping to select the correct phase.
"""

from __future__ import annotations

import warnings

from chemunited_core.common.enums import PhaseKind
from chemunited_core.components.enums import PortAccess

from ..common.constant import MIN_POCKET_VOLUME
from ..hydraulics.models import HydraulicState
from ..transport.models import Pocket
from .models import InventoryState
from .port_map import PortAccessMap

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _merge_species(target: dict[str, float], incoming: dict[str, float]) -> None:
    """Add species moles from *incoming* into *target* in-place."""
    for species_id, moles in incoming.items():
        if moles > 0.0:
            target[species_id] = target.get(species_id, 0.0) + moles


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def assimilate(
    states: dict[str, InventoryState],
    arrivals: dict[str, list[Pocket]],
    hyd_state: HydraulicState,
) -> None:
    """Absorb arriving pockets into vessel inventories and update state.

    Mutates *states* in-place.  Three sub-steps per vessel that has arrivals:

    1. **Pressure update** — write ``HydraulicState.pressures[inv_node_id]``
       into ``state.pressure`` before volume changes.  The hydraulic solver
       is the authoritative source of vessel pressure.

    2. **Volume and species absorption** — add each pocket's volume to the
       matching phase; sum species moles.

    3. **Temperature blend** — volume-weighted average of the vessel's
       pre-arrival thermal mass and the arriving pockets' thermal mass.
       Under the instantaneous thermal-equilibrium assumption both phases
       share the resulting single temperature.

    Arrivals for inventory nodes not present in *states* are silently ignored.

    Parameters
    ----------
    states:
        All live inventory states, keyed by ``inv_node_id``.
    arrivals:
        ``{inv_node_id: [Pocket, ...]}`` from ``TransportResult.arrivals``.
    hyd_state:
        Current hydraulic solve result; used to update pressures.
    """
    # Step 1: pressure update from hydraulic solve (all vessels, not just those
    # with arrivals, so pressures stay current even in quiescent vessels)
    for inv_node_id, state in states.items():
        p = hyd_state.pressures.get(inv_node_id)
        if p is not None:
            state.pressure = p

    # Steps 2 + 3: absorb pockets and blend temperature
    for inv_node_id, pockets in arrivals.items():
        arrival_state = states.get(inv_node_id)
        if arrival_state is None or not pockets:
            continue

        # Snapshot pre-arrival total volume for temperature blend weight
        v_before = arrival_state.liq_volume + arrival_state.gas_volume
        v_arriving = sum(p.volume for p in pockets)

        # Absorb each pocket
        for pocket in pockets:
            if pocket.phase_kind == PhaseKind.LIQUID:
                arrival_state.liq_volume += pocket.volume
                _merge_species(
                    arrival_state.liq_species_moles,
                    pocket.species_moles,
                )
            else:
                arrival_state.gas_volume += pocket.volume
                _merge_species(
                    arrival_state.gas_species_moles,
                    pocket.species_moles,
                )

        # Blend temperature (volume-weighted, instantaneous equilibrium)
        if v_arriving > 0.0:
            t_arriving = sum(p.temperature * p.volume for p in pockets) / v_arriving
            total = v_before + v_arriving
            arrival_state.temperature = (
                v_before * arrival_state.temperature + v_arriving * t_arriving
            ) / total


def emit(
    states: dict[str, InventoryState],
    departures: dict[str, float],
    port_map: PortAccessMap,
) -> dict[str, Pocket]:
    """Draw replacement pockets from vessel inventories for each departing edge.

    For each entry in *departures*, looks up the inventory node and port
    access via *port_map*, then:

    - ``TOP`` port → emits a gas-phase pocket.
    - ``BOTTOM`` port → emits a liquid-phase pocket.

    The emitted pocket carries the vessel's current temperature and pressure
    (post-assimilation, post-reaction if the worker calls reactions between
    assimilate and emit).

    The corresponding volume and species fraction are subtracted from the
    inventory in-place.

    **Phase availability**: the vessel-is-always-full principle means phase
    runout should not occur in a correctly configured network.  If it does
    (e.g. a vessel starts all-gas and liquid is requested), a ``UserWarning``
    is emitted and the pocket is capped at the available volume.  Pockets
    below ``MIN_POCKET_VOLUME`` are not emitted.

    Parameters
    ----------
    states:
        All live inventory states, keyed by ``inv_node_id``.
    departures:
        ``{edge_id: dV}`` from ``TransportResult.departures``.
    port_map:
        Static mapping built once at simulation startup by
        :func:`~chemunited_sim.inventory.port_map.build_port_map`.

    Returns
    -------
    dict[str, Pocket]
        ``{edge_id: Pocket}`` — one pocket per departing edge that has a
        valid port-map entry.  Passed to
        :func:`~chemunited_sim.transport.engine.inject`.
    """
    emitted: dict[str, Pocket] = {}

    for edge_id, dV in departures.items():
        if dV < MIN_POCKET_VOLUME:
            continue

        entry = port_map.get(edge_id)
        if entry is None:
            continue

        state = states.get(entry.inv_node_id)
        if state is None:
            continue

        if entry.access == PortAccess.TOP:
            phase = PhaseKind.GAS
            available = state.gas_volume
            species = state.gas_species_moles
        else:
            phase = PhaseKind.LIQUID
            available = state.liq_volume
            species = state.liq_species_moles

        if available < MIN_POCKET_VOLUME:
            continue  # phase is fully absent — nothing to emit

        if available < dV:
            warnings.warn(
                f"emit: inventory node '{entry.inv_node_id}' phase "
                f"{'GAS' if entry.access == PortAccess.TOP else 'LIQUID'} "
                f"available={available:.3e} m³ < requested dV={dV:.3e} m³ — "
                "emitting available volume only.  Check vessel initial conditions.",
                UserWarning,
                stacklevel=2,
            )
            emit_vol = available
        else:
            emit_vol = dV

        fraction = emit_vol / available
        emit_species = {k: v * fraction for k, v in species.items()}

        # Remove emitted fraction from inventory in-place
        for k in list(species):
            species[k] *= 1.0 - fraction
        if entry.access == PortAccess.TOP:
            state.gas_volume -= emit_vol
        else:
            state.liq_volume -= emit_vol

        emitted[edge_id] = Pocket(
            phase_kind=phase,
            volume=emit_vol,
            species_moles=emit_species,
            temperature=state.temperature,
            pressure=state.pressure,
        )

    return emitted
