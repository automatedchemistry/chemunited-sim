"""PlatformBuilder — constructs ComponentData and EdgeData from a draw/setup.py."""

from __future__ import annotations

from chemunited_core.common.enums import ConnectionType, PhaseKind
from chemunited_core.components import ComponentData, NeutralComponentData
from chemunited_core.compounds import COMPOUNDS, ChemicalEntity
from chemunited_core.connections.edge import EdgeData, EdgeMode
from chemunited_core.figure_registry import COMPONENTS
from loguru import logger

from ..reactions import FirstOrderDecay, ReactionsMap


class PlatformBuilder:
    """Accumulates components and edges produced by build_draw(platform).

    Mirrors the API expected by draw/setup.py scripts so they can be run
    in-process instead of through the GUI.
    """

    def __init__(self) -> None:
        self._components: dict[str, ComponentData] = {}
        self._edges: list[EdgeData] = []
        self._reactions_map: ReactionsMap = {}

    def add_component(
        self,
        name: str,
        figure: str,
        position: tuple[float, float] = (0.0, 0.0),
        angle: int = 0,
        **kwargs,
    ) -> ComponentData:
        if figure not in COMPONENTS:
            raise ValueError(
                f"Unknown figure '{figure}'. Available: {list(COMPONENTS)}"
            )
        if name in self._components:
            raise ValueError(f"Duplicate component name '{name}'")

        data_cls, mode_cls = COMPONENTS[figure]
        mode = mode_cls(name=name, position=position, angle=angle, **kwargs)
        data = data_cls.from_mode(mode)
        self._components[name] = data
        logger.debug("Component added | name='{}' figure='{}'", name, figure)
        return data

    def add_connection(
        self,
        origin: str,
        destiny: str,
        origin_port: int,
        destiny_port: int,
        **kwargs,
    ) -> EdgeData:
        length = kwargs.pop("length", "100 mm")
        diameter = kwargs.pop("diameter", "1 mm")
        classification = kwargs.pop("classification", ConnectionType.HYDRAULIC)
        name = kwargs.pop("name", f"{origin}.{origin_port}-{destiny}.{destiny_port}")

        mode = EdgeMode(
            name=name,
            origin=origin,
            destination=destiny,
            origin_port=origin_port,
            destination_port=destiny_port,
            length=length,
            diameter=diameter,
            classification=classification,
            **kwargs,
        )
        edge = EdgeData.from_mode(mode)
        self._edges.append(edge)
        logger.debug(
            "Edge added | {}.{} -> {}.{}", origin, origin_port, destiny, destiny_port
        )
        return edge

    def add_compound(
        self,
        name: str = "reagent_a",
        molecular_weight: float = 120.0,
        cp_liquid: float = 150.0,
        density_liquid: float = 1050.0,
        cp_gas: float = 29.0,
    ):
        if name in COMPOUNDS:
            raise ValueError(f"Compound '{name}' already exists")
        COMPOUNDS.register(
            ChemicalEntity(
                name=name,
                molecular_weight=molecular_weight,
                cp_liquid=cp_liquid,
                cp_gas=cp_gas,
                density_liquid=density_liquid,
            )
        )
        logger.debug("Compound registered | name='{}' MW={}", name, molecular_weight)

    def add_reaction(
        self,
        target: str,
        reaction_type: str,
        reactant: str,
        product: str,
        rate_constant: float,
        phase: str | PhaseKind = "LIQUID",
        delta_temperature_per_mol_converted: float = 0.0,
    ) -> None:
        if reaction_type != "FirstOrderDecay":
            raise ValueError(
                f"Unknown reaction_type '{reaction_type}'. "
                "Only 'FirstOrderDecay' is supported."
            )
        node_id = f"{target}.Inventory"
        phase_kind = PhaseKind(phase.lower()) if isinstance(phase, str) else phase
        reaction = FirstOrderDecay(
            reactant=reactant,
            product=product,
            rate_constant=rate_constant,
            phase=phase_kind,
            delta_temperature_per_mol_converted=delta_temperature_per_mol_converted,
        )
        self._reactions_map.setdefault(node_id, []).append(reaction)
        logger.debug(
            "Reaction added | target='{}' type='{}' reactant='{}' -> product='{}'",
            target,
            reaction_type,
            reactant,
            product,
        )

    @property
    def reactions_map(self) -> ReactionsMap:
        return dict(self._reactions_map)

    def add_component_data(self, data: ComponentData) -> ComponentData:
        """Add a pre-created ComponentData directly, bypassing the figure registry."""
        if data.name in self._components:
            raise ValueError(f"Duplicate component name '{data.name}'")
        self._components[data.name] = data
        return data

    def __getitem__(self, name: str) -> ComponentData:
        return self._components[name]

    @property
    def hydraulic_components(self) -> list[ComponentData]:
        return [
            c
            for c in self._components.values()
            if not isinstance(c, NeutralComponentData)
        ]

    @property
    def components(self) -> list[ComponentData]:
        return list(self._components.values())

    @property
    def edges(self) -> list[EdgeData]:
        return list(self._edges)
