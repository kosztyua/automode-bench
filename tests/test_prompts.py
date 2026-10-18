"""Tests for automode_bench.prompts against the synthetic capture fixture.

Fixture shape mirrors a real Claude Code 2.1.302 capture: the last message is
["<transcript>\\n", block A (history), block B (action), "</transcript>\\n", "Answer.\\n"]
with cache_control on A and B; the CLAUDE.md slot indents every line by two spaces.
"""
import copy
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
CC = {"type": "ephemeral"}


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

    def test_fixture_has_true_block_shape(self):
        cap = load_capture(FIXTURE)
        for stage in ("stage1", "stage2"):
            msgs = cap[stage]["messages"]
            self.assertEqual(len(msgs), 2)
            md = msgs[0]["content"]
            self.assertEqual(len(md), 1)
            self.assertEqual(md[0]["cache_control"], CC)
            self.assertIn("\n  " + PLACEHOLDER + "\n", md[0]["text"])
            blocks = msgs[1]["content"]
            self.assertEqual([b["text"] for b in blocks[:1]], ["<transcript>\n"])
            self.assertEqual(blocks[-2]["text"], "</transcript>\n")
            self.assertEqual(blocks[-1]["text"], "Answer.\n")
            self.assertEqual([b["cache_control"] for b in blocks[1:3]], [CC, CC])


class BuildRequestTest(unittest.TestCase):
    def setUp(self):
        self.cap = load_capture(FIXTURE)

    def stage(self):
        return copy.deepcopy(self.cap["stage1"])

    def test_transcript_replaced_surroundings_kept(self):
        req = build_request(
            self.cap["stage1"],
            ['{"user":"hi"}', '{"Bash":"ls"}'],
            "anything",
            "model-x",
        )
        self.assertEqual(
            texts(req, -1),
            [
                "<transcript>\n",
                '{"user":"hi"}\n',
                '{"Bash":"ls"}\n',
                "</transcript>\n",
                "Answer.\n",
            ],
        )

    def test_history_and_action_blocks_get_cache_control(self):
        req = build_request(self.cap["stage1"], ["a", "b", "c"], "md", "m")
        blocks = req["messages"][-1]["content"]
        self.assertEqual(blocks[1]["text"], "a\nb\n")  # block A = all but the last line
        self.assertEqual(blocks[2]["text"], "c\n")  # block B = the last line
        self.assertEqual(blocks[1]["cache_control"], CC)
        self.assertEqual(blocks[2]["cache_control"], CC)
        self.assertNotIn("cache_control", blocks[0])
        self.assertNotIn("cache_control", blocks[3])

    def test_cache_control_omitted_when_capture_has_none(self):
        st = self.stage()
        for b in st["messages"][-1]["content"]:
            b.pop("cache_control", None)
        req = build_request(st, ["a", "b"], "md", "m")
        for b in req["messages"][-1]["content"][1:3]:
            self.assertNotIn("cache_control", b)

    def test_single_line_omits_block_a(self):
        req = build_request(self.cap["stage1"], ["only"], "md", "m")
        self.assertEqual(
            texts(req, -1), ["<transcript>\n", "only\n", "</transcript>\n", "Answer.\n"]
        )
        self.assertEqual(req["messages"][-1]["content"][1]["cache_control"], CC)

    def test_no_lines_raises(self):
        with self.assertRaisesRegex(ValueError, "at least one"):
            build_request(self.cap["stage1"], [], "md", "m")

    def test_model_set_metadata_gone_input_not_mutated(self):
        req = build_request(self.cap["stage1"], ["l"], None, "new-model")
        self.assertEqual(req["model"], "new-model")
        self.assertNotIn("metadata", req)
        self.assertEqual(self.cap["stage1"]["model"], "x")
        self.assertIn("metadata", self.cap["stage1"])
        # deep copy: mutating the request must not touch the fixture dict
        req["messages"][0]["content"][0]["text"] = "clobbered"
        self.assertIn(PLACEHOLDER, texts(self.cap["stage1"], 0)[0])

    def test_claude_md_lines_are_indented(self):
        req = build_request(self.cap["stage1"], ["l"], "Allow X\n\nUse Y", "m")
        text = texts(req, 0)[0]
        self.assertIn("\n  Allow X\n  \n  Use Y\n</user_claude_md>", text)
        self.assertNotIn(PLACEHOLDER, json.dumps(req))
        self.assertEqual(req["messages"][0]["content"][0]["cache_control"], CC)

    def test_claude_md_substitution_is_indent_tolerant(self):
        st = self.stage()
        block = st["messages"][0]["content"][0]
        block["text"] = block["text"].replace("\n  " + PLACEHOLDER, "\n\t\t" + PLACEHOLDER)
        req = build_request(st, ["l"], "Allow X\n\nUse Y", "m")
        text = texts(req, 0)[0]
        self.assertIn("\n\t\tAllow X\n\t\t\n\t\tUse Y\n</user_claude_md>", text)
        self.assertNotIn(PLACEHOLDER, json.dumps(req))

    def test_claude_md_none_removes_message(self):
        req = build_request(self.cap["stage1"], ["l"], None, "m")
        self.assertEqual(len(req["messages"]), 1)
        self.assertNotIn(PLACEHOLDER, json.dumps(req))


class BuildRequestDriftTest(unittest.TestCase):
    def setUp(self):
        self.cap = load_capture(FIXTURE)

    def stage(self):
        return copy.deepcopy(self.cap["stage1"])

    def test_missing_start_marker_raises_valueerror(self):
        st = self.stage()
        st["messages"][-1]["content"][0]["text"] = "<nope>\n"
        with self.assertRaisesRegex(ValueError, "capture format drift"):
            build_request(st, ["l"], "md", "m")

    def test_missing_end_marker_raises_valueerror(self):
        st = self.stage()
        st["messages"][-1]["content"][3]["text"] = "no end here\n"
        with self.assertRaisesRegex(ValueError, "capture format drift"):
            build_request(st, ["l"], "md", "m")

    def test_no_blocks_between_markers_raises_valueerror(self):
        st = self.stage()
        st["messages"][-1]["content"] = [
            {"type": "text", "text": "<transcript>\n"},
            {"type": "text", "text": "</transcript>\n"},
            {"type": "text", "text": "Answer.\n"},
        ]
        with self.assertRaisesRegex(ValueError, "capture format drift"):
            build_request(st, ["l"], "md", "m")

    def test_text_block_without_text_raises_valueerror(self):
        st = self.stage()
        st["messages"][-1]["content"].insert(0, {"type": "text"})
        with self.assertRaisesRegex(ValueError, "capture format drift"):
            build_request(st, ["l"], "md", "m")

    def test_unreplaced_placeholder_raises_valueerror(self):
        st = self.stage()
        # placeholder hiding somewhere the line substitution cannot reach
        st["messages"][0]["content"].append(
            {"type": "image", "source": {"url": PLACEHOLDER}}
        )
        with self.assertRaisesRegex(ValueError, "placeholder not replaced"):
            build_request(st, ["l"], "md", "m")

    def test_non_text_blocks_are_skipped(self):
        st = self.stage()
        image = {"type": "image", "source": {}}
        st["messages"][-1]["content"].insert(0, dict(image))
        st["messages"][0]["content"].append(dict(image))
        req = build_request(st, ["a", "b"], "Allow X", "m")
        self.assertEqual(req["messages"][-1]["content"][0], image)
        self.assertIn("\n  Allow X\n", req["messages"][0]["content"][0]["text"])
        self.assertEqual(req["messages"][0]["content"][1], image)


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
