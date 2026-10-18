"""Golden and unit tests for the Markdown report."""
import os
import unittest

from automode_bench import report

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "results")
A = os.path.join(FIX, "a.jsonl")
B = os.path.join(FIX, "b.jsonl")
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
        self.assertEqual([r["id"] for r in rows], ["dstr-a", "dstr-b", "ex-a", "ex-b"])

    def test_render_matches_golden(self):
        with open(EXPECTED, encoding="utf-8") as f:
            expected = f.read().strip("\n")
        self.assertEqual(report.render([A, B], CASES).strip("\n"), expected)

    def test_no_cases_omits_pairs_section(self):
        text = report.render([A, B])
        self.assertNotIn("Pairs judged alike", text)
        self.assertIn("## Summary", text)


if __name__ == "__main__":
    unittest.main()
