"""Check tasks before spending anything on the AI.

A task is only useful if its check fails at the starting commit (there is a bug to fix)
and passes once the real fix is in (the check can actually be satisfied). Tasks that
fail either way, or are slow or flaky, waste money and blur the result.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from . import proc, workspace
from .config import Experiment, Task


def _check_at(exp: Experiment, task: Task, commit: str, ws: Path) -> proc.ProcResult:
    workspace.create(exp.repo, commit, ws)
    env = proc.child_env()
    for cmd in task.setup:
        r = proc.run_shell(cmd, cwd=str(ws), env=env, timeout_s=900)
        if r.exit_code != 0:
            return r
    return proc.run_shell(task.check, cwd=str(ws), env=env, timeout_s=task.check_timeout_s)


def validate(exp: Experiment, log=print) -> list[dict]:
    root = Path(tempfile.gettempdir()) / "callbs-validate"
    rows = []
    for task in exp.tasks:
        ws = root / "repo"
        row = {"task": task.id, "ok": False}
        try:
            start = workspace.resolve_commit(exp.repo, task.commit)
            before = _check_at(exp, task, start, ws)
            row["before"] = "fails (good)" if before.exit_code != 0 else "PASSES (bad: nothing to fix)"
            row["seconds"] = round(before.duration_s, 1)
            ok = before.exit_code != 0 and not before.timed_out
            if task.fix_commit:
                fix = workspace.resolve_commit(exp.repo, task.fix_commit)
                after = _check_at(exp, task, fix, ws)
                row["after"] = "passes (good)" if after.exit_code == 0 else "FAILS (bad: check can't be met)"
                row["seconds"] = max(row["seconds"], round(after.duration_s, 1))
                ok = ok and after.exit_code == 0
            else:
                row["after"] = "not checked (add fix_commit)"
            if row["seconds"] > 120:
                row["note"] = "slow check: costs time on every run"
            row["ok"] = ok
        except Exception as e:  # noqa: BLE001
            row["error"] = str(e)
        finally:
            workspace.remove(ws)
        rows.append(row)
        status = "OK  " if row["ok"] else "DROP"
        log(f"{status} {task.id:<28} before: {row.get('before', '-'):<30} after: {row.get('after', '-'):<32} "
            f"{row.get('seconds', '-')}s {row.get('note', '') or row.get('error', '')}")
    good = sum(r["ok"] for r in rows)
    log(f"\n{good} of {len(rows)} tasks are usable. Remove the DROP ones before spending anything.")
    return rows
