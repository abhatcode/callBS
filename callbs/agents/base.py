from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..config import Arm, Task
from ..pricing import Call


@dataclass
class AgentResult:
    calls: list[Call] = field(default_factory=list)
    exit_code: int | None = None
    timed_out: bool = False
    duration_s: float = 0.0
    is_error: bool = False
    budget_exceeded: bool = False
    error: str | None = None
    num_turns: int | None = None
    reported_cost_usd: float | None = None
    notes: list[str] = field(default_factory=list)
    fatal: bool = False  # environment problem (e.g. expired login): stop the experiment
    stdout: str = ""
    stderr: str = ""


@dataclass
class RunContext:
    task: Task
    arm: Arm
    workspace: Path
    run_dir: Path
    env: dict
    timeout_s: float
    max_run_usd: float | None
    trial: int
    seed: int


class Agent:
    """Runs one task in one workspace and reports the API calls it made."""

    name = "base"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def run(self, ctx: RunContext) -> AgentResult:
        raise NotImplementedError
