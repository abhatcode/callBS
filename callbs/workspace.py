"""Isolated, disposable checkouts of the target repo at a pinned commit."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path


def _git(args: list[str], cwd: str | Path) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def resolve_commit(repo: Path, ref: str) -> str:
    return _git(["rev-parse", "--verify", f"{ref}^{{commit}}"], repo).strip()


def create(repo: Path, commit: str, dest: Path) -> Path:
    """Clone `repo` into `dest` sharing its object store, then check out `commit` detached."""
    if dest.exists():
        remove(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _git(["clone", "--quiet", "--shared", "--no-checkout", str(repo), str(dest)], dest.parent)
    _git(["checkout", "--quiet", "--detach", commit], dest)
    # Agents commit sometimes; give them an identity so that never fails.
    _git(["config", "user.email", "agent@callbs.local"], dest)
    _git(["config", "user.name", "callbs agent"], dest)
    return dest


def restore_paths(ws: Path, commit: str, paths: list[str]) -> None:
    """Put protected paths (e.g. tests/) back the way they were at `commit`."""
    for p in paths:
        tracked = _git(["ls-tree", "-r", "--name-only", commit, "--", p], ws).strip()
        if tracked:
            _git(["checkout", commit, "--", p], ws)
        # Files the agent added under a protected path are removed too.
        _git(["clean", "-fdq", "--", p], ws)


def diff(ws: Path, commit: str) -> tuple[str, str]:
    """Return (stat, full patch) of everything the agent changed, including new files."""
    _git(["add", "-A"], ws)
    stat_out = _git(["diff", "--cached", "--stat", commit], ws)
    patch = _git(["diff", "--cached", commit], ws)
    _git(["reset", "--quiet"], ws)
    return stat_out.strip(), patch


def _on_rm_error(func, path, _exc):
    # Git marks pack files read-only, which blocks deletion on Windows.
    os.chmod(path, stat.S_IWRITE)
    func(path)


def remove(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, onexc=_on_rm_error) if _has_onexc() else shutil.rmtree(path, onerror=_on_rm_error)


def _has_onexc() -> bool:
    import sys
    return sys.version_info >= (3, 12)
