"""Render analysis results as Markdown (terminal/PRs) and a standalone HTML page."""
from __future__ import annotations

import html
import json
import math
from datetime import datetime


def money(x, digits=2) -> str:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "—"
    if abs(x) < 1 and digits == 2:
        digits = 4
    return f"-${-x:,.{digits}f}" if x < 0 else f"${x:,.{digits}f}"


def monthly_phrase(c: dict, spend: float) -> str:
    v = c["monthly_savings"]
    lo, hi = c["monthly_savings_ci"]
    if v >= 0:
        return (f"At {money(spend, 0)}/month of agent spend this saves about {money(v, 0)}/month "
                f"(CI {money(lo, 0)} to {money(hi, 0)}; negative = extra cost).")
    return (f"At {money(spend, 0)}/month of agent spend this adds about {money(-v, 0)}/month "
            f"(CI {money(-hi, 0)} to {money(-lo, 0)}; negative = a saving).")


def pct(x, signed=False, digits=1) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x * 100:+.{digits}f}%" if signed else f"{x * 100:.{digits}f}%"


def pts(x) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x * 100:+.1f} pts"


def ci(pair, fmt=pct) -> str:
    if not pair or any(v is None or (isinstance(v, float) and math.isnan(v)) for v in pair):
        return "—"
    if fmt is pct:
        return f"[{pct(pair[0], True)}, {pct(pair[1], True)}]"
    return f"[{fmt(pair[0])}, {fmt(pair[1])}]"


def _caveats(a: dict, exp_info: dict) -> list[str]:
    notes = []
    arms = a["arms"]
    paired = min((c["paired_tasks"] for c in a["comparisons"]), default=0)
    if paired < 10:
        notes.append(f"Only {paired} paired tasks. Treat results as directional; 20–50 tasks are typical.")
    for s in arms:
        if s["unpriced_models"]:
            notes.append(f"Arm `{s['arm']}` used models with no price entry: {', '.join(s['unpriced_models'])} "
                         "(their cost is counted as $0).")
        if s["reported_cost_mismatches"]:
            notes.append(f"Arm `{s['arm']}`: {s['reported_cost_mismatches']} runs where the agent's own cost "
                         "estimate differs from ours by >5%. Check the price table or proxy rewriting.")
        if s["excluded_runs"]:
            notes.append(f"Arm `{s['arm']}`: {s['excluded_runs']} runs excluded for setup/harness errors.")
    if exp_info.get("agent_type") == "mock":
        notes.append("These numbers come from the **simulated** agent, not real API calls.")
    return notes


def markdown(a: dict, exp_info: dict) -> str:
    L = [f"# callbs report: {exp_info['name']}", ""]
    L.append(f"{a['runs']} runs · {len(a['per_task'])} tasks · total spend {money(a['total_spend'])} · "
             f"baseline arm `{a['baseline']}` · generated {datetime.now():%Y-%m-%d %H:%M}")
    L.append("")
    L.append("## Verdict")
    L.append("")
    for c in a["comparisons"]:
        line = f"- **`{c['arm']}` vs `{c['baseline']}`: {c['verdict']}**"
        if c.get("cps_change") is not None:
            line += (f" — cost per successful task {pct(c['cps_change'], True)} "
                     f"(95% CI {ci(c['cps_change_ci'])}), success rate {pts(c['success_diff'])} "
                     f"(CI {ci(c['success_diff_ci'], pts)}).")
        if c.get("verdict_reason"):
            line += f" {c['verdict_reason'].capitalize()}."
        if c.get("tasks_needed_estimate"):
            line += f" About {c['tasks_needed_estimate']} tasks would resolve an effect this size."
        if c.get("monthly_savings") is not None:
            line += " " + monthly_phrase(c, a["monthly_spend"])
        L.append(line)
    L += ["", "## Arms", "",
          "| Arm | Runs | Success | Cost / success | Cost / run | Cache hit | Prompt tok / run | Median turns | Spend |",
          "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for s in a["arms"]:
        L.append(f"| `{s['arm']}` | {s['runs']} | {pct(s['success_rate'])} | {money(s['cost_per_success'])} | "
                 f"{money(s['cost_per_run'])} | {pct(s['cache_hit_rate'])} | "
                 f"{_k(s['prompt_tokens_per_run'])} | {s['median_turns'] or '—'} | {money(s['total_cost'])} |")
    L += ["", "## Paired comparison", "",
          "| Arm | Tasks | Δ prompt tokens | Δ cost / success | 95% CI | Δ cost / run | Δ success | Cheaper on | Wilcoxon p |",
          "|---|---:|---:|---:|---|---:|---:|---:|---:|"]
    by_arm = {s["arm"]: s for s in a["arms"]}
    for c in a["comparisons"]:
        if c["paired_tasks"] == 0:
            continue
        p = c.get("wilcoxon_p_cost_per_run")
        ta = by_arm[c["baseline"]]["prompt_tokens_per_run"]
        tb = by_arm[c["arm"]]["prompt_tokens_per_run"]
        dtok = tb / ta - 1 if ta and tb is not None else None
        L.append(f"| `{c['arm']}` | {c['paired_tasks']} | {pct(dtok, True)} | "
                 f"{pct(c['cps_change'], True)} | {ci(c['cps_change_ci'])} | "
                 f"{pct(c['cost_per_run_change'], True)} | {pts(c['success_diff'])} | "
                 f"{c['arm_cheaper_on_tasks']}/{c['paired_tasks']} | {'—' if p is None else f'{p:.3f}'} |")
    L += ["", "## Where the money goes", "",
          "| Arm | Uncached input | Cache writes | Cache reads | Output | Server tools | Overhead |",
          "|---|---:|---:|---:|---:|---:|---:|"]
    for s in a["arms"]:
        comp = s["cost_composition"]
        tot = sum(comp.values()) or 1
        L.append(f"| `{s['arm']}` | " + " | ".join(
            pct(comp.get(k, 0) / tot) for k in
            ("input", "cache_write", "cache_read", "output", "server_tools", "overhead")) + " |")
    L += ["", "## Failures", "", "| Arm | " + " | ".join(_OUTCOMES) + " |",
          "|---|" + "---:|" * len(_OUTCOMES)]
    for s in a["arms"]:
        L.append(f"| `{s['arm']}` | " + " | ".join(str(s["outcomes"].get(o, 0)) for o in _OUTCOMES) + " |")
    arms = [s["arm"] for s in a["arms"]]
    L += ["", "## Per task (cost per success, successes/runs)", "",
          "| Task | " + " | ".join(f"`{x}`" for x in arms) + " |", "|---|" + "---:|" * len(arms)]
    for row in a["per_task"]:
        L.append(f"| {row['task']} | " + " | ".join(
            f"{money(row[x]['cost_per_success'])} ({row[x]['succ']}/{row[x]['n']})" for x in arms) + " |")
    notes = _caveats(a, exp_info)
    if notes:
        L += ["", "## Caveats", ""] + [f"- {n}" for n in notes]
    L += ["", "_Cost per successful task = total spend of an arm ÷ tasks it solved (check command passed). "
          "CIs are paired bootstrap over tasks. Costs are priced per API call from provider usage, "
          "including cache write/read tiers._", ""]
    return "\n".join(L)


_OUTCOMES = ("success", "check_failed", "agent_error", "timeout", "budget_exceeded")


def _k(x) -> str:
    if x is None:
        return "—"
    return f"{x / 1000:,.0f}K" if x >= 1000 else f"{x:.0f}"


_VERDICT_TONE = {"SAVES MONEY": "good", "COSTS MORE": "bad", "HURTS QUALITY": "bad",
                 "CHEAPER, QUALITY UNCLEAR": "warn", "INCONCLUSIVE": "neutral", "NO DATA": "neutral"}


def html_page(a: dict, exp_info: dict) -> str:
    e = html.escape
    arms = [s["arm"] for s in a["arms"]]
    max_cps = max((s["cost_per_success"] or 0) for s in a["arms"]) or 1

    cards = []
    for c in a["comparisons"]:
        tone = _VERDICT_TONE.get(c["verdict"], "neutral")
        detail = ""
        if c.get("cps_change") is not None:
            detail = (f"<div class='big'>{pct(c['cps_change'], True)}</div>"
                      f"<div class='sub'>cost per successful task · 95% CI {e(ci(c['cps_change_ci']))}</div>"
                      f"<div class='sub'>success rate {e(pts(c['success_diff']))} · CI {e(ci(c['success_diff_ci'], pts))}</div>")
        if c.get("monthly_savings") is not None:
            detail += f"<div class='sub'>{e(monthly_phrase(c, a['monthly_spend']))}</div>"
        if c.get("tasks_needed_estimate"):
            detail += f"<div class='sub'>~{c['tasks_needed_estimate']} tasks needed to resolve</div>"
        cards.append(f"<section class='card {tone}'><div class='label'>{e(c['arm'])} vs {e(c['baseline'])}</div>"
                     f"<div class='verdict'>{e(c['verdict'])}</div>{detail}"
                     f"<div class='reason'>{e(c.get('verdict_reason', ''))}</div></section>")

    bars = []
    for s in a["arms"]:
        w = 100 * (s["cost_per_success"] or 0) / max_cps
        bars.append(f"<div class='bar-row'><span class='bar-label'>{e(s['arm'])}</span>"
                    f"<span class='bar'><span style='width:{w:.1f}%'></span></span>"
                    f"<span class='bar-val'>{money(s['cost_per_success'])}</span></div>")

    comp_rows = []
    keys = ("input", "cache_write", "cache_read", "output", "server_tools", "overhead")
    for s in a["arms"]:
        comp = s["cost_composition"]
        tot = sum(comp.values()) or 1
        segs = "".join(f"<span class='seg s-{k}' style='width:{100 * comp.get(k, 0) / tot:.2f}%' "
                       f"title='{k}: {pct(comp.get(k, 0) / tot)}'></span>" for k in keys)
        comp_rows.append(f"<div class='bar-row'><span class='bar-label'>{e(s['arm'])}</span>"
                         f"<span class='stack'>{segs}</span></div>")
    legend = "".join(f"<span><i class='seg s-{k}'></i>{k.replace('_', ' ')}</span>" for k in keys)

    md = markdown(a, exp_info)
    tables = _md_tables_to_html(md)
    return _PAGE.format(title=e(exp_info["name"]), cards="".join(cards), bars="".join(bars),
                        comp="".join(comp_rows), legend=legend, tables=tables,
                        meta=e(f"{a['runs']} runs · {len(a['per_task'])} tasks · {len(arms)} arms · "
                               f"spend {money(a['total_spend'])}"),
                        data=json.dumps(clean(a), default=str).replace("</", "<\\/"))


def clean(x):
    """Make analysis output strict-JSON safe (no NaN/Infinity)."""
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {k: clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    return x


def _md_tables_to_html(md: str) -> str:
    """Convert the Markdown report's sections (after the verdict) into HTML."""
    out, in_table, header_done = [], False, False
    started = False
    for line in md.splitlines():
        if line.startswith("## ") and line != "## Verdict":
            started = True
        if not started:
            continue
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if set("".join(cells)) <= set("-: "):
                continue
            if not in_table:
                out.append("<div class='tw'><table>")
                in_table, header_done = True, False
            tag = "th" if not header_done else "td"
            out.append("<tr>" + "".join(f"<{tag}>{_inline(c)}</{tag}>" for c in cells) + "</tr>")
            header_done = True
            continue
        if in_table:
            out.append("</table></div>")
            in_table = False
        if line.startswith("## "):
            out.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.startswith("- "):
            out.append(f"<p class='note'>{_inline(line[2:])}</p>")
        elif line.startswith("_") and line.endswith("_"):
            out.append(f"<p class='foot'>{html.escape(line.strip('_'))}</p>")
    if in_table:
        out.append("</table></div>")
    return "\n".join(out)


def _inline(s: str) -> str:
    import re
    s = html.escape(s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>callbs: {title}</title>
<style>
:root {{ --bg:#fbfaf7; --fg:#1d1d1b; --muted:#6b6a65; --line:#e4e1d8; --card:#ffffff;
  --good:#1f7a4d; --bad:#b4372f; --warn:#a86a00; --neutral:#5b5f6b; --accent:#2f5d8a;
  --c-input:#8c6bb1; --c-cache_write:#e08b3a; --c-cache_read:#5aa469; --c-output:#3b78b5;
  --c-server_tools:#b5577b; --c-overhead:#7a7a7a; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ --bg:#161614; --fg:#ecebe6; --muted:#a3a29b;
  --line:#2f2e2a; --card:#1f1f1c; --good:#5cc08a; --bad:#ef7b72; --warn:#e3a94a; --neutral:#a4a8b3; --accent:#7fb0e0; }} }}
:root[data-theme="dark"] {{ --bg:#161614; --fg:#ecebe6; --muted:#a3a29b; --line:#2f2e2a; --card:#1f1f1c;
  --good:#5cc08a; --bad:#ef7b72; --warn:#e3a94a; --neutral:#a4a8b3; --accent:#7fb0e0; }}
* {{ box-sizing:border-box }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:1080px; margin:0 auto; padding:32px 16px 64px; }}
h1 {{ font-size:26px; margin:0 0 4px; letter-spacing:-.01em }} h2 {{ font-size:17px; margin:36px 0 10px }}
.meta {{ color:var(--muted); margin-bottom:24px }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:14px }}
.card {{ background:var(--card); border:1px solid var(--line); border-left:5px solid var(--neutral); border-radius:10px; padding:16px }}
.card.good {{ border-left-color:var(--good) }} .card.bad {{ border-left-color:var(--bad) }} .card.warn {{ border-left-color:var(--warn) }}
.label {{ color:var(--muted); font-size:13px }} .verdict {{ font-weight:700; font-size:18px; margin:2px 0 6px }}
.good .verdict {{ color:var(--good) }} .bad .verdict {{ color:var(--bad) }} .warn .verdict {{ color:var(--warn) }}
.big {{ font-size:30px; font-weight:700; font-variant-numeric:tabular-nums }}
.sub, .reason {{ color:var(--muted); font-size:13px }} .reason {{ margin-top:8px }}
.panel {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px }}
.bar-row {{ display:flex; align-items:center; gap:10px; margin:6px 0 }}
.bar-label {{ width:140px; flex:none; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-family:ui-monospace,monospace; font-size:13px }}
.bar {{ flex:1; height:14px; background:var(--line); border-radius:4px; overflow:hidden }}
.bar span {{ display:block; height:100%; background:var(--accent) }}
.bar-val {{ width:90px; text-align:right; font-variant-numeric:tabular-nums }}
.stack {{ flex:1; display:flex; height:14px; border-radius:4px; overflow:hidden; background:var(--line) }}
.seg {{ display:inline-block; height:100% }} .legend {{ display:flex; flex-wrap:wrap; gap:14px; color:var(--muted); font-size:12px; margin-top:8px }}
.legend i {{ width:10px; height:10px; border-radius:2px; margin-right:5px; vertical-align:-1px }}
.s-input {{ background:var(--c-input) }} .s-cache_write {{ background:var(--c-cache_write) }} .s-cache_read {{ background:var(--c-cache_read) }}
.s-output {{ background:var(--c-output) }} .s-server_tools {{ background:var(--c-server_tools) }} .s-overhead {{ background:var(--c-overhead) }}
.tw {{ overflow-x:auto; border:1px solid var(--line); border-radius:10px; background:var(--card) }}
table {{ border-collapse:collapse; width:100%; font-size:13.5px; font-variant-numeric:tabular-nums }}
th, td {{ padding:7px 10px; border-bottom:1px solid var(--line); text-align:right; white-space:nowrap }}
th:first-child, td:first-child {{ text-align:left }} th {{ color:var(--muted); font-weight:600 }}
tr:last-child td {{ border-bottom:none }}
code {{ font-family:ui-monospace,monospace; font-size:.92em }}
.note {{ margin:6px 0 }} .foot {{ color:var(--muted); font-size:12.5px; margin-top:28px }}
@media (max-width:600px) {{ .bar-label {{ width:90px }} .bar-val {{ width:70px }} }}
</style></head>
<body><main>
<h1>{title}</h1>
<div class="meta">{meta}</div>
<div class="cards">{cards}</div>
<h2>Cost per successful task</h2>
<div class="panel">{bars}</div>
<h2>Cost composition</h2>
<div class="panel">{comp}<div class="legend">{legend}</div></div>
{tables}
<script type="application/json" id="callbs-data">{data}</script>
</main></body></html>
"""
