"""Claude Code in headless mode (`claude -p --output-format stream-json`)."""
from __future__ import annotations

import json
import shutil

from .. import proc
from ..pricing import TOKEN_FIELDS, Call
from .base import Agent, AgentResult, RunContext

# modelUsage (camelCase, per model, whole session) -> our token fields
_MODEL_USAGE_KEYS = {
    "inputTokens": "input",
    "outputTokens": "output",
    "cacheReadInputTokens": "cache_read",
    "cacheCreationInputTokens": "cache_write",
}


def parse_stream_json(text: str) -> tuple[list[Call], dict]:
    """Extract one Call per API response from a stream-json transcript.

    Claude Code may emit several lines for one assistant message (one per content
    block), each carrying that message's usage, so lines are de-duplicated by message
    id and the last one wins.
    """
    by_id: dict[str, Call] = {}
    order: list[str] = []
    result: dict = {}
    init: dict = {}
    anon = 0
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = ev.get("type")
        if kind == "assistant":
            msg = ev.get("message") or {}
            usage = msg.get("usage")
            if not usage or msg.get("model") == "<synthetic>":  # client-side error messages
                continue
            mid = msg.get("id")
            if not mid:
                anon += 1
                mid = f"_anon{anon}"
            if mid not in by_id:
                order.append(mid)
            by_id[mid] = Call.from_anthropic_usage(msg.get("model", ""), usage)
        elif kind == "result":
            result = ev
        elif kind == "system" and ev.get("subtype") == "init":
            init = ev
    calls = [by_id[i] for i in order]
    calls, notes = _reconcile(calls, result.get("modelUsage") or {})
    meta = {"result": result, "init": init, "notes": notes}
    return calls, meta


def _reconcile(calls: list[Call], model_usage: dict) -> tuple[list[Call], list[str]]:
    """Add usage the stream didn't show per message (e.g. some subagent traffic).

    `modelUsage` in the final result is the session total per model. When it exceeds
    what we summed from individual messages, the gap is added as one synthetic call,
    splitting cache writes by the TTL mix we did observe for that model.
    """
    notes = []
    for model, mu in model_usage.items():
        seen = {k: 0 for k in TOKEN_FIELDS}
        for c in calls:
            if c.model == model:
                for k in TOKEN_FIELDS:
                    seen[k] += getattr(c, k)
        seen_write = seen["cache_write_5m"] + seen["cache_write_1h"]
        gap = {}
        for src, dst in _MODEL_USAGE_KEYS.items():
            have = seen_write if dst == "cache_write" else seen[dst]
            gap[dst] = max((mu.get(src) or 0) - have, 0)
        if not any(gap.values()):
            continue
        share_1h = seen["cache_write_1h"] / seen_write if seen_write else 0.0
        w1h = round(gap["cache_write"] * share_1h)
        calls.append(Call(model=model, input=gap["input"], output=gap["output"],
                          cache_read=gap["cache_read"], cache_write_1h=w1h,
                          cache_write_5m=gap["cache_write"] - w1h))
        notes.append(f"reconciled {sum(gap.values())} tokens for {model} from session totals")
    return calls, notes


class ClaudeCodeAgent(Agent):
    name = "claude-code"

    def command(self, ctx: RunContext) -> list[str]:
        exe = self.cfg.get("executable") or shutil.which("claude") or "claude"
        cmd = [*([exe] if isinstance(exe, str) else exe), "-p", "--output-format", "stream-json", "--verbose", "--no-session-persistence"]
        if self.cfg.get("model"):
            cmd += ["--model", self.cfg["model"]]
        if self.cfg.get("effort"):
            cmd += ["--effort", self.cfg["effort"]]
        # Safe default: the agent may edit files in its throwaway clone but gets no shell.
        # Opt into more with `allowed_tools` or `permission_mode` in [agent].
        cmd += ["--permission-mode", self.cfg.get("permission_mode", "acceptEdits")]
        tools = self.cfg.get("allowed_tools", ["Read", "Edit", "Write", "Glob", "Grep"])
        if tools:
            cmd += ["--allowedTools", ",".join(tools)]
        # Opt-in: Claude Code enforces this with its own price table, which can be stale for
        # newer models (v2.1.170 overprices Haiku 5.5 ~20x) and then cuts runs short.
        if ctx.max_run_usd and self.cfg.get("enforce_run_budget", False):
            cmd += ["--max-budget-usd", str(ctx.max_run_usd)]
        a = ctx.arm
        if a.claude_settings:
            cmd += ["--settings", a.claude_settings]
        if a.mcp_config:
            cmd += ["--mcp-config", a.mcp_config, "--strict-mcp-config"]
        if a.append_system_prompt:
            cmd += ["--append-system-prompt", a.append_system_prompt]
        cmd += list(self.cfg.get("extra_args", [])) + list(a.agent_args)
        return cmd

    def run(self, ctx: RunContext) -> AgentResult:
        cmd = self.command(ctx)
        # The prompt goes over stdin: no shell quoting problems on Windows.
        r = proc.run(cmd, cwd=str(ctx.workspace), env=ctx.env, timeout_s=ctx.timeout_s,
                     stdin_text=ctx.task.prompt)
        calls, meta = parse_stream_json(r.stdout)
        res = meta["result"]
        subtype = res.get("subtype") or ""
        out = AgentResult(
            calls=calls, exit_code=r.exit_code, timed_out=r.timed_out, duration_s=r.duration_s,
            num_turns=res.get("num_turns"), reported_cost_usd=res.get("total_cost_usd"),
            notes=meta["notes"], stdout=r.stdout, stderr=r.stderr,
        )
        out.budget_exceeded = "budget" in subtype
        api_status = res.get("api_error_status")
        if r.timed_out:
            out.is_error, out.error = True, f"timed out after {ctx.timeout_s}s"
        elif not res:
            out.is_error = True
            out.error = f"no result event (exit {r.exit_code}): {r.stderr.strip()[-500:]}"
        elif res.get("is_error") or subtype != "success":
            out.is_error = True
            detail = str(res.get("result") or "").strip()[:300]
            out.error = f"agent ended with {subtype or 'error'}" + (f" (API {api_status})" if api_status else "") \
                + (f": {detail}" if detail else "")
        # Auth/permission failures say nothing about the arm and will repeat on every run.
        out.fatal = api_status in (401, 403)
        return out
