"""Golden and unit tests for the Markdown report."""
import os
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
