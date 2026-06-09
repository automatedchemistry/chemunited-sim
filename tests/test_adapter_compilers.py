"""Tests for adapter graph compilation details."""

from __future__ import annotations

from chemunited_core.figure_registry import COMPONENTS

from chemunited_sim.adapter import compile_graph


def test_solenoid_valve_2_way_common_port_compiles_as_hub():
    defn = COMPONENTS["SolenoidValve2Way"]
    valve = defn.data_class.from_mode(defn.mode_class(name="divertvalve"))

    graph = compile_graph([valve], [])

    assert sorted(
        node_id for node_id in graph.nodes if node_id.startswith("divertvalve.")
    ) == ["divertvalve.0", "divertvalve.1", "divertvalve.2"]
    assert graph.nodes["divertvalve.0"].is_hub is True
    assert graph.nodes["divertvalve.1"].is_hub is False
    assert graph.nodes["divertvalve.2"].is_hub is False
