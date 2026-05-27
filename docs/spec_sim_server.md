# Spec: `chemunited-sim` — simulation server

**Status:** in progress — design review complete, one open question flagged (see `⚠️` markers)
**Replaces:** `spec_step1_cli_launch.md`, `spec_step2_server.md`, `spec_step3_component_commands.md`

---

## Overview

Two servers are launched from the same project files — a **work-server** (real hardware
execution) and a **sim-server** (this package). Both run the same protocol scripts. The
sim runs ahead at CPU pace; the work-server runs at real hardware pace.

```
work-server  ──── same protocol scripts ────  sim-server
     │                                              │
  real hardware                           Worker.step() loop
  (execution pace)                         (CPU pace, fast)
     │                                              │
  execution_id ─────────────────────────►  <execution_id>.db
                                                    │
                                           Qt GUI reads db directly
                                           (WAL mode, no blocking)
```

The UI sends **two commands** to the sim-server: `start` (with the execution ID) and
optionally `stop`. Everything else — simulation progress, results — is read directly from
the `.db` file by the UI.

---

## Protocol execution — design decision

The sim-server executes the same `Process` subclasses (from `chemunited-workflow`) as
the work-server, but routes commands through `ComponentData` objects instead of HTTP
clients. `chemunited-workflow` is therefore a dependency of `chemunited-sim`.

Execution happens in **two phases**:

### Phase 1 — timeline building (dry-run)

Before the simulation starts, the protocol is dry-run with a `TimelineBuilderPlatform`.
Each component slot is backed by a `TimelineBuilderClient` instead of a `ComponentClient`.

```
Process.run_workflow()
    └─ platform["PumpA"].put("infuse", wait_time=60, rate="2ml/min", volume="4ml")
        └─ records TimelineFrame(t=0,   component="PumpA", command="infuse", kwargs={rate, volume})
        └─ records TimelineFrame(t=120, component="PumpA", command="stop",   kwargs={})  ← derived
        └─ cursor_t advances to 60
    └─ platform["ValveB"].put("position", wait_time=1, connect=[(1, 2)])
        └─ records TimelineFrame(t=60, component="ValveB", command="position", kwargs={connect})
        └─ cursor_t advances to 61
    └─ ...
```

Key rules:

- `wait_time` advances the protocol `cursor_t` — it is **not** forwarded to
  `ComponentData.put()`.
- `ComponentData.put()` is **pure**: no state mutation. It returns a `PutResult`
  whose `scheduled` list contains `ScheduledCommand` entries for derived future
  events (e.g. pump auto-stop computed from `volume / rate`).
- `ComponentData.get()` always returns `None` — feedback polling is a no-op in
  simulation.
- Parallel workflow branches (via `ThreadPoolExecutor`) run concurrently during the
  dry-run; each `TimelineBuilderClient` maintains its own `cursor_t`, so branches
  record events at independent sim timestamps naturally.
- The result is a `Timeline`: a sorted list of `TimelineFrame` objects.

> **Note on `put()` return type:** `TimelineBuilderClient.put()` returns `None`,
> while `ComponentClient.put()` returns `requests.Response`. Both satisfy
> `ComponentClientProtocol` (which declares `-> Any`). Workflow node methods that
> inspect the return value of `platform["X"].put(...)` will behave differently
> under simulation — this is a known limitation.

### Phase 2 — sim execution (timeline replay)

The `Worker.step()` loop advances `worker.t` at CPU pace. After each step the sim
thread checks for pending frames and fires them as `worker.t` crosses each timestamp:

```python
components[frame.component].apply(frame.command, **frame.kwargs)
resync_component(graph, components[frame.component])
```

- `ComponentData.apply()` **mutates** internal state (calls `InternalEdge.open()` /
  `close()` on the component's internal edges, changing their `resistance_override`).
- `resync_component()` is a free function in `adapter/graph.py` that propagates
  those mutations into the compiled `HydraulicGraph` by copying `resistance_override`
  from each `InternalEdge` to its corresponding `HydraulicEdge`. Topology never
  changes — only edge attributes are updated. See
  [`adapter/graph.py — resync_component`](#adaptergraphpy--resync_component) below.
- No locking is required — all timeline events are applied in the single sim thread,
  between `Worker.step()` calls.
- When all frames are consumed and `t_end is None`, the simulation completes
  automatically.

### `chemunited_core` interface summary

| Method | Called by | Side effects |
|---|---|---|
| `put(command, **kwargs) → PutResult` | `TimelineBuilderClient` (dry-run) | None — pure computation |
| `apply(command, **kwargs) → None` | Sim thread (execution) | Mutates `ComponentData` internal edges via `open()`/`close()` |
| `get(command, **kwargs) → None` | `TimelineBuilderClient` | None |

### `chemunited_workflow` interface changes

Two files in `chemunited-workflow` must be modified:

**`clients.py`** — add `ComponentClientProtocol`:

```python
class ComponentClientProtocol(Protocol):
    def put(self, command: str, *, wait_time: float = 0.0, **kwargs) -> Any: ...
    def get(self, command: str, **kwargs) -> Any: ...
```

**`platform.py`** — re-type `Platform` from `Mapping[str, ComponentClient]` to
`Mapping[str, ComponentClientProtocol]`. This is a non-breaking change because
`ComponentClient` satisfies the protocol structurally, so all existing code that
builds `Platform` from real `ComponentClient` objects is unaffected.

---

## Goal

```shell
chemunited-sim path/to/project.chemunited [--port 1472] [--db path/to/db/dir]
```

Loads the project, compiles the hydraulic graph, builds the simulation timeline, and
starts a FastAPI server at `localhost:<port>`. The simulation does not start
automatically — the client calls `POST /simulation/start`. OpenAPI docs are available
at `http://localhost:<port>/docs`.

---

## New files

```
src/chemunited_sim/cli/
├── __init__.py
├── builder.py     # PlatformBuilder — add_component / add_connection
├── loader.py      # project discovery + dynamic import of setup.py and process.py
├── main.py        # argparse entry point + uvicorn launch
├── server.py      # FastAPI app, SimulationState, background thread
└── timeline.py    # TimelineFrame, Timeline, TimelineBuilderClient
```

---

## Modified files

| File | Change |
|---|---|
| `pyproject.toml` | Add console script entry point; add `fastapi`, `uvicorn[standard]`, `chemunited-workflow` |
| `worker/config.py` | `t_end: float` → `t_end: float \| None` |
| `worker/runner.py` | Guard `Worker.run()` against `t_end=None` |
| `adapter/graph.py` | Add `resync_component(graph, comp)` free function |
| `recorder/schema.py` | Add `PRAGMA busy_timeout=2000` to `configure_connection()` |
| `chemunited_workflow/clients.py` | Add `ComponentClientProtocol` |
| `chemunited_workflow/platform.py` | Re-type to `Mapping[str, ComponentClientProtocol]` |

---

## `pyproject.toml` additions

```toml
[project.scripts]
chemunited-sim = "chemunited_sim.cli.main:main"

[project.dependencies]
# add:
fastapi = ">=0.100"
uvicorn = {extras = ["standard"], version = ">=0.23"}
chemunited-workflow = ">=<version>"
```

---

## Project structure expected by the loader

```
my_project/
├── draw/
│   └── setup.py      ← must define build_draw(platform)
└── protocols/
    └── process.py    ← must define a Process subclass and ProcessConfig

my_project.chemunited   ← ZIP archive (alternative to folder)
```

`draw/setup.py` must define one function:

```python
def build_draw(platform):  # platform is a PlatformBuilder instance
    platform.add_component(name="PumpA", figure="SyringePump", ...)
    platform.add_component(name="ReactorA", figure="FlowReactor", ...)
    platform.add_connection(origin="PumpA", destiny="ReactorA", ...)
```

`protocols/process.py` must define a `Process` subclass (from `chemunited-workflow`)
and its paired `ProcessConfig`. It is imported with
`importlib.util.spec_from_file_location` under the module name `"project_process"`.
The loader discovers the first concrete `Process` subclass and its associated config
class, instantiates both, and optionally calls `load_parameters`. See
[`cli/loader.py`](#cliloaderpy--project-discovery) for details.

> **Convention — workflow entry node:** `build_workflow()` must define a node named
> `"start"` as the workflow entry point. `build_timeline()` always calls
> `process.run_workflow(start_node="start")`.

---

## CLI arguments

| Argument | Type | Default | Description |
|---|---|---|---|
| `project` | `Path` (positional) | — | Path to a `.chemunited` file or project folder |
| `--port` | `int` | `1472` | Port the FastAPI server listens on |
| `--db` | `Path` | `<project_folder>/log/simulation/` | Directory where `.db` files are saved |
| `--parameters` | `Path \| None` | `None` | Path to a historic parameters JSON file passed to `process.load_parameters()`. If omitted, `load_parameters()` looks for `protocols_historic/parameters.json` relative to `process.py`. |

> ⚠️ **OPEN QUESTION — `--parameters` default behaviour:** Confirm whether the sim
> should always call `load_parameters()` (falling back to the default path when
> `--parameters` is not given), or only call it when the flag is explicitly provided.
> The difference matters when the default `parameters.json` exists but should be
> ignored for a quick ad-hoc simulation run.

`<project_folder>` is resolved from the project path:
- folder input → the folder itself
- ZIP input → the folder the ZIP lives in

---

## `cli/loader.py` — project discovery

### Discovery order

1. `path` is a directory → use `path/draw/setup.py` and `path/protocols/process.py`.
2. `path` is a `.chemunited` ZIP and a same-name sibling folder exists → use sibling
   folder, ignore the ZIP.
3. `path` is a `.chemunited` ZIP, no sibling folder → extract to
   `tempfile.mkdtemp(prefix="chemunited_")` and use from there.

### Process and config instantiation

After loading the module from `process.py`, the loader must:

1. Find the first concrete `Process` subclass defined in the module.
2. Discover the associated `ConfigT` class.
3. Instantiate `ConfigClass()` (no arguments — all fields must have defaults).
4. Instantiate `ProcessClass(config=config_instance)`.
5. Call `process.load_parameters(historic_file=parameters_path)`.

> ⚠️ **OPEN QUESTION — config class discovery:** Two approaches are available:
>
> - **By convention:** the config class is always named `ProcessConfig` in the same
>   module. Simple to implement; fragile if the author uses a different name.
> - **By introspection:** read `ProcessClass.__orig_bases__` and extract the generic
>   type argument `ConfigT`:
>   ```python
>   from typing import get_args
>   for base in ProcessClass.__orig_bases__:
>       args = get_args(base)
>       if args and issubclass(args[0], BaseModel):
>           ConfigClass = args[0]
>           break
>   ```
>   More robust; slightly more complex.
>
> Choose one approach and document it as the project convention before implementing.

### Errors raised

| Condition | Exception |
|---|---|
| `path` does not exist | `FileNotFoundError` |
| `path` is not a directory or `.chemunited` | `ValueError` |
| ZIP is not a valid ZIP file | `ValueError` |
| `draw/setup.py` not found after resolution | `FileNotFoundError` |
| `setup.py` does not define `build_draw` | `AttributeError` |
| `protocols/process.py` not found | `FileNotFoundError` |
| `process.py` defines no `Process` subclass | `AttributeError` |
| Config class cannot be discovered or instantiated | `AttributeError` / `ValidationError` |

---

## `adapter/graph.py` — `resync_component`

New free function added to `adapter/graph.py`:

```python
def resync_component(graph: HydraulicGraph, comp: ComponentData) -> None:
    """Propagate resistance_override mutations from ComponentData into HydraulicGraph.

    Called by the sim thread immediately after ComponentData.apply() fires a
    timeline frame. apply() mutates InternalEdge.resistance_override via
    open() / close(); this function copies those values into the corresponding
    HydraulicEdge objects so the hydraulic solver sees the updated state on
    the next Worker.step().

    Topology is never changed — only HydraulicEdge.resistance_override is written.
    All edges belonging to the component are updated in one pass.
    """
    for (origin, dest), internal_edge in comp.internal_edges.items():
        edge_id = f"{comp.name}.{origin}.{dest}"
        hydraulic_edge = graph.edges.get(edge_id)
        if hydraulic_edge is not None:
            hydraulic_edge.resistance_override = internal_edge.resistance_override
```

The edge ID convention `"{comp.name}.{origin}.{dest}"` is consistent with all
per-component compilers in `adapter/compilers.py`.

---

## `worker/config.py` — `t_end` change

`t_end` becomes optional to support open-ended sim runs (run until all timeline frames
are consumed, or until `POST /simulation/stop`):

```python
@dataclass
class SimConfig:
    dt: float
    t_end: float | None   # None = run until frames exhausted or stop requested
    viscosity: float = ETA_WATER_25C
    bpr_max_iters: int = 20
```

## `worker/runner.py` — guard `run()` against `t_end=None`

The server background thread drives the simulation with `worker.step()` directly and
never calls `worker.run()`. To prevent a silent `TypeError` if `run()` is called with
`t_end=None`, add an explicit guard at the top of `Worker.run()`:

```python
def run(self) -> None:
    if self._config.t_end is None:
        raise ValueError(
            "Worker.run() requires t_end to be set. "
            "For open-ended runs drive the loop with step() directly."
        )
    # ... existing implementation unchanged ...
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

- Looks up `figure` in `COMPONENTS` from `chemunited_core.figure_registry`.
- Raises `ValueError` if the figure is unknown.
- Calls `ModeClass(name=name, figure=figure, position=position, angle=angle, **kwargs)`.
- Calls `DataClass.from_mode(mode)` to build the component.
- Raises `ValueError` if a component with the same `name` was already added.
- Stores and returns the component.

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

- Passes all arguments to `EdgeMode` directly.
- `EdgeMode` defaults: `length="100 mm"`, `diameter="1 mm"`, `classification=HYDRAULIC`.
- Calls `EdgeData.from_mode(mode)` and appends to the internal edge list.

### Properties

```python
@property
def hydraulic_components(self) -> list[ComponentData]:
    # excludes NeutralComponentData instances
    return [c for c in self._components.values()
            if not isinstance(c, NeutralComponentData)]

@property
def components(self) -> list[ComponentData]:
    return list(self._components.values())

@property
def edges(self) -> list[EdgeData]:
    return list(self._edges)
```

---

## `cli/timeline.py` — timeline building

### Data types

```python
@dataclass
class TimelineFrame:
    t: float                        # absolute sim time (seconds)
    component: str                  # component name key in Platform
    command: str
    kwargs: dict = field(default_factory=dict)

@dataclass
class Timeline:
    frames: list[TimelineFrame] = field(default_factory=list)

    def add(self, frame: TimelineFrame) -> None:
        self.frames.append(frame)

    def sorted_frames(self) -> list[TimelineFrame]:
        return sorted(self.frames, key=lambda f: f.t)
```

### `TimelineBuilderClient`

Satisfies `ComponentClientProtocol`. Each instance maintains its own `cursor_t` so
parallel workflow branches record events at correct independent timestamps.

```python
class TimelineBuilderClient:
    def __init__(self, name: str, component: ComponentData, timeline: Timeline) -> None:
        self._name = name
        self._component = component
        self._timeline = timeline
        self._cursor_t: float = 0.0

    def put(self, command: str, *, wait_time: float = 0.0, **kwargs) -> None:
        # record the triggering event at the current cursor
        self._timeline.add(TimelineFrame(
            t=self._cursor_t, component=self._name,
            command=command, kwargs=kwargs,
        ))
        # record derived follow-up events from PutResult.scheduled
        result = self._component.put(command, **kwargs)
        for s in result.scheduled:
            self._timeline.add(TimelineFrame(
                t=self._cursor_t + s.dt,
                component=self._name,
                command=s.command,
                kwargs=s.kwargs,
            ))
        # advance the protocol cursor — not real time
        self._cursor_t += wait_time

    def get(self, command: str, **kwargs) -> None:
        return None
```

### Building the timeline

```python
def build_timeline(process: Process, components: dict[str, ComponentData]) -> Timeline:
    timeline = Timeline()
    sim_platform = Platform({
        name: TimelineBuilderClient(name, component, timeline)
        for name, component in components.items()
    })
    process.platform = sim_platform
    process.run_workflow(start_node="start")   # "start" is a required convention
    return timeline
```

---

## `cli/server.py` — FastAPI app

### `SimConfig`

`SimConfig` is the existing dataclass from `worker/config.py` (now with
`t_end: float | None`). The server always constructs it with `t_end=None` so the
simulation runs until all timeline frames are consumed or `POST /simulation/stop` is
called.

### `SimulationState`

```python
@dataclass
class SimulationState:
    status: SimStatus          # "idle" | "running" | "completed"
    current_t: float           # current simulation time (seconds)
    config: SimConfig
    db_path: Path | None       # path of the active or last .db file
    timeline: Timeline | None  # built once at server startup, reused across runs
    _thread: Thread | None
    _stop_event: Event = field(default_factory=Event)
```

`SimStatus` is a `str` enum: `"idle"`, `"running"`, `"completed"`.

### Background thread

```python
def _simulation_thread(worker, timeline, components, graph, config, stop_event, state, recorder):
    pending = iter(timeline.sorted_frames())
    next_frame = next(pending, None)
    try:
        while not stop_event.is_set():
            if config.t_end is not None and worker.t > config.t_end:
                state.status = SimStatus.COMPLETED
                return
            worker.step()
            state.current_t = worker.t
            while next_frame is not None and worker.t >= next_frame.t:
                components[next_frame.component].apply(next_frame.command, **next_frame.kwargs)
                resync_component(graph, components[next_frame.component])
                next_frame = next(pending, None)
            if next_frame is None and config.t_end is None:
                state.status = SimStatus.COMPLETED
                return
    finally:
        recorder.close()
        if state.status == SimStatus.RUNNING:
            state.status = SimStatus.IDLE
```

`resync_component` is imported from `chemunited_sim.adapter.graph`.

### WAL mode and busy timeout

WAL mode and busy timeout are configured by `configure_connection()` in
`recorder/schema.py`, which is called by `Recorder.__init__()`. The `schema.py`
function must be updated to add `PRAGMA busy_timeout`:

```python
def configure_connection(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA busy_timeout=2000;")   # ← add this line
```

This allows the UI to read the `.db` file concurrently while the sim is writing. The UI
should open the file read-only:

```python
# Python
conn = sqlite3.connect("file:execution_id.db?mode=ro", uri=True)

# Qt
db.setConnectOptions("QSQLITE_OPEN_READONLY");
```

---

## Endpoints

### `GET /status`

Returns the current server state.

**Response 200:**

```json
{
  "status": "idle",
  "current_t": 0.0,
  "config": {
    "dt": 0.1,
    "t_end": null,
    "viscosity": 0.00089
  }
}
```

---

### `POST /simulation/start`

Starts the simulation. The `execution_id` is generated by the work-server and passed
here so both servers log under the same identifier.

**Request body:**

```json
{
  "execution_id": "run_2025-05-26T14-30-00"
}
```

**Side effects:**

- `db_dir` is created if it does not exist.
- DB filename: `<execution_id>.db`
- WAL mode and busy timeout are applied by `Recorder` via `configure_connection()`.
- Component data objects are re-initialised to their initial state.
- A fresh `Worker` is created from `t=0` and the background thread is started with
  the pre-built `timeline`.
- `state.db_path` is updated.
- `state.status` → `"running"`.

**Response 200:**

```json
{
  "status": "running",
  "db_path": "/path/to/log/simulation/run_2025-05-26T14-30-00.db"
}
```

**Response 409:** Simulation is already running.

---

### `POST /simulation/stop`

Aborts the running simulation. Sets the stop event, waits for the thread to finish
(recorder is closed), resets to `"idle"`. The partial `.db` file is kept on disk.

**No request body.**

**Response 200:**

```json
{ "status": "idle" }
```

**Response 409:** No simulation is currently running.

---

### `GET /simulation/db`

Returns the path of the current or most recently completed `.db` file.

**Response 200:**

```json
{
  "db_path": "/path/to/log/simulation/run_2025-05-26T14-30-00.db"
}
```

**Response 404:** No simulation has been run yet in this session.

---

## Full endpoint summary

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/status` | Current status, `current_t`, active `SimConfig` |
| `POST` | `/simulation/start` | Start sim with `execution_id`, returns `db_path` |
| `POST` | `/simulation/stop` | Abort simulation, reset to idle |
| `GET` | `/simulation/db` | Path of the active or last `.db` file |

---

## `cli/main.py` — execution flow

```
parse args (project, --port, --db, --parameters)
    └─ load_project(path)
        └─ build_draw(builder)                        # draw/setup.py
        └─ load_process(path, parameters_path)        # protocols/process.py
            └─ discover Process subclass + ConfigClass
            └─ instantiate config = ConfigClass()
            └─ instantiate process = ProcessClass(config=config)
            └─ process.load_parameters(historic_file=parameters_path)
    └─ compile_graph(
           builder.hydraulic_components,
           builder.edges,
       )
    └─ build_timeline(process, builder.components)   # dry-run via TimelineBuilderClient
    └─ resolve db_dir from --db or project folder default
    └─ build SimulationState(
           status=IDLE,
           config=SimConfig(dt=0.1, t_end=None),
           db_path=None,
           timeline=timeline,
       )
    └─ start FastAPI server via uvicorn on localhost:<port>
```

The compiled `graph`, `builder.components`, and `timeline` are held for the lifetime of
the server and never recomputed. `POST /simulation/start` always creates a fresh `Worker`
and re-initialises component data to its initial state before starting the thread.

---

## Recorder defaults

A new `Recorder` is created on every `POST /simulation/start`:

```python
Recorder(
    db_path=db_path,
    graph=graph,
    dt=config.dt,
    record_interval=2.0,
    platform_name=project_path.stem,
)
```

`configure_connection()` (called inside `Recorder.__init__()`) applies WAL mode,
`synchronous=NORMAL`, `temp_store=MEMORY`, and `busy_timeout=2000` immediately after
the connection is opened.

---

## Out of scope

- Component commands over HTTP — not needed; all commands are pre-computed in the
  timeline at startup.
- Markers / synchronisation table in the db.
- `--viscosity` CLI argument — can be added later if needed.
- Authentication / CORS.
- Reaction map loading.
- Hub patch for valve centre nodes.

---

## References

- `chemunited_core.figure_registry.COMPONENTS`
- `chemunited_core.components.component.ComponentMode` / `ComponentData`
- `chemunited_core.components.command.PutResult` / `ScheduledCommand`
- `chemunited_core.components.internals.InternalEdge.open()` / `.close()`
- `chemunited_core.connections.edge.EdgeMode` / `EdgeData`
- `chemunited_sim.adapter.graph.compile_graph` / `resync_component`
- `chemunited_sim.adapter.models.HydraulicGraph` / `HydraulicEdge`
- `chemunited_sim.worker.runner.Worker`
- `chemunited_sim.worker.config.SimConfig`
- `chemunited_sim.recorder.writer.Recorder`
- `chemunited_sim.recorder.schema.configure_connection`
- `chemunited_sim.cli.timeline.Timeline` / `TimelineFrame` / `TimelineBuilderClient`
- `chemunited_workflow.platform.Platform`
- `chemunited_workflow.clients.ComponentClientProtocol`
- `chemunited_workflow.process.Process`
