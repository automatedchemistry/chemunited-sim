"""Top-level graph compiler for the adapter layer.

:func:`compile_graph` is the single entry point that consumes a flat list of
``ComponentData`` objects and ``EdgeData`` connections and produces a fully
resolved :class:`~chemunited_sim.adapter.models.HydraulicGraph` ready for the
simulation kernel.
"""

from __future__ import annotations

import copy

from chemunited_core.common.enums import ConnectionType
from chemunited_core.components import ComponentData, NeutralComponentData
from chemunited_core.components.enums import InternalEdgeRole
from chemunited_core.components.internals import InventoryNode
from chemunited_core.components.plugflow import PlugFlowComponentData
from chemunited_core.connections.edge import EdgeData
from loguru import logger

from .compilers import _COMPILERS, compile_component
from .models import HeatLink, HydraulicEdge, HydraulicGraph, HydraulicNode


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


def _port_category(
    comp: ComponentData, port_number: int | str
) -> ConnectionType | None:
    port = comp.ports_by_number.get(port_number)
    return None if port is None else port.category


def _is_heat_provider(comp: ComponentData) -> bool:
    return isinstance(comp, NeutralComponentData)


def _is_heat_target(comp: ComponentData) -> bool:
    return bool(getattr(comp, "heat_exchange", False)) and not _is_heat_provider(comp)


def _compile_heat_link(
    edge_data: EdgeData,
    components_by_name: dict[str, ComponentData],
    linked_targets: set[str],
) -> HeatLink:
    endpoints = [
        (edge_data.origin, edge_data.origin_port),
        (edge_data.destination, edge_data.destination_port),
    ]
    resolved: list[tuple[ComponentData, int | str]] = []

    for comp_name, port_number in endpoints:
        comp = components_by_name.get(comp_name)
        if comp is None:
            raise ValueError(
                f"HEAT edge '{edge_data.name}' references unknown component "
                f"'{comp_name}'"
            )
        if _port_category(comp, port_number) != ConnectionType.HEAT:
            raise ValueError(
                f"HEAT edge '{edge_data.name}' endpoint "
                f"'{comp_name}.{port_number}' is not a HEAT port"
            )
        resolved.append((comp, port_number))

    providers = [(comp, port) for comp, port in resolved if _is_heat_provider(comp)]
    targets = [(comp, port) for comp, port in resolved if _is_heat_target(comp)]

    if len(providers) != 1 or len(targets) != 1:
        names = ", ".join(f"{comp.name}.{port}" for comp, port in resolved)
        raise ValueError(
            f"HEAT edge '{edge_data.name}' must connect exactly one thermal "
            f"controller to one heat-enabled target; got {names}"
        )

    provider, provider_port = providers[0]
    target, target_port = targets[0]
    if target.name in linked_targets:
        raise ValueError(
            f"Component '{target.name}' has multiple HEAT controllers; only one "
            "thermal provider per target is supported"
        )
    linked_targets.add(target.name)

    return HeatLink(
        provider_component=provider.name,
        provider_port=provider_port,
        target_component=target.name,
        target_port=target_port,
    )


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
       non-HYDRAULIC edges silently, logs warnings for edges whose
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
        are silently ignored.  Edges whose endpoint nodes are missing log a
        warning and are skipped.

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
    components_by_name: dict[str, ComponentData] = {}

    # ------------------------------------------------------------------
    # Pass 1: component compilers
    # ------------------------------------------------------------------
    for comp in components:
        if comp.name in seen_names:
            raise ValueError(f"Duplicate component name '{comp.name}'")
        seen_names.add(comp.name)
        components_by_name[comp.name] = comp

        comp.apply_air_defaults()

        compiler = next(
            (_COMPILERS[cls] for cls in type(comp).mro() if cls in _COMPILERS),
            compile_component,
        )
        nodes, comp_edges = compiler(comp)

        for node in nodes:
            all_nodes[node.node_id] = node
        for edge in comp_edges:
            if (
                isinstance(comp, PlugFlowComponentData)
                and edge.role == InternalEdgeRole.TRANSPORT
            ):
                edge.content = list(comp.content)
            all_edges[edge.edge_id] = edge

        # Inventory snapshot (deep copy to decouple from live component state)
        if comp.internal_inventory is not None:
            inv_id = f"{comp.name}.Inventory"
            all_inventory[inv_id] = copy.deepcopy(comp.internal_inventory)

    # ------------------------------------------------------------------
    # Pass 2: external edges
    # ------------------------------------------------------------------
    for edge_data in edges:
        if edge_data.classification != ConnectionType.HYDRAULIC:
            continue  # silently skip non-HYDRAULIC edges

        origin_node_id = f"{edge_data.origin}.{edge_data.origin_port}"
        dest_node_id = f"{edge_data.destination}.{edge_data.destination_port}"

        if origin_node_id not in all_nodes:
            logger.warning(
                "compile_graph: skipping edge '{}' - origin node '{}' not in graph "
                "(port may be CAPPED or component missing).",
                edge_data.name,
                origin_node_id,
            )
            continue
        if dest_node_id not in all_nodes:
            logger.warning(
                "compile_graph: skipping edge '{}' - destination node '{}' not in "
                "graph "
                "(port may be CAPPED or component missing).",
                edge_data.name,
                dest_node_id,
            )
            continue

        if edge_data.name in all_edges:
            raise ValueError(f"Duplicate external edge ID '{edge_data.name}'")

        edge_data.apply_air_defaults()

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
            content=list(edge_data.content),
        )

    # ------------------------------------------------------------------
    # Pass 3: heat links
    # ------------------------------------------------------------------
    heat_links: list[HeatLink] = []
    linked_heat_targets: set[str] = set()
    for edge_data in edges:
        if edge_data.classification != ConnectionType.HEAT:
            continue
        heat_links.append(
            _compile_heat_link(edge_data, components_by_name, linked_heat_targets)
        )

    # ------------------------------------------------------------------
    # Assembly
    # ------------------------------------------------------------------
    return HydraulicGraph(
        nodes=all_nodes,
        edges=all_edges,
        inventory_nodes=all_inventory,
        heat_links=heat_links,
    )
