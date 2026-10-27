# callbs

**Does your coding-agent cost optimizer actually save money on *your* repo?**

Context compressors, output filters, retrieval layers and gateways advertise "60–95% token
savings". Independent A/B tests keep finding the opposite: fewer tokens, *higher* bills,
because compression breaks the prompt cache, adds turns, or makes the agent fail more
(see [docs/RESEARCH.md](docs/RESEARCH.md)).

`callbs` replays real tasks from your repo with and without an optimizer and reports
what matters to the person paying the bill:

- **cost per successful task** (priced per API call, with cache write/read tiers)
- **success rate** (your own check command, run after protected files are restored)
- **cache hit rate** and where the money goes (input / cache writes / cache reads / output)
- **failure rate** by type (check failed, agent error, timeout, budget cap)
- a verdict with confidence intervals: `SAVES MONEY`, `COSTS MORE`, `HURTS QUALITY`,
  `INCONCLUSIVE` (with an estimate of how many tasks would settle it)

It does not care which optimizer wins. An arm is just a configuration: env vars, a
sidecar proxy, a Claude Code settings file (hooks), an MCP config, setup commands.

No dependencies beyond Python 3.11+ and git.

## Try it with no API spend

```bash
python -m callbs demo --open
```

Builds a throwaway repo with 16 bug-fix tasks and runs four arms through a simulated agent.
The simulation shows the usual trap: the aggressive compressor sends 27% fewer prompt
tokens but costs 41% more per solved task, because it keeps invalidating the cache.

## See where your current spend goes (free)

```bash
python -m callbs profile --days 30
```

Reads Claude Code's local transcripts and prices every API call. If 40% of your bill is
cache reads at 0.05x input price, a compressor that shrinks that context saves very little,
and one that rewrites history can turn cheap reads into expensive writes.

## Run a real experiment

```bash
python -m callbs init experiment.toml        # template with example arms
python -m callbs mine --repo ../my-repo -o tasks.toml   # draft tasks from past sessions
python -m callbs run experiment.toml --dry-run          # schedule + exact agent command, no spend
python -m callbs run experiment.toml --limit 4          # small smoke test
python -m callbs run experiment.toml                    # the rest (resumable)
python -m callbs report experiment.toml --monthly-spend 25000 --open
```

### Tasks

A task is a prompt, the commit to start from, and a command that exits 0 only when the
task is done. Good sources: closed issues or merged PRs, checked out at the commit before
the fix, with the PR's tests as the check.

```toml
[[tasks]]
id = "dates-tz"
commit = "a1b2c3d"
prompt = "Parsing '2026-03-29T02:30' in Europe/Berlin crashes. Fix utils/dates.py."
check = "python -m pytest tests/test_dates.py -q"
restore_paths = ["tests/"]   # agent edits here are undone before the check
setup = ["pip install -e ."]
```

### Arms

```toml
[[arms]]
name = "baseline"

[[arms]]                                   # a compression proxy
name = "proxy"
sidecar = { command = "my-compressor serve --port {port}" }
env = { ANTHROPIC_BASE_URL = "http://127.0.0.1:{port}" }
overhead_usd_per_run = 0.02                # licence, or the proxy's own LLM calls

[[arms]]                                   # hook-based output filter
name = "filter"
claude_settings = "arms/filter.settings.json"

[[arms]]                                   # retrieval via MCP
name = "search"
mcp_config = "arms/search.mcp.json"
```

### Agents

| `[agent] type` | What runs |
|---|---|
| `claude-code` | `claude -p --output-format stream-json` with your model/effort; usage read per API response. By default the agent can only read and edit files (`acceptEdits` + `allowed_tools = ["Read","Edit","Write","Glob","Grep"]`); widen `allowed_tools` if tasks need a shell. An auth failure (401/403) stops the experiment instead of burning through every run. |
| `command` | any CLI; usage from Claude stream-json on stdout, or a JSONL file of Anthropic `usage` objects |
| `mock` | the simulator used by `demo` and the tests |

## How the comparison is made

- Every run gets a fresh clone at the task's commit, outside your project tree.
- Runs are scheduled trial by trial; inside each (task, trial) block the arms run in random
  order, so time-of-day drift and warm caches don't favour one arm.
- Cost is computed per API call from the usage the provider returned, with the right price
  for uncached input, 5-minute and 1-hour cache writes, cache reads, output, web search,
  fast mode, and Haiku 5.5's long-prompt tier. The agent's own cost estimate is kept for a
  cross-check.
- Cost per successful task = an arm's total spend ÷ tasks it solved.
- Confidence intervals come from a paired bootstrap over tasks; the Wilcoxon signed-rank
  p-value on per-task cost is exact.
- Setup/harness failures are excluded and reported; agent failures count against the arm.
- `budget_usd` stops scheduling when the experiment has spent that much.

Outputs land in `results_dir`: `results.jsonl` (one line per run), `runs/<task>__<arm>__t<n>/`
(stream log, patch, check output, per-call usage), `report.md`, `report.html`, `summary.json`.

## Known limits (v0.1)

- Usage comes from what the agent reports. A proxy that makes its own LLM calls with its
  own key is invisible unless you add it as `overhead_usd_per_run`. A metering proxy that
  sits at the provider edge is the planned fix.
- Prices are Anthropic first-party list prices (`callbs/data/anthropic_prices.json`).
  Pass `pricing = "my_prices.json"` for Bedrock/Vertex or negotiated rates.
- Subscription plans (Pro/Max) are not billed per token; the relative comparison still holds.

## Tests

```bash
python -m unittest discover -s tests
```
