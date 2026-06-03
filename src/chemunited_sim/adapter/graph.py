"""Top-level graph compiler for the adapter layer.

:func:`compile_graph` is the single entry point that consumes a flat list of
``ComponentData`` objects and ``EdgeData`` connections and produces a fully
resolved :class:`~chemunited_sim.adapter.models.HydraulicGraph` ready for the
simulation kernel.
"""

from __future__ import annotations

import copy
import warnings

from chemunited_core.common.enums import ConnectionType
from chemunited_core.components import BackPressureRegulatorData, ComponentData
from chemunited_core.components.enums import InternalEdgeRole
from chemunited_core.components.internals import InventoryNode
from chemunited_core.connections.edge import EdgeData

from .compilers import _COMPILERS, compile_component
from .models import HydraulicEdge, HydraulicGraph, HydraulicNode


def resync_component(graph: HydraulicGraph, comp: ComponentData) -> None:
    """Propagate resistance_override from ComponentData into HydraulicGraph.

    Called immediately after ComponentData.apply(). Topology is never changed;
    only edge attributes are written.

    HydraulicNode.boundary is a shared reference (not deep-copied at compile
    time), so flow-source and pressure-control updates propagate automatically
    via sync_internal_state() — no node propagation needed here.
    """
    for (origin, dest), internal_edge in comp.internal_edges.items():
        edge_id = f"{comp.name}.{origin}.{dest}"
        hydraulic_edge = graph.edges.get(edge_id)
        if hydraulic_edge is not None:
            hydraulic_edge.resistance_override = internal_edge.resistance_override


def compile_graph(
    components: list[ComponentData],
    edges: list[EdgeData],
) -> HydraulicGraph:
    """Compile a component-and-connection description into a HydraulicGraph.

    The function performs three passes:

    1. **Component pass** — dispatches each component to its dedicated
       compiler (or the generic fallback), collects nodes and internal edges,
       takes a deep-copy snapshot of any ``internal_inventory``, and registers
       BPR edge IDs.

    2. **External-edge pass** — iterates ``EdgeData`` connections, skips
       non-HYDRAULIC edges silently, emits ``UserWarning`` for edges whose
       endpoint nodes are absent (e.g. CAPPED ports), and inserts
       ``TRANSPORT`` edges with geometry taken from ``EdgeData``.

    3. **Assembly** — wraps everything in a :class:`HydraulicGraph` and
       returns it.

    Parameters
    ----------
    components:
        All component instances that belong to the simulation domain.
        Component names must be unique; a ``ValueError`` is raised otherwise.
    edges:
        All directed connections between component ports.  Non-hydraulic edges
        are silently ignored.  Edges whose endpoint nodes are missing emit a
        ``UserWarning`` and are skipped.

    Returns
    -------
    HydraulicGraph
        A fully populated graph containing nodes, internal and external edges,
        inventory snapshots, and the BPR edge registry.

    Raises
    ------
    ValueError
        If two components share the same name, or if two external edges share
        the same ``EdgeData.name``.

    Notes
    -----
    - All length values are in metres (m).
    - All diameter values are in metres (m).
    - Resistance values are in Pa·s/m³.
    """
    seen_names: set[str] = set()
    all_nodes: dict[str, HydraulicNode] = {}
    all_edges: dict[str, HydraulicEdge] = {}
    all_inventory: dict[str, InventoryNode] = {}
    bpr_edge_ids: list[str] = []

    # ------------------------------------------------------------------
    # Pass 1: component compilers
    # ------------------------------------------------------------------
    for comp in components:
        if comp.name in seen_names:
            raise ValueError(f"Duplicate component name '{comp.name}'")
        seen_names.add(comp.name)

        compiler = next(
            (_COMPILERS[cls] for cls in type(comp).mro() if cls in _COMPILERS),
            compile_component,
        )
        nodes, comp_edges = compiler(comp)

        for node in nodes:
            all_nodes[node.node_id] = node
        for edge in comp_edges:
            all_edges[edge.edge_id] = edge

        # Inventory snapshot (deep copy to decouple from live component state)
        if comp.internal_inventory is not None:
            inv_id = f"{comp.name}.Inventory"
            all_inventory[inv_id] = copy.deepcopy(comp.internal_inventory)

        # Register BPR edges for the in-place worker
        if isinstance(comp, BackPressureRegulatorData):
            bpr_edge_ids.extend(e.edge_id for e in comp_edges)

    # ------------------------------------------------------------------
    # Pass 2: external edges
    # ------------------------------------------------------------------
    for edge_data in edges:
        if edge_data.classification != ConnectionType.HYDRAULIC:
            continue  # silently skip non-HYDRAULIC edges

        origin_node_id = f"{edge_data.origin}.{edge_data.origin_port}"
        dest_node_id = f"{edge_data.destination}.{edge_data.destination_port}"

        if origin_node_id not in all_nodes:
            warnings.warn(
                f"compile_graph: skipping edge '{edge_data.name}' — "
                f"origin node '{origin_node_id}' not in graph "
                f"(port may be CAPPED or component missing).",
                UserWarning,
                stacklevel=2,
            )
            continue
        if dest_node_id not in all_nodes:
            warnings.warn(
                f"compile_graph: skipping edge '{edge_data.name}' — "
                f"destination node '{dest_node_id}' not in graph "
                f"(port may be CAPPED or component missing).",
                UserWarning,
                stacklevel=2,
            )
            continue

        if edge_data.name in all_edges:
            raise ValueError(f"Duplicate external edge ID '{edge_data.name}'")

        all_edges[edge_data.name] = HydraulicEdge(
            edge_id=edge_data.name,
            origin_node_id=origin_node_id,
            destination_node_id=dest_node_id,
            length=edge_data.length_value,
            diameter=edge_data.diameter_value,
            role=InternalEdgeRole.TRANSPORT,
            resistance_override=None,
            component=None,
            is_external=True,
        )

    # ------------------------------------------------------------------
    # Assembly
    # ------------------------------------------------------------------
    return HydraulicGraph(
        nodes=all_nodes,
        edges=all_edges,
        inventory_nodes=all_inventory,
        bpr_edges=bpr_edge_ids,
    )
