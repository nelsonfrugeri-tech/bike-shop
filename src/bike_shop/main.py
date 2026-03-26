from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import tempfile

from slack_bolt.adapter.socket_mode import SocketModeHandler

from bike_shop.config import AGENT_REGISTRY, load_config
from bike_shop.handlers import create_handler

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    stream=sys.stdout,
)
logging.getLogger("slack_bolt").setLevel(logging.INFO)
logging.getLogger("slack_sdk").setLevel(logging.INFO)
logger = logging.getLogger(__name__)

PID_DIR = os.path.join(tempfile.gettempdir(), "bike-shop")


def _pid_file(agent_name: str) -> str:
    return os.path.join(PID_DIR, f"{agent_name}.pid")


def _is_running(agent_name: str) -> int | None:
    """Return PID if agent is already running, else None."""
    path = _pid_file(agent_name)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        import subprocess as _sp
        out = _sp.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
        if "bike_shop" not in out.stdout:
            raise ProcessLookupError("stale pid")
        return pid
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        os.unlink(path)
        return None


def _write_pid(agent_name: str) -> None:
    os.makedirs(PID_DIR, exist_ok=True)
    with open(_pid_file(agent_name), "w") as f:
        f.write(str(os.getpid()))


def _remove_pid(agent_name: str) -> None:
    path = _pid_file(agent_name)
    if os.path.exists(path):
        os.unlink(path)


def _stop_agent(agent_name: str) -> None:
    """Stop a running agent by sending SIGTERM."""
    pid = _is_running(agent_name)
    if pid is None:
        print(f"{agent_name} is not running.")
        return
    os.kill(pid, signal.SIGTERM)
    print(f"Sent SIGTERM to {agent_name} (PID {pid}).")


def _status() -> None:
    """Show status of all agents."""
    for name in sorted(AGENT_REGISTRY):
        pid = _is_running(name)
        if pid:
            print(f"  {name}: running (PID {pid})")
        else:
            print(f"  {name}: stopped")


def _connect_agent(agent_name: str) -> tuple[str, SocketModeHandler]:
    """Connect a single agent and return (name, handler). Does NOT block."""
    existing = _is_running(agent_name)
    if existing:
        logger.error("%s is already running (PID %d). Use --stop first.", agent_name, existing)
        sys.exit(1)

    config = load_config(agent_name)
    logger.info("Starting %s (%s) [%s]...", config.name, config.role, config.bot_user_id)

    handler = create_handler(config)
    handler.connect()
    _write_pid(agent_name)

    logger.info("%s connected via Socket Mode. Waiting for @mentions...", config.name)
    return agent_name, handler


def _wait_with_shutdown(agents: list[tuple[str, SocketModeHandler]]) -> None:
    """Block until SIGINT/SIGTERM, then gracefully shut down all agents."""
    import time

    stop = False

    def _shutdown(sig, frame):
        nonlocal stop
        if stop:
            return
        stop = True
        for name, handler in agents:
            logger.info("Shutting down %s...", name)
            handler.close()
            _remove_pid(name)
            logger.info("%s stopped.", name)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    while not stop:
        time.sleep(1)


def _start_agent(agent_name: str) -> None:
    """Start a single agent with graceful shutdown."""
    name, handler = _connect_agent(agent_name)
    _wait_with_shutdown([(name, handler)])


def _start_all() -> None:
    """Start all agents and block with shared shutdown."""
    import concurrent.futures

    agents: list[tuple[str, SocketModeHandler]] = []
    with concurrent.futures.ThreadPoolExecutor() as pool:
        futures = {pool.submit(_connect_agent, name): name for name in sorted(AGENT_REGISTRY)}
        for future in concurrent.futures.as_completed(futures):
            try:
                agents.append(future.result())
            except SystemExit as e:
                logger.error("Failed to start %s: %s", futures[future], e)

    if not agents:
        logger.error("No agents started.")
        sys.exit(1)

    logger.info("All %d agents connected.", len(agents))
    _wait_with_shutdown(agents)


def _parse_agent_arg(value: str) -> str | None:
    """Parse 'agent:name' format, return agent name or None for 'agent:all'."""
    if not value.startswith("agent:"):
        return value
    name = value.split(":", 1)[1]
    return name if name != "all" else None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bike Shop — team agents via Socket Mode",
    )
    parser.add_argument(
        "command",
        nargs="?",
        help="agent:tyrell | agent:elliot | agent:mr-robot | agent:all",
    )
    parser.add_argument(
        "--stop",
        metavar="AGENT",
        help="Stop a running agent (e.g. agent:tyrell or tyrell)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show status of all agents",
    )
    args = parser.parse_args()

    if args.status:
        _status()
        return

    if args.stop:
        name = _parse_agent_arg(args.stop)
        if name and name in AGENT_REGISTRY:
            _stop_agent(name)
        else:
            print(f"Unknown agent: {args.stop}")
            print(f"Available: {', '.join(sorted(AGENT_REGISTRY))}")
        return

    if args.command:
        name = _parse_agent_arg(args.command)
        if name is None:
            _start_all()
        elif name in AGENT_REGISTRY:
            _start_agent(name)
        else:
            print(f"Unknown agent: {args.command}")
            print(f"Available: agent:all, " + ", ".join(f"agent:{n}" for n in sorted(AGENT_REGISTRY)))
        return

    parser.print_help()


if __name__ == "__main__":
    main()
