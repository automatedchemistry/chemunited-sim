"""Reaction engine for chemunited-sim.

:func:`apply` iterates the reactions map and calls each reaction's ``step``
method against the corresponding :class:`~chemunited_sim.inventory.InventoryState`.
"""

from __future__ import annotations

from ..inventory.models import InventoryState
from .models import ReactionsMap


def apply(
    states: dict[str, InventoryState],
    reactions_map: ReactionsMap,
    dt: float,
) -> None:
    """Apply all registered reactions to their inventory nodes for one step.

    Reactions execute sequentially within each node in the order they appear
    in the list.  Products of reaction *N* are available as inputs to
    reaction *N+1* within the same step, enabling simple reaction cascades
    without a coupled solver.

    Nodes that are absent from *reactions_map* are untouched.  Nodes present
    in *reactions_map* but absent from *states* (e.g. removed mid-run) are
    silently skipped.

    Mutates *states* in-place; returns ``None``.

    Parameters
    ----------
    states:
        All live inventory states, keyed by ``inv_node_id``.
    reactions_map:
        ``{inv_node_id: [Reaction, ...]}`` — the per-node reaction registry.
        An empty list at a key is a no-op (equivalent to
        :class:`~chemunited_sim.reactions.models.NullReaction`).
    dt:
        Simulation time step in seconds.
    """
    for inv_node_id, reactions in reactions_map.items():
        state = states.get(inv_node_id)
        if state is None:
            continue
        for reaction in reactions:
            reaction.step(state, dt)
