# Spec: Step 1 — CLI launch (`chemunited-sim`)

**Status:** planned  
**Scope:** Add a terminal entry point that loads a project, compiles the hydraulic graph, and runs the simulation.

---

## Goal

Enable the user to run a simulation directly from the terminal:

```shell
chemunited-sim path/to/project.chemunited
```

The project path can be either a `.chemunited` ZIP archive or an uncompressed project folder.

---

## New files

```
src/chemunited_sim/cli/
├── __init__.py
├── builder.py     # PlatformBuilder
├── loader.py      # project discovery + dynamic import of setup.py
└── main.py        # argparse entry point + orchestration
```

---

## Modified files

### `pyproject.toml`

Add the console script entry point:

```toml
[project.scripts]
chemunited-sim = "chemunited_sim.cli.main:main"
```

---

## Project structure expected by the loader

A project exposes its topology via `draw/setup.py`:

```
my_project/
└── draw/
    └── setup.py   ← must define build_draw(platform)
```

For distributed projects the same folder is packed as a ZIP:

```
my_project.chemunited   ← ZIP archive containing draw/setup.py
```

---

## `draw/setup.py` — user contract

The file must define one function:

```python
def build_draw(platform):  # platform is a PlatformBuilder instance
    ...
```

Physical quantity kwargs are passed as plain strings — `ChemUnitQuantity` conversion
is handled internally by the underlying Pydantic `ModeClass` validators.

Example:

```python
def build_draw(platform):
    platform.add_component(
        name="PumpA",
        figure="SyringePump",
        position=(0.0, 0.0),
        angle=0,
        flow_rate="3 ml/min",
    )
    platform.add_component(
        name="ReactorA",
        figure="FlowReactor",
        position=(200.0, 0.0),
        angle=0,
        length="10 m",
        diameter="1 mm",
    )
    platform.add_connection(
        origin="PumpA",
        destiny="ReactorA",
        origin_port=1,
        destiny_port=1,
        length="5 cm",
        diameter="1 mm",
    )
```

---

## `cli/builder.py` — `PlatformBuilder`

### `add_component`

```python
def add_component(
    self,
    name: str,
    figure: str,
    position: tuple[float, float] = (0.0, 0.0),
    angle: int = 0,
    **kwargs,
) -> ComponentData:
```

**Behaviour:**

- Looks up `figure` in `COMPONENTS` from `chemunited_core.figure_registry`.
- Raises `ValueError` if the figure name is unknown (includes the list of valid names in the message).
- Gets the `(DataClass, ModeClass)` pair from the registry.
- Calls `ModeClass(name=name, figure=figure, position=position, angle=angle, **kwargs)`.
  `name`, `figure`, `position`, and `angle` are all standard `ComponentMode` fields, so they
  flow through without any special handling. Any other `ModeClass` field (e.g. `flow_rate`,
  `capacity`, `length`, `diameter`) can be passed as a kwarg as a plain string — Pydantic's
  `ChemQuantityValidator` handles the conversion internally.
- Calls `DataClass.from_mode(mode)` to build the component.
- Raises `ValueError` if a component with the same `name` was already added.
- Stores the component internally and returns it, so the caller can further configure it if needed.

### `add_connection`

```python
def add_connection(
    self,
    origin: str,
    destiny: str,
    origin_port: int,
    destiny_port: int,
    **kwargs,
) -> EdgeData:
```

**Behaviour:**

- Passes all arguments (including `**kwargs`) directly to `EdgeMode`.
- `EdgeMode` natively accepts `destiny` and `destiny_port` via `AliasChoices`, so no translation is needed.
- Any `EdgeMode` field can be overridden via kwargs: `length`, `diameter`, `classification`, `straight_path`, `air_pressure_line`.
- `EdgeMode` defaults apply when kwargs are not supplied: `length="100 mm"`, `diameter="1 mm"`, `classification=HYDRAULIC`.
- String values for physical quantities (e.g. `"5 cm"`) are accepted and converted internally by Pydantic.
- `EdgeMode.check_flow_rules` automatically zeroes `length` and `diameter` for non-HYDRAULIC edges.
- Calls `EdgeData.from_mode(mode)` and appends to the internal edge list.
- `EdgeData.name` is a computed property (`"{origin}_{port}_{destination}_{port}"`), so no manual name generation is needed.

### `hydraulic_components` property

Returns all components that are **not** `NeutralComponentData` instances:

```python
@property
def hydraulic_components(self) -> list[ComponentData]:
    return [c for c in self._components.values()
            if not isinstance(c, NeutralComponentData)]
```

Figures filtered out by this rule (from the current registry):

| Figure | Type |
|---|---|
| `TemperatureControl` | `TemperatureControlData(NeutralComponentData)` |
| `PeltierCoolerTemperatureControl` | `PeltierCoolerTemperatureControlData(NeutralComponentData)` |
| `MultiChannelADC/DAC/Relay` | `MultiChannelData(NeutralComponentData)` |
| `PhotoSensor` | `NeutralComponentData` |
| `PowerControl` | `NeutralComponentData` |
| `PowerSwitch` | `NeutralComponentData` |
| `LengthControl` | `NeutralComponentData` |
| `PhidgetBubbleSensorPowerComponent` | `NeutralComponentData` |

Generic `ComponentData` figures (e.g. `HPLCPump`, `FlowMeter`, `MFCComponent`) are **not** filtered — they pass through to `compile_graph` and produce empty nodes/edges via the generic fallback compiler, which is already handled gracefully.

### `components` property

Returns all components including neutral ones (full list, for inspection):

```python
@property
def components(self) -> list[ComponentData]:
    return list(self._components.values())
```

### `edges` property

Returns all edges (hydraulic and non-hydraulic):

```python
@property
def edges(self) -> list[EdgeData]:
    return list(self._edges)
```

Non-hydraulic edges are silently skipped by `compile_graph`, so passing all edges is safe.

---

## `cli/loader.py` — project discovery

### Discovery order

Given a `path` argument:

1. **`path` is a directory** → use `path/draw/setup.py` directly.
2. **`path` is a `.chemunited` ZIP and a same-name sibling folder exists** → use the sibling folder's `draw/setup.py`, ignore the ZIP.
3. **`path` is a `.chemunited` ZIP and no sibling folder exists** → extract the ZIP to `tempfile.mkdtemp(prefix="chemunited_")` and use `draw/setup.py` from there. The temp directory is left on disk (read-only usage, no cleanup needed).

```
my_project.chemunited   ← ZIP
my_project/             ← sibling folder (if present, takes priority)
└── draw/
    └── setup.py
```

### Errors raised

| Condition | Exception |
|---|---|
| `path` does not exist | `FileNotFoundError` |
| `path` suffix is not a directory or `.chemunited` | `ValueError` |
| ZIP is not a valid ZIP file | `ValueError` |
| `draw/setup.py` not found after resolution | `FileNotFoundError` |
| `setup.py` does not define `build_draw` | `AttributeError` |

### Dynamic import

`setup.py` is imported with `importlib.util.spec_from_file_location` under the module name
`"project_setup"`. The `build_draw` function is called with a fresh `PlatformBuilder` instance.
The populated builder is returned.

---

## `cli/main.py` — entry point

### Arguments (Step 1 scope)

| Argument | Type | Description |
|---|---|---|
| `project` | `Path` (positional) | Path to a `.chemunited` file or project folder |

Arguments deferred to Step 2: `--dt`, `--t-end`, `--output`, `--viscosity`.

### Execution flow

```
parse args
    └─ load_project(path)          # loader.py
        └─ build_draw(builder)     # draw/setup.py
    └─ compile_graph(
           builder.hydraulic_components,
           builder.edges,
       )                           # adapter.graph
    └─ Worker(graph, components, SimConfig(...)).run()
    └─ print("Simulation complete.")
```

### Default `SimConfig` for Step 1

```python
SimConfig(dt=0.1, t_end=60.0)
```

Both values are hardcoded for now and will become CLI arguments in Step 2.

---

## Out of scope for this step

- `--dt`, `--t-end`, and other simulation parameters (Step 2)
- `Recorder` / SQLite output (separate step)
- Reaction map loading (separate step)
- Hub patch for valve centre nodes (carried over from `full_platform.py` — needs a dedicated step)
- API layer (`api.py` inside the ZIP)

---

## References

- `chemunited_core.figure_registry.COMPONENTS` — figure → `(DataClass, ModeClass)` registry
- `chemunited_core.components.component.ComponentMode` — base mode with `name`, `figure`, `position`, `angle`
- `chemunited_core.components.component.ComponentData` — base dataclass with the same four fields
- `chemunited_core.connections.edge.EdgeMode` — accepts `destiny` / `destiny_port` via `AliasChoices`
- `chemunited_core.connections.edge.EdgeData.name` — computed property, no manual naming needed
- `chemunited_core.common.metadata.Element.from_mode` — maps Pydantic Mode fields to dataclass `__init__`
- `chemunited_sim.adapter.graph.compile_graph` — silently skips non-HYDRAULIC edges and empty-port components
- `chemunited_sim.adapter.compilers.compile_component` — generic fallback for unrecognised `ComponentData` subtypes
