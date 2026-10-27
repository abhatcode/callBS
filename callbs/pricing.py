"""Per-API-call pricing.

Costs are computed per call, not from session totals, because some prices depend on
the individual request (Haiku 5.5's long-prompt tier, fast mode) and because cache
writes come in two TTLs priced differently.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_PRICES = Path(__file__).parent / "data" / "anthropic_prices.json"

TOKEN_FIELDS = ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output")
COST_CATEGORIES = ("input", "cache_write", "cache_read", "output", "server_tools")


@dataclass
class Call:
    """Token usage of one API request, as billed by the provider."""

    model: str
    input: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0
    output: int = 0
    web_search: int = 0
    fast: bool = False

    @property
    def prompt_tokens(self) -> int:
        return self.input + self.cache_write_5m + self.cache_write_1h + self.cache_read

    @classmethod
    def from_anthropic_usage(cls, model: str, usage: dict) -> "Call":
        """Build from an Anthropic Messages API `usage` object."""
        total_write = usage.get("cache_creation_input_tokens") or 0
        split = usage.get("cache_creation") or {}
        w1h = split.get("ephemeral_1h_input_tokens") or 0
        w5m = split.get("ephemeral_5m_input_tokens")
        if w5m is None:
            w5m = max(total_write - w1h, 0)
        server = usage.get("server_tool_use") or {}
        return cls(
            model=model or "unknown",
            input=usage.get("input_tokens") or 0,
            cache_write_5m=w5m,
            cache_write_1h=w1h,
            cache_read=usage.get("cache_read_input_tokens") or 0,
            output=usage.get("output_tokens") or 0,
            web_search=server.get("web_search_requests") or 0,
            fast=usage.get("speed") == "fast",
        )


_DATE_SUFFIX = re.compile(r"([-@]\d{8})?(-v\d+(:\d+)?)?$")


def normalize_model(model: str) -> str:
    m = (model or "").lower().strip()
    m = re.sub(r"\[.*?\]$", "", m)  # "claude-opus-5-5[1m]"
    for prefix in ("us.anthropic.", "eu.anthropic.", "global.anthropic.", "anthropic."):
        if m.startswith(prefix):
            m = m[len(prefix):]
    return _DATE_SUFFIX.sub("", m)


@dataclass
class CostBreakdown:
    input: float = 0.0
    cache_write: float = 0.0
    cache_read: float = 0.0
    output: float = 0.0
    server_tools: float = 0.0
    unpriced_calls: int = 0
    unpriced_models: set = field(default_factory=set)

    @property
    def total(self) -> float:
        return self.input + self.cache_write + self.cache_read + self.output + self.server_tools

    def add(self, other: "CostBreakdown") -> None:
        for k in COST_CATEGORIES:
            setattr(self, k, getattr(self, k) + getattr(other, k))
        self.unpriced_calls += other.unpriced_calls
        self.unpriced_models |= other.unpriced_models

    def to_dict(self) -> dict:
        d = {k: round(getattr(self, k), 6) for k in COST_CATEGORIES}
        d["total"] = round(self.total, 6)
        d["unpriced_calls"] = self.unpriced_calls
        d["unpriced_models"] = sorted(self.unpriced_models)
        return d


class PriceTable:
    def __init__(self, data: dict):
        self.models: dict = data["models"]
        self.web_search_price = data.get("server_tools", {}).get("web_search_per_request", 0.0)
        # Longest key first so "claude-opus-5-5" wins over "claude-opus-5".
        self._keys = sorted(self.models, key=len, reverse=True)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "PriceTable":
        with open(path or DEFAULT_PRICES, encoding="utf-8") as f:
            return cls(json.load(f))

    def lookup(self, model: str) -> dict | None:
        m = normalize_model(model)
        for key in self._keys:
            if m == key or m.startswith(key + "-") or m.startswith(key + "@"):
                return self.models[key]
        return None

    def cost(self, call: Call) -> CostBreakdown:
        out = CostBreakdown()
        rates = self.lookup(call.model)
        if rates is None:
            out.unpriced_calls = 1
            out.unpriced_models = {call.model}
            return out
        mult = rates.get("fast_multiplier", 1.0) if call.fast else 1.0
        long_tier = rates.get("long_prompt")
        if long_tier and call.prompt_tokens > long_tier["threshold_tokens"]:
            rates = long_tier
        per = 1e-6 * mult
        out.input = call.input * rates["input"] * per
        out.cache_write = (call.cache_write_5m * rates["cache_write_5m"]
                           + call.cache_write_1h * rates["cache_write_1h"]) * per
        out.cache_read = call.cache_read * rates["cache_read"] * per
        out.output = call.output * rates["output"] * per
        out.server_tools = call.web_search * self.web_search_price
        return out


def summarize_calls(calls: list[Call], prices: PriceTable) -> dict:
    """Aggregate tokens and cost for a list of calls (one agent run or one session)."""
    tokens = {k: 0 for k in TOKEN_FIELDS}
    tokens["web_search"] = 0
    cost = CostBreakdown()
    by_model: dict[str, dict] = {}
    for c in calls:
        cb = prices.cost(c)
        cost.add(cb)
        m = by_model.setdefault(c.model, {k: 0 for k in TOKEN_FIELDS} | {"calls": 0, "cost_usd": 0.0})
        m["calls"] += 1
        m["cost_usd"] += cb.total
        for k in TOKEN_FIELDS:
            v = getattr(c, k)
            tokens[k] += v
            m[k] += v
        tokens["web_search"] += c.web_search
    return {
        "calls": len(calls),
        "tokens": tokens,
        "cost": cost.to_dict(),
        "cache_hit_rate": cache_hit_rate(tokens),
        "by_model": by_model,
    }


def cache_hit_rate(tokens: dict) -> float | None:
    """Share of prompt tokens served from cache."""
    prompt = (tokens.get("input", 0) + tokens.get("cache_write_5m", 0)
              + tokens.get("cache_write_1h", 0) + tokens.get("cache_read", 0))
    return tokens.get("cache_read", 0) / prompt if prompt else None


def call_to_dict(c: Call) -> dict:
    return asdict(c)
