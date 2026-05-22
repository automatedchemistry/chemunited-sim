# Architecture

`chemunited-sim` is split into small domain modules. The modules share a
compiled `HydraulicGraph` and pass explicit state objects between steps.

## Data Flow

```text
chemunited-core components + edges
        |
        v
adapter.compile_graph(...)
        |
        v
HydraulicGraph
        |
        v
Worker.step()
```

The package does not read GUI objects directly during simulation. It consumes
`chemunited-core` component and connection data, compiles a graph, and then
runs from the compiled graph and runtime state.

## Worker Step Order

`Worker.step()` applies one operator-splitting time step:

1. Solve hydraulics.
2. Record the current state if a `Recorder` is attached and the time is due.
3. Advance transport pockets through edges.
4. Assimilate arrived pockets into vessel inventories.
5. Apply reactions to inventory states.
6. Emit replacement pockets from inventories.
7. Inject emitted pockets into transport queues.
8. Advance simulation time by `dt`.

Because recording happens before transport advances, a full `Worker.run()`
with a recorder captures `t=0`.

## Hydraulic Graph

The adapter compiles a platform into:

- `HydraulicNode`: a component port, inventory node, or junction hub.
- `HydraulicEdge`: an internal component channel or external connection.
- `HydraulicGraph`: dictionaries of nodes and edges, inventory snapshots, and
  a list of BPR-controlled edge IDs.

All lengths, diameters, volumes, pressures, flows, temperatures, and
resistances are converted to SI values before simulation modules consume them.

## Hydraulics

`chemunited_sim.hydraulics.solve()` builds a sparse nodal admittance system and
solves for absolute pressure at each node. Flows are then back-computed for
each edge.

Boundary handling:

- Pressure boundaries pin a node to an absolute pressure.
- Flow boundaries add a source/sink term to the right-hand side.
- Connected components without pressure boundaries are anchored to atmosphere.

Resistance handling:

- Junction edges are nearly lossless.
- Transport edges use Hagen-Poiseuille resistance.
- Edge `resistance_override` takes priority, which is how BPRs and closed
  valve channels are represented.

## Transport

Transport edges hold FIFO queues of immutable `Pocket` objects. A pocket stores
phase, volume, species moles, temperature, and pressure.

Deque orientation is fixed to graph orientation:

- `deque[0]` is closest to the destination node.
- `deque[-1]` is closest to the origin node.
- Positive flow pops from the destination end.
- Negative flow pops from the origin end.

Pockets that exit a transport edge may reach an inventory, a hub, or a boundary
node. Hub arrivals are merged by phase and redistributed to outgoing transport
edges according to displaced volume.

## Inventory

Vessel inventory state is seeded from the inventory snapshots stored in the
compiled graph. During each step:

- Arriving pockets increase the appropriate phase volume and species moles.
- Vessel pressure is refreshed from the hydraulic state.
- Temperature is updated by volume-weighted blending.
- Departing volumes emit gas from top ports or liquid from bottom ports.

## Reactions

Reactions are attached through a `ReactionsMap`:

```python
{
    "reactor.Inventory": [reaction_1, reaction_2],
}
```

Any object with `step(state, dt) -> None` can be a reaction. Built-in reaction
models include `NullReaction`, `FirstOrderDecay`, and
`StoichiometricReaction`.

## Back-Pressure Regulators

The worker pre-resolves BPR edges at construction. On every step it iterates
hydraulic solves until each BPR edge state is stable:

- Open when upstream pressure is at or above the setpoint.
- Closed when upstream pressure is below the setpoint.

Closed BPR edges use the large hydraulic resistance constant from
`chemunited-core`.
