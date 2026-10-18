"""Tests for backends.call: local HTTP server for anthropic/openai, fake claude on PATH for claude-cli."""
import json
import os
import shutil
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from automode_bench import backends


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.requests.append(
            {"path": self.path, "headers": dict(self.headers), "body": json.loads(raw)}
        )
        self.send_response(self.server.status)
        if 300 <= self.server.status < 400:
            self.send_header("Location", self.path)
        self.end_headers()
        self.wfile.write(self.server.reply)

    def log_message(self, *args):
        pass


ANTHROPIC_REQUEST = {
    "model": "m",
    "max_tokens": 2112,
    "stop_sequences": ["</block>"],
    "system": [{"type": "text", "text": "sys a"}, {"type": "text", "text": "sys b"}],
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "one\n"}, {"type": "text", "text": "two\n"}]}
    ],
}


class CallTest(unittest.TestCase):
    def serve(self, status=200, reply=b"{}"):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        srv.status = status
        srv.reply = reply
        srv.requests = []
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        return "http://127.0.0.1:%d" % srv.server_address[1], srv

    def test_anthropic_happy(self):
        url, srv = self.serve(reply=json.dumps({
            "content": [
                {"type": "thinking", "text": "ignored"},
                {"type": "text", "text": "<block>no"},
                {"type": "text", "text": " and more"},
            ],
            "usage": {"input_tokens": 5, "cache_read_input_tokens": 4},
        }).encode())
        res = backends.call("anthropic", url, ANTHROPIC_REQUEST, key="k1")
        self.assertEqual(res["text"], "<block>no and more")
        self.assertEqual(res["usage"], {"input_tokens": 5, "cache_read_input_tokens": 4})
        self.assertIsNone(res["error"])
        self.assertGreater(res["seconds"], 0)
        req = srv.requests[0]
        headers = {k.lower(): v for k, v in req["headers"].items()}
        self.assertEqual(req["path"], "/v1/messages")
        self.assertEqual(headers["x-api-key"], "k1")
        self.assertNotIn("authorization", headers)
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(req["body"], ANTHROPIC_REQUEST)

    def test_trailing_slash(self):
        url, srv = self.serve(reply=json.dumps({"content": [], "usage": {}}).encode())
        backends.call("anthropic", url + "/", ANTHROPIC_REQUEST)
        self.assertEqual(srv.requests[0]["path"], "/v1/messages")

    def test_openai_request_shape(self):
        url, srv = self.serve(reply=json.dumps({
            "choices": [{"message": {"content": "<block>no"}}],
            "usage": {"total_tokens": 7},
        }).encode())
        res = backends.call("openai", url, ANTHROPIC_REQUEST)
        req = srv.requests[0]
        headers = {k.lower(): v for k, v in req["headers"].items()}
        self.assertEqual(headers["authorization"], "Bearer local")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertNotIn("x-api-key", headers)
        self.assertEqual(req["path"], "/v1/chat/completions")
        self.assertEqual(req["body"]["model"], "m")
        self.assertEqual(req["body"]["max_tokens"], 2112)
        self.assertEqual(req["body"]["temperature"], 0)
        self.assertEqual(req["body"]["stop"], ["</block>"])
        self.assertNotIn("stop_sequences", req["body"])
        self.assertNotIn("system", req["body"])
        self.assertEqual(
            req["body"]["messages"],
            [
                {"role": "system", "content": "sys a\nsys b"},
                {"role": "user", "content": "one\ntwo\n"},
            ],
        )
        self.assertEqual(res["text"], "<block>no")
        self.assertEqual(res["usage"], {"total_tokens": 7})
        self.assertIsNone(res["error"])

    def test_openai_reasoning_effort_passed_only_when_set(self):
        url, srv = self.serve(reply=json.dumps({
            "choices": [{"message": {"content": "<block>no"}}]}).encode())
        backends.call("openai", url, ANTHROPIC_REQUEST)
        backends.call("openai", url, {**ANTHROPIC_REQUEST, "reasoning_effort": "none"})
        self.assertNotIn("reasoning_effort", srv.requests[0]["body"])
        self.assertEqual(srv.requests[1]["body"]["reasoning_effort"], "none")

    def test_openai_null_content(self):
        url, _ = self.serve(reply=json.dumps({"choices": [{"message": {"content": None}}], "usage": {}}).encode())
        res = backends.call("openai", url, ANTHROPIC_REQUEST)
        self.assertEqual(res["text"], "")
        self.assertEqual(res["error"], "empty reply")

    def test_non_string_reply_text(self):
        reply = json.dumps({"choices": [{"message": {"content": ["a"]}}], "usage": {}}).encode()
        url, _ = self.serve(reply=reply)
        res = backends.call("openai", url, ANTHROPIC_REQUEST)
        self.assertEqual(res["text"], "")
        self.assertEqual(res["error"], "empty reply")

    def test_http_error_includes_body(self):
        url, _ = self.serve(status=500, reply=b"boom")
        res = backends.call("anthropic", url, ANTHROPIC_REQUEST)
        self.assertEqual(res["text"], "")
        self.assertEqual(res["error"], "HTTP 500: boom")

    def test_http_error_body_truncated_to_200(self):
        url, _ = self.serve(status=400, reply=b"x" * 500)
        res = backends.call("anthropic", url, ANTHROPIC_REQUEST)
        self.assertIn("x" * 200, res["error"])
        self.assertNotIn("x" * 201, res["error"])

    def test_redirect_refused(self):
        url, _ = self.serve(status=302, reply=b"")
        res = backends.call("anthropic", url, ANTHROPIC_REQUEST)
        self.assertEqual(res["text"], "")
        self.assertIn("redirect", res["error"].lower())

    def test_empty_reply_200(self):
        url, _ = self.serve(reply=json.dumps({"content": [], "usage": {}}).encode())
        res = backends.call("anthropic", url, ANTHROPIC_REQUEST)
        self.assertEqual(res["text"], "")
        self.assertEqual(res["error"], "empty reply")

    def test_malformed_request_never_raises(self):
        url, _ = self.serve()
        res = backends.call("openai", url, {})
        self.assertEqual(res["text"], "")
        self.assertIsNotNone(res["error"])

    def test_json_list_reply_never_raises(self):
        url, _ = self.serve(reply=b"[1, 2]")
        res = backends.call("anthropic", url, ANTHROPIC_REQUEST)
        self.assertEqual(res["text"], "")
        self.assertIsNotNone(res["error"])

    def test_non_json_body(self):
        url, _ = self.serve(reply=b"<html>not json</html>")
        res = backends.call("anthropic", url, ANTHROPIC_REQUEST)
        self.assertEqual(res["text"], "")
        self.assertIsNotNone(res["error"])

    def test_connection_refused(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        res = backends.call("anthropic", "http://127.0.0.1:%d" % port, ANTHROPIC_REQUEST, timeout=2)
        self.assertEqual(res["text"], "")
        self.assertIsNotNone(res["error"])

    def test_unknown_backend(self):
        res = backends.call("bogus", "http://127.0.0.1:1", ANTHROPIC_REQUEST)
        self.assertEqual(res["text"], "")
        self.assertIsNotNone(res["error"])

    def test_backend_never_mutates_request(self):
        url, _ = self.serve(reply=json.dumps({"content": [], "usage": {}}).encode())
        before = json.dumps(ANTHROPIC_REQUEST)
        backends.call("openai", url, ANTHROPIC_REQUEST)
        self.assertEqual(json.dumps(ANTHROPIC_REQUEST), before)


FAKE_ECHO = """
import json, os, sys
argv = sys.argv[1:]
stdin = sys.stdin.read()
system = ""
if "--system-prompt-file" in argv:
    with open(argv[argv.index("--system-prompt-file") + 1]) as f:
        system = f.read()
env = {k: os.environ.get(k) for k in
       ("CLAUDE_CODE_DISABLE_CLAUDE_MDS", "AUTOMODE_FAKE_MARKER", "PATH")}
print(json.dumps({
    "result": json.dumps({"argv": argv, "stdin": stdin, "system": system, "cwd": os.getcwd(),
                          "env": env}),
    "is_error": False,
    "usage": {"input_tokens": 3},
}))
"""


class ClaudeCliTest(unittest.TestCase):
    def fake(self, body):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, "claude"), "w") as f:
            f.write("#!/usr/bin/env python3\n" + body)
        os.chmod(os.path.join(d, "claude"), 0o755)
        old = os.environ["PATH"]
        os.environ["PATH"] = d + os.pathsep + old
        self.addCleanup(os.environ.__setitem__, "PATH", old)

    def test_invocation_and_result(self):
        self.fake(FAKE_ECHO)
        res = backends.call("claude-cli", "", ANTHROPIC_REQUEST)
        self.assertIsNone(res["error"])
        self.assertEqual(res["usage"], {"input_tokens": 3})
        payload = json.loads(res["text"])
        argv = payload["argv"]
        self.assertEqual(argv[0], "-p")
        self.assertEqual(argv[argv.index("--model") + 1], "m")
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertEqual(payload["system"], "sys a\nsys b")
        self.assertEqual(payload["stdin"], "one\ntwo\n")
        cwd = payload["cwd"]
        self.assertTrue(os.path.realpath(cwd).startswith(os.path.realpath(tempfile.gettempdir())))
        self.assertNotEqual(cwd, os.getcwd())
        self.assertFalse(os.path.exists(cwd))

    def test_billing_header_and_message_separation(self):
        self.fake(FAKE_ECHO)
        req = {"model": "m", "max_tokens": 5,
               "system": [{"type": "text", "text": "x-anthropic-billing-header: cc_version=1;"},
                          {"type": "text", "text": "policy"}],
               "messages": [{"role": "user", "content": [{"type": "text", "text": "md"}]},
                            {"role": "user", "content": [{"type": "text", "text": "<t>\n"}, {"type": "text", "text": "x\n"}]}]}
        payload = json.loads(backends.call("claude-cli", "", req)["text"])
        self.assertEqual(payload["system"], "policy")
        self.assertEqual(payload["stdin"], "md\n\n<t>\nx\n")

    def test_child_env_inherits_and_disables_claude_mds(self):
        self.fake(FAKE_ECHO)
        os.environ["AUTOMODE_FAKE_MARKER"] = "kept"
        self.addCleanup(os.environ.pop, "AUTOMODE_FAKE_MARKER", None)
        # unset so the test proves the backend sets it, not the ambient env
        os.environ.pop("CLAUDE_CODE_DISABLE_CLAUDE_MDS", None)
        payload = json.loads(backends.call("claude-cli", "", ANTHROPIC_REQUEST)["text"])
        self.assertEqual(payload["env"]["CLAUDE_CODE_DISABLE_CLAUDE_MDS"], "1")
        self.assertEqual(payload["env"]["AUTOMODE_FAKE_MARKER"], "kept")
        self.assertTrue(payload["env"]["PATH"])

    def test_nonzero_exit(self):
        self.fake("import sys\nsys.stderr.write('kaboom')\nsys.exit(3)\n")
        res = backends.call("claude-cli", "", ANTHROPIC_REQUEST)
        self.assertEqual(res["text"], "")
        self.assertIn("3", res["error"])
        self.assertIn("kaboom", res["error"])

    def test_refusal_reason_from_stdout(self):
        self.fake('import sys\nprint(\'{"stop_reason": "refusal", "result": ""}\')\nsys.exit(1)\n')
        res = backends.call("claude-cli", "", ANTHROPIC_REQUEST)
        self.assertEqual(res["text"], "")
        self.assertEqual(res["error"], "exit 1: stop_reason refusal")

    def test_is_error_flag(self):
        self.fake('print(\'{"result": "oops", "is_error": true}\')\n')
        res = backends.call("claude-cli", "", ANTHROPIC_REQUEST)
        self.assertEqual(res["text"], "")
        self.assertIn("oops", res["error"])

    def test_non_json_stdout(self):
        self.fake("print('not json at all')\n")
        res = backends.call("claude-cli", "", ANTHROPIC_REQUEST)
        self.assertEqual(res["text"], "")
        self.assertIsNotNone(res["error"])

    def test_empty_result(self):
        self.fake('print(\'{"result": "", "is_error": false}\')\n')
        res = backends.call("claude-cli", "", ANTHROPIC_REQUEST)
        self.assertEqual(res["text"], "")
        self.assertIsNotNone(res["error"])

    def test_claude_missing_on_path(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        old = os.environ["PATH"]
        os.environ["PATH"] = d
        self.addCleanup(os.environ.__setitem__, "PATH", old)
        res = backends.call("claude-cli", "", ANTHROPIC_REQUEST)
        self.assertEqual(res["text"], "")
        self.assertIn("claude", res["error"])

    def test_timeout(self):
        self.fake("import time\ntime.sleep(30)\n")
        res = backends.call("claude-cli", "", ANTHROPIC_REQUEST, timeout=1)
        self.assertEqual(res["text"], "")
        self.assertIn("timed out", res["error"].lower())
        self.assertLess(res["seconds"], 10)

    def test_never_mutates_request(self):
        self.fake(FAKE_ECHO)
        before = json.dumps(ANTHROPIC_REQUEST)
        backends.call("claude-cli", "", ANTHROPIC_REQUEST)
        self.assertEqual(json.dumps(ANTHROPIC_REQUEST), before)


if __name__ == "__main__":
    unittest.main()
