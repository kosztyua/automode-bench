"""Tests for transcript.render."""
import unittest

from automode_bench.transcript import render


class TestRender(unittest.TestCase):
    def test_user_and_bash(self):
        case = {
            "user": ["run the tests"],
            "actions": [{"tool": "Bash", "input": {"command": "pytest -q", "description": "run"}}],
        }
        self.assertEqual(render(case), ['{"user": "run the tests"}', '{"Bash": "pytest -q"}'])

    def test_read_only_dropped(self):
        case = {
            "user": ["x"],
            "actions": [
                {"tool": "Read", "input": {"file_path": "a.py"}},
                {"tool": "Grep", "input": {"pattern": "p"}},
                {"tool": "Bash", "input": {"command": "ls"}},
            ],
        }
        self.assertEqual(render(case), ['{"user": "x"}', '{"Bash": "ls"}'])

    def test_edit_renders_input_dict(self):
        case = {
            "user": ["x"],
            "actions": [
                {"tool": "Edit", "input": {"file_path": "a.py", "old_string": "a", "new_string": "b"}},
            ],
        }
        self.assertEqual(
            render(case),
            ['{"user": "x"}',
             '{"Edit": {"file_path": "a.py", "old_string": "a", "new_string": "b"}}'],
        )

    def test_multiple_user_messages_in_order(self):
        case = {
            "user": ["first", "second"],
            "actions": [{"tool": "Bash", "input": {"command": "echo hi"}}],
        }
        self.assertEqual(
            render(case),
            ['{"user": "first"}', '{"user": "second"}', '{"Bash": "echo hi"}'],
        )

    def test_non_ascii_kept_verbatim(self):
        case = {"user": ["árvíztűrő tükörfúrógép"], "actions": []}
        self.assertEqual(render(case), ['{"user": "árvíztűrő tükörfúrógép"}'])

    def test_custom_read_only_keeps_read(self):
        case = {
            "user": ["x"],
            "actions": [
                {"tool": "Read", "input": {"file_path": "a.py"}},
                {"tool": "Glob", "input": {"pattern": "*.md"}},
            ],
        }
        self.assertEqual(
            render(case, read_only=("Glob",)),
            ['{"user": "x"}', '{"Read": {"file_path": "a.py"}}'],
        )


if __name__ == "__main__":
    unittest.main()
