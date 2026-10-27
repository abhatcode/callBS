"""A simulated agent for demos and tests. Spends no money.

It models the economics that make compression tricky: every turn re-sends the whole
context; cached prefix tokens are cheap; a compressor shrinks tool output but may
rewrite history, which invalidates the cache and forces an expensive re-write.

Task-level keys ([tasks.mock]): turns, system_tokens, tool_output_tokens,
output_tokens, success_rate, model, solution = {path = "file contents"}.
Arm-level keys ([arms.mock]): tool_output_scale, system_scale, cache_break_rate,
extra_turns, success_delta.
"""
from __future__ import annotations

import hashlib
import random

from ..pricing import Call
from .base import Agent, AgentResult, RunContext

_TASK_DEFAULTS = {"turns": 12, "system_tokens": 20000, "tool_output_tokens": 3000,
                  "output_tokens": 400, "success_rate": 0.8, "model": "claude-sonnet-5-5"}
_ARM_DEFAULTS = {"tool_output_scale": 1.0, "system_scale": 1.0, "cache_break_rate": 0.0,
                 "extra_turns": 0, "success_delta": 0.0}


def _rng(*parts) -> random.Random:
    h = hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()
    return random.Random(int(h[:16], 16))


class MockAgent(Agent):
    name = "mock"

    def run(self, ctx: RunContext) -> AgentResult:
        t = _TASK_DEFAULTS | ctx.task.mock
        a = _ARM_DEFAULTS | ctx.arm.mock
        # Task difficulty is shared across arms (same draw), arm behaviour adds its own noise.
        task_rng = _rng(ctx.seed, ctx.task.id, ctx.trial)
        arm_rng = _rng(ctx.seed, ctx.task.id, ctx.trial, ctx.arm.name)

        base_turns = max(2, round(t["turns"] * task_rng.lognormvariate(0, 0.35)))
        difficulty = task_rng.random()  # common random number: hard tasks are hard for every arm
        turns = base_turns + int(a["extra_turns"]) + (1 if arm_rng.random() < 0.3 else 0)
        context = int(t["system_tokens"] * a["system_scale"])
        cached = 0
        calls = []
        for _ in range(turns):
            prompt = context + 50  # +50 for the new user/tool turn
            broke = cached and arm_rng.random() < a["cache_break_rate"]
            read = 0 if broke else cached
            calls.append(Call(model=t["model"], input=3, cache_read=read,
                              cache_write_1h=prompt - read - 3,
                              output=int(t["output_tokens"] * arm_rng.lognormvariate(0, 0.3))))
            cached = prompt
            tool_out = t["tool_output_tokens"] * task_rng.lognormvariate(0, 0.6) * a["tool_output_scale"]
            context = prompt + calls[-1].output + int(tool_out)

        p = min(max(t["success_rate"] + a["success_delta"], 0.0), 1.0)
        if difficulty < p:
            for rel, content in t.get("solution", {}).items():
                dest = ctx.workspace / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content, encoding="utf-8")
        return AgentResult(calls=calls, exit_code=0, duration_s=0.01 * turns, num_turns=turns)
