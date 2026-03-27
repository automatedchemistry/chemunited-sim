"""Adapter module - compiles ComponentData + EdgeData into a HydraulicGraph.

Public interface:
    compile_graph(components, edges) -> HydraulicGraph
"""
from .graph import compile_graph
from .models import HydraulicEdge, HydraulicGraph, HydraulicNode

__all__ = ["compile_graph", "HydraulicNode", "HydraulicEdge", "HydraulicGraph"]
