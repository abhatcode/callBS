"""Subprocess helpers: timeouts that kill the whole process tree, and sidecar processes."""
from __future__ import annotations

import os
import shlex
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass

IS_WINDOWS = sys.platform == "win32"

# Env vars that make a nested Claude Code refuse to start or behave like a child session.
_STRIP_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")


def child_env(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


@dataclass
class ProcResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float


def _popen_kwargs() -> dict:
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, check=False)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def run(cmd: list[str] | str, cwd: str, env: dict, timeout_s: float | None,
        stdin_text: str | None = None, shell: bool = False) -> ProcResult:
    start = time.monotonic()
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, shell=shell,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", **_popen_kwargs(),
    )
    timed_out = False
    try:
        out, err = proc.communicate(input=stdin_text, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(proc)
        out, err = proc.communicate()
    return ProcResult(proc.returncode, out or "", err or "", timed_out, time.monotonic() - start)


def run_shell(command: str, cwd: str, env: dict, timeout_s: float | None) -> ProcResult:
    return run(command, cwd=cwd, env=env, timeout_s=timeout_s, shell=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Sidecar:
    """A helper process (e.g. a compression proxy) that lives for one agent run."""

    def __init__(self, command: str, cwd: str, env: dict, port: int, ready_timeout_s: float = 30):
        self.command = command
        self.port = port
        self.log_path = None
        args = command if IS_WINDOWS else shlex.split(command)
        self.proc = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, **_popen_kwargs())
        self._wait_ready(ready_timeout_s)

    def _wait_ready(self, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"sidecar exited early with code {self.proc.returncode}: {self.command}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.2)
        self.stop()
        raise RuntimeError(f"sidecar not listening on port {self.port} after {timeout_s}s: {self.command}")

    def stop(self) -> None:
        kill_tree(self.proc)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()
