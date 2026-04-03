"""Tests for the idle-based watchdog in ClaudeProvider."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from bike_shop.providers.claude import (
    _IdleTimeoutError,
    _run_with_idle_watchdog,
)

# All tests use short timeouts for speed.
IDLE = 2  # seconds
MAX = 10  # seconds
GRACE = 1  # seconds


def _python(script: str) -> list[str]:
    """Build a command that runs an inline Python script."""
    return [sys.executable, "-u", "-c", textwrap.dedent(script)]


class TestHealthyProcess:
    """Process that emits output regularly should NOT be killed."""

    def test_completes_normally(self, tmp_path: object) -> None:
        cmd = _python("""\
            import time, sys
            for i in range(5):
                print(f"line {i}", flush=True)
                time.sleep(0.3)
        """)
        result = _run_with_idle_watchdog(
            cmd,
            idle_timeout=IDLE,
            max_timeout=MAX,
            grace_period=GRACE,
            cwd=str(tmp_path),
            env=os.environ.copy(),
            agent_name="test",
        )
        assert result.returncode == 0
        assert "line 0" in result.stdout
        assert "line 4" in result.stdout

    def test_fast_process_completes(self, tmp_path: object) -> None:
        cmd = _python('print("done", flush=True)')
        result = _run_with_idle_watchdog(
            cmd,
            idle_timeout=IDLE,
            max_timeout=MAX,
            grace_period=GRACE,
            cwd=str(tmp_path),
            env=os.environ.copy(),
            agent_name="test",
        )
        assert result.returncode == 0
        assert "done" in result.stdout


class TestStuckProcess:
    """Process that stops producing output should be killed after idle timeout."""

    def test_killed_after_idle(self, tmp_path: object) -> None:
        cmd = _python("""\
            import time, sys
            print("start", flush=True)
            time.sleep(60)  # hang forever
        """)
        start = time.time()
        with pytest.raises(_IdleTimeoutError, match="idle"):
            _run_with_idle_watchdog(
                cmd,
                idle_timeout=IDLE,
                max_timeout=MAX,
                grace_period=GRACE,
                cwd=str(tmp_path),
                env=os.environ.copy(),
                agent_name="test",
            )
        elapsed = time.time() - start
        # Should be killed around IDLE seconds, not MAX
        assert elapsed < IDLE + GRACE + 3


class TestAbsoluteTimeout:
    """Process emitting output forever should be killed at max absolute timeout."""

    def test_killed_at_absolute_timeout(self, tmp_path: object) -> None:
        cmd = _python("""\
            import time, sys
            while True:
                print("alive", flush=True)
                time.sleep(0.5)
        """)
        short_max = 3
        start = time.time()
        with pytest.raises(_IdleTimeoutError, match="absolute safety timeout"):
            _run_with_idle_watchdog(
                cmd,
                idle_timeout=IDLE,
                max_timeout=short_max,
                grace_period=GRACE,
                cwd=str(tmp_path),
                env=os.environ.copy(),
                agent_name="test",
            )
        elapsed = time.time() - start
        assert elapsed < short_max + GRACE + 3


class TestOutputCollection:
    """Verify stdout is fully collected from incremental reads."""

    def test_all_lines_collected(self, tmp_path: object) -> None:
        n = 50
        cmd = _python(f"""\
            for i in range({n}):
                print(f"line {{i}}", flush=True)
        """)
        result = _run_with_idle_watchdog(
            cmd,
            idle_timeout=IDLE,
            max_timeout=MAX,
            grace_period=GRACE,
            cwd=str(tmp_path),
            env=os.environ.copy(),
            agent_name="test",
        )
        assert result.returncode == 0
        lines = [l for l in result.stdout.strip().splitlines() if l.startswith("line")]
        assert len(lines) == n

    def test_stderr_collected(self, tmp_path: object) -> None:
        cmd = _python("""\
            import sys
            print("out", flush=True)
            print("err", file=sys.stderr, flush=True)
        """)
        result = _run_with_idle_watchdog(
            cmd,
            idle_timeout=IDLE,
            max_timeout=MAX,
            grace_period=GRACE,
            cwd=str(tmp_path),
            env=os.environ.copy(),
            agent_name="test",
        )
        assert "out" in result.stdout
        assert "err" in result.stderr


class TestGracefulKill:
    """Verify SIGTERM is sent before SIGKILL."""

    def test_sigterm_received(self, tmp_path: object) -> None:
        """Process that traps SIGTERM writes a marker file; verify the file exists."""
        marker = tmp_path / "sigterm_received"
        cmd = _python(f"""\
            import signal, sys, time, pathlib

            def handler(sig, frame):
                pathlib.Path("{marker}").write_text("yes")
                sys.exit(0)

            signal.signal(signal.SIGTERM, handler)
            print("start", flush=True)
            time.sleep(60)
        """)
        with pytest.raises(_IdleTimeoutError):
            _run_with_idle_watchdog(
                cmd,
                idle_timeout=IDLE,
                max_timeout=MAX,
                grace_period=GRACE,
                cwd=str(tmp_path),
                env=os.environ.copy(),
                agent_name="test",
            )
        assert marker.exists(), "SIGTERM handler was never called"
        assert marker.read_text() == "yes"


class TestProcessCrash:
    """Process that crashes immediately should return without timeout."""

    def test_crash_returns_nonzero(self, tmp_path: object) -> None:
        cmd = _python("raise SystemExit(42)")
        result = _run_with_idle_watchdog(
            cmd,
            idle_timeout=IDLE,
            max_timeout=MAX,
            grace_period=GRACE,
            cwd=str(tmp_path),
            env=os.environ.copy(),
            agent_name="test",
        )
        assert result.returncode == 42
