"""Simulation worker — operator-splitting time-step loop.

``Worker`` is the sole orchestrator: it calls each domain module in the
correct order every time step and delegates all physics to them.  No physics
logic lives here.

Operator-splitting order (CLAUDE.md):
  1. solve hydraulics
  2. update pump, MFC and BPR resistances for next tick
  3. advance transport
  4. assimilate
  5. react
  6. emit
  7. inject
  8. advance time → record if due
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from chemunited_core.common.constant import R_MAX_HYDRAULIC
from chemunited_core.common.enums import PhaseKind
from chemunited_core.components import (
    BackPressureRegulatorData,
    ComponentData,
    MassFlowControllerData,
    PlugFlowComponentData,
    PumpData,
    VesselComponentData,
)
from chemunited_core.figure_registry.pumps import SyringePumpData
from loguru import logger

from ..adapter.models import HydraulicEdge, HydraulicGraph
from ..hydraulics.models import HydraulicState
from ..hydraulics.solver import solve
from ..inventory.engine import (
    HeatExchangeEntry,
    apply_heat_exchange,
    assimilate,
    emit,
    emit_from_sources,
)
from ..inventory.initialiser import build_inventory_states
from ..inventory.models import InventoryState
from ..inventory.port_map import PortAccessMap, build_port_map
from ..inventory.source_map import SourceMap, build_source_map
from ..reactions.engine import apply
from ..reactions.models import ReactionsMap
from ..recorder.writer import Recorder
from ..transport.engine import (
    TransportHeatExchangeEntry,
    advance,
    apply_transport_heat_exchange,
    inject,
)
from ..transport.initialiser import build_initial_state
from ..transport.models import TransportState
from .config import SimConfig

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


@dataclass
class _BprEntry:
    """Pre-resolved BPR edge metadata for fast per-tick resistance update."""

    edge: HydraulicEdge
    edge_id: str
    setpoint_pa: float
    upstream_node_id: str
    downstream_node_id: str


@dataclass
class _MfcEntry:
    """Pre-resolved MFC edge metadata for fast per-step resistance update."""

    comp: MassFlowControllerData
    upstream_node_id: str
    downstream_node_id: str
    edge: HydraulicEdge


@dataclass
class _PumpEntry:
    """Pre-resolved pump edge metadata for fast per-step resistance update."""

    comp: PumpData
    upstream_node_id: str
    downstream_node_id: str
    edge: HydraulicEdge


@dataclass
class _VesselHeatEntry:
    """Live vessel heat-exchange metadata.

    The wall temperature is intentionally not cached here; HEAT links may point
    at controllers whose setpoint changes during the run.
    """

    comp: ComponentData
    inv_node_id: str


@dataclass
class _TransportHeatEntry:
    """Live transport-edge heat-exchange metadata."""

    comp: ComponentData
    edge: HydraulicEdge


def _build_bpr_entries(
    graph: HydraulicGraph,
    components: list[ComponentData],
) -> list[_BprEntry]:
    """Resolve BPR metadata from the component list."""
    entries: list[_BprEntry] = []
    for comp in components:
        if not isinstance(comp, BackPressureRegulatorData):
            continue
        edge_id = f"{comp.name}.1.2"
        edge = graph.edges.get(edge_id)
        if edge is None:
            continue
        entries.append(
            _BprEntry(
                edge=edge,
                edge_id=edge_id,
                setpoint_pa=comp.setpoint_pa,
                upstream_node_id=f"{comp.name}.1",
                downstream_node_id=f"{comp.name}.2",
            )
        )
    return entries


def _build_pump_entries(
    graph: HydraulicGraph,
    components: list[ComponentData],
) -> list[_PumpEntry]:
    """Resolve pump metadata from the compiled graph."""
    entries: list[_PumpEntry] = []
    for comp in components:
        if not isinstance(comp, PumpData):
            continue
        edge_id = f"{comp.name}.1.2"
        edge = graph.edges.get(edge_id)
        if edge is None:
            continue
        entries.append(
            _PumpEntry(
                comp=comp,
                upstream_node_id=f"{comp.name}.1",
                downstream_node_id=f"{comp.name}.2",
                edge=edge,
            )
        )
    return entries


def _build_mfc_entries(
    graph: HydraulicGraph,
    components: list[ComponentData],
) -> list[_MfcEntry]:
    """Resolve MFC metadata from the compiled graph."""
    entries: list[_MfcEntry] = []
    for comp in components:
        if not isinstance(comp, MassFlowControllerData):
            continue
        edge_id = f"{comp.name}.1.2"
        edge = graph.edges.get(edge_id)
        if edge is None:
            continue
        entries.append(
            _MfcEntry(
                comp=comp,
                upstream_node_id=f"{comp.name}.1",
                downstream_node_id=f"{comp.name}.2",
                edge=edge,
            )
        )
    return entries


def _build_vessel_heat_entries(
    components: list[ComponentData],
) -> list[_VesselHeatEntry]:
    """Build live heat-exchange entries for all vessels."""
    entries: list[_VesselHeatEntry] = []
    for comp in components:
        if not isinstance(comp, VesselComponentData):
            continue
        if not comp.heat_exchange:
            continue
        # Multi-well components (e.g. VialData) register one inventory node per
        # well under keys like "A1", "B2" etc.  Single-inventory vessels use
        # the standard "Inventory" key.
        if hasattr(comp, "internal_inventories") and comp.internal_inventories:
            for inv_key in comp.internal_inventories:
                entries.append(
                    _VesselHeatEntry(
                        comp=comp,
                        inv_node_id=f"{comp.name}.{inv_key}",
                    )
                )
        else:
            entries.append(
                _VesselHeatEntry(
                    comp=comp,
                    inv_node_id=f"{comp.name}.Inventory",
                )
            )
    return entries


def _build_transport_heat_entries(
    graph: HydraulicGraph,
    components: list[ComponentData],
) -> list[_TransportHeatEntry]:
    """Build live heat-exchange entries for plug-flow reactor edges."""
    entries: list[_TransportHeatEntry] = []
    for comp in components:
        if not isinstance(comp, PlugFlowComponentData):
            continue
        if not getattr(comp, "heat_exchange", False):
            continue
        edge = graph.edges.get(f"{comp.name}.1.2")
        if edge is None:
            continue
        entries.append(_TransportHeatEntry(comp=comp, edge=edge))
    return entries


def _build_heat_provider_map(
    graph: HydraulicGraph,
    components_by_name: dict[str, ComponentData],
) -> dict[str, ComponentData]:
    """Return target component name -> thermal provider component."""
    providers: dict[str, ComponentData] = {}
    for link in graph.heat_links:
        provider = components_by_name.get(link.provider_component)
        target = components_by_name.get(link.target_component)
        if provider is None or target is None:
            continue
        providers[target.name] = provider
    return providers


def _temperature_value(comp: ComponentData) -> float:
    try:
        return float(getattr(comp, "temperature_value"))
    except (TypeError, ValueError):
        raise ValueError(
            f"Component '{comp.name}' temperature_value is not numeric"
        ) from None


def _wall_temperature(
    comp: ComponentData,
    heat_provider_by_target: dict[str, ComponentData],
) -> float:
    source = heat_provider_by_target.get(comp.name, comp)
    if not hasattr(source, "temperature_value"):
        raise ValueError(
            f"Component '{source.name}' does not expose temperature_value for "
            f"heat target '{comp.name}'"
        )
    return _temperature_value(source)


def _resolve_vessel_heat_entries(
    entries: list[_VesselHeatEntry],
    heat_provider_by_target: dict[str, ComponentData],
) -> list[HeatExchangeEntry]:
    resolved: list[HeatExchangeEntry] = []
    for entry in entries:
        comp = entry.comp
        resolved.append(
            HeatExchangeEntry(
                inv_node_id=entry.inv_node_id,
                U=float(getattr(comp, "heat_transfer_coefficient_value")),
                contact_area=float(getattr(comp, "contact_area")),
                T_wall=_wall_temperature(comp, heat_provider_by_target),
            )
        )
    return resolved


def _resolve_transport_heat_entries(
    entries: list[_TransportHeatEntry],
    heat_provider_by_target: dict[str, ComponentData],
) -> list[TransportHeatExchangeEntry]:
    resolved: list[TransportHeatExchangeEntry] = []
    for entry in entries:
        comp = entry.comp
        resolved.append(
            TransportHeatExchangeEntry(
                edge_id=entry.edge.edge_id,
                U=float(getattr(comp, "heat_transfer_coefficient_value")),
                diameter=entry.edge.diameter,
                T_wall=_wall_temperature(comp, heat_provider_by_target),
            )
        )
    return resolved


def _syringe_phase(comp: SyringePumpData) -> PhaseKind:
    return (
        PhaseKind.LIQUID if getattr(comp, "direction_upward", True) else PhaseKind.GAS
    )


def _scale_species(
    species_moles: dict[str, float],
    template_volume: float,
    target_volume: float,
) -> dict[str, float]:
    if template_volume <= 0.0 or target_volume <= 0.0:
        return {}
    return {
        species_id: (moles / template_volume) * target_volume
        for species_id, moles in species_moles.items()
    }


def _scale_phase(state: InventoryState, factor: float) -> None:
    state.liq_volume *= factor
    state.gas_volume *= factor
    for species in (state.liq_species_moles, state.gas_species_moles):
        for species_id in list(species):
            species[species_id] *= factor


def _initialise_syringe_inventory_states(
    graph: HydraulicGraph,
    source_map: SourceMap,
    states: dict[str, InventoryState],
) -> None:
    seen: set[str] = set()
    for entry in source_map.values():
        if not isinstance(entry.comp, SyringePumpData):
            continue
        if entry.comp.name in seen:
            continue
        seen.add(entry.comp.name)

        state = states.get(entry.inv_node_id)
        inv_node = graph.inventory_nodes.get(entry.inv_node_id)
        if state is None or inv_node is None:
            continue

        phase = _syringe_phase(entry.comp)
        capacity = entry.comp.syringe_volume.to_base_units().magnitude
        actual_volume = entry.comp.syringe_actual_volume.to_base_units().magnitude
        if actual_volume > capacity:
            logger.warning(
                "worker: SyringePump '{}' actual volume {:.3e} m^3 exceeds "
                "capacity {:.3e} m^3 - clamping.",
                entry.comp.name,
                actual_volume,
                capacity,
            )
            actual_volume = capacity

        state.capacity = capacity
        if phase == PhaseKind.LIQUID:
            content = inv_node.liq_content
            state.liq_volume = actual_volume
            state.gas_volume = 0.0
            state.liq_species_moles = _scale_species(
                dict(content.initial_species),
                content.volume,
                actual_volume,
            )
            state.gas_species_moles = {}
        else:
            content = inv_node.gas_content
            state.gas_volume = actual_volume
            state.liq_volume = 0.0
            state.gas_species_moles = _scale_species(
                dict(content.initial_species),
                content.volume,
                actual_volume,
            )
            state.liq_species_moles = {}

        state.temperature = content.initial_temperature
        if content.initial_pressure > 0.0:
            state.pressure = content.initial_pressure


def _clamp_syringe_inventory_capacity(
    source_map: SourceMap,
    states: dict[str, InventoryState],
) -> None:
    seen: set[str] = set()
    for entry in source_map.values():
        if not isinstance(entry.comp, SyringePumpData):
            continue
        if entry.comp.name in seen:
            continue
        seen.add(entry.comp.name)

        state = states.get(entry.inv_node_id)
        if state is None:
            continue
        capacity = entry.comp.syringe_volume.to_base_units().magnitude
        total = state.liq_volume + state.gas_volume
        if total <= capacity:
            continue
        logger.warning(
            "worker: SyringePump '{}' exceeded fill capacity; "
            "inventory volume {:.3e} m^3 clamped to {:.3e} m^3.",
            entry.comp.name,
            total,
            capacity,
        )
        factor = capacity / total if total > 0.0 else 0.0
        _scale_phase(state, factor)


def _build_variable_volume_inventory_ids(source_map: SourceMap) -> set[str]:
    """Inventory ids whose runtime volume can change, currently syringes."""
    return {
        entry.inv_node_id
        for entry in source_map.values()
        if isinstance(entry.comp, SyringePumpData)
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class Worker:
    """Operator-splitting simulation loop for one platform configuration.

    Initialises all sub-module states once on construction and exposes
    :meth:`step` (single time step) and :meth:`run` (full simulation) as the
    primary interface.

    **Pump, BPR and MFC**: after each hydraulic solve, resistance overrides are
    updated from the solved pressures so the next tick's solve uses correct
    values.  Pump uses R = dp/Q (negative when working against back-pressure —
    active element model).  BPR uses a proportional model that drives upstream
    pressure toward the setpoint.  MFC uses R = dp / setpoint_flow.

    Parameters
    ----------
    graph:
        Compiled hydraulic graph (produced by ``adapter.compile_graph``).
    components:
        All live component instances in the simulation domain.
    config:
        Time-stepping and solver parameters.
    reactions_map:
        ``{inv_node_id: [Reaction, ...]}`` — optional reaction network.
        Defaults to an empty map (no reactions).
    recorder:
        Optional :class:`~chemunited_sim.recorder.Recorder` instance.
        When provided, :meth:`run` closes it after the last step.
    """

    def __init__(
        self,
        graph: HydraulicGraph,
        components: list[ComponentData],
        config: SimConfig,
        reactions_map: ReactionsMap | None = None,
        recorder: Recorder | None = None,
    ) -> None:
        self._graph = graph
        self._config = config
        self._reactions_map: ReactionsMap = reactions_map or {}
        self._recorder = recorder
        self._components_by_name = {comp.name: comp for comp in components}
        self._heat_provider_by_target = _build_heat_provider_map(
            graph, self._components_by_name
        )

        # Initialise sub-module states
        self._transport_state: TransportState = build_initial_state(graph)
        self._inv_states: dict[str, InventoryState] = build_inventory_states(graph)
        self._port_map: PortAccessMap = build_port_map(graph, components)
        self._source_map: SourceMap = build_source_map(graph, components)
        self._variable_volume_inventory_ids = _build_variable_volume_inventory_ids(
            self._source_map
        )
        _initialise_syringe_inventory_states(
            self._graph,
            self._source_map,
            self._inv_states,
        )
        self._bpr_entries: list[_BprEntry] = _build_bpr_entries(graph, components)
        self._mfc_entries: list[_MfcEntry] = _build_mfc_entries(graph, components)
        self._pump_entries: list[_PumpEntry] = _build_pump_entries(graph, components)
        vessel_heat_entries = _build_vessel_heat_entries(components)
        self._dynamic_vessel_heat_entries = [
            entry
            for entry in vessel_heat_entries
            if entry.comp.name in self._heat_provider_by_target
        ]
        self._static_vessel_heat_entries = _resolve_vessel_heat_entries(
            [
                entry
                for entry in vessel_heat_entries
                if entry.comp.name not in self._heat_provider_by_target
            ],
            self._heat_provider_by_target,
        )
        transport_heat_entries = _build_transport_heat_entries(graph, components)
        self._dynamic_transport_heat_entries = [
            entry
            for entry in transport_heat_entries
            if entry.comp.name in self._heat_provider_by_target
        ]
        self._static_transport_heat_entries = _resolve_transport_heat_entries(
            [
                entry
                for entry in transport_heat_entries
                if entry.comp.name not in self._heat_provider_by_target
            ],
            self._heat_provider_by_target,
        )

        self._t: float = 0.0
        self._hyd_state: HydraulicState | None = None

    # ------------------------------------------------------------------
    # Properties (read-only observation)
    # ------------------------------------------------------------------

    @property
    def t(self) -> float:
        """Current simulation time in seconds."""
        return self._t

    @property
    def inv_states(self) -> dict[str, InventoryState]:
        """Live inventory states, keyed by ``inv_node_id``."""
        return self._inv_states

    @property
    def transport_state(self) -> TransportState:
        """Current FIFO pocket queues for all transport edges."""
        return self._transport_state

    @property
    def hyd_state(self) -> HydraulicState | None:
        """Most recent hydraulic solve result.  ``None`` before the first step."""
        return self._hyd_state

    # ------------------------------------------------------------------
    # Step interface
    # ------------------------------------------------------------------

    def step(self) -> None:
        """Execute one operator-splitting time step.

        Records the state at the *current* time (before advancing) so
        ``recorder.record(t=0)`` captures the initial condition.  After
        returning, :attr:`t` has been incremented by ``config.dt``.
        """
        # 1. Solve hydraulics
        hyd_state = solve(self._graph, self._config.viscosity)

        # 2a. Update pump and MFC resistances from this tick's ΔP for the next solve
        for entry in self._pump_entries:
            dp = hyd_state.pressures.get(
                entry.upstream_node_id, 0.0
            ) - hyd_state.pressures.get(entry.downstream_node_id, 0.0)
            entry.comp.update_resistance(dp)
            entry.edge.resistance_override = entry.comp.internal_edges[
                (1, 2)
            ].resistance_override

        for entry in self._mfc_entries:
            dp = hyd_state.pressures.get(
                entry.upstream_node_id, 0.0
            ) - hyd_state.pressures.get(entry.downstream_node_id, 0.0)
            entry.comp.update_resistance(dp)
            entry.edge.resistance_override = entry.comp.internal_edges[
                (1, 2)
            ].resistance_override

        # 2b. Update BPR resistance from this tick's pressure and flow.
        # When closed, P_upstream is genuine — use it to decide whether to open.
        # When open, P_upstream = P_downstream (JUNCTION R=0) and cannot be used
        # for the open/close decision.  Instead, compute R = (setpoint − P_down) / Q
        # to drive P_upstream toward the setpoint.  If P_downstream already exceeds
        # the setpoint, set R=None (fully open) and let the network settle naturally.
        for bpr_entry in self._bpr_entries:
            was_closed = bpr_entry.edge.resistance_override == R_MAX_HYDRAULIC
            if was_closed:
                p_up = hyd_state.pressures.get(bpr_entry.upstream_node_id, 0.0)
                if p_up <= bpr_entry.setpoint_pa:
                    bpr_entry.edge.resistance_override = R_MAX_HYDRAULIC
                else:
                    bpr_entry.edge.resistance_override = None
            else:
                p_down = hyd_state.pressures.get(bpr_entry.downstream_node_id, 0.0)
                target_dp = bpr_entry.setpoint_pa - p_down
                q = abs(hyd_state.flows.get(bpr_entry.edge_id, 0.0))
                if target_dp > 0 and q > 0:
                    bpr_entry.edge.resistance_override = target_dp / q or None
                else:
                    bpr_entry.edge.resistance_override = None

        # 3. Record current state at self._t
        if self._recorder is not None:
            self._recorder.record(
                self._t, hyd_state, self._transport_state, self._inv_states
            )

        # 4. Apply plug-flow wall heat exchange before moving pockets.
        transport_heat_entries = self._static_transport_heat_entries
        if self._dynamic_transport_heat_entries:
            transport_heat_entries = (
                transport_heat_entries
                + _resolve_transport_heat_entries(
                    self._dynamic_transport_heat_entries,
                    self._heat_provider_by_target,
                )
            )
        if transport_heat_entries:
            apply_transport_heat_exchange(
                self._transport_state,
                transport_heat_entries,
                self._config.dt,
            )

        # 5. Advance transport
        result = advance(self._graph, hyd_state, self._transport_state, self._config.dt)

        # 6. Assimilate arriving pockets into inventories
        assimilate(
            self._inv_states,
            result.arrivals,
            hyd_state,
            self._variable_volume_inventory_ids,
        )
        _clamp_syringe_inventory_capacity(self._source_map, self._inv_states)

        # 7. Apply reactions
        apply(self._inv_states, self._reactions_map, self._config.dt)

        # 7.5. Apply vessel wall heat exchange
        vessel_heat_entries = self._static_vessel_heat_entries
        if self._dynamic_vessel_heat_entries:
            vessel_heat_entries = vessel_heat_entries + _resolve_vessel_heat_entries(
                self._dynamic_vessel_heat_entries,
                self._heat_provider_by_target,
            )
        if vessel_heat_entries:
            apply_heat_exchange(
                self._inv_states,
                vessel_heat_entries,
                self._config.dt,
            )

        # 8. Emit replacement pockets from inventories
        emitted = emit(
            self._inv_states,
            result.departures,
            self._port_map,
            self._variable_volume_inventory_ids,
        )

        # 8.5. Emit from FlowSource boundary components (infinite or finite syringe)
        source_emitted = emit_from_sources(
            self._source_map,
            self._graph,
            hyd_state,
            self._inv_states,
            self._config.dt,
        )

        # 9. Inject emitted pockets into transport queues
        self._transport_state = inject(
            result.next_state, {**emitted, **source_emitted}, hyd_state, self._graph
        )

        # 10. Advance time
        self._t += self._config.dt
        self._hyd_state = hyd_state

    # ------------------------------------------------------------------
    # Run interface
    # ------------------------------------------------------------------

    def run(self, stop_condition: Callable[[], bool] | None = None) -> None:
        """Run the simulation until ``config.t_end`` or *stop_condition* fires.

        Exactly one of the two termination criteria must be reachable:

        - **``t_end``** (set in :class:`~chemunited_sim.worker.config.SimConfig`):
          steps until ``t >= t_end``, using integer-tick arithmetic to avoid
          floating-point drift.
        - **``stop_condition``**: a zero-argument callable polled once per tick;
          the loop exits on the first tick where it returns ``True``.  Useful
          for open-ended standalone scripts — pass e.g. ``threading.Event().is_set``
          or ``lambda: worker.t >= 60.0``.

        If both are provided, whichever fires first wins.  If neither is
        provided a ``ValueError`` is raised immediately.

        Closes the recorder after the last step if one was provided.
        """
        if self._config.t_end is None and stop_condition is None:
            raise ValueError(
                "Worker.run() requires either t_end (set in SimConfig) or a "
                "stop_condition callable.  For server-driven runs the worker "
                "thread calls step() directly and does not use this method."
            )
        dt = self._config.dt
        n_end = (
            round(self._config.t_end / dt) if self._config.t_end is not None else None
        )
        while True:
            tick = round(self._t / dt)
            if n_end is not None and tick > n_end:
                break
            if stop_condition is not None and stop_condition():
                break
            self.step()
        if self._recorder is not None:
            self._recorder.close()
