"""Per-step FIFO transport engine for chemunited-sim.

:func:`advance` moves pockets one step forward along every transport edge
according to the signed volumetric flows in a :class:`HydraulicState`.

:func:`inject` inserts inventory-emitted pockets into edge queues at the
correct inflow end, ready for the next call to :func:`advance`.
"""

from __future__ import annotations

import warnings
from collections import defaultdict, deque

from chemunited_core.common.constant import R_MAX_HYDRAULIC
from chemunited_core.components.enums import InternalEdgeRole

from ..adapter.models import HydraulicEdge, HydraulicGraph
from ..common.constant import MAX_JUNCTION_HOPS, MIN_POCKET_VOLUME
from ..hydraulics.models import HydraulicState
from .junction import merge_arriving_pockets
from .models import Pocket, TransportResult, TransportState


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _scale_pocket(pocket: Pocket, fraction: float) -> Pocket:
    """Return a new Pocket with volume and species scaled by *fraction*."""
    return Pocket(
        phase_kind=pocket.phase_kind,
        volume=pocket.volume * fraction,
        species_moles={k: v * fraction for k, v in pocket.species_moles.items()},
        temperature=pocket.temperature,
        pressure=pocket.pressure,
    )


def _pop_volume(
    queue: deque[Pocket],
    dV: float,
    from_left: bool,
) -> list[Pocket]:
    """Pop up to *dV* m³ of pockets from *queue*, splitting the boundary pocket.

    Parameters
    ----------
    queue:
        The edge queue to drain (modified in place).
    dV:
        Volume to extract in m³.
    from_left:
        ``True`` to pop from the left end (destination-ward; used when
        ``Q > 0``).  ``False`` to pop from the right end (origin-ward;
        used when ``Q < 0``).

    Returns
    -------
    list[Pocket]
        Exiting pockets.  Their total volume equals *dV* unless the queue
        ran dry (total < *dV*).
    """
    exiting: list[Pocket] = []
    accumulated = 0.0

    while queue and accumulated < dV:
        p = queue.popleft() if from_left else queue.pop()
        remaining = dV - accumulated

        if p.volume <= remaining:
            exiting.append(p)
            accumulated += p.volume
        else:
            # Pocket straddles the displacement boundary — split it.
            exit_frac = remaining / p.volume
            exiting.append(_scale_pocket(p, exit_frac))
            stay = _scale_pocket(p, 1.0 - exit_frac)
            if from_left:
                queue.appendleft(stay)
            else:
                queue.append(stay)
            break

    return exiting


def _build_indices(
    graph: HydraulicGraph,
) -> tuple[
    dict[str, list[tuple[str, HydraulicEdge]]],
    dict[str, list[tuple[str, HydraulicEdge]]],
]:
    """Build node-to-edges lookup tables for JUNCTION and TRANSPORT edges.

    Returns
    -------
    junction_neighbors:
        ``node_id → [(neighbor_node_id, HydraulicEdge), ...]`` for every
        JUNCTION edge touching that node.
    transport_by_node:
        ``node_id → [(edge_id, HydraulicEdge), ...]`` for every TRANSPORT
        edge whose origin or destination is that node.
    """
    junction_neighbors: dict[str, list[tuple[str, HydraulicEdge]]] = defaultdict(list)
    transport_by_node: dict[str, list[tuple[str, HydraulicEdge]]] = defaultdict(list)

    for eid, edge in graph.edges.items():
        if edge.role == InternalEdgeRole.JUNCTION:
            junction_neighbors[edge.origin_node_id].append(
                (edge.destination_node_id, edge)
            )
            junction_neighbors[edge.destination_node_id].append(
                (edge.origin_node_id, edge)
            )
        elif edge.role == InternalEdgeRole.TRANSPORT:
            transport_by_node[edge.origin_node_id].append((eid, edge))
            transport_by_node[edge.destination_node_id].append((eid, edge))

    return junction_neighbors, transport_by_node


def _route_pocket(
    pocket: Pocket,
    node_id: str,
    junction_neighbors: dict[str, list[tuple[str, HydraulicEdge]]],
    inventory_nodes: set[str],
    hub_nodes: set[str],
    arrivals: dict[str, list[Pocket]],
    hub_staging: dict[str, list[Pocket]],
    hyd_state: HydraulicState,
) -> None:
    """Route a pocket to its final destination, following JUNCTION edges.

    Traversal terminates when the pocket reaches:

    - An inventory node → appended to ``arrivals``.
    - A hub node → staged in ``hub_staging`` for merging.
    - A dead-end port (boundary component: FlowSource, PressureControl)
      → silently discarded (absorbed by the BC).

    Parameters
    ----------
    pocket:
        The pocket to route.
    node_id:
        The node at which the pocket has just arrived.
    junction_neighbors, inventory_nodes, hub_nodes:
        Pre-built indices from :func:`_build_indices`.
    arrivals, hub_staging:
        Output accumulators (modified in place).
    hyd_state:
        Used to determine JUNCTION flow direction at each hop.
    """
    current = node_id
    for _ in range(MAX_JUNCTION_HOPS):
        if current in inventory_nodes:
            arrivals[current].append(pocket)
            return
        if current in hub_nodes:
            hub_staging[current].append(pocket)
            return

        # Follow the JUNCTION edge whose flow points away from current.
        next_node: str | None = None
        for neighbor_id, edge in junction_neighbors.get(current, []):
            Q = hyd_state.flows.get(edge.edge_id, 0.0)
            if edge.origin_node_id == current and Q > MIN_POCKET_VOLUME:
                next_node = neighbor_id
                break
            if edge.destination_node_id == current and Q < -MIN_POCKET_VOLUME:
                next_node = neighbor_id
                break

        if next_node is None:
            # Dead end — pocket absorbed by boundary component.
            return
        current = next_node

    warnings.warn(
        f"Pocket routing exceeded {MAX_JUNCTION_HOPS} JUNCTION hops from node "
        f"'{node_id}' — pocket discarded.",
        UserWarning,
        stacklevel=4,
    )


def _connects_to_inventory(
    node_id: str,
    graph: HydraulicGraph,
    junction_neighbors: dict[str, list[tuple[str, HydraulicEdge]]],
) -> bool:
    """Return True if *node_id* is or directly neighbours an inventory node."""
    if node_id in graph.inventory_nodes:
        return True
    return any(
        nb in graph.inventory_nodes
        for nb, _ in junction_neighbors.get(node_id, [])
    )


def _process_hub(
    hub_id: str,
    staged: list[Pocket],
    hyd_state: HydraulicState,
    dt: float,
    junction_neighbors: dict[str, list[tuple[str, HydraulicEdge]]],
    transport_by_node: dict[str, list[tuple[str, HydraulicEdge]]],
    new_queues: dict[str, deque[Pocket]],
) -> None:
    """Merge pockets at a hub and distribute to outgoing transport edges.

    Algorithm:

    1. Merge all staged pockets by phase (via
       :func:`~chemunited_sim.transport.junction.merge_arriving_pockets`).
    2. Find outgoing TRANSPORT edges reachable from this hub via a JUNCTION
       edge to a port, where the TRANSPORT edge carries flow away from the
       port.
    3. Distribute each merged pocket across outgoing edges proportionally to
       their displaced volume ``|Q|·dt``.
    4. Inject pocket fragments at the inflow end of each outgoing edge.
    """
    merged = merge_arriving_pockets(staged)
    if not merged:
        return

    # Outgoing transport edges keyed by edge_id → dV
    outgoing_dv: dict[str, float] = {}

    for port_id, junc_edge in junction_neighbors.get(hub_id, []):
        Q_junc = hyd_state.flows.get(junc_edge.edge_id, 0.0)
        hub_to_port = (
            junc_edge.origin_node_id == hub_id and Q_junc > MIN_POCKET_VOLUME
        ) or (
            junc_edge.destination_node_id == hub_id and Q_junc < -MIN_POCKET_VOLUME
        )
        if not hub_to_port:
            continue

        for trans_id, trans_edge in transport_by_node.get(port_id, []):
            Q_trans = hyd_state.flows.get(trans_id, 0.0)
            away_from_port = (
                trans_edge.origin_node_id == port_id and Q_trans > MIN_POCKET_VOLUME
            ) or (
                trans_edge.destination_node_id == port_id
                and Q_trans < -MIN_POCKET_VOLUME
            )
            if away_from_port:
                outgoing_dv[trans_id] = abs(Q_trans) * dt
                break

    if not outgoing_dv:
        return

    total_out = sum(outgoing_dv.values())
    if total_out < MIN_POCKET_VOLUME:
        return

    for merged_pocket in merged:
        for trans_id, dV in outgoing_dv.items():
            frac = dV / total_out
            vol = merged_pocket.volume * frac
            if vol < MIN_POCKET_VOLUME:
                continue
            fragment = _scale_pocket(merged_pocket, frac)
            Q_trans = hyd_state.flows.get(trans_id, 0.0)
            if Q_trans >= 0:
                # Inflow at origin end → append to right
                new_queues[trans_id].append(fragment)
            else:
                # Inflow at destination end → appendleft to left
                new_queues[trans_id].appendleft(fragment)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def advance(
    graph: HydraulicGraph,
    hyd_state: HydraulicState,
    transport_state: TransportState,
    dt: float,
) -> TransportResult:
    """Advance the transport state by one time step.

    The function performs two passes:

    **Pass 1 — TRANSPORT edges:**
    For each open transport edge, compute the displaced volume
    ``dV = |Q|·dt``, pop that volume worth of pockets from the exit end of
    the queue (splitting the boundary pocket if necessary), and route each
    exiting pocket to its destination:

    - Inventory node → ``arrivals[inv_node_id]``.
    - Hub node (junction centre) → staged for Pass 2.
    - Dead-end port (boundary component) → discarded.

    Any JUNCTION passthrough hops are followed in the same step.
    Edges that are closed (``resistance_override ≥ R_MAX_HYDRAULIC/2``) or
    have zero flow carry their queues forward unchanged.

    **Pass 2 — Hub merging:**
    For each hub that received staged pockets, merge by phase (ideal mixing),
    then distribute proportionally to the displaced volumes of outgoing
    transport edges.

    After both passes, pockets below
    :data:`~chemunited_sim.common.constant.MIN_POCKET_VOLUME` are discarded.

    Parameters
    ----------
    graph:
        Compiled hydraulic graph (static for this step).
    hyd_state:
        Pressures and signed flows from the hydraulic solve for this step.
    transport_state:
        FIFO queues from the previous step (or from
        :func:`~chemunited_sim.transport.initialiser.build_initial_state`).
    dt:
        Simulation time step in seconds.

    Returns
    -------
    TransportResult
        ``next_state`` — updated queues.
        ``arrivals``   — pockets that reached each inventory node this step.
        ``departures`` — volume (m³) that left the inventory-connected end of
                         each transport edge (one entry per edge where the
                         inflow end neighbours an inventory node).
    """
    junction_neighbors, transport_by_node = _build_indices(graph)
    inventory_nodes: set[str] = set(graph.inventory_nodes.keys())
    hub_nodes: set[str] = {n.node_id for n in graph.nodes.values() if n.is_hub}

    # Initialise new queues — copy existing; add empty deques for new edges.
    new_queues: dict[str, deque[Pocket]] = {}
    for eid, edge in graph.edges.items():
        if edge.role != InternalEdgeRole.TRANSPORT:
            continue
        existing = transport_state.edge_queues.get(eid)
        new_queues[eid] = deque(existing) if existing is not None else deque()

    arrivals: dict[str, list[Pocket]] = defaultdict(list)
    hub_staging: dict[str, list[Pocket]] = defaultdict(list)
    departures: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Pass 1: TRANSPORT edge movement
    # ------------------------------------------------------------------
    for eid, edge in graph.edges.items():
        if edge.role != InternalEdgeRole.TRANSPORT:
            continue

        # Skip closed edges (R_MAX_HYDRAULIC means no flow)
        if (
            edge.resistance_override is not None
            and edge.resistance_override >= R_MAX_HYDRAULIC / 2.0
        ):
            continue

        Q = hyd_state.flows.get(eid, 0.0)
        if Q == 0.0:
            continue

        dV = abs(Q) * dt
        if dV < MIN_POCKET_VOLUME:
            continue

        # from_left: True = pop from left (destination end) when Q > 0.
        from_left = Q > 0
        exit_node_id = edge.destination_node_id if Q > 0 else edge.origin_node_id
        inflow_node_id = edge.origin_node_id if Q > 0 else edge.destination_node_id

        exiting = _pop_volume(new_queues[eid], dV, from_left)

        for pocket in exiting:
            _route_pocket(
                pocket,
                exit_node_id,
                junction_neighbors,
                inventory_nodes,
                hub_nodes,
                arrivals,
                hub_staging,
                hyd_state,
            )

        if _connects_to_inventory(inflow_node_id, graph, junction_neighbors):
            departures[eid] = dV

    # ------------------------------------------------------------------
    # Pass 2: Hub merging and distribution
    # ------------------------------------------------------------------
    for hub_id, staged in hub_staging.items():
        _process_hub(
            hub_id,
            staged,
            hyd_state,
            dt,
            junction_neighbors,
            transport_by_node,
            new_queues,
        )

    # ------------------------------------------------------------------
    # Discard sub-threshold pocket fragments
    # ------------------------------------------------------------------
    for eid in new_queues:
        new_queues[eid] = deque(
            p for p in new_queues[eid] if p.volume >= MIN_POCKET_VOLUME
        )

    return TransportResult(
        next_state=TransportState(edge_queues=new_queues),
        arrivals=dict(arrivals),
        departures=departures,
    )


def inject(
    state: TransportState,
    emitted: dict[str, Pocket],
    hyd_state: HydraulicState,
) -> TransportState:
    """Inject inventory-emitted pockets into transport edge queues.

    Called by the worker after the inventory emit step.  Each emitted pocket
    is appended to the **inflow end** of the specified edge, determined by the
    flow direction in *hyd_state*:

    - ``Q ≥ 0``: inflow at the origin end → ``append`` (right).
    - ``Q < 0``: inflow at the destination end → ``appendleft`` (left).

    Parameters
    ----------
    state:
        The transport state to augment (a shallow copy is made internally).
    emitted:
        ``{edge_id: Pocket}`` — one pocket per edge to inject.  Provided by
        the inventory module, which resolves the edge from the port's access
        type (TOP → gas, BOTTOM → liquid).
    hyd_state:
        Flows from the most recent hydraulic solve, used to determine which
        end of the edge is the inflow end.

    Returns
    -------
    TransportState
        New state with the emitted pockets inserted.
    """
    new_queues = {eid: deque(q) for eid, q in state.edge_queues.items()}

    for edge_id, pocket in emitted.items():
        if pocket.volume < MIN_POCKET_VOLUME:
            continue
        queue = new_queues.get(edge_id)
        if queue is None:
            continue
        Q = hyd_state.flows.get(edge_id, 0.0)
        if Q >= 0:
            queue.append(pocket)
        else:
            queue.appendleft(pocket)

    return TransportState(edge_queues=new_queues)
