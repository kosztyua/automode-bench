"""Tests for automode_bench.prompts against the synthetic capture fixture."""
import json
import os
import tempfile
import unittest

from automode_bench.prompts import (
    PLACEHOLDER,
    build_request,
    capture_dir,
    load_capture,
)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "capture")


def texts(request: dict, msg_index: int) -> list[str]:
    return [b["text"] for b in request["messages"][msg_index]["content"]]


class LoadCaptureTest(unittest.TestCase):
    def test_loads_three_keys(self):
        cap = load_capture(FIXTURE)
        self.assertEqual(set(cap), {"stage1", "stage2", "format"})
        self.assertEqual(cap["stage1"]["max_tokens"], 2112)
        self.assertEqual(cap["stage1"]["stop_sequences"], ["</block>"])
        self.assertEqual(cap["stage2"]["max_tokens"], 10240)
        self.assertNotIn("stop_sequences", cap["stage2"])
        self.assertEqual(cap["format"]["claude_code"], "0.0.0")


class BuildRequestTest(unittest.TestCase):
    def setUp(self):
        self.cap = load_capture(FIXTURE)

    def test_transcript_replaced_surroundings_kept(self):
        req = build_request(
            self.cap["stage1"],
            ['{"user": "hi"}', '{"Bash": "ls"}'],
            "anything",
            "model-x",
        )
        self.assertEqual(
            texts(req, -1),
            [
                "Judge the last action.\n",
                "<transcript>\n",
                '{"user": "hi"}\n',
                '{"Bash": "ls"}\n',
                "</transcript>\nAnswer.\n",
            ],
        )

    def test_model_set_metadata_gone_input_not_mutated(self):
        req = build_request(self.cap["stage1"], ["l"], None, "new-model")
        self.assertEqual(req["model"], "new-model")
        self.assertNotIn("metadata", req)
        self.assertEqual(self.cap["stage1"]["model"], "x")
        self.assertIn("metadata", self.cap["stage1"])
        # deep copy: mutating the request must not touch the fixture dict
        req["messages"][0]["content"][0]["text"] = "clobbered"
        self.assertIn(PLACEHOLDER, texts(self.cap["stage1"], 0)[0])

    def test_claude_md_substituted(self):
        req = build_request(self.cap["stage1"], ["l"], "Allow X", "m")
        self.assertEqual(texts(req, 0), ["CLAUDE.md:\nAllow X\n"])
        self.assertNotIn(PLACEHOLDER, json.dumps(req))

    def test_claude_md_none_removes_message(self):
        req = build_request(self.cap["stage1"], ["l"], None, "m")
        self.assertEqual(len(req["messages"]), 1)
        self.assertNotIn(PLACEHOLDER, json.dumps(req))

    def test_empty_lines_leaves_only_transcript_open(self):
        req = build_request(self.cap["stage1"], [], "md", "m")
        self.assertEqual(
            texts(req, -1),
            ["Judge the last action.\n", "<transcript>\n", "</transcript>\nAnswer.\n"],
        )


class CaptureDirTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("HOME")
        os.environ["HOME"] = self.home.name

    def tearDown(self):
        if self.old_home is not None:
            os.environ["HOME"] = self.old_home
        self.home.cleanup()

    def test_explicit_version(self):
        expected = os.path.join(self.home.name, ".automode-bench", "1.2.3")
        self.assertEqual(capture_dir("1.2.3"), expected)

    def test_picks_newest_subdir(self):
        base = os.path.join(self.home.name, ".automode-bench")
        old = os.path.join(base, "old")
        new = os.path.join(base, "new")
        os.makedirs(old)
        os.makedirs(new)
        os.utime(old, (1000, 1000))
        os.utime(new, (2000, 2000))
        self.assertEqual(capture_dir(), new)

    def test_no_capture_raises(self):
        with self.assertRaises(FileNotFoundError):
            capture_dir()
        os.makedirs(os.path.join(self.home.name, ".automode-bench"))
        with self.assertRaises(FileNotFoundError):
            capture_dir()


if __name__ == "__main__":
    unittest.main()
