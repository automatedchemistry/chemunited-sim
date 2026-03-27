"""Inventory runtime state for chemunited-sim vessels.

``InventoryState`` is a mutable dataclass that holds the thermodynamic state
of one vessel inventory node.  It is mutated in-place by ``assimilate`` and
``emit`` each time step.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class InventoryState:
    """Mutable runtime state for one vessel inventory node.

    A vessel is always full: ``liq_volume + gas_volume == capacity`` is the
    invariant maintained by the hydraulic solver's flow-conservation
    guarantee.  The inventory module does not enforce this per-step; it relies
    on the solver.

    All quantities are in SI units.

    Attributes
    ----------
    node_id:
        Inventory node identifier, e.g. ``"vessel1.Inventory"``.
    capacity:
        Fixed geometric volume of the vessel in m³.  Set at construction
        from ``InventoryNode.liq_content.volume + gas_content.volume``; never
        changed at runtime.
    pressure:
        Current vessel pressure in Pa.  Written from ``HydraulicState``
        at the start of each ``assimilate`` call before volumes change.
    temperature:
        Single shared temperature in K (thermal equilibrium assumption —
        both liquid and gas phases share one temperature and reach
        equilibrium instantaneously).
    liq_volume:
        Current liquid volume in m³.
    gas_volume:
        Current gas volume in m³.
    liq_species_moles:
        Liquid-phase composition: ``{species_id: moles}``.
        Empty dict means pure liquid carrier (no tracked species).
    gas_species_moles:
        Gas-phase composition: ``{species_id: moles}``.
        Empty dict means pure gas carrier (air).
    """

    node_id: str
    capacity: float
    pressure: float
    temperature: float
    liq_volume: float
    gas_volume: float
    liq_species_moles: dict[str, float] = field(default_factory=dict)
    gas_species_moles: dict[str, float] = field(default_factory=dict)
