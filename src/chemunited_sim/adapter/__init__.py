"""Adapter module - compiles ComponentData + EdgeData into a HydraulicGraph.

Public interface:
    compile_graph(components, edges) -> HydraulicGraph
    resync_component(graph, comp) -> None
"""

from .graph import compile_graph, resync_component
from .models import HydraulicEdge, HydraulicGraph, HydraulicNode

__all__ = [
    "compile_graph",
    "resync_component",
    "HydraulicNode",
    "HydraulicEdge",
    "HydraulicGraph",
]
