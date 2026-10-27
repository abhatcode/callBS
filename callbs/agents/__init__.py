from .base import Agent, AgentResult, RunContext
from .claude_code import ClaudeCodeAgent
from .command import CommandAgent
from .mock import MockAgent

AGENTS = {cls.name: cls for cls in (ClaudeCodeAgent, CommandAgent, MockAgent)}


def make_agent(cfg: dict) -> Agent:
    kind = cfg.get("type", "claude-code")
    if kind not in AGENTS:
        raise ValueError(f"unknown agent type {kind!r}; choose from {sorted(AGENTS)}")
    return AGENTS[kind](cfg)


__all__ = ["Agent", "AgentResult", "RunContext", "make_agent", "AGENTS"]
