"""Paired comparison of arms: cost per successful task, success rate, cache hit rate."""
from __future__ import annotations

import math
import random
from collections import defaultdict
from statistics import median

from .runner import EXCLUDED_OUTCOMES

N_BOOT = 4000


def _per_task(records: list[dict]) -> dict[str, dict[str, dict]]:
    """task -> arm -> {cost, succ, n} summed over trials."""
    out: dict = defaultdict(lambda: defaultdict(lambda: {"cost": 0.0, "succ": 0, "n": 0}))
    for r in records:
        if r["outcome"] in EXCLUDED_OUTCOMES:
            continue
        cell = out[r["task"]][r["arm"]]
        cell["cost"] += r["cost_usd"]
        cell["succ"] += int(r["success"])
        cell["n"] += 1
    return out


def arm_summary(records: list[dict], arm: str) -> dict:
    rows = [r for r in records if r["arm"] == arm and r["outcome"] not in EXCLUDED_OUTCOMES]
    excluded = sum(1 for r in records if r["arm"] == arm and r["outcome"] in EXCLUDED_OUTCOMES)
    n = len(rows)
    succ = sum(r["success"] for r in rows)
    cost = sum(r["cost_usd"] for r in rows)
    tok = defaultdict(int)
    comp = defaultdict(float)
    for r in rows:
        for k, v in (r.get("tokens") or {}).items():
            tok[k] += v
        for k in ("input", "cache_write", "cache_read", "output", "server_tools"):
            comp[k] += (r.get("cost_breakdown") or {}).get(k, 0.0)
    comp["overhead"] = sum(r.get("overhead_usd", 0.0) for r in rows)
    prompt = tok["input"] + tok["cache_write_5m"] + tok["cache_write_1h"] + tok["cache_read"]
    outcomes = defaultdict(int)
    for r in rows:
        outcomes[r["outcome"]] += 1
    turns = [r["num_turns"] for r in rows if r.get("num_turns")]
    mismatches = [r for r in rows if r.get("reported_cost_usd") and r.get("api_cost_usd")
                  and abs(r["reported_cost_usd"] - r["api_cost_usd"]) > 0.05 * r["reported_cost_usd"]]
    unpriced = sorted({m for r in rows for m in (r.get("cost_breakdown") or {}).get("unpriced_models", [])})
    return {
        "arm": arm, "runs": n, "excluded_runs": excluded, "successes": succ,
        "success_rate": succ / n if n else None,
        "total_cost": cost,
        "cost_per_run": cost / n if n else None,
        "cost_per_success": cost / succ if succ else None,
        "cache_hit_rate": tok["cache_read"] / prompt if prompt else None,
        "prompt_tokens_per_run": prompt / n if n else None,
        "output_tokens_per_run": tok["output"] / n if n else None,
        "median_turns": median(turns) if turns else None,
        "outcomes": dict(outcomes),
        "cost_composition": dict(comp),
        "reported_cost_mismatches": len(mismatches),
        "unpriced_models": unpriced,
    }


def _cps(cells) -> float:
    cost = sum(c["cost"] for c in cells)
    succ = sum(c["succ"] for c in cells)
    return cost / succ if succ else math.inf


def _rate(cells) -> float:
    n = sum(c["n"] for c in cells)
    return sum(c["succ"] for c in cells) / n if n else math.nan


def _cpr(cells) -> float:
    n = sum(c["n"] for c in cells)
    return sum(c["cost"] for c in cells) / n if n else math.nan


def _ci(values: list[float], level: float = 0.95) -> tuple[float, float]:
    vals = sorted(v for v in values if not math.isnan(v))
    if not vals:
        return (math.nan, math.nan)
    lo_i = int((1 - level) / 2 * len(vals))
    hi_i = min(len(vals) - 1, int((1 + level) / 2 * len(vals)))
    return vals[lo_i], vals[hi_i]


def wilcoxon_signed_rank(diffs: list[float]) -> float | None:
    """Two-sided p-value. Exact distribution (handles ties via doubled ranks)."""
    d = [x for x in diffs if x != 0]
    n = len(d)
    if n == 0:
        return None
    order = sorted(range(n), key=lambda i: abs(d[i]))
    ranks2 = [0] * n  # ranks times 2, so tied (half) ranks stay integers
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs(d[order[j + 1]]) == abs(d[order[i]]):
            j += 1
        for k in range(i, j + 1):
            ranks2[order[k]] = i + j + 2  # 2 * average of (i+1 .. j+1)
        i = j + 1
    w_plus = sum(r for r, x in zip(ranks2, d) if x > 0)
    total = sum(ranks2)
    # dist[s] = number of sign assignments whose positive-rank sum (x2) equals s
    dist = [0] * (total + 1)
    dist[0] = 1
    for r in ranks2:
        for s in range(total, r - 1, -1):
            dist[s] += dist[s - r]
    count = 2 ** n
    lower = min(w_plus, total - w_plus)
    tail = sum(dist[: lower + 1]) / count
    return min(1.0, 2 * tail)


def compare(records: list[dict], baseline: str, arm: str, quality_margin: float = 0.05,
            monthly_spend: float | None = None, seed: int = 0) -> dict:
    per_task = _per_task(records)
    tasks = [t for t, arms in per_task.items() if arms[baseline]["n"] and arms[arm]["n"]]
    out = {"arm": arm, "baseline": baseline, "paired_tasks": len(tasks)}
    if not tasks:
        out["verdict"] = "NO DATA"
        return out

    A = [per_task[t][baseline] for t in tasks]
    B = [per_task[t][arm] for t in tasks]
    cps_a, cps_b = _cps(A), _cps(B)
    out.update(
        cost_per_success=(cps_a, cps_b),
        cps_change=cps_b / cps_a - 1 if math.isfinite(cps_a) and math.isfinite(cps_b) else None,
        cost_per_run_change=_cpr(B) / _cpr(A) - 1 if _cpr(A) else None,
        success_rate=(_rate(A), _rate(B)),
        success_diff=_rate(B) - _rate(A),
    )

    rng = random.Random(seed)
    boot_cps, boot_cpr, boot_sr = [], [], []
    k = len(tasks)
    for _ in range(N_BOOT):
        idx = [rng.randrange(k) for _ in range(k)]
        a = [A[i] for i in idx]
        b = [B[i] for i in idx]
        ca, cb = _cps(a), _cps(b)
        boot_cps.append(cb / ca - 1 if math.isfinite(ca) and math.isfinite(cb) else math.nan)
        boot_cpr.append(_cpr(b) / _cpr(a) - 1 if _cpr(a) else math.nan)
        boot_sr.append(_rate(b) - _rate(a))
    out["cps_change_ci"] = _ci(boot_cps)
    out["cost_per_run_change_ci"] = _ci(boot_cpr)
    out["success_diff_ci"] = _ci(boot_sr)

    diffs = [b["cost"] / b["n"] - a["cost"] / a["n"] for a, b in zip(A, B)]
    out["wilcoxon_p_cost_per_run"] = wilcoxon_signed_rank(diffs)
    out["arm_cheaper_on_tasks"] = sum(1 for d in diffs if d < 0)

    lo, hi = out["cps_change_ci"]
    s_lo, s_hi = out["success_diff_ci"]
    if math.isnan(lo):
        verdict = "INCONCLUSIVE"
        reason = "one arm had no successes in some resamples; add tasks or trials"
    elif s_hi < -quality_margin:
        verdict, reason = "HURTS QUALITY", f"solves fewer tasks (more than {quality_margin:.0%} below baseline)"
    elif hi < 0 and s_lo >= -quality_margin:
        verdict, reason = "SAVES MONEY", "cheaper per successful task, quality within margin"
    elif hi < 0:
        verdict, reason = "CHEAPER, QUALITY UNCLEAR", "cheaper per success but a quality drop can't be ruled out"
    elif lo > 0:
        verdict, reason = "COSTS MORE", "more expensive per successful task"
    else:
        verdict, reason = "INCONCLUSIVE", "the confidence interval spans zero"
    out["verdict"], out["verdict_reason"] = verdict, reason

    # How many tasks would resolve an effect of the observed size. CI widths shrink ~1/sqrt(n);
    # use the side of the interval that crosses zero, since bootstrap CIs are asymmetric.
    eff = out["cps_change"]
    half = (hi - lo) / 2 if not math.isnan(lo) else None
    if verdict == "INCONCLUSIVE" and eff and half:
        crossing = (eff - lo) if eff > 0 else (hi - eff)
        out["tasks_needed_estimate"] = max(k + 1, math.ceil(k * (crossing / abs(eff)) ** 2))
    out["detectable_change"] = half

    if monthly_spend and out["cps_change"] is not None:
        out["monthly_savings"] = -out["cps_change"] * monthly_spend
        out["monthly_savings_ci"] = (-hi * monthly_spend, -lo * monthly_spend)
    return out


def per_task_table(records: list[dict], arms: list[str]) -> list[dict]:
    per_task = _per_task(records)
    rows = []
    for t in sorted(per_task):
        row = {"task": t}
        for a in arms:
            c = per_task[t][a]
            row[a] = {"n": c["n"], "succ": c["succ"],
                      "cost_per_run": c["cost"] / c["n"] if c["n"] else None,
                      "cost_per_success": c["cost"] / c["succ"] if c["succ"] else None}
        rows.append(row)
    return rows


def analyze(records: list[dict], arms: list[str], baseline: str, quality_margin: float = 0.05,
            monthly_spend: float | None = None, seed: int = 0) -> dict:
    return {
        "baseline": baseline,
        "arms": [arm_summary(records, a) for a in arms],
        "comparisons": [compare(records, baseline, a, quality_margin, monthly_spend, seed)
                        for a in arms if a != baseline],
        "per_task": per_task_table(records, arms),
        "total_spend": sum(r["cost_usd"] for r in records),
        "runs": len(records),
        "quality_margin": quality_margin,
        "monthly_spend": monthly_spend,
    }
