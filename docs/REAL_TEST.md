# Running a real optimizer test

Goal: one result you could show a buyer. "On <repo>, <optimizer> changed cost per
successful task by X% (95% CI …) with no loss in success rate."

Budget: about $50–250 of API spend for 20 tasks × 3 arms × 3 trials on Sonnet.

---

## 1. Prepare the machine (10 min)

1. Update Claude Code so its built-in prices and model list are current:
   ```powershell
   claude update
   claude --version     # write this down; it goes in the report
   ```
2. Create an API key for experiments only: https://console.anthropic.com, then
   *Settings → Workspaces → New workspace "callbs"*. Set a **spend limit** on that
   workspace (e.g. $150), then create a key inside it.
3. Use the key **only in the terminal that runs callbs**, so your normal Claude Code
   stays on your Pro plan:
   ```powershell
   $env:ANTHROPIC_API_KEY = "sk-ant-..."   # this terminal only; closes with it
   ```
4. Install callbs:
   ```powershell
   git clone https://github.com/abhatcode/callBS
   cd callBS
   python -m pip install -e .
   callbs demo            # sanity check, no spend
   ```

## 2. Pick the target repo (15 min)

You need a repo whose tests run on your machine in under a minute or two. Pick one:

- **A public Python library with good tests** (recommended for a publishable result),
  e.g. `more-itertools`, `attrs`, `python-dateutil`, `click`. Clone it next to callBS
  and confirm `python -m pytest -q` passes.
- **One of your repos**, if it has tests. Run `callbs mine --repo ..\stock-ranker -o tasks.toml`
  to draft tasks from your past sessions, then write a check for each.

## 3. Build ~20 tasks from real bug fixes (1–2 h; this is the important part)

For each merged bug-fix PR or commit that added or changed a test:

| Field | What to put |
|---|---|
| `commit` | the commit **before** the fix: `<fix_sha>^` |
| `prompt` | the issue text as a user would write it. Don't hint at the fix. |
| `check` | bring in the fix's tests, then run only them (see below) |
| `restore_paths` | `["tests/"]` so the agent can't pass by editing tests |

The tests that prove the fix don't exist yet at the starting commit, so the check
pulls them from the fix commit after the agent is done (hidden tests, SWE-bench style):

```toml
[[tasks]]
id = "dateutil-1234"
commit = "a1b2c3d^"
prompt = "parse('2026-03-29T02:30', tzinfos=...) raises for Europe/Berlin during the DST gap. It should return the shifted time."
check = "git checkout a1b2c3d -- tests/test_parser.py && python -m pytest tests/test_parser.py -q -k dst"
restore_paths = ["tests/"]
setup = ["python -m pip install -q -e ."]
```

Find candidates fast:
```powershell
git log --oneline --grep="fix" -- tests/ | Select-Object -First 60
```
Before you keep a task, check that it is valid by hand. The check must **fail** at
`<fix_sha>^` and **pass** at `<fix_sha>`. Drop tasks that are flaky or take more than 2 minutes.

## 4. Choose the arms

Start with three. Each answers a different question:

```toml
[agent]
type = "claude-code"
model = "claude-sonnet-5-5"
effort = "medium"
# rtk compresses shell output, so every arm must be allowed to use the shell:
allowed_tools = ["Read", "Edit", "Write", "Glob", "Grep", "Bash"]

[[arms]]
name = "baseline"

[[arms]]
name = "no-mcp"            # tool-schema filtering: the lever research says works
mcp_config = "arms/empty.mcp.json"   # contents: {"mcpServers": {}}

[[arms]]
name = "rtk"               # vendor claims 60–90% savings; JetBrains measured +7.6%
claude_settings = "arms/rtk.settings.json"
```

**rtk setup:** install it, then run its init command **in a scratch folder** and copy
the `hooks` block it writes into `arms/rtk.settings.json`. Don't run its global init.
That changes `~/.claude/settings.json`, and the hook would then run in the
baseline too, which spoils the comparison. Afterwards, check `~/.claude/settings.json` has no
rtk hook.

**A proxy optimizer (Headroom, Kompressor, …)** is set up as a sidecar:
```toml
[[arms]]
name = "headroom"
sidecar = { command = "headroom proxy --port {port}" }   # use the tool's real command
env = { ANTHROPIC_BASE_URL = "http://127.0.0.1:{port}" }
overhead_usd_per_run = 0.0   # add its licence cost or its own LLM calls if any
```

## 5. Experiment settings

```toml
[experiment]
name = "rtk-vs-baseline-<repo>"
repo = "../<repo>"
baseline = "baseline"
trials = 3
parallel = 2           # 2–4 with an API key; more risks rate limits
budget_usd = 150       # callbs stops scheduling here
timeout_s = 1200
monthly_spend_usd = 25000   # a typical buyer's bill, for the $/month line
tasks_file = "tasks.toml"
```

## 6. Run it

```powershell
callbs run real.toml --dry-run          # schedule + exact agent command, $0
callbs run real.toml --limit 3          # smoke test, about $1–3
```
Open `runs/<name>/runs/*/` and check one run of each arm:
- `check_output.txt` shows the hidden tests ran (not "file not found")
- `patch.diff` is a real change
- `agent_stdout.jsonl`: for the rtk arm, its hook actually fired

Then run the rest. It resumes where it left off, and Ctrl-C is safe:
```powershell
callbs run real.toml --open
```
If you hit rate limits or login errors, re-run with `--retry-errors`.

## 7. Read the result

- **SAVES MONEY / COSTS MORE**: the cost-per-success confidence interval is entirely on one side of 0.
- **INCONCLUSIVE**: the report says roughly how many tasks would settle it. Add tasks, not trials.
- **HURTS QUALITY**: cheaper, but it solves fewer tasks. This is the most important finding to show buyers.
- Check the **Caveats** section. If "agent's own cost estimate differs" appears on every run, Claude Code's
  price table is out of date; callbs's own numbers come from the official price page.

Keep `results.jsonl`, `report.html` and the `claude --version` output together. That's your evidence.
