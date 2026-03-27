"""Hydraulic network solver for chemunited-sim.

Builds a sparse nodal admittance system from a :class:`HydraulicGraph`,
applies boundary conditions, anchors pressure-undetermined connected
components to atmospheric pressure, and solves for node pressures and
edge flows using ``scipy.sparse.linalg.spsolve``.
"""

from __future__ import annotations

import math

import numpy as np
import scipy.sparse
import scipy.sparse.csgraph
import scipy.sparse.linalg

from chemunited_core.common.constant import ATMOSPHERE_PRESSURE_PA
from chemunited_core.components.enums import BoundaryConditionKind, InternalEdgeRole

from ..adapter.models import HydraulicEdge, HydraulicGraph
from ..common.constant import ETA_WATER_25C, R_JUNCTION
from .models import HydraulicSolveError, HydraulicState


def _compute_resistance(edge: HydraulicEdge, viscosity: float) -> float:
    """Return the hydraulic resistance for a single edge in Pa·s/m³.

    Decision tree:

    1. ``resistance_override is not None`` → return the override directly.
    2. ``role == JUNCTION`` → return :data:`R_JUNCTION` (1 µPa·s/m³).
    3. Otherwise (TRANSPORT) → Hagen–Poiseuille:
       ``R = (128·η·L) / (π·D⁴)``.

    Parameters
    ----------
    edge:
        The hydraulic edge to evaluate.
    viscosity:
        Dynamic viscosity of the carrier fluid in Pa·s.

    Returns
    -------
    float
        Resistance in Pa·s/m³.

    Raises
    ------
    HydraulicSolveError
        If the edge is a TRANSPORT edge with non-positive diameter.
    """
    if edge.resistance_override is not None:
        return edge.resistance_override
    if edge.role == InternalEdgeRole.JUNCTION:
        return R_JUNCTION
    # TRANSPORT: Hagen–Poiseuille
    if edge.diameter <= 0.0:
        raise HydraulicSolveError(
            f"Edge '{edge.edge_id}': TRANSPORT edge has diameter={edge.diameter} m — "
            "a positive diameter is required to compute Hagen-Poiseuille resistance."
        )
    return (128.0 * viscosity * edge.length) / (math.pi * edge.diameter**4)


def solve(
    graph: HydraulicGraph,
    viscosity: float = ETA_WATER_25C,
) -> HydraulicState:
    """Solve steady-state pressures and volumetric flows for a HydraulicGraph.

    The solver performs four passes:

    1. **Conductance stamping** — builds the sparse nodal admittance matrix
       *Y* and the right-hand-side vector *Q* by iterating all edges.
    2. **Component detection** — finds connected subgraphs using
       ``scipy.sparse.csgraph.connected_components`` on the structural
       adjacency (before any row replacement).
    3. **Boundary conditions** — applies Dirichlet (PRESSURE) BCs via row
       replacement and Neumann (FLOW) BCs via RHS addition.
    4. **Atmospheric anchor** — any connected component that has no
       Dirichlet BC is pinned to :data:`ATMOSPHERE_PRESSURE_PA` at its
       first node to prevent a singular system.

    After ``scipy.sparse.linalg.spsolve``, edge flows are back-computed as
    ``Q_ij = G_ij · (P_i − P_j)`` (signed: positive = origin → destination).

    Parameters
    ----------
    graph:
        The compiled hydraulic network to solve.
    viscosity:
        Dynamic viscosity of the carrier fluid in Pa·s.
        Defaults to water at 25 °C (8.9 × 10⁻⁴ Pa·s).

    Returns
    -------
    HydraulicState
        Absolute pressures (Pa) at every node and signed volumetric flows
        (m³/s) on every edge.

    Raises
    ------
    HydraulicSolveError
        If a TRANSPORT edge has a non-positive diameter, or if the linear
        system cannot be solved despite the atmospheric anchor.
    """
    nodes = graph.nodes
    edges = graph.edges

    if not nodes:
        return HydraulicState(pressures={}, flows={})

    # ------------------------------------------------------------------
    # Index: node_id → row/column position in the matrix
    # ------------------------------------------------------------------
    node_ids: list[str] = list(nodes.keys())
    node_idx: dict[str, int] = {nid: i for i, nid in enumerate(node_ids)}
    n = len(node_ids)

    # ------------------------------------------------------------------
    # Pass 1: stamp conductances into the nodal admittance matrix
    # ------------------------------------------------------------------
    Y = scipy.sparse.lil_matrix((n, n), dtype=float)
    Q = np.zeros(n, dtype=float)
    edge_conductances: dict[str, float] = {}

    for edge_id, edge in edges.items():
        R = _compute_resistance(edge, viscosity)
        G = 1.0 / R
        i = node_idx[edge.origin_node_id]
        j = node_idx[edge.destination_node_id]
        Y[i, i] += G
        Y[j, j] += G
        Y[i, j] -= G
        Y[j, i] -= G
        edge_conductances[edge_id] = G

    # ------------------------------------------------------------------
    # Pass 2: connected-component detection (structural, before BC rows)
    # ------------------------------------------------------------------
    adj_rows: list[int] = []
    adj_cols: list[int] = []
    for edge in edges.values():
        i = node_idx[edge.origin_node_id]
        j = node_idx[edge.destination_node_id]
        adj_rows += [i, j]
        adj_cols += [j, i]

    adj = scipy.sparse.csr_matrix(
        (np.ones(len(adj_rows), dtype=float), (adj_rows, adj_cols)),
        shape=(n, n),
    )
    n_components, labels = scipy.sparse.csgraph.connected_components(
        adj, directed=False
    )

    # ------------------------------------------------------------------
    # Pass 3: boundary conditions
    # ------------------------------------------------------------------
    dirichlet_components: set[int] = set()

    for node_id, node in nodes.items():
        if node.boundary is None:
            continue
        i = node_idx[node_id]
        bc = node.boundary
        if bc.kind == BoundaryConditionKind.PRESSURE:
            # Dirichlet: replace row with identity, pin pressure in RHS
            Y[i, :] = 0.0
            Y[i, i] = 1.0
            Q[i] = bc.value
            dirichlet_components.add(int(labels[i]))
        elif bc.kind == BoundaryConditionKind.FLOW:
            # Neumann: add external injection to RHS (positive = source)
            Q[i] += bc.value

    # ------------------------------------------------------------------
    # Pass 4: anchor pressure-undetermined components to atmospheric
    # ------------------------------------------------------------------
    for comp_idx in range(n_components):
        if comp_idx not in dirichlet_components:
            anchor = int(np.where(labels == comp_idx)[0][0])
            Y[anchor, :] = 0.0
            Y[anchor, anchor] = 1.0
            Q[anchor] = float(ATMOSPHERE_PRESSURE_PA)

    # ------------------------------------------------------------------
    # Solve Y · P = Q
    # ------------------------------------------------------------------
    Y_csr = Y.tocsr()
    try:
        P: np.ndarray = scipy.sparse.linalg.spsolve(Y_csr, Q)
    except Exception as exc:
        raise HydraulicSolveError(
            f"Hydraulic solve failed — check graph connectivity: {exc}"
        ) from exc

    pressures: dict[str, float] = {
        node_ids[i]: float(P[i]) for i in range(n)
    }

    # ------------------------------------------------------------------
    # Back-compute signed edge flows: Q_ij = G_ij · (P_i − P_j)
    # ------------------------------------------------------------------
    flows: dict[str, float] = {}
    for edge_id, edge in edges.items():
        G = edge_conductances[edge_id]
        i = node_idx[edge.origin_node_id]
        j = node_idx[edge.destination_node_id]
        flows[edge_id] = G * (float(P[i]) - float(P[j]))

    return HydraulicState(pressures=pressures, flows=flows)
