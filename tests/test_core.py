import json
import math
import shutil
import tempfile
import unittest
from pathlib import Path

from callbs import config, report, stats
from callbs.agents.claude_code import parse_stream_json
from callbs.pricing import Call, PriceTable, normalize_model, summarize_calls
from callbs.runner import Runner, load_results, schedule

PRICES = PriceTable.load()


class PricingTest(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_model("claude-opus-5-5[1m]"), "claude-opus-5-5")
        self.assertEqual(normalize_model("us.anthropic.claude-sonnet-4-5-20250929-v1:0"), "claude-sonnet-4-5")
        self.assertEqual(normalize_model("claude-haiku-4-5-20251001"), "claude-haiku-4-5")

    def test_longest_prefix_wins(self):
        self.assertEqual(PRICES.lookup("claude-opus-5-5")["input"], 4.0)
        self.assertEqual(PRICES.lookup("claude-opus-5")["input"], 5.0)
        self.assertIsNone(PRICES.lookup("gpt-5"))

    def test_cost_by_tier(self):
        c = Call(model="claude-sonnet-5-5", input=1_000_000, cache_write_5m=1_000_000,
                 cache_write_1h=1_000_000, cache_read=1_000_000, output=1_000_000, web_search=10)
        cb = PRICES.cost(c)
        self.assertAlmostEqual(cb.input, 2.0)
        self.assertAlmostEqual(cb.cache_write, 2.5 + 4.0)
        self.assertAlmostEqual(cb.cache_read, 0.10)
        self.assertAlmostEqual(cb.output, 10.0)
        self.assertAlmostEqual(cb.server_tools, 0.10)

    def test_haiku_long_prompt_tier_is_per_request(self):
        small = PRICES.cost(Call(model="claude-haiku-5-5", input=100_000))
        big = PRICES.cost(Call(model="claude-haiku-5-5", input=100_001))
        self.assertAlmostEqual(small.input, 0.01)
        self.assertAlmostEqual(big.input, 100_001 * 0.5e-6)

    def test_fast_mode(self):
        cb = PRICES.cost(Call(model="claude-opus-5-5", output=1_000_000, fast=True))
        self.assertAlmostEqual(cb.output, 40.0)

    def test_unpriced_is_flagged(self):
        s = summarize_calls([Call(model="mystery-1", input=10)], PRICES)
        self.assertEqual(s["cost"]["unpriced_models"], ["mystery-1"])
        self.assertEqual(s["cost"]["total"], 0)

    def test_usage_ttl_split(self):
        c = Call.from_anthropic_usage("claude-opus-5-5", {
            "input_tokens": 2, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 5,
            "output_tokens": 7, "cache_creation": {"ephemeral_1h_input_tokens": 60, "ephemeral_5m_input_tokens": 40}})
        self.assertEqual((c.cache_write_1h, c.cache_write_5m), (60, 40))
        c2 = Call.from_anthropic_usage("m", {"cache_creation_input_tokens": 30})
        self.assertEqual(c2.cache_write_5m, 30)


def _usage(inp, out, read=0, w1h=0):
    return {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
            "cache_creation_input_tokens": w1h,
            "cache_creation": {"ephemeral_1h_input_tokens": w1h, "ephemeral_5m_input_tokens": 0}}


class StreamJsonTest(unittest.TestCase):
    def test_dedupe_and_reconcile(self):
        lines = [
            {"type": "system", "subtype": "init", "model": "claude-sonnet-5-5"},
            # same message emitted twice (two content blocks) -> one call
            {"type": "assistant", "message": {"id": "m1", "model": "claude-sonnet-5-5", "usage": _usage(3, 10, 0, 1000)}},
            {"type": "assistant", "message": {"id": "m1", "model": "claude-sonnet-5-5", "usage": _usage(3, 50, 0, 1000)}},
            {"type": "assistant", "message": {"id": "m2", "model": "claude-sonnet-5-5", "usage": _usage(3, 20, 1000, 200)}},
            "not json",
            {"type": "result", "subtype": "success", "is_error": False, "num_turns": 2, "total_cost_usd": 0.01,
             "modelUsage": {
                 "claude-sonnet-5-5": {"inputTokens": 6, "outputTokens": 70, "cacheReadInputTokens": 1000,
                                       "cacheCreationInputTokens": 1200},
                 "claude-haiku-5-5": {"inputTokens": 500, "outputTokens": 40}}},
        ]
        text = "\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines)
        calls, meta = parse_stream_json(text)
        self.assertEqual(len(calls), 3)  # m1, m2, plus a reconciled haiku call
        self.assertEqual(calls[0].output, 50)
        self.assertEqual(calls[2].model, "claude-haiku-5-5")
        self.assertEqual(calls[2].input, 500)
        self.assertEqual(meta["result"]["num_turns"], 2)


def _rec(task, arm, trial, cost, success):
    return {"key": f"{task}|{arm}|{trial}", "task": task, "arm": arm, "trial": trial, "cost_usd": cost,
            "success": success, "outcome": "success" if success else "check_failed",
            "tokens": {"input": 1, "cache_write_5m": 0, "cache_write_1h": 10, "cache_read": 90, "output": 5},
            "cost_breakdown": {"input": 0, "cache_write": cost / 2, "cache_read": cost / 4, "output": cost / 4,
                               "server_tools": 0, "unpriced_models": []}}


class StatsTest(unittest.TestCase):
    def test_wilcoxon_exact(self):
        # all 8 diffs positive -> p = 2 / 2^8
        self.assertAlmostEqual(stats.wilcoxon_signed_rank([1, 2, 3, 4, 5, 6, 7, 8]), 2 / 256)
        self.assertEqual(stats.wilcoxon_signed_rank([0, 0]), None)
        p = stats.wilcoxon_signed_rank([1, -1, 2, -2])  # symmetric with ties
        self.assertAlmostEqual(p, 1.0)

    def test_verdicts(self):
        recs = []
        for t in range(20):
            for n in range(2):
                recs.append(_rec(f"t{t}", "base", n, 1.0 + 0.1 * (t % 3), True))
                recs.append(_rec(f"t{t}", "cheap", n, 0.7 + 0.1 * (t % 3), True))
                recs.append(_rec(f"t{t}", "pricey", n, 1.4 + 0.1 * (t % 3), True))
                recs.append(_rec(f"t{t}", "broken", n, 0.5, t % 2 == 0))
        a = stats.analyze(recs, ["base", "cheap", "pricey", "broken"], "base", monthly_spend=10000)
        v = {c["arm"]: c["verdict"] for c in a["comparisons"]}
        self.assertEqual(v["cheap"], "SAVES MONEY")
        self.assertEqual(v["pricey"], "COSTS MORE")
        self.assertEqual(v["broken"], "HURTS QUALITY")
        cheap = next(c for c in a["comparisons"] if c["arm"] == "cheap")
        self.assertGreater(cheap["monthly_savings"], 0)
        md = report.markdown(a, {"name": "t"})
        self.assertIn("SAVES MONEY", md)
        json.dumps(report.clean(a))  # strict JSON

    def test_noise_is_inconclusive(self):
        recs = []
        for t in range(6):
            recs.append(_rec(f"t{t}", "a", 0, 1.0, True))
            recs.append(_rec(f"t{t}", "b", 0, [0.6, 1.5][t % 2], True))
        a = stats.analyze(recs, ["a", "b"], "a")
        self.assertEqual(a["comparisons"][0]["verdict"], "INCONCLUSIVE")

    def test_setup_errors_excluded(self):
        recs = [_rec("t0", "a", 0, 1.0, True), _rec("t0", "b", 0, 1.0, True)]
        bad = _rec("t0", "b", 1, 0.0, False)
        bad["outcome"] = "setup_error"
        s = stats.arm_summary(recs + [bad], "b")
        self.assertEqual((s["runs"], s["excluded_runs"]), (1, 1))


class EndToEndTest(unittest.TestCase):
    def test_demo_experiment(self):
        from callbs import demo
        tmp = Path(tempfile.mkdtemp())
        try:
            cfg = demo.build(tmp, n_tasks=4, trials=2)
            exp = config.load(cfg)
            self.assertEqual(len(schedule(exp)), 4 * 4 * 2)
            runner = Runner(exp, PRICES, log=lambda *_: None)
            runner.run(limit=6)
            self.assertEqual(len(load_results(exp.results_dir / "results.jsonl")), 6)
            runner = Runner(exp, PRICES, log=lambda *_: None)
            runner.run()  # resumes; does not redo the first 6
            recs = load_results(exp.results_dir / "results.jsonl")
            self.assertEqual(len(recs), 32)
            self.assertTrue(all(r["outcome"] in ("success", "check_failed") for r in recs), recs[0])
            self.assertTrue(any(r["success"] for r in recs))
            ok = next(r for r in recs if r["success"])
            self.assertIn("lib/f", ok["diff_stat"])
            self.assertGreater(ok["cost_usd"], 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_restore_paths_blocks_test_tampering(self):
        from callbs import demo
        tmp = Path(tempfile.mkdtemp())
        try:
            cfg = demo.build(tmp, n_tasks=1, trials=1)
            text = cfg.read_text()
            # A "solution" that deletes the assertion instead of fixing the code.
            text = text.replace('"lib/f0.py"', '"tests/test_f0.py"')
            text = text.replace("success_rate = ", "success_rate = 1.0, old_rate = ")
            cfg.write_text(text)
            exp = config.load(cfg)
            exp.arms = exp.arms[:2]
            Runner(exp, PRICES, log=lambda *_: None).run()
            recs = load_results(exp.results_dir / "results.jsonl")
            self.assertTrue(recs and all(not r["success"] for r in recs))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


FAKE_CLAUDE_AUTH_FAIL = r'''
import json, sys
sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init", "model": "claude-haiku-5-5"}))
print(json.dumps({"type": "assistant", "message": {"id": "x", "model": "<synthetic>",
      "usage": {"input_tokens": 0, "output_tokens": 0}}}))
print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 401,
      "result": "Failed to authenticate. API Error: 401", "total_cost_usd": 0, "modelUsage": {}}))
sys.exit(1)
'''


class EarlyStopTest(unittest.TestCase):
    def _exp(self, tmp, arms):
        from callbs import demo
        exp = config.load(demo.build(tmp, n_tasks=12, trials=3))
        exp.arms = [a for a in exp.arms if a.name in arms]
        exp.early_stop = True
        return exp

    def test_stops_when_answer_is_clear(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            exp = self._exp(tmp, {"baseline", "schema-filter"})  # cheaper on every task
            r = Runner(exp, PRICES, log=lambda *_: None)
            r.run()
            n = len(load_results(exp.results_dir / "results.jsonl"))
            self.assertEqual(n, 12 * 2)  # one trial, then stopped
            self.assertIn("SAVES MONEY", r.early_stopped)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_retires_decided_arm_and_keeps_testing_unclear_one(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            # schema-filter is clear after one pass; compressor-light is noise around zero.
            exp = self._exp(tmp, {"baseline", "schema-filter", "compressor-light"})
            r = Runner(exp, PRICES, log=lambda *_: None)
            r.run()
            self.assertIsNone(r.early_stopped)
            self.assertIn("schema-filter", r.retired)
            recs = load_results(exp.results_dir / "results.jsonl")
            count = lambda arm: sum(1 for x in recs if x["arm"] == arm)
            self.assertEqual(count("schema-filter"), 12)          # one pass only
            self.assertEqual(count("compressor-light"), 12 * 3)   # all three
            self.assertEqual(count("baseline"), 12 * 3)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ValidateTest(unittest.TestCase):
    def test_flags_good_and_bad_tasks(self):
        import subprocess
        from callbs import demo
        from callbs.validate import validate
        tmp = Path(tempfile.mkdtemp())
        try:
            exp = config.load(demo.build(tmp, n_tasks=2, trials=1))
            # Commit the real fix for f0 only; f1 stays broken at the "fix" commit.
            (exp.repo / "lib" / "f0.py").write_text("def f(x):\n    return x * 2\n")
            git = lambda *a: subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a],
                                            cwd=exp.repo, check=True, capture_output=True)
            git("commit", "-qam", "fix f0")
            for t in exp.tasks:
                t.commit, t.fix_commit = "HEAD~1", "HEAD"
            rows = {r["task"]: r for r in validate(exp, log=lambda *_: None)}
            self.assertTrue(rows["fix-f00"]["ok"], rows["fix-f00"])
            self.assertFalse(rows["fix-f01"]["ok"], rows["fix-f01"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class FatalErrorTest(unittest.TestCase):
    def test_auth_failure_stops_experiment(self):
        import sys
        from callbs import demo
        tmp = Path(tempfile.mkdtemp())
        try:
            fake = tmp / "fake_claude.py"
            fake.write_text(FAKE_CLAUDE_AUTH_FAIL)
            cfg = demo.build(tmp, n_tasks=3, trials=1)
            exp = config.load(cfg)
            exp.agent = {"type": "claude-code", "executable": [sys.executable, str(fake)]}
            exp.parallel = 1
            Runner(exp, PRICES, log=lambda *_: None).run()
            recs = load_results(exp.results_dir / "results.jsonl")
            self.assertEqual(len(recs), 1)  # stopped after the first run
            self.assertEqual(recs[0]["outcome"], "setup_error")
            self.assertIn("401", recs[0]["error"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ConfigTest(unittest.TestCase):
    def test_unknown_keys_rejected(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            p = tmp / "e.toml"
            p.write_text('[experiment]\nrepo="."\n[[arms]]\nname="a"\n[[arms]]\nname="b"\ncolour="red"\n'
                         '[[tasks]]\nid="x"\nprompt="p"\ncheck="true"\n')
            with self.assertRaises(config.ConfigError):
                config.load(p)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
