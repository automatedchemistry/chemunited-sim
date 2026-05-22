# Spec: Step 3 — Component commands

**Status:** planned  
**Depends on:** Step 2 (`spec_step2_server.md`), `chemunited-core` Step 3 (`CommandEffect`, `receive_command`)  
**Scope:** Allow clients to send runtime commands to individual components — while the
simulation is running, idle, or completed — through a new family of HTTP endpoints.

---

## Goal

```http
POST /components/PumpA/command
Content-Type: application/json

{"command": "set_flow_rate", "params": {"flow_rate": "5 ml/min"}}
```

Commands are atomic with respect to the simulation loop: they always land
between two `Worker.step()` calls, never mid-step. While idle or completed
they update the component's initial conditions for the next run.

---

## New files

None — all changes are in `cli/server.py` and `cli/main.py`.

---

## Modified files

| File | Change |
|---|---|
| `cli/server.py` | Add `sim_lock`, `components_by_name`, `_apply_effect`, three new endpoints |
| `cli/main.py` | Pass all components (not just hydraulic) into `SimulationState` |

---

## Dependencies

```toml
# No new pyproject.toml entries required.
# chemunited-core must expose:
#   chemunited_core.components.command.CommandEffect
#   ComponentData.receive_command(command, **kwargs) -> CommandEffect
#   ComponentData.available_commands() -> list[str]
```

---

## `sim_lock`

A `threading.Lock` is added to `SimulationState`. It serialises access between
the background simulation thread and any HTTP handler that needs to touch
component state or the hydraulic graph.

**Acquired in the background thread** — wrapping each `worker.step()` call:

```python
def _simulation_thread(worker, config, stop_event, state, recorder):
    try:
        while not stop_event.is_set():
            if config.t_end is not None:
                if round(worker.t / config.dt) > round(config.t_end / config.dt):
                    state.status = SimStatus.COMPLETED
                    return
            with state.sim_lock:          # ← new
                worker.step()
            state.current_t = worker.t
    finally:
        recorder.close()
        if state.status == SimStatus.RUNNING:
            state.status = SimStatus.IDLE
```

The lock is held only for the duration of a single step — one `dt`. Commands
are never blocked for more than one simulation tick.

**Acquired in `POST /components/{name}/command`** — wrapping the `receive_command`
call and the subsequent `_apply_effect` bridge, so both are applied atomically
before the next step begins.

---

## `SimulationState` — additions

```python
@dataclass
class SimulationState:
    status: SimStatus
    current_t: float
    config: SimConfig
    db_path: Path | None
    components_by_name: dict[str, ComponentData]   # ← new: all components
    sim_lock: Lock = field(default_factory=Lock)   # ← new: step / command mutex
    _thread: Thread | None = None
    _stop_event: Event = field(default_factory=Event)
```

`components_by_name` is populated once at startup from `builder.components`
(the full component list, including neutral ones — a client may attempt to
command any component by name and should receive a clear error).
It is never modified after initialisation — components are mutated in-place
by `receive_command`, not replaced.

---

## `_apply_effect`

Private helper in `server.py`. Translates a `CommandEffect` returned by
`receive_command` into in-place mutations on the compiled `HydraulicGraph`.

```python
def _apply_effect(
    effect: CommandEffect,
    comp_name: str,
    graph: HydraulicGraph,
) -> None:
    """Bridge CommandEffect.edge_overrides → HydraulicEdge.resistance_override.

    Required for valve-type components where resistance_override is a value
    copy in the compiled graph and is not reachable via shared references.
    No-op for components that use shared-reference propagation (e.g. pumps).
    """
    for (origin, dest), override in effect.edge_overrides.items():
        edge_id = f"{comp_name}.{origin}.{dest}"
        hydraulic_edge = graph.edges.get(edge_id)
        if hydraulic_edge is not None:
            hydraulic_edge.resistance_override = override
```

Called inside the `sim_lock` block in `POST /components/{name}/command`,
immediately after `receive_command` returns.

---

## Command timing behaviour

### While `IDLE`

The command updates the `ComponentData` in-place and — via `_apply_effect` —
patches the compiled graph. The graph is never recompiled between runs;
`POST /simulation/start` always creates a fresh `Worker` from the same graph.
Because the graph now reflects the patched state, the command becomes the
**initial condition** for the next run.

For pumps: `sync_internal_state()` inside `receive_command` mutates
`port.boundary.value` via the shared reference — the graph node is already
consistent. `_apply_effect` is a no-op.

For valves: `_apply_effect` updates `HydraulicEdge.resistance_override` to
match the new rotor position. The next `Worker` starts with the correct
channel topology.

### While `RUNNING`

The command endpoint blocks until the `sim_lock` is available (at most one
`dt`). Once acquired, `receive_command` and `_apply_effect` run atomically.
The change takes effect on the very next `worker.step()` call — within one
simulation tick of the HTTP request completing.

### While `COMPLETED`

Identical to `IDLE` — the command updates initial conditions for the next run.

---

## `cli/main.py` — change

Pass `builder.components` (all components, including neutral ones) to
`SimulationState` instead of `builder.hydraulic_components`:

```
parse args (project, --port, --db)
    └─ load_project(path)
        └─ build_draw(builder)
    └─ compile_graph(
           builder.hydraulic_components,
           builder.edges,
       )
    └─ resolve db_dir
    └─ build SimulationState(
           status=IDLE,
           config=SimConfig(dt=0.1, t_end=None),
           components_by_name={c.name: c for c in builder.components},  ← changed
           ...
       )
    └─ start FastAPI server via uvicorn on localhost:<port>
```

---

## Endpoints

### `GET /components`

Returns the full component list with their command vocabularies.
Useful for clients to discover what the loaded platform exposes.

**Response 200:**

```json
[
  {
    "name": "PumpA",
    "figure": "SyringePump",
    "is_electronic": true,
    "available_commands": ["set_flow_rate", "stop"]
  },
  {
    "name": "ValveA",
    "figure": "RotaryValve",
    "is_electronic": true,
    "available_commands": ["rotate_clockwise", "rotate_counterclockwise"]
  },
  {
    "name": "ReactorA",
    "figure": "FlowReactor",
    "is_electronic": false,
    "available_commands": []
  }
]
```

No request body. Always returns 200 (the list may be empty if the platform has
no components, which would be a malformed project).

---

### `GET /components/{name}`

Returns the command vocabulary of a single component.

**Response 200:**

```json
{
  "name": "PumpA",
  "figure": "SyringePump",
  "is_electronic": true,
  "available_commands": ["set_flow_rate", "stop"]
}
```

**Response 404:** No component with this name exists in the loaded platform.

---

### `POST /components/{name}/command`

Sends a command to a component. The command is applied atomically with respect
to the simulation loop regardless of the current simulation status.

**Request body:**

```json
{
  "command": "set_flow_rate",
  "params": {
    "flow_rate": "5 ml/min"
  }
}
```

`params` is a free-form JSON object whose keys and value types are defined by
the component's `receive_command` implementation. All fields are optional at
the schema level — validation is delegated to `receive_command` itself, which
raises `ValueError` for missing or invalid parameters.

Physical quantities are accepted as plain strings (`"5 ml/min"`) and converted
internally by `ChemQuantityValidator`.

**Responses:**

| Code | Condition |
|---|---|
| `200` | Command applied successfully |
| `400` | `receive_command` raised `ValueError` — unknown command or invalid parameters |
| `404` | No component with this name in the loaded platform |
| `409` | Component is not electronic and does not accept commands |

**Implementation sketch:**

```python
@app.post("/components/{name}/command")
async def send_command(name: str, body: ComponentCommandRequest):
    comp = state.components_by_name.get(name)
    if comp is None:
        raise HTTPException(404, f"Component '{name}' not found.")
    if not comp.is_electronic:
        raise HTTPException(
            409,
            f"Component '{name}' ({comp.figure}) is not electronic "
            "and does not accept commands.",
        )
    try:
        with state.sim_lock:
            effect = comp.receive_command(body.command, **body.params)
            _apply_effect(effect, name, graph)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"component": name, "command": body.command, "status": "applied"}
```

`ComponentCommandRequest` is a Pydantic model:

```python
class ComponentCommandRequest(BaseModel):
    command: str
    params: dict[str, Any] = {}
```

---

## Full endpoint summary

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/status` | Server state, current `t`, active `SimConfig` (Step 2) |
| `POST` | `/simulation/configure` | Update `dt`, `t_end`, `viscosity` (Step 2) |
| `POST` | `/simulation/start` | Create fresh `Worker`, start thread (Step 2) |
| `POST` | `/simulation/stop` | Abort simulation, reset to idle (Step 2) |
| `GET` | `/simulation/db` | Path of current or last `.db` file (Step 2) |
| `DELETE` | `/simulation/db` | Delete a `.db` file by path (Step 2) |
| `GET` | `/components` | List all components with command vocabularies ← **new** |
| `GET` | `/components/{name}` | Single component info and commands ← **new** |
| `POST` | `/components/{name}/command` | Send a command to a component ← **new** |

---

## Error cases

| Situation | HTTP code | Source |
|---|---|---|
| Component name not in platform | `404` | endpoint guard |
| Component is not electronic | `409` | endpoint guard (checked before `receive_command`) |
| Command name not in `available_commands` | `400` | `ValueError` from `receive_command` |
| Required parameter missing | `400` | `ValueError` from `receive_command` |
| Parameter value unparseable as physical quantity | `400` | `ValueError` from `ChemQuantityValidator` inside `receive_command` |

---

## Out of scope for this step

- Streaming component state changes (WebSocket)
- Command history / audit log (the `.db` already records the simulation state)
- Queuing multiple commands for deferred execution
- Commanding neutral components (`NeutralComponentData`) — these are passive utensils with no actuatable parameters
- `BPR` setpoint changes at runtime (the worker caches `setpoint_pa` in `_BprEntry` at construction; updating it requires extending `_BprEntry` or rebuilding the entries list — deferred to a later step)
- Reaction map updates at runtime

---

## References

- `chemunited_core.components.command.CommandEffect` — bridge contract between core and sim
- `chemunited_core.components.component.ComponentData.receive_command` — domain logic lives here
- `chemunited_core.components.component.ComponentData.available_commands` — command vocabulary introspection
- `chemunited_sim.adapter.models.HydraulicGraph` — `graph.edges[edge_id].resistance_override` mutated by `_apply_effect`
- `chemunited_sim.worker.runner.Worker.step` — wrapped in `sim_lock` in the background thread
