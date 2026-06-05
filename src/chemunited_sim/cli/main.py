"""CLI entry point — parse arguments, initialise state, launch uvicorn."""

from __future__ import annotations

import argparse
import queue
from pathlib import Path

import uvicorn

from ..worker.config import SimConfig
from . import server as _server_module
from .clock import SimClock
from .server import SimStatus, SimulationState, _do_load_project, app


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="chemunited-sim",
        description="Start the chemunited simulation server.",
    )
    parser.add_argument(
        "project",
        nargs="?",
        type=Path,
        default=None,
        help="Path to a project folder or .chemunited ZIP. "
        "If omitted the server starts in NO_PROJECT state.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=1472,
        help="Port for the FastAPI server (default: 1472).",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("./simulations/"),
        metavar="PATH",
        help="Directory where .db files are saved (default: ./simulations/).",
    )
    args = parser.parse_args()

    db_dir = args.db.resolve()
    clock = SimClock()
    cmd_queue: queue.Queue = queue.Queue()

    state = SimulationState(
        sim_status=SimStatus.NO_PROJECT,
        current_t=0.0,
        config=SimConfig(),
        db_path=None,
        project=None,
        clock=clock,
        cmd_queue=cmd_queue,
        db_dir=db_dir,
    )
    _server_module._state = state

    if args.project is not None:
        _do_load_project(args.project.resolve(), state)

    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
