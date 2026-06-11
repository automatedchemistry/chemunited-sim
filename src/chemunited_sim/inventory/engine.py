"""Inventory assimilation and emission engine for chemunited-sim.

``assimilate`` absorbs arriving pockets into vessel phase inventories and
updates temperature (thermal-mass weighted blend n·Cp, instantaneous equilibrium).

``emit`` draws replacement pockets from vessel inventories for each departing
transport edge, using the port-access mapping to select the correct phase.
"""

from __future__ import annotations

from chemunited_core.common.enums import PhaseKind
from chemunited_core.compounds import COMPOUNDS
from chemunited_core.compounds.entity import IDEAL_GAS_CONSTANT
from chemunited_core.components.enums import PortAccess
from loguru import logger

from ..adapter.models import HydraulicGraph
from ..common.constant import AMBIENT_TEMPERATURE_K, ATMOSPHERE_PRESSURE_PA, MIN_POCKET_VOLUME
from ..hydraulics.models import HydraulicState
from ..transport.models import Pocket
from .models import InventoryState
from .port_map import PortAccessMap
from .source_map import SourceMap

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _merge_species(target: dict[str, float], incoming: dict[str, float]) -> None:
    """Add species moles from *incoming* into *target* in-place."""
    for species_id, moles in incoming.items():
        if moles > 0.0:
            target[species_id] = target.get(species_id, 0.0) + moles


def _thermal_mass(species_moles: dict[str, float], phase_kind: PhaseKind) -> float:
    """Sum of n_i * Cp_i (J/K) for all tracked species."""
    phase_str = "liquid" if phase_kind == PhaseKind.LIQUID else "gas"
    return sum(
        moles * COMPOUNDS[s].cp(phase_str)
        for s, moles in species_moles.items()
        if s in COMPOUNDS
    )


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

    3. **Temperature blend** — thermal-mass weighted average (n·Cp) of the
       vessel and arriving pockets. Falls back to volume-weighted for
       untracked carrier fluid. Both phases share the resulting temperature
       (instantaneous equilibrium assumption).

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

        # Blend temperature — thermal-mass weighted (n·Cp), volume fallback for untracked carrier
        if v_arriving > 0.0:
            c_before = (
                _thermal_mass(arrival_state.liq_species_moles, PhaseKind.LIQUID)
                + _thermal_mass(arrival_state.gas_species_moles, PhaseKind.GAS)
            )
            c_arriving = sum(_thermal_mass(p.species_moles, p.phase_kind) for p in pockets)
            total_c = c_before + c_arriving

            if total_c > 0.0:
                h_arriving = sum(_thermal_mass(p.species_moles, p.phase_kind) * p.temperature for p in pockets)
                arrival_state.temperature = (c_before * arrival_state.temperature + h_arriving) / total_c
            else:
                logger.warning(
                    "No tracked species in vessel {} or arriving pockets — falling back to volume-weighted temperature blend.",
                    inv_node_id,
                )
                total = v_before + v_arriving
                t_arriving = sum(p.temperature * p.volume for p in pockets) / v_arriving
                arrival_state.temperature = (v_before * arrival_state.temperature + v_arriving * t_arriving) / total


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
    (e.g. a vessel starts all-gas and liquid is requested), a warning is
    logged and the pocket is capped at the available volume.  Pockets below
    ``MIN_POCKET_VOLUME`` are not emitted.

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
            logger.warning(
                "emit: inventory node '{}' phase {} available={:.3e} m^3 < "
                "requested dV={:.3e} m^3 - emitting available volume only. "
                "Check vessel initial conditions.",
                entry.inv_node_id,
                "GAS" if entry.access == PortAccess.TOP else "LIQUID",
                available,
                dV,
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


def emit_from_sources(
    source_map: SourceMap,
    graph: HydraulicGraph,
    hyd_state: HydraulicState,
    syringe_actual: dict[str, float],
    dt: float,
) -> dict[str, Pocket]:
    """Emit species-carrying pockets from FlowSource boundary components.

    Unlike vessel emission, FlowSource nodes are infinite reservoirs — their
    declared concentration (``initial_species / liq_content.volume``) stays
    constant and is never depleted.  SyringePump nodes are finite: their
    remaining liquid volume is tracked in *syringe_actual* and updated here;
    when exhausted the pocket falls back to air.

    Parameters
    ----------
    source_map:
        ``{edge_id: SourceEdgeEntry}`` built once at startup.
    graph:
        The compiled hydraulic graph; used to read the immutable inventory
        snapshot for concentration calculations.
    hyd_state:
        Current hydraulic solve result; provides edge flows and node pressures.
    syringe_actual:
        Mutable mapping of ``{comp_name: remaining_liquid_m3}`` for every
        SyringePump component.  Updated in-place as liquid is dispensed.
    dt:
        Simulation time step in seconds.

    Returns
    -------
    dict[str, Pocket]
        ``{edge_id: Pocket}`` — one pocket per source edge with nonzero flow.
    """
    from chemunited_core.figure_registry.pumps import SyringePumpData  # local to avoid circular

    emitted: dict[str, Pocket] = {}

    for edge_id, entry in source_map.items():
        q = abs(hyd_state.flows.get(edge_id, 0.0))
        dV = q * dt
        if dV < MIN_POCKET_VOLUME:
            continue

        inv_node = graph.inventory_nodes.get(entry.inv_node_id)
        P = hyd_state.pressures.get(entry.source_node_id, ATMOSPHERE_PRESSURE_PA)

        # Determine phase preference from component type / direction_upward
        is_syringe = isinstance(entry.comp, SyringePumpData)
        direction_upward = getattr(entry.comp, "direction_upward", True)
        primary_liquid = direction_upward  # True → emit liquid; False → emit gas

        if is_syringe:
            remaining = syringe_actual.get(entry.comp.name, 0.0)
            emit_vol = min(dV, remaining) if remaining > MIN_POCKET_VOLUME else 0.0

            if emit_vol > MIN_POCKET_VOLUME and primary_liquid:
                # Emit liquid pocket
                liq = inv_node.liq_content if inv_node is not None else None
                if liq is not None and liq.volume > 0 and liq.initial_species:
                    species_moles = {
                        k: (n / liq.volume) * emit_vol
                        for k, n in liq.initial_species.items()
                    }
                else:
                    species_moles = {}
                syringe_actual[entry.comp.name] = remaining - emit_vol
                if syringe_actual[entry.comp.name] <= 0:
                    logger.warning(
                        "emit_from_sources: SyringePump '{}' ran dry",
                        entry.comp.name,
                    )
                emitted[edge_id] = Pocket(
                    phase_kind=PhaseKind.LIQUID,
                    volume=emit_vol,
                    species_moles=species_moles,
                    temperature=AMBIENT_TEMPERATURE_K,
                    pressure=P,
                )
            else:
                # Liquid exhausted (or direction_upward=False) → air fallback
                n_air = P / (IDEAL_GAS_CONSTANT * AMBIENT_TEMPERATURE_K)
                emitted[edge_id] = Pocket(
                    phase_kind=PhaseKind.GAS,
                    volume=dV,
                    species_moles={"air": n_air * dV},
                    temperature=AMBIENT_TEMPERATURE_K,
                    pressure=P,
                )
        else:
            # FlowSourceData base class — infinite reservoir, constant concentration
            if primary_liquid:
                liq = inv_node.liq_content if inv_node is not None else None
                if liq is not None and liq.volume > 0 and liq.initial_species:
                    species_moles = {
                        k: (n / liq.volume) * dV
                        for k, n in liq.initial_species.items()
                    }
                    emitted[edge_id] = Pocket(
                        phase_kind=PhaseKind.LIQUID,
                        volume=dV,
                        species_moles=species_moles,
                        temperature=AMBIENT_TEMPERATURE_K,
                        pressure=P,
                    )
                else:
                    # No liquid declared → fall back to air
                    n_air = P / (IDEAL_GAS_CONSTANT * AMBIENT_TEMPERATURE_K)
                    emitted[edge_id] = Pocket(
                        phase_kind=PhaseKind.GAS,
                        volume=dV,
                        species_moles={"air": n_air * dV},
                        temperature=AMBIENT_TEMPERATURE_K,
                        pressure=P,
                    )
            else:
                # direction_upward=False → gas source
                gas = inv_node.gas_content if inv_node is not None else None
                if gas is not None and gas.volume > 0 and gas.initial_species:
                    species_moles = {
                        k: (n / gas.volume) * dV
                        for k, n in gas.initial_species.items()
                    }
                else:
                    n_air = P / (IDEAL_GAS_CONSTANT * AMBIENT_TEMPERATURE_K)
                    species_moles = {"air": n_air * dV}
                emitted[edge_id] = Pocket(
                    phase_kind=PhaseKind.GAS,
                    volume=dV,
                    species_moles=species_moles,
                    temperature=AMBIENT_TEMPERATURE_K,
                    pressure=P,
                )

    return emitted
