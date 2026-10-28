"""Runs the experiment: every task x arm x trial, paired and interleaved."""
from __future__ import annotations

import json
import random
import tempfile
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from . import proc, workspace
from .agents import RunContext, make_agent
from .config import Arm, Experiment, Task
from .pricing import PriceTable, call_to_dict, summarize_calls

# Infrastructure failures: excluded from comparisons because they say nothing about the arm.
EXCLUDED_OUTCOMES = {"setup_error"}


def run_key(task_id: str, arm: str, trial: int) -> str:
    return f"{task_id}|{arm}|{trial}"


def schedule(exp: Experiment) -> list[tuple[Task, Arm, int]]:
    """Trial-major order with arms shuffled inside each (task, trial) block.

    Running all arms of a task back to back, in random order, keeps time-of-day drift,
    provider load and warm prompt caches from systematically favouring one arm.
    """
    rng = random.Random(exp.seed)
    order = []
    for trial in range(exp.trials):
        for task in exp.tasks:
            arms = list(exp.arms)
            rng.shuffle(arms)
            order += [(task, arm, trial) for arm in arms]
    return order


def load_results(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    # A re-run of the same key replaces the earlier record.
    latest = {r["key"]: r for r in rows}
    return list(latest.values())


def _fmt(value, port):
    return value.replace("{port}", str(port)) if isinstance(value, str) else value


class Runner:
    def __init__(self, exp: Experiment, prices: PriceTable, log=print, retry_errors: bool = False):
        self.exp = exp
        self.prices = prices
        self.log = log
        self.retry_errors = retry_errors
        self.agent = make_agent(exp.agent)
        self.results_path = exp.results_dir / "results.jsonl"
        self.ws_root = Path(tempfile.gettempdir()) / "callbs-ws" / exp.results_dir.name
        self._lock = threading.Lock()
        # Workspaces live at a fixed set of paths, one per worker. Agents put the working
        # directory in their system prompt, so a unique path per run would make every run
        # miss the prompt cache, which is not how developers working in one checkout behave.
        self._slots: queue.SimpleQueue[int] = queue.SimpleQueue()
        for i in range(max(1, exp.parallel)):
            self._slots.put(i)
        self._commits: dict[str, str] = {}
        self.spent = 0.0
        self.abort_reason: str | None = None
        self.early_stopped: str | None = None
        self.retired: dict[str, str] = {}  # arm -> verdict, no longer scheduled

    def run(self, limit: int | None = None) -> list[dict]:
        exp = self.exp
        exp.results_dir.mkdir(parents=True, exist_ok=True)
        existing = load_results(self.results_path)
        skip = {r["key"] for r in existing
                if not (self.retry_errors and r["outcome"] in EXCLUDED_OUTCOMES | {"agent_error", "timeout"})}
        self.spent = sum(r["cost_usd"] for r in existing)
        for t in exp.tasks:
            self._commits[t.id] = workspace.resolve_commit(exp.repo, t.commit)

        todo = [(t, a, n) for t, a, n in schedule(exp) if run_key(t.id, a.name, n) not in skip]
        if limit is not None:
            todo = todo[:limit]
        self.log(f"{len(todo)} runs to do, {len(skip)} already recorded, ${self.spent:.2f} spent so far")

        new = []
        # Run one trial (a full pass over every task and arm) at a time, so early stopping
        # can look at complete passes and never at a half-finished one.
        for trial in sorted({n for _, _, n in todo}):
            if self.abort_reason or self._over_budget():
                break
            if exp.early_stop and trial > 0:
                for arm, verdict in self.decided_arms().items():
                    if arm not in self.retired:
                        self.retired[arm] = verdict
                        self.log(f"\nEARLY STOP for `{arm}` before trial {trial + 1}: {verdict} "
                                 f"at {exp.early_stop_ci:.0%} confidence; no more runs for it")
                candidates = {a.name for a in exp.arms} - {exp.baseline}
                if candidates <= set(self.retired):
                    self.early_stopped = "; ".join(f"{a}: {v}" for a, v in self.retired.items())
                    self.log("every candidate has a clear answer; experiment finished early")
                    break
            batch = [x for x in todo if x[2] == trial and x[1].name not in self.retired]
            new += self._run_batch(batch)
        if self.abort_reason:
            self.log(f"\nSTOPPED: the agent environment is broken, not the arm: {self.abort_reason}\n"
                     "Fix it, then re-run with --retry-errors.")
        return new

    def _over_budget(self) -> bool:
        return self.exp.budget_usd is not None and self.spent >= self.exp.budget_usd

    def decided_arms(self) -> dict[str, str]:
        """Candidate arms whose answer is already clear, at a stricter confidence level.

        Checking for a result after every pass and stopping as soon as one appears makes
        flukes more likely, so this uses 99% intervals instead of the report's 95%.
        """
        from . import stats  # local import: stats imports this module
        exp = self.exp
        records = load_results(self.results_path)
        decided = {}
        for arm in exp.arms:
            if arm.name == exp.baseline:
                continue
            c = stats.compare(records, exp.baseline, arm.name, exp.quality_margin,
                              seed=exp.seed, ci_level=exp.early_stop_ci)
            enough = c["paired_tasks"] >= min(len(exp.tasks), exp.early_stop_min_tasks)
            if enough and c["verdict"] in stats.DECISIVE:
                decided[arm.name] = c["verdict"]
        return decided

    def _run_batch(self, todo: list) -> list[dict]:
        exp = self.exp
        new = []
        with ThreadPoolExecutor(max_workers=max(1, exp.parallel)) as pool:
            pending = {}
            it = iter(todo)

            def submit_next() -> bool:
                if self.abort_reason:
                    return False
                if exp.budget_usd is not None and self.spent >= exp.budget_usd:
                    return False
                nxt = next(it, None)
                if nxt is None:
                    return False
                pending[pool.submit(self.run_one, *nxt)] = nxt
                return True

            for _ in range(max(1, exp.parallel)):
                submit_next()
            while pending:
                fut = next(as_completed(pending))
                pending.pop(fut)
                rec = fut.result()
                new.append(rec)
                with self._lock:
                    self.spent += rec["cost_usd"]
                    with open(self.results_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(rec) + "\n")
                self.log(f"  {rec['task']:<24} {rec['arm']:<16} t{rec['trial']}  "
                         f"{rec['outcome']:<15} ${rec['cost_usd']:.4f}  (total ${self.spent:.2f})")
                if not submit_next() and self._over_budget():
                    self.log(f"budget of ${exp.budget_usd} reached; stopping after in-flight runs")
        return new

    def run_one(self, task: Task, arm: Arm, trial: int) -> dict:
        exp = self.exp
        key = run_key(task.id, arm.name, trial)
        slug = f"{task.id}__{arm.name}__t{trial}"
        run_dir = exp.results_dir / "runs" / slug
        run_dir.mkdir(parents=True, exist_ok=True)
        slot = self._slots.get()
        ws = self.ws_root / f"repo{slot}"
        commit = self._commits[task.id]
        rec = {"key": key, "task": task.id, "arm": arm.name, "trial": trial, "commit": commit,
               "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "outcome": None, "success": False, "error": None, "cost_usd": 0.0}
        t0 = time.monotonic()
        sidecar = None
        res = None
        try:
            port = proc.free_port()
            env = proc.child_env({k: _fmt(v, port) for k, v in arm.env.items()})
            try:
                workspace.create(exp.repo, commit, ws)
                for cmd in task.setup + arm.setup:
                    r = proc.run_shell(_fmt(cmd, port), cwd=str(ws), env=env, timeout_s=900)
                    if r.exit_code != 0:
                        raise RuntimeError(f"setup `{cmd}` exited {r.exit_code}: {r.stderr.strip()[-400:]}")
                if arm.sidecar:
                    sidecar = proc.Sidecar(_fmt(arm.sidecar["command"], port), cwd=str(ws), env=env,
                                           port=port, ready_timeout_s=arm.sidecar.get("ready_timeout_s", 30))
            except Exception as e:  # noqa: BLE001 - any setup failure is recorded, not raised
                rec.update(outcome="setup_error", error=str(e))
                return rec

            ctx = RunContext(task=task, arm=arm, workspace=ws, run_dir=run_dir, env=env,
                             timeout_s=exp.timeout_s, max_run_usd=exp.max_run_usd,
                             trial=trial, seed=exp.seed)
            res = self.agent.run(ctx)
            if sidecar:
                sidecar.stop()
            (run_dir / "agent_stdout.jsonl").write_text(res.stdout, encoding="utf-8")
            if res.stderr:
                (run_dir / "agent_stderr.txt").write_text(res.stderr, encoding="utf-8")
            if res.fatal:
                self.abort_reason = res.error
                rec.update(outcome="setup_error", error=res.error,
                           cost_usd=summarize_calls(res.calls, self.prices)["cost"]["total"])
                return rec

            workspace.restore_paths(ws, commit, task.restore_paths)
            diff_stat, patch = workspace.diff(ws, commit)
            (run_dir / "patch.diff").write_text(patch, encoding="utf-8")
            chk = proc.run_shell(task.check, cwd=str(ws), env=env, timeout_s=task.check_timeout_s)
            (run_dir / "check_output.txt").write_text(chk.stdout + "\n--- stderr ---\n" + chk.stderr,
                                                      encoding="utf-8")
            passed = chk.exit_code == 0 and not chk.timed_out

            usage = summarize_calls(res.calls, self.prices)
            (run_dir / "calls.json").write_text(json.dumps([call_to_dict(c) for c in res.calls]),
                                                encoding="utf-8")
            if passed:
                outcome = "success"
            elif res.timed_out:
                outcome = "timeout"
            elif res.budget_exceeded:
                outcome = "budget_exceeded"
            elif res.is_error:
                outcome = "agent_error"
            else:
                outcome = "check_failed"
            rec.update(
                outcome=outcome, success=passed, error=res.error,
                cost_usd=round(usage["cost"]["total"] + arm.overhead_usd_per_run, 6),
                api_cost_usd=usage["cost"]["total"], overhead_usd=arm.overhead_usd_per_run,
                cost_breakdown=usage["cost"], tokens=usage["tokens"],
                cache_hit_rate=usage["cache_hit_rate"], api_calls=usage["calls"],
                by_model=usage["by_model"], num_turns=res.num_turns,
                reported_cost_usd=res.reported_cost_usd, agent_exit=res.exit_code, agent_auth=res.auth,
                agent_duration_s=round(res.duration_s, 2), check_exit=chk.exit_code,
                diff_stat=diff_stat, notes=res.notes,
            )
            return rec
        except Exception as e:  # noqa: BLE001
            rec.update(outcome="setup_error", error=f"harness error: {e!r}")
            if res is not None:  # the agent already spent money; keep the budget honest
                rec["cost_usd"] = summarize_calls(res.calls, self.prices)["cost"]["total"]
            return rec
        finally:
            if sidecar:
                sidecar.stop()
            rec["duration_s"] = round(time.monotonic() - t0, 2)
            if not exp.keep_workspaces:
                try:
                    workspace.remove(ws)
                except OSError as e:
                    rec.setdefault("notes", []).append(f"could not remove workspace: {e}")
            self._slots.put(slot)
