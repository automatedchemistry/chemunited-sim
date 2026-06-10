"""SimClient and SimCommand — workflow-side interface for mode 1 simulation."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from chemunited_core.components import ComponentData

from .clock import SimClock


@dataclass
class SimCommand:
    component: str
    command: str
    kwargs: dict = field(default_factory=dict)
    pre_applied: bool = False


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
    ) -> None:
        self._name = name
        self._component = component
        self._clock = clock
        self._queue = cmd_queue

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
        result = self._component.apply(command, **kwargs)
        self._queue.put(
            SimCommand(component=self._name, command=command, kwargs=kwargs, pre_applied=True)
        )
        for s in result.scheduled:
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
        result = self._component.get(command, **kwargs)
        self._wait(wait_time)
        return result

    def _deferred_enqueue(self, dt: float, command: str, kwargs: dict) -> None:
        t0 = self._clock.now()
        while self._clock.now() - t0 < dt:
            time.sleep(0.0001)
        self._queue.put(SimCommand(component=self._name, command=command, kwargs=kwargs))

    def _wait(self, duration: float) -> None:
        t0 = self._clock.now()
        while self._clock.now() - t0 < duration:
            time.sleep(0.0001)
