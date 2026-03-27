"""Initial transport state builder for chemunited-sim.

Fills every TRANSPORT-role edge with a single air pocket sized to the
geometric internal volume of the edge.  JUNCTION-role edges are excluded
(they are lossless connectors with no physical volume).
"""

from __future__ import annotations

import math
from collections import deque

from chemunited_core.common.enums import PhaseKind
from chemunited_core.components.enums import InternalEdgeRole

from ..adapter.models import HydraulicGraph
from ..common.constant import (
    AMBIENT_TEMPERATURE_K,
    ATMOSPHERE_PRESSURE_PA,
    MIN_POCKET_VOLUME,
)
from .models import Pocket, TransportState


def build_initial_state(graph: HydraulicGraph) -> TransportState:
    """Create a :class:`TransportState` with all transport edges filled with air.

    Each TRANSPORT edge receives one :class:`Pocket` whose volume equals the
    geometric cross-sectional volume of the edge:
    ``V = π · (D/2)² · L``.

    Edges with zero or negative diameter are assigned an empty queue.
    Edges whose computed volume falls below
    :data:`~chemunited_sim.common.constant.MIN_POCKET_VOLUME` are also left
    empty.

    Parameters
    ----------
    graph:
        The compiled hydraulic graph whose edges define edge geometry.

    Returns
    -------
    TransportState
        One entry per TRANSPORT edge in ``graph.edges``.
    """
    edge_queues: dict[str, deque[Pocket]] = {}

    for edge_id, edge in graph.edges.items():
        if edge.role != InternalEdgeRole.TRANSPORT:
            continue

        if edge.diameter <= 0.0 or edge.length <= 0.0:
            edge_queues[edge_id] = deque()
            continue

        volume = math.pi * (edge.diameter / 2.0) ** 2 * edge.length

        if volume < MIN_POCKET_VOLUME:
            edge_queues[edge_id] = deque()
            continue

        edge_queues[edge_id] = deque(
            [
                Pocket(
                    phase_kind=PhaseKind.GAS,
                    volume=volume,
                    species_moles={},
                    temperature=AMBIENT_TEMPERATURE_K,
                    pressure=ATMOSPHERE_PRESSURE_PA,
                )
            ]
        )

    return TransportState(edge_queues=edge_queues)
