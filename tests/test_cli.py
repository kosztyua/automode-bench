"""CLI wiring: cases validation, run against a fake backend, report, error exits."""
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from automode_bench.__main__ import main

HERE = os.path.dirname(os.path.abspath(__file__))
CAPTURE = os.path.join(HERE, "fixtures", "capture")
GOOD = os.path.join(HERE, "fixtures", "cli", "good.jsonl")
BAD = os.path.join(HERE, "fixtures", "cli", "bad.jsonl")


class _Server(ThreadingHTTPServer):
    requests: list = []


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        self.server.requests.append((self.path, body))
        data = json.dumps({
            "content": [{"type": "text", "text": "<block>no"}],
            "usage": {"input_tokens": 7, "cache_read_input_tokens": 90},
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._home = os.environ.get("HOME")
        self.addCleanup(lambda: os.environ.update(HOME=self._home))

    def test_cases_valid_prints_counts(self):
        code, out, _ = _run_cli(["cases", "--cases", GOOD])
        self.assertEqual(code, 0)
        self.assertIn("2 cases", out)
        self.assertIn("benign", out)
        self.assertIn("destructive", out)

    def test_cases_invalid_exits_2_lists_all_problems(self):
        code, _, err = _run_cli(["cases", "--cases", BAD])
        self.assertEqual(code, 2)
        self.assertIn("bad-cat", err)
        self.assertIn("bad-label", err)

    def test_cases_no_match_exits_2(self):
        code, _, err = _run_cli(
            ["cases", "--cases", os.path.join(self.tmp.name, "none-*.jsonl")])
        self.assertEqual(code, 2)
        self.assertIn("no cases", err)

    def test_run_without_capture_exits_2(self):
        os.environ["HOME"] = self.tmp.name
        code, _, err = _run_cli(["run", "--backend", "anthropic", "--model", "m"])
        self.assertEqual(code, 2)
        self.assertIn("capture", err.lower())

    def _start_server(self):
        _Server.requests = []
        server = _Server(("127.0.0.1", 0), _Handler)
        server.daemon_threads = True
        import threading
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def test_run_writes_results_header_and_rows(self):
        url = self._start_server()
        out_path = os.path.join(self.tmp.name, "res", "r.jsonl")
        code, out, err = _run_cli([
            "run", "--backend", "anthropic", "--url", url, "--model", "testmodel",
            "--cases", GOOD, "--capture", CAPTURE, "--out", out_path, "--conc", "16",
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("dangerous allowed:", out)
        with open(out_path) as f:
            lines = [json.loads(ln) for ln in f if ln.strip()]
        header, rows = lines[0], lines[1:]
        self.assertEqual(header["backend"], "anthropic")
        self.assertEqual(header["model"], "testmodel")
        self.assertEqual(header["url_host"], "127.0.0.1")
        self.assertEqual(header["claude_code"], "0.0.0")
        self.assertRegex(header["cases_hash"], r"^[0-9a-f]{12}$")
        self.assertTrue(header["started"])
        self.assertEqual(len(rows), 2)  # --conc 16 on 2 cases still completes, in order
        self.assertEqual([r["id"] for r in rows], ["cli-benign-1", "cli-danger-1"])
        self.assertTrue(all(r["final"] == "allow" for r in rows))
        # stage 1 said no everywhere: exactly one request per case, no stage 2
        self.assertEqual(len(_Server.requests), 2)
        self.assertTrue(all(path == "/v1/messages" for path, _ in _Server.requests))
        # transcript was injected in the captured (compact JSON) format; thread order is not
        # guaranteed, so search all recorded bodies
        texts = ["".join(b["text"] for b in body["messages"][-1]["content"])
                 for _, body in _Server.requests]
        line = json.dumps({"Bash": "rm -rf ~"}, ensure_ascii=False, separators=(",", ":"))
        self.assertTrue(any(line in t for t in texts), texts)

    def test_run_think_adds_budget(self):
        url = self._start_server()
        code, _, err = _run_cli([
            "run", "--backend", "anthropic", "--url", url, "--model", "m",
            "--cases", GOOD, "--capture", CAPTURE, "--think",
            "--out", os.path.join(self.tmp.name, "r.jsonl"),
        ])
        self.assertEqual(code, 0, err)
        body = _Server.requests[0][1]
        self.assertEqual(body["thinking"], {"type": "enabled", "budget_tokens": 1056})

    def test_run_think_with_non_anthropic_backend_exits_2(self):
        for backend in ("openai", "claude-cli"):
            code, _, err = _run_cli([
                "run", "--backend", backend, "--model", "m",
                "--cases", GOOD, "--capture", CAPTURE, "--think",
                "--out", os.path.join(self.tmp.name, "r.jsonl"),
            ])
            self.assertEqual(code, 2, backend)
            self.assertIn("--think is only supported with --backend anthropic", err)

    def test_run_reasoning_effort_with_non_openai_backend_exits_2(self):
        code, _, err = _run_cli([
            "run", "--backend", "anthropic", "--model", "m", "--cases", GOOD,
            "--capture", CAPTURE, "--reasoning-effort", "none",
            "--out", os.path.join(self.tmp.name, "r.jsonl"),
        ])
        self.assertEqual(code, 2)
        self.assertIn("--reasoning-effort is only supported with --backend openai", err)

    def _run_with_format(self, cap_dir, url):
        return _run_cli([
            "run", "--backend", "anthropic", "--url", url, "--model", "m",
            "--cases", GOOD, "--capture", cap_dir,
            "--out", os.path.join(self.tmp.name, "r.jsonl"),
        ])

    def test_run_warns_when_capture_format_checks_failed(self):
        url = self._start_server()
        for key in ("render_ok", "claude_md_removed_ok"):
            cap = os.path.join(self.tmp.name, "cap-" + key)
            shutil.copytree(CAPTURE, cap)
            with open(os.path.join(cap, "format.json")) as f:
                fmt = json.load(f)
            fmt[key] = False
            with open(os.path.join(cap, "format.json"), "w") as f:
                json.dump(fmt, f)
            code, _, err = self._run_with_format(cap, url)
            self.assertEqual(code, 0, err)
            self.assertIn("WARNING: capture format checks failed; run capture again", err)
        # a capture without failing checks stays quiet
        code, _, err = self._run_with_format(CAPTURE, url)
        self.assertEqual(code, 0, err)
        self.assertNotIn("capture format checks failed", err)

    def test_run_report_round_trip(self):
        url = self._start_server()
        out_path = os.path.join(self.tmp.name, "r.jsonl")
        code, _, err = _run_cli([
            "run", "--backend", "anthropic", "--url", url, "--model", "m",
            "--cases", GOOD, "--capture", CAPTURE, "--out", out_path,
        ])
        self.assertEqual(code, 0, err)
        code, out, err = _run_cli(["report", out_path, "--cases", GOOD])
        self.assertEqual(code, 0, err)
        self.assertIn("## Summary", out)


if __name__ == "__main__":
    unittest.main()
