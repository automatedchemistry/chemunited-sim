# Spec: Step 2 — FastAPI simulation server

**Status:** planned  
**Depends on:** Step 1 (`spec_step1_cli_launch.md`)  
**Scope:** Transform the CLI from a run-and-exit script into a persistent FastAPI server
that exposes simulation control and result access over HTTP.

---

## Goal

```shell
chemunited-sim path/to/project.chemunited [--port 1472] [--db path/to/db/dir]
```

The command loads the project, compiles the hydraulic graph, and starts a FastAPI
server at `localhost:<port>`. The simulation does not start automatically — the
client must call `POST /simulation/start`. The OpenAPI docs are available at
`http://localhost:<port>/docs`.

---

## New files

```
src/chemunited_sim/cli/
└── server.py     # FastAPI app, SimulationState, background thread logic
```

---

## Modified files

| File | Change |
|---|---|
| `cli/main.py` | Add `--port` and `--db` arguments; launch uvicorn instead of `Worker.run()` |
| `worker/config.py` | `t_end: float` → `t_end: float | None` |
| `pyproject.toml` | Add `fastapi` and `uvicorn[standard]` to dependencies |

---

## New CLI arguments

| Argument | Type | Default | Description |
|---|---|---|---|
| `--port` | `int` | `1472` | Port the FastAPI server listens on |
| `--db` | `Path` | `<project_folder>/log/simulation/` | Directory where `.db` files are saved |

`<project_folder>` is resolved from the project path:
- folder input → the folder itself
- ZIP input → the folder the ZIP lives in

---

## Dependencies

Add to `pyproject.toml`:

```toml
dependencies = [
    ...
    "fastapi>=0.100",
    "uvicorn[standard]>=0.23",
]
```

---

## `SimConfig` change

`t_end` becomes optional. `None` means the simulation runs indefinitely until
`POST /simulation/stop` is called.

```python
@dataclass
class SimConfig:
    dt: float
    t_end: float | None = None   # None = run until stop
    viscosity: float = ETA_WATER_25C
    bpr_max_iters: int = 20
```

`Worker.run()` is **not modified** — it is never called in Step 2 (the server drives
`Worker.step()` directly). The `t_end=None` case is handled entirely in the background
thread inside `server.py`.

---

## Server state machine

```
         configure / start
              │
    ┌─────────▼─────────┐
    │        IDLE        │◄──── stop (abort)
    └─────────┬─────────┘
              │  POST /simulation/start
    ┌─────────▼─────────┐
    │      RUNNING       │
    └────┬──────────┬───┘
         │          │
    t >= t_end    stop        ← t_end=None: this branch is never reached
    (t_end set)               │
         │          │
    ┌────▼───┐  ┌───▼────┐
    │COMPLETE│  │  IDLE  │
    └────────┘  └────────┘
```

- **IDLE** — no simulation running; parameters can be configured freely.
- **RUNNING** — background thread is executing `Worker.step()` in a loop.
- **COMPLETED** — simulation reached `t_end` (only reachable when `t_end` is set).
- **stop** always aborts and resets to **IDLE** (no pause/resume).
- **start** always creates a fresh `Worker` from `t=0` regardless of previous state.
- With `t_end=None` the only exit from **RUNNING** is `POST /simulation/stop`.

---

## Background thread

`Worker.run()` is a blocking synchronous loop and cannot be called directly from
an async FastAPI endpoint. The server instead drives the simulation manually with
`Worker.step()` inside a `threading.Thread`, checking a `threading.Event` for
stop signals each iteration.

`Worker` is **not modified** — all control logic lives in `server.py`.

```python
def _simulation_thread(worker, config, stop_event, state, recorder):
    try:
        while not stop_event.is_set():
            if config.t_end is not None:
                if round(worker.t / config.dt) > round(config.t_end / config.dt):
                    state.status = SimStatus.COMPLETED
                    return
            worker.step()
            state.current_t = worker.t
    finally:
        recorder.close()
        if state.status == SimStatus.RUNNING:
            # stop() was called — reset to idle
            state.status = SimStatus.IDLE
```

---

## `SimulationState`

Internal singleton that holds all mutable server state. Not exposed directly
to the client — only through the endpoint responses.

```python
@dataclass
class SimulationState:
    status: SimStatus          # IDLE | RUNNING | COMPLETED
    current_t: float           # current simulation time (seconds)
    config: SimConfig          # active simulation parameters
    db_path: Path | None       # path of the running or last completed .db file
    _thread: Thread | None     # background simulation thread
    _stop_event: Event         # signals the thread to abort
```

`SimStatus` is a `str` enum: `"idle"`, `"running"`, `"completed"`.

---

## Endpoints

### `GET /status`

Returns the current server and simulation state.

**Response 200:**

```json
{
  "status": "idle",
  "current_t": 0.0,
  "config": {
    "dt": 0.1,
    "t_end": 60.0,
    "viscosity": 0.00089
  }
}
```

`t_end` is `null` when the simulation is configured to run indefinitely.  
`status` is one of `"idle"`, `"running"`, `"completed"`.  
`current_t` advances in real time while the simulation runs.

---

### `POST /simulation/configure`

Updates simulation parameters. Only allowed when status is `"idle"` or `"completed"`.

**Request body** (all fields optional — only provided fields are updated):

```json
{
  "dt": 0.1,
  "t_end": 120.0,
  "viscosity": 0.00089
}
```

Set `t_end` to `null` explicitly to configure an indeterminate run:

```json
{
  "t_end": null
}
```

**Responses:**

| Code | Condition |
|---|---|
| `200` | Parameters updated, returns the full updated config |
| `409` | Simulation is currently running |
| `422` | Invalid parameter values (Pydantic validation) |

---

### `POST /simulation/start`

Creates a fresh `Worker` from `t=0` and starts the background thread.
A new timestamped `.db` file is created in the configured db directory.

**No request body.**

**Responses:**

| Code | Condition |
|---|---|
| `200` | Simulation started |
| `409` | Simulation is already running |

**Side effects:**

- `db_dir` is created if it does not exist.
- DB filename: `simulation_YYYY-MM-DDTHH-MM-SS.db`
- `state.db_path` is updated to the new file path.
- `state.status` → `"running"`.

---

### `POST /simulation/stop`

Aborts the running simulation. Sets the stop event and waits for the background
thread to finish cleanly (recorder is closed before the thread exits).
Resets state to `"idle"`. The partial `.db` file is kept on disk.

**No request body.**

**Responses:**

| Code | Condition |
|---|---|
| `200` | Simulation stopped |
| `409` | No simulation is currently running |

---

### `GET /simulation/db`

Returns the filesystem path of the current or most recently completed `.db` file.

**Response 200:**

```json
{
  "db_path": "/home/user/myproject/log/simulation/simulation_2025-05-22T14-30-00.db"
}
```

**Response 404:** No simulation has been run yet in this session (`db_path` is `None`).

---

### `DELETE /simulation/db`

Deletes the `.db` file at a given path. Useful for cleaning up runs during
development. Not allowed while the file is the active db of a running simulation.

**Request body:**

```json
{
  "db_path": "/home/user/myproject/log/simulation/simulation_2025-05-22T14-30-00.db"
}
```

**Responses:**

| Code | Condition |
|---|---|
| `200` | File deleted |
| `400` | File does not exist |
| `409` | Cannot delete the db file of a currently running simulation |

---

## Full endpoint summary

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/status` | Server state, current `t`, active `SimConfig` |
| `POST` | `/simulation/configure` | Update `dt`, `t_end` (or `null`), `viscosity` |
| `POST` | `/simulation/start` | Create fresh `Worker` from `t=0`, start thread |
| `POST` | `/simulation/stop` | Abort running simulation, reset to idle |
| `GET` | `/simulation/db` | Path of the running or last completed `.db` file |
| `DELETE` | `/simulation/db` | Delete a `.db` file by path |

---

## `cli/main.py` — updated execution flow

```
parse args (project, --port, --db)
    └─ load_project(path)              # loader.py
        └─ build_draw(builder)         # draw/setup.py
    └─ compile_graph(
           builder.hydraulic_components,
           builder.edges,
       )
    └─ resolve db_dir from --db or project folder default
    └─ build SimulationState(
           status=IDLE,
           config=SimConfig(dt=0.1, t_end=None),
           ...
       )
    └─ start FastAPI server via uvicorn on localhost:<port>
```

The compiled `graph` and `builder.hydraulic_components` are passed to `server.py`
and held for the lifetime of the server. They are never recompiled — `start` always
creates a new `Worker` from the same graph.

---

## Recorder integration

Step 2 is where the `Recorder` is introduced into the CLI flow.

- A new `Recorder` is instantiated on every `POST /simulation/start`.
- The db directory is created automatically if it does not exist (`mkdir -p`).
- The `Recorder` is passed to the background thread and closed in the `finally` block,
  whether the simulation completes naturally or is aborted.
- The `Recorder` configuration (record interval, platform name) uses fixed defaults
  for now and will be exposed as parameters in a later step.

Default recorder settings:

```python
Recorder(
    db_path=db_path,
    graph=graph,
    dt=config.dt,
    record_interval=2.0,
    platform_name=project_path.stem,
)
```

---

## Out of scope for this step

- Reaction map loading
- Hub patch for valve centre nodes
- Streaming simulation data (WebSocket)
- Authentication / CORS configuration
- Multiple simultaneous simulations
- Recorder configuration via API

---

## References

- `chemunited_sim.worker.runner.Worker` — `step()` is called directly; `run()` is not used
- `chemunited_sim.worker.config.SimConfig` — `t_end: float | None`, `dt`, `viscosity`, `bpr_max_iters`
- `chemunited_sim.recorder.writer.Recorder` — created per run, closed in thread `finally`
- `fastapi.FastAPI` — app instance lives in `server.py`
- `uvicorn.run` — called from `main.py` to start the server
