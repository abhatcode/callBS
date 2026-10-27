"""Build a throwaway repo + experiment that runs end to end with the simulated agent."""
from __future__ import annotations

import json
import random
import subprocess
import sys
from pathlib import Path

ARMS = """
[[arms]]
name = "baseline"

[[arms]]
name = "compressor-aggressive"
# Shrinks tool output a lot, but rewrites history often (cache misses) and sometimes
# hides a detail the agent needed (extra turns, slightly lower success).
mock = {{ tool_output_scale = 0.45, cache_break_rate = 0.12, extra_turns = 1, success_delta = -0.04 }}

[[arms]]
name = "compressor-light"
mock = {{ tool_output_scale = 0.8, cache_break_rate = 0.03 }}

[[arms]]
name = "schema-filter"
# Drops unused tool schemas from the system prompt: smaller fixed prefix every turn.
mock = {{ system_scale = 0.45 }}
"""


def build(out: Path, n_tasks: int = 16, trials: int = 3, seed: int = 7) -> Path:
    out = out.resolve()
    repo = out / "demo-repo"
    (repo / "lib").mkdir(parents=True, exist_ok=True)
    (repo / "tests").mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    py = json.dumps(sys.executable)[1:-1].replace("\\\\", "/")
    tasks = []
    for i in range(n_tasks):
        (repo / "lib" / f"f{i}.py").write_text(f"def f(x):\n    return x * {i + 2} + 1  # bug\n")
        (repo / "tests" / f"test_f{i}.py").write_text(
            "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))\n"
            f"from lib.f{i} import f\nassert f(3) == {3 * (i + 2)}, f(3)\nprint('ok')\n")
        mock = {"turns": rng.randint(6, 26), "tool_output_tokens": rng.randint(800, 6000),
                "system_tokens": rng.choice([18000, 26000, 41000]),
                "success_rate": round(rng.uniform(0.6, 0.95), 2),
                "solution": {f"lib/f{i}.py": f"def f(x):\n    return x * {i + 2}\n"}}
        tasks.append(
            f'[[tasks]]\nid = "fix-f{i:02d}"\n'
            f'prompt = "lib/f{i}.py returns the wrong value; make tests/test_f{i}.py pass."\n'
            f'check = "\\"{py}\\" tests/test_f{i}.py"\n'
            f'restore_paths = ["tests/"]\n'
            f"mock = {_inline_table(mock)}\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=demo@callbs.local", "-c", "user.name=demo",
                    "commit", "-qm", "demo repo"], cwd=repo, check=True)

    cfg = out / "experiment.toml"
    cfg.write_text(
        "# Demo experiment: simulated agent, no API calls, no spend.\n"
        "[experiment]\n"
        'name = "demo"\nrepo = "demo-repo"\nbaseline = "baseline"\n'
        f"trials = {trials}\nseed = {seed}\nparallel = 4\nmonthly_spend_usd = 20000\n\n"
        '[agent]\ntype = "mock"\n'
        + ARMS.format() + "\n" + "\n".join(tasks), encoding="utf-8")
    return cfg


def _inline_table(d: dict) -> str:
    parts = []
    for k, v in d.items():
        k = k if k.replace("_", "").isalnum() else json.dumps(k)
        if isinstance(v, dict):
            parts.append(f"{k} = {_inline_table(v)}")
        else:
            parts.append(f"{k} = {json.dumps(v)}")
    return "{ " + ", ".join(parts) + " }"
