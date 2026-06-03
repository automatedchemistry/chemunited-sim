"""SimClient and SimCommand — workflow-side interface for mode 1 simulation."""

from __future__ import annotations

import queue
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


class SimClient:
    """Satisfies ComponentClientProtocol for mode 1 (workflow) simulation.

    put() enqueues a SimCommand and busy-waits on SimClock until wait_time
    elapses. get() delegates directly to ComponentData.get(), enabling
    feedback-dependent protocol branching.
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

    def put(self, command: str, *, wait_time: float = 0.0, **kwargs: Any) -> None:
        t0 = self._clock.now()
        self._queue.put(SimCommand(component=self._name, command=command, kwargs=kwargs))
        while self._clock.now() - t0 < wait_time:
            time.sleep(0.0001)

    def wait_sim_time(self, sim_seconds: float) -> None:
        """Block the workflow thread until sim_seconds of simulated time elapse."""
        t0 = self._clock.now()
        while self._clock.now() - t0 < sim_seconds:
            time.sleep(0.0001)

    def get(self, command: str, **kwargs: Any) -> Any:
        return self._component.get(command, **kwargs)
