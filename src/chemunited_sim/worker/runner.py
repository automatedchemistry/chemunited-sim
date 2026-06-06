"""Simulation worker — operator-splitting time-step loop.

``Worker`` is the sole orchestrator: it calls each domain module in the
correct order every time step and delegates all physics to them.  No physics
logic lives here.

Operator-splitting order (CLAUDE.md):
  1. solve hydraulics
  2. update MFC and BPR resistances for next tick
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
from chemunited_core.components import (
    BackPressureRegulatorData,
    ComponentData,
    MassFlowControllerData,
)

from chemunited_core.figure_registry.pumps import SyringePumpData
from loguru import logger

from ..adapter.models import HydraulicEdge, HydraulicGraph
from ..hydraulics.models import HydraulicState
from ..hydraulics.solver import solve
from ..inventory.engine import assimilate, emit, emit_from_sources
from ..inventory.initialiser import build_inventory_states
from ..inventory.models import InventoryState
from ..inventory.port_map import PortAccessMap, build_port_map
from ..inventory.source_map import SourceMap, build_source_map
from ..reactions.engine import apply
from ..reactions.models import ReactionsMap
from ..recorder.writer import Recorder
from ..transport.engine import advance, inject
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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class Worker:
    """Operator-splitting simulation loop for one platform configuration.

    Initialises all sub-module states once on construction and exposes
    :meth:`step` (single time step) and :meth:`run` (full simulation) as the
    primary interface.

    **BPR and MFC**: after each hydraulic solve, resistance overrides for BPR
    and MFC edges are updated from the solved pressures and flows so that the
    next tick's solve uses the correct values.  BPR uses a proportional model
    (resistance = excess_dp / Q) that drives upstream pressure toward the
    setpoint.  MFC uses resistance = dp / setpoint_flow.

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

        # Initialise sub-module states
        self._transport_state: TransportState = build_initial_state(graph)
        self._inv_states: dict[str, InventoryState] = build_inventory_states(graph)
        self._port_map: PortAccessMap = build_port_map(graph, components)
        self._source_map: SourceMap = build_source_map(graph, components)
        self._syringe_actual: dict[str, float] = {
            entry.comp.name: entry.comp.syringe_actual_volume.to_base_units().magnitude
            for entry in self._source_map.values()
            if isinstance(entry.comp, SyringePumpData)
        }
        self._bpr_entries: list[_BprEntry] = _build_bpr_entries(graph, components)
        self._mfc_entries: list[_MfcEntry] = _build_mfc_entries(graph, components)

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

        # 2a. Update MFC resistance from this tick's ΔP for the next solve
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

        # 4. Advance transport
        result = advance(self._graph, hyd_state, self._transport_state, self._config.dt)

        # 5. Assimilate arriving pockets into inventories
        assimilate(self._inv_states, result.arrivals, hyd_state)

        # 6. Apply reactions
        apply(self._inv_states, self._reactions_map, self._config.dt)

        # 7. Emit replacement pockets from inventories
        emitted = emit(self._inv_states, result.departures, self._port_map)

        # 7.5. Emit from FlowSource boundary components (infinite or finite syringe)
        source_emitted = emit_from_sources(
            self._source_map, self._graph, hyd_state, self._syringe_actual, self._config.dt
        )

        # Track syringe withdraw (Q < 0 means fluid drawn back into syringe)
        for edge_id, entry in self._source_map.items():
            if not isinstance(entry.comp, SyringePumpData):
                continue
            q = hyd_state.flows.get(edge_id, 0.0)
            if q < 0:
                dV = abs(q) * self._config.dt
                self._syringe_actual[entry.comp.name] += dV
                cap = entry.comp.syringe_volume.to_base_units().magnitude
                if self._syringe_actual[entry.comp.name] > cap:
                    logger.warning(
                        "worker: SyringePump '{}' exceeded fill capacity",
                        entry.comp.name,
                    )

        # 8. Inject emitted pockets into transport queues
        self._transport_state = inject(
            result.next_state, {**emitted, **source_emitted}, hyd_state
        )

        # 9. Advance time
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
