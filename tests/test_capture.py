"""Tests for the capture fake-agent proxy (no claude binary needed)."""
import json
import unittest
import urllib.request

from automode_bench import capture, prompts

EXPECTED_STEPS = [
    ("Read", {"file_path": "README.md"}),
    ("Grep", {"pattern": "x"}),
    ("Glob", {"pattern": "*.md"}),
    ("Edit", {"file_path": "notes.txt", "old_string": "a", "new_string": "b"}),
    ("Write", {"file_path": "new.txt", "content": "hello\n"}),
    (
        "Bash",
        {"command": 'echo capture > "$AUTOMODE_BENCH_OUT/out.txt"', "description": "write a file"},
    ),
]


def agent_body(n: int, split: bool = False) -> dict:
    """An agent request whose history carries n tool_result blocks."""
    results = [
        {"type": "tool_result", "tool_use_id": f"t{i}", "content": "x"}
        for i in range(n)
    ]
    messages = (
        [{"role": "user", "content": [r]} for r in results]
        if split
        else [{"role": "user", "content": results}]
    )
    return {"tools": [{"name": "Read"}], "messages": messages}


def classifier_body() -> dict:
    return {
        "system": [
            {"type": "text", "text": "x-anthropic-billing-header: cc_version=0;"},
            {"type": "text", "text": "You are a security monitor."},
        ],
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
    }


def post(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


class AgentReplyTest(unittest.TestCase):
    def test_sequence(self):
        for n, (name, inp) in enumerate(EXPECTED_STEPS):
            msg = capture.agent_reply(agent_body(n))
            self.assertEqual(msg["stop_reason"], "tool_use", f"n={n}")
            block = msg["content"][0]
            self.assertEqual(block["type"], "tool_use")
            self.assertEqual(block["name"], name, f"n={n}")
            self.assertEqual(block["input"], inp, f"n={n}")
            self.assertEqual(block["id"], f"toolu_cap_{n}")
        msg = capture.agent_reply(agent_body(len(EXPECTED_STEPS)))
        self.assertEqual(msg["stop_reason"], "end_turn")
        self.assertEqual(msg["content"], [{"type": "text", "text": "done"}])

    def test_counts_across_messages(self):
        msg = capture.agent_reply(agent_body(3, split=True))
        self.assertEqual(msg["content"][0]["name"], "Edit")


class ClassifierTest(unittest.TestCase):
    def test_is_classifier(self):
        self.assertTrue(capture.is_classifier(classifier_body()))
        body = classifier_body()
        body["tools"] = []
        self.assertFalse(capture.is_classifier(body))
        self.assertFalse(capture.is_classifier({"system": "You are an agent."}))
        self.assertFalse(capture.is_classifier({"messages": []}))

    def test_classifier_reply(self):
        first = capture.classifier_reply(0)
        self.assertEqual(
            first["content"][0]["text"], "<block>yes</block><reason>capture</reason>"
        )
        second = capture.classifier_reply(1)
        self.assertEqual(second["content"][0]["text"], "<block>no</block>")
        self.assertEqual(second["stop_reason"], "end_turn")


def parse_sse(data: str) -> list[dict]:
    events = []
    for frame in data.split("\n\n"):
        if not frame.strip():
            continue
        for line in frame.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: ") :]))
            else:
                assert line.startswith("event: "), f"unexpected SSE line: {line!r}"
    return events


class SseTest(unittest.TestCase):
    def test_tool_use_round_trip(self):
        msg = capture.agent_reply(agent_body(len(EXPECTED_STEPS) - 1))  # the Bash step
        events = parse_sse(capture.sse(msg).decode())
        self.assertEqual(events[0]["type"], "message_start")
        self.assertEqual(events[-1]["type"], "message_stop")
        partials = "".join(
            e["delta"]["partial_json"]
            for e in events
            if e["type"] == "content_block_delta"
            and e["delta"]["type"] == "input_json_delta"
        )
        self.assertEqual(json.loads(partials), msg["content"][0]["input"])
        delta = next(e for e in events if e["type"] == "message_delta")
        self.assertEqual(delta["delta"]["stop_reason"], msg["stop_reason"])

    def test_text_round_trip(self):
        msg = capture.classifier_reply(0)
        events = parse_sse(capture.sse(msg).decode())
        text = "".join(
            e["delta"]["text"]
            for e in events
            if e["type"] == "content_block_delta" and e["delta"]["type"] == "text_delta"
        )
        self.assertEqual(text, msg["content"][0]["text"])


def dumps(obj: dict) -> str:
    """A transcript line, Claude Code's compact JSON."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def transcript_body(lines: list[str]) -> dict:
    """A classifier request: block A = all but the last line, block B = the last line."""
    cache = {"type": "ephemeral"}
    blocks = [
        {"type": "text", "text": "Judge the last action.\n"},
        {"type": "text", "text": "<transcript>\n"},
        {
            "type": "text",
            "text": "".join(line + "\n" for line in lines[:-1]),
            "cache_control": cache,
        },
        {"type": "text", "text": lines[-1] + "\n", "cache_control": cache},
        {"type": "text", "text": "</transcript>\nAnswer with <block>."},
    ]
    return {"messages": [{"role": "user", "content": blocks}]}


class TranscriptLinesTest(unittest.TestCase):
    def test_two_blocks_split_into_lines(self):
        lines = [
            dumps({"user": "Do the steps."}),
            dumps({"Edit": {"file_path": "notes.txt", "adds": "b", "removes": "a",
                            "removesTruncated": False}}),
            dumps({"Write": "new.txt: hello\n"}),
        ]
        self.assertEqual(capture._transcript_lines(transcript_body(lines)), lines)

    def test_drops_blank_lines(self):
        body = transcript_body(["a"])
        between = body["messages"][0]["content"]
        between[2]["text"] = "\n\nb\n\n"
        between[3]["text"] = "\nc\n\n"
        self.assertEqual(capture._transcript_lines(body), ["b", "c"])


class NormalizePathsTest(unittest.TestCase):
    def test_replaces_config_path_everywhere(self):
        obj = {
            "system": [{"type": "text", "text": "Contents of "
                        "/tmp/x/automode-bench-config-abc123/CLAUDE.md (user's global):"}],
            "messages": [[{"text": 'line "C:/tmp/automode-bench-config-9/CLAUDE.md" end'}]],
            "n": 5,
            "other": "/tmp/automode-bench-work-abc/CLAUDE.md and "
                     "/tmp/automode-bench-config-abc123/settings.json",
        }
        out = capture.normalize_paths(obj)
        self.assertEqual(
            [b["text"] for b in out["system"]],
            ["Contents of /home/user/.claude/CLAUDE.md (user's global):"],
        )
        self.assertEqual(out["messages"][0][0]["text"],
                         'line "/home/user/.claude/CLAUDE.md" end')
        self.assertEqual(out["n"], 5)
        self.assertEqual(out["other"], "/tmp/automode-bench-work-abc/CLAUDE.md and "
                                       "/tmp/automode-bench-config-abc123/settings.json")

    def test_input_untouched(self):
        obj = {"text": "/tmp/automode-bench-config-abc123/CLAUDE.md"}
        capture.normalize_paths(obj)
        self.assertEqual(obj["text"], "/tmp/automode-bench-config-abc123/CLAUDE.md")


class ReadOnlyToolsTest(unittest.TestCase):
    def test_unobserved_tools(self):
        lines = [
            dumps({"user": "Do the steps."}),
            dumps({"Edit": {"file_path": "notes.txt", "adds": "b", "removes": "a",
                            "removesTruncated": False}}),
            dumps({"Write": "new.txt: hello\n"}),
            "not json",
            dumps([1, 2]),
        ]
        self.assertEqual(capture.read_only_tools(lines), ["Read", "Grep", "Glob", "Bash"])

    def test_all_observed(self):
        lines = [dumps({name: {}}) for name, _ in EXPECTED_STEPS]
        self.assertEqual(capture.read_only_tools(lines), [])


class ClaudeMdTextTest(unittest.TestCase):
    def test_placeholder_only(self):
        self.assertEqual(capture.CLAUDE_MD_TEXT.splitlines(), [prompts.PLACEHOLDER])


class ServeTest(unittest.TestCase):
    def setUp(self):
        self.log: list = []
        self.server = capture.serve(self.log)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_agent_request(self):
        resp = post(self.base + "/v1/messages", agent_body(0))
        self.assertEqual(resp["content"][0]["name"], "Read")
        self.assertEqual(self.log, [])

    def test_classifier_calls_logged_and_answered(self):
        first = post(self.base + "/v1/messages", classifier_body())
        second = post(self.base + "/v1/messages", classifier_body())
        self.assertIn("<block>yes</block>", first["content"][0]["text"])
        self.assertEqual(second["content"][0]["text"], "<block>no</block>")
        self.assertEqual(len(self.log), 2)
        self.assertIn("security monitor", self.log[0]["system"][1]["text"])

    def test_plain_request_gets_ok(self):
        resp = post(self.base + "/v1/messages", {"messages": []})
        self.assertEqual(resp["content"][0]["text"], "ok")

    def test_count_tokens(self):
        resp = post(self.base + "/v1/messages/count_tokens", {"messages": []})
        self.assertEqual(resp, {"input_tokens": 1})

    def test_get(self):
        with urllib.request.urlopen(self.base + "/api/models", timeout=10) as resp:
            self.assertEqual(json.load(resp), {"data": []})

    def test_streaming(self):
        req = urllib.request.Request(
            self.base + "/v1/messages",
            data=json.dumps({**agent_body(0), "stream": True}).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
        self.assertIn("event: message_start", body)
        self.assertIn("event: message_stop", body)
        events = parse_sse(body)
        self.assertEqual(events[-1]["type"], "message_stop")


if __name__ == "__main__":
    unittest.main()
