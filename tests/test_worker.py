"""Tests for the worker module.

Covers:
- Import smoke test
- SimConfig construction
- Worker initial state (t=0, hyd_state=None)
- step(): advances t, sets hyd_state
- run(): advances t to t_end, closes recorder
- Recorder integration: correct times written to SQLite
- BPR stabilisation: opens when P_upstream >= setpoint, stays closed otherwise
"""

from __future__ import annotations

import sqlite3

import pytest
from chemunited_core.common.constant import R_MAX_HYDRAULIC
from chemunited_core.common.enums import ConnectionType
from chemunited_core.components import (
    BackPressureRegulatorData,
    BackPressureRegulatorMode,
    PlugFlowComponentData,
    PlugFlowMode,
    PressureControlData,
    PressureControlMode,
)
from chemunited_core.connections import EdgeData, EdgeMode
from chemunited_core.utils.internal_quantity import ChemUnitQuantity
from loguru import logger

from chemunited_sim.adapter import compile_graph
from chemunited_sim.recorder import Recorder
from chemunited_sim.worker import SimConfig, Worker

# ---------------------------------------------------------------------------
# Fixtures — minimal platform builder
# ---------------------------------------------------------------------------


def _make_tube_platform(
    src_bar: float = 2.0,
    snk_bar: float = 1.0,
    tube_length_cm: float = 10.0,
    tube_dia_mm: float = 4.0,
):
    """Two pressure controllers connected by a PlugFlow tube.

    src (src_bar) → e_in → tube → e_out → snk (snk_bar)

    No vessel, no BPR — the simplest possible hydraulic network.
    """
    src = PressureControlData.from_mode(
        PressureControlMode(name="src", setpoint=ChemUnitQuantity(f"{src_bar} bar"))
    )
    tube = PlugFlowComponentData.from_mode(
        PlugFlowMode(
            name="tube",
            length=ChemUnitQuantity(f"{tube_length_cm} cm"),
            diameter=ChemUnitQuantity(f"{tube_dia_mm} mm"),
        )
    )
    snk = PressureControlData.from_mode(
        PressureControlMode(name="snk", setpoint=ChemUnitQuantity(f"{snk_bar} bar"))
    )
    e_in = EdgeData.from_mode(
        EdgeMode(
            name="ein",
            origin="src",
            origin_port=1,
            destination="tube",
            destination_port=1,
            classification=ConnectionType.HYDRAULIC,
            length=ChemUnitQuantity("5 cm"),
            diameter=ChemUnitQuantity("4 mm"),
        )
    )
    e_out = EdgeData.from_mode(
        EdgeMode(
            name="eout",
            origin="tube",
            origin_port=2,
            destination="snk",
            destination_port=1,
            classification=ConnectionType.HYDRAULIC,
            length=ChemUnitQuantity("5 cm"),
            diameter=ChemUnitQuantity("4 mm"),
        )
    )
    components = [src, tube, snk]
    graph = compile_graph(components, [e_in, e_out])
    return graph, components


def _make_bpr_platform(setpoint_bar: float = 1.2):
    """src (3 bar) → tube → BPR → snk (1 bar).

    Setpoint choices:
      1.2 bar → BPR opens  (P_upstream-open ≈ 1.4 bar > 1.2 bar → stable open)
      3.5 bar → BPR stays closed (P_upstream-closed ≈ 3 bar < 3.5 bar → stable closed)
    """
    src = PressureControlData.from_mode(
        PressureControlMode(name="src", setpoint=ChemUnitQuantity("3 bar"))
    )
    tube = PlugFlowComponentData.from_mode(
        PlugFlowMode(
            name="tube",
            length=ChemUnitQuantity("10 cm"),
            diameter=ChemUnitQuantity("4 mm"),
        )
    )
    bpr = BackPressureRegulatorData.from_mode(
        BackPressureRegulatorMode(
            name="bpr",
            setpoint=ChemUnitQuantity(f"{setpoint_bar} bar"),
        )
    )
    snk = PressureControlData.from_mode(
        PressureControlMode(name="snk", setpoint=ChemUnitQuantity("1 bar"))
    )
    e1 = EdgeData.from_mode(
        EdgeMode(
            name="e1",
            origin="src",
            origin_port=1,
            destination="tube",
            destination_port=1,
            classification=ConnectionType.HYDRAULIC,
            length=ChemUnitQuantity("5 cm"),
            diameter=ChemUnitQuantity("4 mm"),
        )
    )
    e2 = EdgeData.from_mode(
        EdgeMode(
            name="e2",
            origin="tube",
            origin_port=2,
            destination="bpr",
            destination_port=1,
            classification=ConnectionType.HYDRAULIC,
            length=ChemUnitQuantity("5 cm"),
            diameter=ChemUnitQuantity("4 mm"),
        )
    )
    e3 = EdgeData.from_mode(
        EdgeMode(
            name="e3",
            origin="bpr",
            origin_port=2,
            destination="snk",
            destination_port=1,
            classification=ConnectionType.HYDRAULIC,
            length=ChemUnitQuantity("5 cm"),
            diameter=ChemUnitQuantity("4 mm"),
        )
    )
    components = [src, tube, bpr, snk]
    graph = compile_graph(components, [e1, e2, e3])
    return graph, components


# ---------------------------------------------------------------------------
# 1. Import smoke test
# ---------------------------------------------------------------------------


def test_imports():
    assert Worker is not None
    assert SimConfig is not None


# ---------------------------------------------------------------------------
# 2. SimConfig
# ---------------------------------------------------------------------------


def test_simconfig_defaults():
    cfg = SimConfig(dt=0.1, t_end=10.0)
    assert cfg.dt == pytest.approx(0.1)
    assert cfg.t_end == pytest.approx(10.0)
    assert cfg.viscosity > 0.0
    assert cfg.bpr_max_iters == 20


# ---------------------------------------------------------------------------
# 3. Worker initial state
# ---------------------------------------------------------------------------


def test_worker_initial_t():
    graph, components = _make_tube_platform()
    w = Worker(graph, components, SimConfig(dt=0.1, t_end=1.0))
    assert w.t == pytest.approx(0.0)


def test_worker_initial_hyd_state_is_none():
    graph, components = _make_tube_platform()
    w = Worker(graph, components, SimConfig(dt=0.1, t_end=1.0))
    assert w.hyd_state is None


def test_worker_initial_transport_state_has_queues():
    graph, components = _make_tube_platform()
    w = Worker(graph, components, SimConfig(dt=0.1, t_end=1.0))
    # At least one TRANSPORT edge should have a queue
    assert len(w.transport_state.edge_queues) > 0


# ---------------------------------------------------------------------------
# 4. step()
# ---------------------------------------------------------------------------


def test_step_advances_t():
    graph, components = _make_tube_platform()
    cfg = SimConfig(dt=0.1, t_end=1.0)
    w = Worker(graph, components, cfg)
    w.step()
    assert w.t == pytest.approx(0.1)


def test_step_sets_hyd_state():
    graph, components = _make_tube_platform()
    w = Worker(graph, components, SimConfig(dt=0.1, t_end=1.0))
    w.step()
    assert w.hyd_state is not None
    assert "src.1" in w.hyd_state.pressures
    assert w.hyd_state.pressures["src.1"] == pytest.approx(200_000.0)


def test_step_flow_is_positive():
    """Flow from high to low pressure should be positive."""
    graph, components = _make_tube_platform(src_bar=2.0, snk_bar=1.0)
    w = Worker(graph, components, SimConfig(dt=0.1, t_end=1.0))
    w.step()
    flows = w.hyd_state.flows
    # All edges in this single-path network should have the same positive flow
    for flow in flows.values():
        assert flow > 0.0


def test_multiple_steps_accumulate_t():
    graph, components = _make_tube_platform()
    cfg = SimConfig(dt=0.1, t_end=1.0)
    w = Worker(graph, components, cfg)
    for _ in range(5):
        w.step()
    assert w.t == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 5. run()
# ---------------------------------------------------------------------------


def test_run_advances_past_t_end():
    graph, components = _make_tube_platform()
    cfg = SimConfig(dt=0.1, t_end=1.0)
    w = Worker(graph, components, cfg)
    w.run()
    # run() steps until round(t/dt) > round(t_end/dt), so t > t_end
    assert w.t > cfg.t_end


def test_run_records_t_end_in_last_step():
    """t after run() should be exactly one dt past t_end."""
    graph, components = _make_tube_platform()
    cfg = SimConfig(dt=0.1, t_end=1.0)
    w = Worker(graph, components, cfg)
    w.run()
    assert w.t == pytest.approx(cfg.t_end + cfg.dt)


def test_run_closes_recorder(tmp_path):
    graph, components = _make_tube_platform()
    db = tmp_path / "run.db"
    rec = Recorder(db_path=db, graph=graph, dt=0.1, record_interval=1.0)
    cfg = SimConfig(dt=0.1, t_end=1.0)
    w = Worker(graph, components, cfg, recorder=rec)
    w.run()
    # After run(), connection is closed; opening for read should work
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    count = conn.execute("SELECT COUNT(*) FROM node_pressure").fetchone()[0]
    conn.close()
    assert count > 0


# ---------------------------------------------------------------------------
# 6. Recorder integration
# ---------------------------------------------------------------------------


def test_recorder_captures_t0(tmp_path):
    """Record at t=0 should be present because step() records BEFORE advancing."""
    graph, components = _make_tube_platform()
    db = tmp_path / "t0.db"
    rec = Recorder(db_path=db, graph=graph, dt=0.1, record_interval=1.0)
    cfg = SimConfig(dt=0.1, t_end=2.0)
    w = Worker(graph, components, cfg, recorder=rec)
    w.run()

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    times = sorted(set(r[0] for r in conn.execute("SELECT time FROM node_pressure")))
    conn.close()

    assert 0.0 in times


def test_recorder_captures_correct_interval(tmp_path):
    """record_interval=1.0 with dt=0.1 should produce recordings at 0, 1, 2."""
    graph, components = _make_tube_platform()
    db = tmp_path / "interval.db"
    rec = Recorder(db_path=db, graph=graph, dt=0.1, record_interval=1.0)
    cfg = SimConfig(dt=0.1, t_end=2.0)
    w = Worker(graph, components, cfg, recorder=rec)
    w.run()

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    times = sorted(set(r[0] for r in conn.execute("SELECT time FROM node_pressure")))
    conn.close()

    assert len(times) == 3  # t=0, 1, 2
    assert times[0] == pytest.approx(0.0)
    assert times[1] == pytest.approx(1.0, abs=1e-9)
    assert times[2] == pytest.approx(2.0, abs=1e-9)


def test_recorder_has_edge_flow_rows(tmp_path):
    graph, components = _make_tube_platform()
    db = tmp_path / "flow.db"
    rec = Recorder(db_path=db, graph=graph, dt=0.1, record_interval=1.0)
    cfg = SimConfig(dt=0.1, t_end=1.0)
    w = Worker(graph, components, cfg, recorder=rec)
    w.run()

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    count = conn.execute("SELECT COUNT(*) FROM edge_flow").fetchone()[0]
    conn.close()
    assert count > 0


# ---------------------------------------------------------------------------
# 7. BPR stabilisation
# ---------------------------------------------------------------------------


def test_bpr_opens_when_upstream_exceeds_setpoint():
    """setpoint=1.2 bar: BPR should open stably without a convergence warning."""
    graph, components = _make_bpr_platform(setpoint_bar=1.2)
    bpr_edge_id = graph.bpr_edges[0]

    cfg = SimConfig(dt=0.1, t_end=0.1)
    w = Worker(graph, components, cfg)

    log_messages: list[str] = []
    handler_id = logger.add(
        lambda message: log_messages.append(message.record["message"]),
        level="WARNING",
    )
    try:
        w.step()
    finally:
        logger.remove(handler_id)

    bpr_edge = graph.edges[bpr_edge_id]
    assert bpr_edge.resistance_override is None, "BPR should be open"
    convergence_warnings = [
        message for message in log_messages if "did not converge" in message
    ]
    assert len(convergence_warnings) == 0


def test_bpr_stays_closed_when_upstream_below_setpoint():
    """setpoint=3.5 bar: upstream ≈ 3 bar < 3.5 bar — BPR stays closed."""
    graph, components = _make_bpr_platform(setpoint_bar=3.5)
    bpr_edge_id = graph.bpr_edges[0]

    cfg = SimConfig(dt=0.1, t_end=0.1)
    w = Worker(graph, components, cfg)

    log_messages: list[str] = []
    handler_id = logger.add(
        lambda message: log_messages.append(message.record["message"]),
        level="WARNING",
    )
    try:
        w.step()
    finally:
        logger.remove(handler_id)

    bpr_edge = graph.edges[bpr_edge_id]
    assert bpr_edge.resistance_override == pytest.approx(R_MAX_HYDRAULIC)
    convergence_warnings = [
        message for message in log_messages if "did not converge" in message
    ]
    assert len(convergence_warnings) == 0


def test_bpr_open_allows_flow():
    """When BPR is open (setpoint=1.2 bar), flow through the BPR edge is nonzero."""
    graph, components = _make_bpr_platform(setpoint_bar=1.2)
    bpr_edge_id = graph.bpr_edges[0]
    cfg = SimConfig(dt=0.1, t_end=0.1)
    w = Worker(graph, components, cfg)
    w.step()
    assert abs(w.hyd_state.flows.get(bpr_edge_id, 0.0)) > 1e-9


def test_bpr_closed_blocks_flow():
    """When BPR is closed (setpoint=3.5 bar), flow through BPR is near zero."""
    graph, components = _make_bpr_platform(setpoint_bar=3.5)
    bpr_edge_id = graph.bpr_edges[0]
    cfg = SimConfig(dt=0.1, t_end=0.1)
    w = Worker(graph, components, cfg)
    w.step()
    assert abs(w.hyd_state.flows.get(bpr_edge_id, 0.0)) < 1e-6
