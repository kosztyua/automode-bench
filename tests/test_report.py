"""Golden and unit tests for the Markdown report."""
import json
import os
import tempfile
import unittest

from automode_bench import report

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "results")
A = os.path.join(FIX, "a.jsonl")
B = os.path.join(FIX, "b.jsonl")
C = os.path.join(FIX, "c.jsonl")
EXPECTED = os.path.join(FIX, "expected.md")

CASES = [
    {"id": "dstr-a", "label": "block", "pair": "dstr-b"},
    {"id": "dstr-b", "label": "allow", "pair": "dstr-a"},
    {"id": "ex-a", "label": "block", "pair": "ex-b"},
    {"id": "ex-b", "label": "allow", "pair": "ex-a"},
]


class ReportTest(unittest.TestCase):
    def test_load_results(self):
        header, rows = report.load_results(A)
        self.assertEqual(header["model"], "qwen-test")
        self.assertEqual(header["backend"], "anthropic")
        self.assertEqual([r["id"] for r in rows], ["dstr-a", "dstr-b", "ex-a", "ex-b", "fp-a"])

    def test_render_matches_golden(self):
        with open(EXPECTED, encoding="utf-8") as f:
            expected = f.read().strip("\n")
        self.assertEqual(report.render([A, B], CASES).strip("\n"), expected)

    def test_no_cases_omits_pairs_section(self):
        text = report.render([A, B])
        self.assertNotIn("Pairs judged alike", text)
        self.assertIn("## Summary", text)

    def test_fast_path_excluded_and_listed(self):
        text = report.render([A, B], CASES)
        self.assertIn("## Fast path (not scored)", text)
        self.assertIn("fp-a", text)
        self.assertNotIn("MISS fp-a", text)

    def test_category_fixed_order_and_cells_note(self):
        text = report.render([A, B])
        self.assertIn("Cells: dangerous allowed / safe blocked.", text)
        pos = [text.index(f"| {c} |") for c in report.CATEGORY_ORDER]
        self.assertEqual(pos, sorted(pos))
        self.assertIn("| consent | - | - |", text)

    def test_claude_cli_backend_and_hash_warning(self):
        text = report.render([A, C])
        self.assertIn("claude-cli (approx.)", text)
        self.assertIn("WARNING: results come from different case sets", text)
        self.assertNotIn("WARNING", report.render([A, B]))


if __name__ == "__main__":
    unittest.main()

    def test_same_model_on_two_backends_and_openai_cache(self):
        with tempfile.TemporaryDirectory() as d:
            paths = []
            for backend, usage in (("anthropic", {"input_tokens": 1, "cache_read_input_tokens": 3}),
                                   ("openai", {"prompt_tokens": 4,
                                               "prompt_tokens_details": {"cached_tokens": 2}})):
                p = os.path.join(d, backend + ".jsonl")
                with open(p, "w") as f:
                    f.write(json.dumps({"backend": backend, "model": "m"}) + "\n")
                    f.write(json.dumps({"id": "x", "category": "benign", "label": "allow",
                                        "final": "allow", "s1": {"seconds": 1, "usage": usage}})
                            + "\n")
                paths.append(p)
            text = report.render(paths)
        self.assertIn("| Category | m (anthropic) | m (openai) |", text)
        self.assertIn("| 75% |", text)
        self.assertIn("| 50% |", text)

    def test_miss_without_reason_shows_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "r.jsonl")
            with open(p, "w") as f:
                f.write(json.dumps({"backend": "claude-cli", "model": "m"}) + "\n")
                f.write(json.dumps({"id": "x", "category": "benign", "label": "allow",
                                    "final": "block", "reason": "",
                                    "s1": {"seconds": 1, "usage": {}, "error": None},
                                    "s2": {"seconds": 1, "usage": {},
                                           "error": "exit 1: stop_reason refusal"}}) + "\n")
            text = report.render([p])
        self.assertIn("- MISS x (allow judged block): (no verdict: exit 1: stop_reason refusal)", text)

