# Running a real test on $10

**The question:** does Headroom (a free, open-source tool that shrinks what Claude Code
sends to Anthropic) make each finished coding job cheaper or more expensive?

**Budget:** $10 of Anthropic API credit, with a hard cap so you can't go over.
Everything else is free.

| Stage | What you do | Costs | Why it matters |
|---|---|---|---|
| A | Pick a repo and write about 12 jobs | $0 | The jobs are the test. Bad jobs give a meaningless answer. |
| B | `callbs validate` | $0 | Throws out broken jobs before any AI runs, so no money is spent on them. |
| C | Set up the $10 key with a hard cap | $0 | Makes overspending impossible. |
| D | Smoke test: 2 runs | ~$0.10 | Confirms Headroom is really in the path before you pay for 70 runs. |
| E | Main test on Claude Haiku 5.5 | ≤ $5 | The cheapest model gives a clear answer from enough runs. |
| F | Check the result on Claude Sonnet 5.5 | ≤ $4 | Companies mostly use Sonnet or Opus. Checks the answer holds there. |
| G | Compare callbs to your real Anthropic bill | $0 | Proves callbs's numbers match a real invoice. That's what you're selling. |

---

## Why spend API money at all, instead of the Pro plan?

1. **Headroom needs it.** Headroom works by sitting between Claude Code and
   Anthropic. Its docs only describe that working with an API key, so it's not known
   whether your Pro login works through it.
2. **Proof the numbers are right (stage G).** callbs works out costs from Anthropic's
   price list. Only a pay-per-use API account produces a bill you can compare it
   against. "Our numbers match your invoice to the cent" is the strongest thing you
   can tell a buyer.
3. **It's how buyers pay.** Companies with big bills pay per use, like this.

Anything that doesn't need Headroom, like checking your jobs work, stays on Pro or
costs nothing at all.

---

## A. Pick a repo and write the jobs (free, 1–2 hours)

Use **more-itertools**: a popular open-source Python library with fast tests that works on Windows.

```powershell
cd C:\Users\fivem\Desktop
git clone https://github.com/more-itertools/more-itertools
cd more-itertools
python -m pip install -e .
python -m pytest -q          # should pass
```

Find past bug fixes that came with a test:
```powershell
git log --oneline -i --grep="fix" -- tests/ | Select-Object -First 40
```

For each one, write a job in `C:\Users\fivem\Desktop\callBS\examples\tasks.toml`:

```toml
[[tasks]]
id = "chunked-strict"
commit = "a1b2c3d^"          # the commit BEFORE the fix (the ^ means "parent of")
fix_commit = "a1b2c3d"       # the real fix, used only by `callbs validate`
prompt = "chunked(..., strict=True) doesn't raise when the last chunk is short. It should raise ValueError."
check = "git checkout a1b2c3d -- tests && python -m pytest tests/test_more.py -q -k chunked"
restore_paths = ["tests/"]
```

- **prompt:** describe the bug the way someone would report it. Don't give away the fix.
- **check:** brings in the fix's tests *after* the AI is done, then runs them. The AI never
  sees the answer key, so it can't cheat.
- Aim for 12 jobs. Prefer small, clear bugs: they're cheaper and the results are cleaner.

## B. Validate the jobs (free, no AI)

```powershell
cd C:\Users\fivem\Desktop\callBS
python -m pip install -e .
callbs validate examples/headroom-test.toml
```
Every job must say **OK**: the test fails before the fix and passes after it. Delete
any **DROP** job from `tasks.toml`. A broken job wastes money on every run and adds
noise to the answer.

## C. Set up the $10 (free, 10 minutes)

1. Go to **platform.claude.com** and sign in.
2. **Settings → Billing**: add $10 of credit.
3. **Settings → Workspaces → Create workspace**, name it `callbs`.
   Set its **spend limit to $10**. This is the hard cap: Anthropic stops serving
   requests at $10, whatever happens on your computer.
4. Inside the `callbs` workspace, **create an API key**. Copy it.
5. Open a new PowerShell window **just for testing** and paste the key there:
   ```powershell
   $env:ANTHROPIC_API_KEY = "sk-ant-..."
   ```
   - It lasts only until you close that window. Your normal Claude Code in other
     windows stays on your Pro plan.
   - **Never** put the key in a file, the config, or the repo.
   - Don't use `setx`. That saves it permanently, and then all your Claude Code use
     would be billed to the $10.

## D. Install Headroom and smoke test (~$0.10)

```powershell
python -m pip install "headroom-ai[all]"
headroom proxy --port 8787      # quick check it starts, then Ctrl+C
```

Run two jobs (one with Headroom, one without):
```powershell
callbs run examples/headroom-test.toml --limit 2
```
Open `examples\runs\headroom-haiku\runs\` and check, for each run:
- `check_output.txt` shows the tests ran (not "file not found")
- `patch.diff` contains a real code change
- the Headroom run's `agent_stdout.jsonl` has no connection errors

Then run `callbs report examples/headroom-test.toml`. The two runs should use different
amounts of input text. If they're identical, Headroom isn't in the path. Stop and fix that.

The report also warns if runs were billed to different accounts.
Every run must say `ANTHROPIC_API_KEY`.

## E. Main test on Haiku 5.5 (≤ $5)

```powershell
callbs run examples/headroom-test.toml --open
```
- 12 jobs × 2 setups × 3 repeats = up to 72 runs.
- `budget_usd = 5` stops it at $5.
- `early_stop = true` stops paying once the answer is clear, often after the first repeat.
- Safe to stop with Ctrl+C. Running the same command again continues where it left off.
- If something goes wrong (e.g. the key is missing), callbs stops after one run instead of burning money.

## F. Check it on Sonnet 5.5 (≤ $4)

In `examples/headroom-test.toml`:
- set `model = "claude-sonnet-5-5"`, `name = "headroom-sonnet"`, `trials = 1` and `budget_usd = 4`

Then run it again. Sonnet costs about 20x more per word than Haiku, so this is only
4–8 runs. It won't prove anything on its own. It shows whether Sonnet points the same
way as the Haiku result.

## G. Compare with your real bill (free)

1. Wait an hour, then open **platform.claude.com → Usage**, filtered to the
   `callbs` workspace.
2. Compare that total to `total spend` at the top of the two `report.md` files.
3. They should match within a few percent. Write down both numbers. That's your proof
   the measurements are right.

---

## What you'll have at the end

- A verdict for Headroom on real code: **saves money / costs more / hurts quality /
  not clear yet**, with how sure the result is.
- How much less (or more) text Headroom sent, set against how the bill changed. That
  gap is the whole story.
- callbs's numbers set against your real Anthropic bill.

If the answer is "not clear yet", the report says roughly how many more jobs would
settle it. Add jobs rather than repeats.
