"""Any agent CLI, as long as it reports usage in a format we can read.

Config:
    [agent]
    type = "command"
    command = "my-agent --prompt-file {prompt_file}"
    usage_format = "claude-stream-json"   # stdout is Claude Code stream-json
    # or: usage_format = "anthropic-jsonl" and usage_file = "{run_dir}/usage.jsonl"
    #     (one JSON object per API call: {"model": ..., "usage": {Anthropic usage object}})
"""
from __future__ import annotations

import json

from .. import proc
from ..pricing import Call
from .base import Agent, AgentResult, RunContext
from .claude_code import parse_stream_json


class CommandAgent(Agent):
    name = "command"

    def run(self, ctx: RunContext) -> AgentResult:
        prompt_file = ctx.run_dir / "prompt.txt"
        prompt_file.write_text(ctx.task.prompt, encoding="utf-8")
        fmt = {"prompt_file": str(prompt_file), "workspace": str(ctx.workspace),
               "run_dir": str(ctx.run_dir)}
        command = self.cfg["command"].format(**fmt)
        r = proc.run_shell(command, cwd=str(ctx.workspace), env=ctx.env, timeout_s=ctx.timeout_s)
        out = AgentResult(exit_code=r.exit_code, timed_out=r.timed_out, duration_s=r.duration_s,
                          stdout=r.stdout, stderr=r.stderr)
        usage_format = self.cfg.get("usage_format", "claude-stream-json")
        if usage_format == "claude-stream-json":
            out.calls, meta = parse_stream_json(r.stdout)
            out.notes = meta["notes"]
            out.reported_cost_usd = meta["result"].get("total_cost_usd")
            out.num_turns = meta["result"].get("num_turns")
        elif usage_format == "anthropic-jsonl":
            path = self.cfg["usage_file"].format(**fmt)
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            row = json.loads(line)
                            out.calls.append(Call.from_anthropic_usage(row.get("model", ""), row["usage"]))
            except FileNotFoundError:
                out.notes.append(f"usage file not found: {path}")
        else:
            raise ValueError(f"unknown usage_format {usage_format!r}")
        if r.timed_out:
            out.is_error, out.error = True, f"timed out after {ctx.timeout_s}s"
        elif r.exit_code != 0:
            out.is_error, out.error = True, f"exit code {r.exit_code}: {r.stderr.strip()[-500:]}"
        return out
