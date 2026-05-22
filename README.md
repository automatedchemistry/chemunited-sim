# chemunited-sim

`chemunited-sim` is a dynamic simulation engine for fluidic automation
platforms described with `chemunited-core` components and connections.

The package compiles a ChemUnited platform into a flat hydraulic graph, solves
steady-state pressures and flows, moves liquid/gas pockets through transport
edges, updates vessel inventories, applies simple reaction models, and can
record simulation state to SQLite.

## Install

From the repository root:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

For development tools:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

`chemunited-core` is installed directly from GitHub through `pyproject.toml`.

## Minimal Usage

```python
from chemunited_sim.adapter import compile_graph
from chemunited_sim.worker import SimConfig, Worker

graph = compile_graph(components, edges)
worker = Worker(graph, components, SimConfig(dt=0.1, t_end=10.0))
worker.run()

print(worker.t)
print(worker.hyd_state.pressures)
print(worker.hyd_state.flows)
```

See [docs/quickstart.md](docs/quickstart.md) for a complete runnable example.

## Documentation

| Page | Purpose |
|---|---|
| [docs/index.md](docs/index.md) | Package overview and documentation map |
| [docs/quickstart.md](docs/quickstart.md) | Installation and a runnable first simulation |
| [docs/architecture.md](docs/architecture.md) | Simulation loop, graph model, and module boundaries |
| [docs/api-reference.md](docs/api-reference.md) | Public import paths and API summary |
| [docs/recording.md](docs/recording.md) | SQLite recorder schema and usage |
| [docs/examples.md](docs/examples.md) | Existing examples and what they demonstrate |

## Development

```powershell
.\.venv\Scripts\pytest.exe
pre-commit run --all-files
```

The test suite covers the worker loop, BPR stabilisation, transport recording,
and SQLite recorder behavior.
