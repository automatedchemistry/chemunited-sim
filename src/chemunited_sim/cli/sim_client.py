"""SimClient and SimCommand — workflow-side interface for mode 1 simulation."""

from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from chemunited_core.components import ComponentData

from .clock import SimClock


@dataclass
class SimCommand:
    component: str
    command: str
    kwargs: dict = field(default_factory=dict)
    pre_applied: bool = False


def _normalize_connect_pairs(parsed: object) -> list[tuple[int, int]] | None:
    if not isinstance(parsed, list) or not parsed:
        return None
    # Flat pair: [a, b] - both elements are numbers, not nested lists.
    if len(parsed) == 2 and all(isinstance(x, (int, float)) for x in parsed):
        return [tuple(parsed)]  # type: ignore[list-item]
    # One or more pairs: [[a, b], ...] - every element is itself a 2-element list.
    if all(isinstance(item, list) and len(item) == 2 for item in parsed):
        return [tuple(item) for item in parsed]
    return None


def iter_apply_kwargs(command: str, kwargs: dict) -> Iterator[dict]:
    """Normalize a "position" command's ``connect`` kwarg before calling apply().

    ``PositionParameter.connect`` (chemunited_core.protocols.valves) is
    deliberately str-typed for a GUI dropdown, and its JSON shape isn't
    consistent - a flat pair "[a, b]" or a wrapped list of pairs
    "[[a, b]]"/"[[a, b], [c, d]]". ``ComponentData.apply()`` expects a single
    flat pair, so detect the shape, unwrap, and iterate - one ``apply()`` call
    per pair. Mirrors chemunited-orchestrator's pool_commands.py, which does
    the same unwrapping for its live GUI-replay path; this is the equivalent
    for the actual physics application here.
    """
    if command == "position" and isinstance(kwargs.get("connect"), str):
        try:
            parsed = json.loads(kwargs["connect"])
        except (json.JSONDecodeError, ValueError, TypeError):
            parsed = None
        pairs = _normalize_connect_pairs(parsed)
        if pairs:
            for pair in pairs:
                yield {**kwargs, "connect": pair}
            return
    yield kwargs


def write_pool_log(
    project_path: Path,
    component: str,
    *,
    method: str,
    command: str,
    params: dict | None,
    wait_time: float,
    wait_feedback_status: bool,
    feedback_status_command: str,
    feedback_answer: str,
) -> None:
    """Append one command entry to project_path/log/pool/<component>.jsonl.

    Mirrors chemunited-workflow's ComponentClient._write_json_log format so the
    same viewer tooling can render simulated runs.
    """
    path = project_path / "log" / "pool" / f"{component}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "component": component,
        "method": method,
        "command": command,
        "params": params,
        "wait_time": wait_time,
        "wait_feedback_status": wait_feedback_status,
        "feedback_status_command": feedback_status_command,
        "feedback_answer": feedback_answer,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(data) + "\n")


class SimClient:
    """Satisfies ComponentClientProtocol for mode 1 (workflow) simulation.

    put() calls apply() immediately (setting component state on the workflow
    thread), enqueues a pre-applied SimCommand so the worker resyncs the graph,
    and spawns a daemon thread for each scheduled follow-up (e.g. auto-stop
    after a volume dispense). get() delegates directly to ComponentData.get().
    """

    def __init__(
        self,
        name: str,
        component: ComponentData,
        clock: SimClock,
        cmd_queue: queue.Queue,
        project_path: Path,
    ) -> None:
        self._name = name
        self._component = component
        self._clock = clock
        self._queue = cmd_queue
        self._project_path = project_path

    def put(
        self,
        command: str,
        *,
        wait_time: float = 0.0,
        wait_feedback_status: bool = False,
        feedback_status_command: str = "",
        feedback_answer: str = "true",
        **kwargs: Any,
    ) -> None:
        write_pool_log(
            self._project_path,
            self._name,
            method="PUT",
            command=command,
            params=kwargs or None,
            # sim time the command was sent, not the wait_time argument above
            wait_time=self._clock.now(),
            wait_feedback_status=wait_feedback_status,
            feedback_status_command=feedback_status_command,
            feedback_answer=feedback_answer,
        )
        scheduled = []
        for call_kwargs in iter_apply_kwargs(command, kwargs):
            result = self._component.apply(command, **call_kwargs)
            scheduled.extend(result.scheduled)
        self._queue.put(
            SimCommand(
                component=self._name, command=command, kwargs=kwargs, pre_applied=True
            )
        )
        for s in scheduled:
            threading.Thread(
                target=self._deferred_enqueue,
                args=(s.dt, s.command, s.kwargs),
                daemon=True,
            ).start()
        self._wait(wait_time)

    def get(
        self,
        command: str,
        *,
        wait_time: float = 0.0,
        wait_feedback_status: bool = False,
        feedback_status_command: str = "",
        feedback_answer: str = "true",
        **kwargs: Any,
    ) -> Any:
        write_pool_log(
            self._project_path,
            self._name,
            method="GET",
            command=command,
            params=kwargs or None,
            wait_time=self._clock.now(),
            wait_feedback_status=wait_feedback_status,
            feedback_status_command=feedback_status_command,
            feedback_answer=feedback_answer,
        )
        result = self._component.get(command, **kwargs)
        self._wait(wait_time)
        return result

    def _deferred_enqueue(self, dt: float, command: str, kwargs: dict) -> None:
        t0 = self._clock.now()
        while self._clock.now() - t0 < dt:
            time.sleep(0.0001)
        self._queue.put(
            SimCommand(component=self._name, command=command, kwargs=kwargs)
        )

    def _wait(self, duration: float) -> None:
        t0 = self._clock.now()
        while self._clock.now() - t0 < duration:
            time.sleep(0.0001)
