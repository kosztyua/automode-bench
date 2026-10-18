"""Tests for backends.call against a local stdlib HTTP server (no network)."""
import json
import socket
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
        res = backends.call("claude-cli", "http://127.0.0.1:1", ANTHROPIC_REQUEST)
        self.assertEqual(res["text"], "")
        self.assertIsNotNone(res["error"])

    def test_backend_never_mutates_request(self):
        url, _ = self.serve(reply=json.dumps({"content": [], "usage": {}}).encode())
        before = json.dumps(ANTHROPIC_REQUEST)
        backends.call("openai", url, ANTHROPIC_REQUEST)
        self.assertEqual(json.dumps(ANTHROPIC_REQUEST), before)


if __name__ == "__main__":
    unittest.main()
