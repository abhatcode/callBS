"""Experiment configuration (TOML)."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    pass


@dataclass
class Task:
    id: str
    prompt: str
    check: str
    commit: str = "HEAD"
    setup: list[str] = field(default_factory=list)
    restore_paths: list[str] = field(default_factory=list)  # reset after the agent, before the check
    check_timeout_s: float = 900
    mock: dict = field(default_factory=dict)  # only read by the mock agent


@dataclass
class Arm:
    name: str
    env: dict = field(default_factory=dict)
    setup: list[str] = field(default_factory=list)
    sidecar: dict | None = None          # {command, ready_timeout_s}; "{port}" is substituted
    claude_settings: str | None = None   # path passed to --settings
    mcp_config: str | None = None        # path passed to --mcp-config (with --strict-mcp-config)
    append_system_prompt: str | None = None
    agent_args: list[str] = field(default_factory=list)
    overhead_usd_per_run: float = 0.0    # licence fees, the optimizer's own LLM calls, etc.
    mock: dict = field(default_factory=dict)


@dataclass
class Experiment:
    name: str
    repo: Path
    tasks: list[Task]
    arms: list[Arm]
    agent: dict
    baseline: str
    trials: int = 3
    seed: int = 0
    parallel: int = 1
    budget_usd: float | None = None
    max_run_usd: float | None = None
    timeout_s: float = 1800
    quality_margin: float = 0.05
    monthly_spend_usd: float | None = None
    early_stop: bool = False          # stop between trials once every arm has a clear verdict
    early_stop_ci: float = 0.99       # stricter than the report's 95%: repeated looks invite flukes
    early_stop_min_tasks: int = 10
    keep_workspaces: bool = False  # leaves the last checkout of each worker slot in place
    results_dir: Path = Path("runs")
    pricing: Path | None = None
    base_dir: Path = Path(".")

    def arm(self, name: str) -> Arm:
        for a in self.arms:
            if a.name == name:
                return a
        raise KeyError(name)


def _resolve(base: Path, p: str | None) -> Path | None:
    if p is None:
        return None
    q = Path(p).expanduser()
    return q if q.is_absolute() else (base / q).resolve()


def _build(cls, raw: dict, where: str):
    allowed = set(cls.__dataclass_fields__)
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)}; allowed: {sorted(allowed)}")
    try:
        return cls(**raw)
    except TypeError as e:
        raise ConfigError(f"{where}: {e}") from None


def load(path: str | Path) -> Experiment:
    path = Path(path).resolve()
    base = path.parent
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    exp = dict(raw.get("experiment", {}))
    if "repo" not in exp:
        raise ConfigError("[experiment] needs `repo` (path to a local git repository)")

    task_rows = list(raw.get("tasks", []))
    tasks_file = exp.pop("tasks_file", None)
    if tasks_file:
        with open(_resolve(base, tasks_file), "rb") as f:
            task_rows += tomllib.load(f).get("tasks", [])
    tasks = [_build(Task, t, f"tasks[{i}]") for i, t in enumerate(task_rows)]
    arms = [_build(Arm, a, f"arms[{i}]") for i, a in enumerate(raw.get("arms", []))]

    if not tasks:
        raise ConfigError("no [[tasks]] defined")
    if len(arms) < 2:
        raise ConfigError("define at least two [[arms]]: a baseline and one candidate "
                          "(uncomment one of the example arms in the template)")
    for kind, items in (("task", tasks), ("arm", arms)):
        ids = [getattr(x, "id", None) or x.name for x in items]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ConfigError(f"duplicate {kind} ids: {sorted(dupes)}")

    for a in arms:
        a.claude_settings = str(_resolve(base, a.claude_settings)) if a.claude_settings else None
        a.mcp_config = str(_resolve(base, a.mcp_config)) if a.mcp_config else None

    baseline = exp.pop("baseline", arms[0].name)
    if baseline not in [a.name for a in arms]:
        raise ConfigError(f"baseline arm {baseline!r} is not defined")

    agent = dict(raw.get("agent", {"type": "claude-code"}))
    exp["repo"] = _resolve(base, exp["repo"])
    exp["results_dir"] = _resolve(base, exp.get("results_dir", f"runs/{exp.get('name', path.stem)}"))
    exp["pricing"] = _resolve(base, exp.get("pricing"))
    exp.setdefault("name", path.stem)
    known = set(Experiment.__dataclass_fields__) - {"tasks", "arms", "agent", "baseline", "base_dir"}
    unknown = set(exp) - known
    if unknown:
        raise ConfigError(f"[experiment]: unknown keys {sorted(unknown)}")
    return Experiment(tasks=tasks, arms=arms, agent=agent, baseline=baseline, base_dir=base, **exp)
