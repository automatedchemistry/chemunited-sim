"""Simulation worker — operator-splitting time-step loop.

``Worker`` is the sole orchestrator: it calls each domain module in the
correct order every time step and delegates all physics to them.  No physics
logic lives here.

Operator-splitting order (CLAUDE.md):
  1. solve hydraulics  (with BPR edge stabilisation)
  2. advance transport
  3. assimilate
  4. react
  5. emit
  6. inject
  7. advance time → record if due
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass

from chemunited_core.common.constant import R_MAX_HYDRAULIC
from chemunited_core.components import ComponentData

from ..adapter.models import HydraulicEdge, HydraulicGraph
from ..hydraulics.models import HydraulicState
from ..hydraulics.solver import solve
from ..inventory.engine import assimilate, emit
from ..inventory.initialiser import build_inventory_states
from ..inventory.models import InventoryState
from ..inventory.port_map import PortAccessMap, build_port_map
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
    """Pre-resolved BPR edge metadata for fast per-step toggling."""

    edge: HydraulicEdge
    setpoint_pa: float
    upstream_node_id: str


def _build_bpr_entries(
    graph: HydraulicGraph,
    components: list[ComponentData],
) -> list[_BprEntry]:
    """Resolve BPR metadata from the graph's bpr_edges list.

    Uses duck typing (``hasattr(comp, 'setpoint_pa')``) so the worker does
    not need to import the concrete ``BackPressureRegulatorData`` class.

    The upstream node is ``edge.origin_node_id`` — the BPR always places
    port 1 (upstream) at the edge origin.
    """
    comp_by_name: dict[str, ComponentData] = {c.name: c for c in components}
    entries: list[_BprEntry] = []
    for edge_id in graph.bpr_edges:
        edge = graph.edges.get(edge_id)
        if edge is None or edge.component is None:
            continue
        comp = comp_by_name.get(edge.component)
        if comp is None or not hasattr(comp, "setpoint_pa"):
            continue
        entries.append(
            _BprEntry(
                edge=edge,
                setpoint_pa=float(comp.setpoint_pa),  # type: ignore[union-attr]
                upstream_node_id=edge.origin_node_id,
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

    **BPR stabilisation**: before each step the hydraulic solve is iterated
    until the set of open BPR edges no longer changes (converges in O(1)
    iterations for well-posed networks).  A :class:`UserWarning` is emitted if
    ``config.bpr_max_iters`` is exhausted without convergence.

    Parameters
    ----------
    graph:
        Compiled hydraulic graph (produced by ``adapter.compile_graph``).
    components:
        All live component instances in the simulation domain.  Passed to
        the inventory port-map builder and used to resolve BPR setpoints.
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
        self._bpr_entries: list[_BprEntry] = _build_bpr_entries(graph, components)

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
        # 1. Solve hydraulics (with BPR edge stabilisation)
        hyd_state = self._solve_stable()

        # 2. Record current state at self._t
        if self._recorder is not None:
            self._recorder.record(
                self._t, hyd_state, self._transport_state, self._inv_states
            )

        # 3. Advance transport
        result = advance(self._graph, hyd_state, self._transport_state, self._config.dt)

        # 4. Assimilate arriving pockets into inventories
        assimilate(self._inv_states, result.arrivals, hyd_state)

        # 5. Apply reactions
        apply(self._inv_states, self._reactions_map, self._config.dt)

        # 6. Emit replacement pockets from inventories
        emitted = emit(self._inv_states, result.departures, self._port_map)

        # 7. Inject emitted pockets into transport queues
        self._transport_state = inject(result.next_state, emitted, hyd_state)

        # 8. Advance time
        self._t += self._config.dt
        self._hyd_state = hyd_state

    # ------------------------------------------------------------------
    # Run interface
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Run the simulation from the current time to ``config.t_end``.

        Steps the simulation until ``t`` reaches ``t_end`` (inclusive).
        Uses integer-tick arithmetic to avoid floating-point drift:

            n_end = round(t_end / dt)
            while round(t / dt) <= n_end: step()

        Closes the recorder after the last step if one was provided.
        """
        if self._config.t_end is None:
            raise ValueError(
                "Worker.run() requires t_end to be set. "
                "For open-ended runs, drive the loop with step() directly."
            )
        dt = self._config.dt
        n_end = round(self._config.t_end / dt)
        while round(self._t / dt) <= n_end:
            self.step()
        if self._recorder is not None:
            self._recorder.close()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _solve_stable(self) -> HydraulicState:
        """Solve hydraulics with iterative BPR edge stabilisation.

        If there are no BPR edges the solver is called exactly once.
        Otherwise, solves, toggles BPR edges that changed state, and
        repeats until the edge set is stable or ``bpr_max_iters`` is reached.
        A :class:`UserWarning` is emitted when the iteration limit is hit.
        """
        if not self._bpr_entries:
            return solve(self._graph, self._config.viscosity)

        for iteration in range(self._config.bpr_max_iters):
            hyd_state = solve(self._graph, self._config.viscosity)
            changed = False
            for entry in self._bpr_entries:
                p_upstream = hyd_state.pressures.get(entry.upstream_node_id, 0.0)
                should_open = p_upstream >= entry.setpoint_pa
                is_open = entry.edge.resistance_override is None
                if should_open != is_open:
                    if should_open:
                        entry.edge.resistance_override = None
                    else:
                        entry.edge.resistance_override = R_MAX_HYDRAULIC
                    changed = True
            if not changed:
                return hyd_state

        warnings.warn(
            f"Worker._solve_stable: BPR edge set did not converge after "
            f"{self._config.bpr_max_iters} iterations at t={self._t:.6f} s.  "
            "Check network configuration.",
            UserWarning,
            stacklevel=3,
        )
        return solve(self._graph, self._config.viscosity)
