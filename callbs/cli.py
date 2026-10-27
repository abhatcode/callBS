"""callbs: does your agent cost optimizer actually save money on your repo?"""
from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path

from . import __version__, config, report, stats, transcripts
from .pricing import PriceTable, normalize_model
from .runner import Runner, load_results, schedule

TEMPLATE = '''# callbs experiment. Docs: README.md
[experiment]
name = "my-optimizer-trial"
repo = "../path/to/your/repo"     # local git repo; each run gets a fresh clone at the task's commit
baseline = "baseline"
trials = 3                        # runs per task per arm (agents are noisy: 3+ recommended)
parallel = 1
budget_usd = 50                   # stop scheduling new runs past this total
max_run_usd = 5                   # per-run cap passed to the agent
timeout_s = 1800
quality_margin = 0.05             # a 5-point drop in success rate counts as "hurts quality"
# monthly_spend_usd = 25000       # turns the result into $/month
# tasks_file = "tasks.toml"       # or `callbs mine` to draft tasks from your history

[agent]
type = "claude-code"
model = "claude-sonnet-5-5"
# effort = "high"
# extra_args = []

[[arms]]
name = "baseline"

# A compression proxy that sits between the agent and the API:
# [[arms]]
# name = "proxy-compressor"
# sidecar = { command = "my-compressor serve --port {port}" }
# env = { ANTHROPIC_BASE_URL = "http://127.0.0.1:{port}" }
# overhead_usd_per_run = 0.02     # licence or the compressor's own LLM calls

# A hook-based output filter (Claude Code settings file with hooks):
# [[arms]]
# name = "output-filter"
# claude_settings = "arms/filter.settings.json"

# A retrieval / MCP setup:
# [[arms]]
# name = "code-search-mcp"
# mcp_config = "arms/search.mcp.json"

[[tasks]]
id = "example"
commit = "HEAD"
prompt = "Fix the failing date parsing in utils/dates.py so tests/test_dates.py passes."
check = "python -m pytest tests/test_dates.py -q"
restore_paths = ["tests/"]        # reset after the agent so it can't pass by editing tests
# setup = ["pip install -e ."]
'''


def _prices(path=None) -> PriceTable:
    return PriceTable.load(path)


def cmd_init(args):
    dest = Path(args.path)
    if dest.exists() and not args.force:
        sys.exit(f"{dest} exists (use --force to overwrite)")
    dest.write_text(TEMPLATE, encoding="utf-8")
    print(f"wrote {dest}. Edit repo, arms and tasks, then: callbs run {dest} --dry-run")


def cmd_run(args):
    exp = config.load(args.config)
    if args.dry_run:
        order = schedule(exp)
        print(f"{exp.name}: {len(exp.tasks)} tasks x {len(exp.arms)} arms x {exp.trials} trials = {len(order)} runs")
        print(f"repo {exp.repo} · agent {exp.agent.get('type')} · results {exp.results_dir}")
        if exp.agent.get("type", "claude-code") == "claude-code":
            from .agents import RunContext, make_agent
            ag = make_agent(exp.agent)
            t, a, n = order[0]
            ctx = RunContext(t, a, Path("<workspace>"), Path("<run_dir>"), {}, exp.timeout_s,
                             exp.max_run_usd, n, exp.seed)
            print("first command:", " ".join(ag.command(ctx)))
        for t, a, n in order[: args.show]:
            print(f"  {t.id:<24} {a.name:<20} trial {n}")
        return
    runner = Runner(exp, _prices(exp.pricing), retry_errors=args.retry_errors)
    runner.run(limit=args.limit)
    _write_report(exp, open_browser=args.open)


def _write_report(exp, monthly=None, open_browser=False):
    records = load_results(exp.results_dir / "results.jsonl")
    if not records:
        print("no results yet")
        return
    arms = [a.name for a in exp.arms]
    a = stats.analyze(records, arms, exp.baseline, exp.quality_margin,
                      monthly or exp.monthly_spend_usd, exp.seed)
    info = {"name": exp.name, "agent_type": exp.agent.get("type", "claude-code")}
    md = report.markdown(a, info)
    (exp.results_dir / "report.md").write_text(md, encoding="utf-8")
    page = exp.results_dir / "report.html"
    page.write_text(report.html_page(a, info), encoding="utf-8")
    (exp.results_dir / "summary.json").write_text(json.dumps(report.clean(a), indent=2, default=str),
                                                  encoding="utf-8")
    print()
    print(md)
    print(f"report: {page}")
    if open_browser:
        webbrowser.open(page.as_uri())


def cmd_report(args):
    exp = config.load(args.config)
    _write_report(exp, monthly=args.monthly_spend, open_browser=args.open)


def cmd_profile(args):
    p = transcripts.profile(_prices(args.pricing), days=args.days or None, project_filter=args.project)
    if args.json:
        print(json.dumps(p, indent=2, default=str))
    else:
        print(transcripts.format_profile(p))


def cmd_mine(args):
    rows = transcripts.mine(Path(args.repo), limit=args.limit)
    if not rows:
        sys.exit(f"no Claude Code sessions found with cwd {Path(args.repo).resolve()}")
    text = transcripts.tasks_toml(rows, args.check)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {len(rows)} draft tasks to {args.out}")
    else:
        print(text)


def cmd_demo(args):
    from . import demo
    cfg = demo.build(Path(args.out), n_tasks=args.tasks, trials=args.trials)
    print(f"demo experiment at {cfg}")
    exp = config.load(cfg)
    Runner(exp, _prices()).run()
    _write_report(exp, open_browser=args.open)


def cmd_prices(args):
    pt = _prices(args.pricing)
    if args.model:
        print(normalize_model(args.model), "->", pt.lookup(args.model))
        return
    print(f"{'model':<20} {'input':>7} {'5m write':>9} {'1h write':>9} {'cache rd':>9} {'output':>7}  ($/MTok)")
    for m, r in pt.models.items():
        print(f"{m:<20} {r['input']:>7} {r['cache_write_5m']:>9} {r['cache_write_1h']:>9} "
              f"{r['cache_read']:>9} {r['output']:>7}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="callbs", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="write an experiment template")
    s.add_argument("path", nargs="?", default="experiment.toml")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("run", help="run (or resume) an experiment, then report")
    s.add_argument("config")
    s.add_argument("--dry-run", action="store_true", help="show the schedule and agent command; spend nothing")
    s.add_argument("--show", type=int, default=12, help="rows of the schedule to print with --dry-run")
    s.add_argument("--limit", type=int, help="only do the next N runs")
    s.add_argument("--retry-errors", action="store_true", help="re-run runs that ended in setup/agent errors")
    s.add_argument("--open", action="store_true", help="open the HTML report")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("report", help="analyze recorded results")
    s.add_argument("config")
    s.add_argument("--monthly-spend", type=float, help="your monthly agent spend, for $/month estimates")
    s.add_argument("--open", action="store_true")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("profile", help="where your Claude Code spend goes (from local transcripts)")
    s.add_argument("--days", type=int, default=30, help="0 = all time")
    s.add_argument("--project", help="substring filter on project folder name")
    s.add_argument("--pricing")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_profile)

    s = sub.add_parser("mine", help="draft replay tasks from past Claude Code sessions in a repo")
    s.add_argument("--repo", required=True)
    s.add_argument("--check", default="python -m pytest -q", help="placeholder check command")
    s.add_argument("--limit", type=int, default=30)
    s.add_argument("-o", "--out")
    s.set_defaults(fn=cmd_mine)

    s = sub.add_parser("demo", help="run a simulated experiment end to end (no API calls)")
    s.add_argument("--out", default="callbs-demo")
    s.add_argument("--tasks", type=int, default=16)
    s.add_argument("--trials", type=int, default=3)
    s.add_argument("--open", action="store_true")
    s.set_defaults(fn=cmd_demo)

    s = sub.add_parser("prices", help="show the price table, or look up one model")
    s.add_argument("model", nargs="?")
    s.add_argument("--pricing")
    s.set_defaults(fn=cmd_prices)

    args = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252
        except (AttributeError, ValueError):
            pass
    try:
        args.fn(args)
    except config.ConfigError as e:
        sys.exit(f"config error: {e}")


if __name__ == "__main__":
    main()
