"""Read Claude Code's local transcripts (~/.claude/projects/**/*.jsonl).

Two uses:
  profile - where an existing bill goes (input / cache writes / cache reads / output),
            which bounds what any optimizer can save. Free: no API calls.
  mine    - turn real past prompts into draft replay tasks pinned to the commit the
            repo was at when the session started.
"""
from __future__ import annotations

import json
import os
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .pricing import Call, PriceTable, summarize_calls


def projects_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(base) / "projects" if base else Path.home() / ".claude" / "projects"


def iter_events(path: Path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.startswith("{"):
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def _ts(ev) -> datetime | None:
    t = ev.get("timestamp")
    if not t:
        return None
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return None


def collect_calls(root: Path, since: datetime | None = None, project_filter: str | None = None):
    """Yield (project, session, timestamp, Call), one per unique API response.

    The same response can appear in several files (resumed or forked sessions), so
    responses are de-duplicated by request id / message id across all files.
    """
    seen: set[str] = set()
    for proj in sorted(p for p in root.iterdir() if p.is_dir()):
        if project_filter and project_filter.lower() not in proj.name.lower():
            continue
        for f in proj.rglob("*.jsonl"):
            for ev in iter_events(f):
                if ev.get("type") != "assistant":
                    continue
                msg = ev.get("message") or {}
                usage = msg.get("usage")
                if not usage or msg.get("model") == "<synthetic>":
                    continue
                ts = _ts(ev)
                if since and ts and ts < since:
                    continue
                rid = ev.get("requestId") or msg.get("id")
                if rid:
                    if rid in seen:
                        continue
                    seen.add(rid)
                yield proj.name, ev.get("sessionId"), ts, Call.from_anthropic_usage(msg.get("model", ""), usage)


def profile(prices: PriceTable, days: int | None = 30, project_filter: str | None = None,
            root: Path | None = None) -> dict:
    root = root or projects_dir()
    since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    per_proj: dict[str, list[Call]] = defaultdict(list)
    sessions: dict[str, set] = defaultdict(set)
    first, last = None, None
    for proj, sess, ts, call in collect_calls(root, since, project_filter):
        per_proj[proj].append(call)
        sessions[proj].add(sess)
        if ts:
            first = ts if first is None or ts < first else first
            last = ts if last is None or ts > last else last
    all_calls = [c for cs in per_proj.values() for c in cs]
    out = {"root": str(root), "days": days, "first": first and first.isoformat(),
           "last": last and last.isoformat(), "total": summarize_calls(all_calls, prices), "projects": []}
    for proj, cs in per_proj.items():
        s = summarize_calls(cs, prices)
        s["project"] = proj
        s["sessions"] = len(sessions[proj])
        out["projects"].append(s)
    out["projects"].sort(key=lambda s: s["cost"]["total"], reverse=True)
    return out


def format_profile(p: dict) -> str:
    t = p["total"]
    cost = t["cost"]
    tot = cost["total"] or 1
    L = [f"Claude Code usage from {p['root']}",
         f"window: last {p['days']} days" if p["days"] else "window: all time",
         f"{t['calls']:,} API calls · cost at list price ${cost['total']:,.2f} · "
         f"cache hit rate {_pct(t['cache_hit_rate'])}", "",
         "Where the money goes:"]
    for k, label in (("input", "uncached input"), ("cache_write", "cache writes"),
                     ("cache_read", "cache reads"), ("output", "output"), ("server_tools", "server tools")):
        L.append(f"  {label:<15} ${cost[k]:>10,.2f}  {cost[k] / tot:6.1%}")
    L += ["", "What that bounds:",
          "  A tool-output compressor mostly shrinks cache writes (new content) and the cache reads",
          "  of later turns. It cannot touch output tokens, and if it rewrites earlier context it",
          "  turns cheap cache reads back into cache writes. Only an A/B replay tells the net effect.",
          ""]
    if p["projects"]:
        L.append(f"{'project':<48} {'sessions':>8} {'calls':>8} {'cost':>11} {'cache hit':>9}")
        for s in p["projects"][:20]:
            L.append(f"{s['project'][:48]:<48} {s['sessions']:>8} {s['calls']:>8,} "
                     f"${s['cost']['total']:>10,.2f} {_pct(s['cache_hit_rate']):>9}")
    if cost["unpriced_models"]:
        L.append(f"\nunpriced models (counted as $0): {', '.join(cost['unpriced_models'])}")
    L.append("\nNote: this is API list price. Subscription plans bill differently; the relative split still holds.")
    return "\n".join(L)


def _pct(x):
    return "—" if x is None else f"{x:.1%}"


def _user_text(msg) -> str | None:
    content = msg.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        text = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    else:
        return None
    text = text.strip()
    if not text or text.startswith("<") or text.startswith("Caveat:"):
        return None  # slash-command wrappers, system reminders, hook output
    return text


def mine(repo: Path, root: Path | None = None, limit: int = 30, min_chars: int = 40) -> list[dict]:
    """Draft tasks from the first real prompt of each past session run inside `repo`."""
    root = root or projects_dir()
    repo = repo.resolve()
    want = str(repo).lower().rstrip("\\/")
    found = []
    for proj in root.iterdir():
        if not proj.is_dir():
            continue
        for f in proj.glob("*.jsonl"):
            for ev in iter_events(f):
                if ev.get("type") != "user" or ev.get("isMeta") or ev.get("isSidechain"):
                    continue
                cwd = (ev.get("cwd") or "").lower().rstrip("\\/")
                if cwd != want:
                    break  # sessions live in one cwd; skip the file
                text = _user_text(ev.get("message") or {})
                if text and len(text) >= min_chars:
                    found.append({"session": ev.get("sessionId"), "timestamp": ev.get("timestamp"),
                                  "branch": ev.get("gitBranch"), "prompt": text})
                    break
    found.sort(key=lambda r: r["timestamp"] or "", reverse=True)
    for r in found[:limit]:
        r["commit"] = _commit_at(repo, r["timestamp"])
    return found[:limit]


def _commit_at(repo: Path, iso_ts: str | None) -> str | None:
    if not iso_ts:
        return None
    r = subprocess.run(["git", "rev-list", "-1", f"--before={iso_ts}", "--all"], cwd=repo,
                       capture_output=True, text=True)
    return r.stdout.strip() or None


def tasks_toml(rows: list[dict], check: str) -> str:
    L = ["# Draft tasks mined from your Claude Code history.",
         "# Review each one: keep tasks with a clear, checkable outcome, and write a `check`",
         "# command that passes only when the task is done (e.g. a specific test).", ""]
    for i, r in enumerate(rows, 1):
        L += ["[[tasks]]", f'id = "mined-{i:02d}"',
              f'commit = "{r["commit"] or "HEAD"}"  # repo state at {r["timestamp"]}',
              f"prompt = {_toml_multiline(r['prompt'])}", f"check = {json.dumps(check)}", ""]
    return "\n".join(L)


def _toml_multiline(s: str) -> str:
    if "'''" not in s:
        return f"'''\n{s}'''"  # literal string: backslashes (Windows paths) stay as-is
    esc = s.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    return f'"""\n{esc}"""'
