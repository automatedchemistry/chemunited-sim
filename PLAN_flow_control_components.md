# New components: peristaltic pump and mass flow controller

## Transport layer summary (key rule)

Pockets reaching a hub node are **staged and re-emitted** along outgoing edges.
Pockets reaching a FlowSource/PressureControl node are **discarded** (dead-end).
Both components need a **hub at port 1** so content from upstream is not lost.

---

## Component 1 — Peristaltic pump

**Behaviour**: forces exactly Q from port 1 → port 2. Q=0 → fully blocked (no passive flow).

**Hydraulic model**: hub + junction edge (1→2) + Neumann BC at port 2.

```
upstream → port 1 (hub) ─JUNCTION─> port 2 (Neumann +Q) → downstream
```

- **Q > 0**: junction edge OPEN, port 2 Neumann BC = +Q → pump forces flow
- **Q = 0**: junction edge CLOSED (R_MAX), port 2 Neumann BC = 0 → blocks passive flow ✓

`sync_internal_state`:
```python
if self.flow_rate_si > 0:
    self.internal_edges[(1, 2)].open()
    self.ports_by_number[2].boundary.value = self.flow_rate_si
else:
    self.internal_edges[(1, 2)].close()   # R_MAX — no passive flow
    self.ports_by_number[2].boundary.value = 0.0
```

Transport: pockets staged at hub (port 1) are re-emitted along the junction edge toward port 2 whenever the edge is open. ✓

---

## Component 2 — Mass flow controller

**Behaviour**: allows flow ≤ setpoint in direction 1→2. Resistance is adjusted to limit flow to setpoint. If upstream pressure is too low to drive setpoint, natural flow passes (less than setpoint). Q=0 → fully closed.

**Hydraulic model**: hub + variable-resistance junction edge (1→2). **No Neumann BC**.

```
upstream → port 1 (hub) ─JUNCTION(R_adj)─> port 2 → downstream
```

- **setpoint = 0**: junction edge CLOSED (R_MAX) → blocks all flow ✓
- **setpoint > 0**: `R_adj = ΔP_prev / Q_setpoint`
  - If natural flow at R=0 is already < setpoint: R=0 (fully open, flow passes freely)
  - Otherwise: R limits flow to ≈ setpoint

`R_adj` must be updated **after each hydraulic solve** using the pressure drop across the MFC from the previous step. This requires a runner-level hook similar to how the BPR is updated each tick.

`update_resistance` (called each tick by the runner with the solved ΔP):
```python
def update_resistance(self, dp: float) -> None:
    if self.setpoint_si <= 0.0:
        self.internal_edges[(1, 2)].close()
    else:
        r = dp / self.setpoint_si if dp > 0 else 0.0
        self.internal_edges[(1, 2)].open()
        self.internal_edges[(1, 2)].resistance_override = max(r, 0.0) or None
```

Transport: same hub + junction path as the pump. When junction is open, pockets flow 1→2. ✓

---

## Shared internal structure

Both components extend `ValveComponentData`:

- `hub_ports = (1,)` — port 1 is the hub (staged transport)
- `ports_by_number = {1: Port(hub=True), 2: Port(...)}`
- `internal_edges = {(1, 2): InternalEdge(role=JUNCTION)}`
- **Compiler**: `compile_valve` already handles `ValveComponentData` subclasses — no new compiler needed

The difference is entirely in `sync_internal_state` / `update_resistance`.

---

## Files to create/modify

### chemunited-core

**New file**: `src/chemunited_core/figure_registry/flow_control.py`
- `PeristalticPumpMode` / `PeristalticPumpData(ValveComponentData)`
- `MassFlowControllerMode` / `MassFlowControllerData(ValveComponentData)`

**Modify**: `src/chemunited_core/figure_registry/__init__.py`
- Register `PeristalticPump` and `MassFlowController` figures

### chemunited-sim

**Modify**: `src/chemunited_sim/worker/runner.py`
- Add a per-tick update step for MFC components
- After each hydraulic solve: read `P_port1 - P_port2` from `hyd_state`, call `component.update_resistance(dp)`
- Pattern: similar to how BPR state is updated each tick
