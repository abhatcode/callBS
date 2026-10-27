# Research brief: a measurement layer for agent cost optimizations

Date: 2026-10-27

## The claim we are testing

Context compressors, output filters, retrieval layers and gateways promise
"60–95% token savings" for coding agents (Claude Code, Codex, Cursor, etc.).
Teams with five-figure monthly agent bills have no neutral way to check whether
any of them lowers **dollars per successfully completed task** on *their* repo.

## What the evidence says (as of Oct 2026)

| Source | Finding | Why it matters for us |
|---|---|---|
| "Token Reduction Is Not Cost Reduction" (arXiv 2607.12161) | Strongest compression cut tool-output tokens 38.4% but **raised billed cost 6.8%**. Token cut vs cost cut correlated at r = 0.15. Cache creation + cache reads dominate input-side cost. Aggressive compression lowered patch success on SWE-bench Go. | Token counts are the wrong metric. Must price with cache tiers and count successes. |
| JetBrains A/B of rtk on Claude Code (Jul 2026) | Vendor claims 60–90% savings; A/B with 80 clean pairs found **+7.6% median cost** at low effort, no change at high effort, quality unchanged. | Paired replay on real tasks overturns vendor numbers. This is exactly the product. |
| Cost attribution of compression gateways (arXiv 2609.22114) | Tool-schema filtering saves 21K–57K tokens/turn (linear in turns) and is the only reliably positive lever. Content compression is ~2%/turn but compounds ~quadratically because compressed reads are re-sent. | Effects depend on session length → must replay *full* multi-turn tasks, not single requests. Report per-lever attribution later. |
| Hystax: Kompressor vs Headroom on Terminal-Bench 2.1 | Paired per-task means over 87 tasks, provider-billed cost incl. cache discount: Kompressor −22.7%, Headroom −8.2%, solve rate within 1 pt. | Vendor-run, but the method (paired, billed cost, solve rate) is the right shape. Shows real money on the table *sometimes*. |
| AWS "Tokenomics for coding agents" + aws-samples harness | Build tasks from your own closed issues, pinned to a pre-fix commit; hold harness fixed; change one lever at a time; re-run on a schedule. | Validates "replay your own tasks" as practitioner best practice. Their tool compares models/harnesses, not optimizers. |
| HarnessTax, Artificial Analysis Coding Agent Index, Databricks | Same model costs 2–5x more per task across harnesses; cheaper per-token models can cost more per task. | Per-task cost is the unit buyers already understand. |
| Observability (Langfuse, Helicone, Braintrust) | Track spend and run evals, but none ships a controlled A/B replay of a coding agent with/without an optimizer. | Adjacent, not competing. We can export to them later. |

## Gaps in the market

1. **No vendor-neutral A/B harness.** Existing benchmarks are run by compressor vendors
   (Hystax/OptScale, Token Optimizer MCP, sqz) or compare models/harnesses, not optimizers.
2. **Public benchmarks ≠ your repo.** SWE-bench/Terminal-Bench resolve rates don't predict
   cost on a monorepo with a 40-tool MCP setup.
3. **Cache economics are ignored.** Most "savings" price removed tokens at the uncached
   input rate. On Opus 5.5 / Sonnet 5.5 a cache read costs 0.05x input, so removing
   cached tokens saves almost nothing, while breaking the cache prefix costs 1.25–2x.

## Design rules taken from the research

1. **Metric = cost per successful task**, priced per API call with the provider's own tiers
   (uncached input, 5m write, 1h write, cache read, output, server tools, long-context tier).
2. **Paired design.** Same task, same commit, same model, arms interleaved in random order
   per block so time-of-day drift and cache warmth don't favor one arm.
3. **Objective success check** (test command) run *after* restoring protected paths, so an
   agent can't pass by editing tests.
4. **Report uncertainty.** Bootstrap CIs over tasks, Wilcoxon signed-rank on per-task cost,
   explicit "inconclusive — need ~N tasks" instead of a point estimate.
5. **Quality guard.** A cheaper arm that solves fewer tasks is flagged, not celebrated.
6. **Count hidden costs.** Sidecar/proxy arms can declare per-run overhead (licence, their
   own LLM calls). Cross-check our computed cost against the agent's self-reported cost.
7. **Free first step:** profile existing Claude Code transcripts to show where the bill goes
   (e.g. "62% of your spend is cache reads — an output compressor can touch at most X%").

## Pricing used (Anthropic first-party, $/MTok, fetched 2026-10-27)

See `callbs/data/anthropic_prices.json`. Notable: Opus 5.5 and Sonnet 5.5 cache reads are
0.05x input; Fable 5.1 cache reads are 0.025x; Haiku 5.5 switches to a 5x tier when a
request's prompt exceeds 100K tokens. Web search is $10 per 1,000 searches.

## Sources

- https://arxiv.org/abs/2607.12161
- https://blog.jetbrains.com/ai/2026/07/rtk-claude-code-token-savings/
- https://arxiv.org/abs/2609.22114
- https://hystax.com/context-compression-coding-agents/
- https://aws.amazon.com/blogs/aws-cloud-financial-management/tokenomics-for-coding-agents-benchmark-cost-and-quality-on-your-own-code/
- https://venturebeat.com/orchestration/a-coding-agents-harness-can-triple-its-cost-without-making-it-more-accurate-study-finds
- https://www.databricks.com/blog/benchmarking-coding-agents-databricks-multi-million-line-codebase
- https://github.com/ooples/token-optimizer-mcp
- https://pinggy.io/blog/tools_to_reduce_ai_coding_agent_token_usage/
- https://platform.claude.com/docs/en/about-claude/pricing
