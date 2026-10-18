"""Fake-agent HTTP proxy that drives one `claude -p` run to capture classifier prompts."""
import json
import os
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .prompts import PLACEHOLDER, build_request

AGENT_STEPS: list[tuple[str, dict]] = [
    ("Read", {"file_path": "README.md"}),
    ("Grep", {"pattern": "x"}),
    ("Glob", {"pattern": "*.md"}),
    ("Edit", {"file_path": "notes.txt", "old_string": "a", "new_string": "b"}),
    # Outside the cwd on purpose: auto mode allows in-project writes without asking the classifier.
    ("Bash", {"command": 'echo capture > "$AUTOMODE_BENCH_OUT/out.txt"', "description": "write a file"}),
]


def _message(content: list, stop: str) -> dict:
    return {
        "id": "msg_cap",
        "type": "message",
        "role": "assistant",
        "model": "bench",
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def agent_reply(body: dict) -> dict:
    """The scripted main agent: one step per tool_result seen so far, then 'done'."""
    n = sum(
        1
        for m in body.get("messages", [])
        for b in m.get("content", [])
        if isinstance(b, dict) and b.get("type") == "tool_result"
    )
    if n >= len(AGENT_STEPS):
        return _message([{"type": "text", "text": "done"}], "end_turn")
    name, inp = AGENT_STEPS[n]
    return _message(
        [{"type": "tool_use", "id": f"toolu_cap_{n}", "name": name, "input": inp}],
        "tool_use",
    )


def classifier_reply(index: int) -> dict:
    """Force a stage-2 call: answer the first classifier request with yes, the rest with no."""
    text = (
        "<block>yes</block><reason>capture</reason>"
        if index == 0
        else "<block>no</block>"
    )
    return _message([{"type": "text", "text": text}], "end_turn")


def is_classifier(body: dict) -> bool:
    """Claude Code's auto-mode classifier: 'security monitor' system text, no tools."""
    if "tools" in body:
        return False
    system = body.get("system")
    texts = system if isinstance(system, str) else "".join(
        b.get("text", "") for b in system or [] if isinstance(b, dict)
    )
    return "security monitor" in texts


def _event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


def sse(message: dict) -> bytes:
    """Render a message dict as Anthropic streaming (SSE) events."""
    out = [_event("message_start", {"type": "message_start", "message": {**message, "content": []}})]
    for i, block in enumerate(message["content"]):
        if block["type"] == "text":
            start = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": block["text"]}
        else:
            start = {**block, "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        out.append(_event("content_block_start", {"type": "content_block_start", "index": i, "content_block": start}))
        out.append(_event("content_block_delta", {"type": "content_block_delta", "index": i, "delta": delta}))
        out.append(_event("content_block_stop", {"type": "content_block_stop", "index": i}))
    out.append(_event("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": message["stop_reason"], "stop_sequence": None},
        "usage": {"output_tokens": 1},
    }))
    out.append(_event("message_stop", {"type": "message_stop"}))
    return "".join(out).encode()


def serve(log: list) -> ThreadingHTTPServer:
    """Start the proxy on a free port (daemon thread); classifier request bodies are appended to log."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._json({"data": []})

        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("content-length", 0)))
            if self.path.rstrip("/").endswith("count_tokens"):
                return self._json({"input_tokens": 1})
            body = json.loads(raw)
            if is_classifier(body):
                log.append(body)
                message = classifier_reply(len(log) - 1)
            elif body.get("tools"):
                message = agent_reply(body)
            else:
                message = _message([{"type": "text", "text": "ok"}], "end_turn")
            if body.get("stream"):
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                self.wfile.write(sse(message))
                return
            self._json(message)

        def _json(self, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _claude_version(claude: str, timeout: int) -> str:
    proc = _run_claude([claude, "--version"], None, timeout)
    if not proc.stdout.split():
        raise RuntimeError(f"`{claude} --version` printed nothing")
    return proc.stdout.split()[0]


def _run_claude(cmd: list[str], cwd: str | None, timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, cwd=cwd, timeout=timeout, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"`{cmd[0]}` not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"`{cmd[0]}` timed out after {timeout}s") from exc


def _claude_run(claude: str, work: str, out_dir: str, timeout: int, with_claude_md: bool) -> list:
    """One `claude -p --permission-mode auto` run against a fresh proxy; returns logged classifier bodies."""
    config = tempfile.mkdtemp(prefix="automode-bench-config-")
    if with_claude_md:
        with open(os.path.join(config, "CLAUDE.md"), "w") as f:
            f.write(f"Bench capture marker.\n{PLACEHOLDER}\n")
    log: list = []
    server = serve(log)
    env = dict(
        os.environ,
        ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_address[1]}",
        ANTHROPIC_AUTH_TOKEN="x",
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        CLAUDE_CONFIG_DIR=config,
        ANTHROPIC_DEFAULT_HAIKU_MODEL="bench",
        ANTHROPIC_DEFAULT_SONNET_MODEL="bench",
        ANTHROPIC_DEFAULT_OPUS_MODEL="bench",
        AUTOMODE_BENCH_OUT=out_dir,
    )
    try:
        subprocess.run(
            [claude, "-p", "--permission-mode", "auto", "Do the steps."],
            cwd=work, env=env, timeout=timeout,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"`{claude}` not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"capture run timed out after {timeout}s (is auto mode available?)"
        ) from exc
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(config, ignore_errors=True)
    return log


def _transcript_lines(body: dict) -> list[str]:
    """The transcript lines of a captured classifier request, as sent."""
    blocks = body["messages"][-1]["content"]
    start = next(i for i, b in enumerate(blocks) if b["text"].startswith("<transcript>"))
    end = next(i for i, b in enumerate(blocks) if b["text"].startswith("</transcript>"))
    return [b["text"].rstrip("\n") for b in blocks[start + 1 : end]]


def run_capture(
    out_root: str = "~/.automode-bench", claude: str = "claude", timeout: int = 120
) -> str:
    """Run claude twice against the proxy; write stage1/stage2/format into the versioned capture dir."""
    version = _claude_version(claude, timeout)
    work = tempfile.mkdtemp(prefix="automode-bench-work-")
    out_dir = tempfile.mkdtemp(prefix="automode-bench-out-")
    with open(os.path.join(work, "README.md"), "w") as f:
        f.write("Capture fixture.\n")
    with open(os.path.join(work, "notes.txt"), "w") as f:
        f.write("a\n")
    try:
        log = _claude_run(claude, work, out_dir, timeout, True)
        plain_log = _claude_run(claude, work, out_dir, timeout, False)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(out_dir, ignore_errors=True)
    if len(log) < 2:
        raise RuntimeError(
            f"captured {len(log)} classifier call(s), need at least 2 "
            "(is `--permission-mode auto` available in this version?)"
        )
    stage1, stage2 = log[0], log[1]
    # The last classifier call sees every earlier action, so it shows what Claude Code drops.
    lines = _transcript_lines(log[-1])
    observed = set()
    for line in lines:
        try:
            observed.update(json.loads(line))
        except ValueError:
            continue
    fmt = {
        "claude_code": version,
        "read_only": [name for name, _ in AGENT_STEPS if name not in observed],
        "lines": lines,
        "claude_md_removed_ok": False,
    }
    if plain_log:
        rebuilt = build_request(stage1, lines, None, plain_log[0]["model"])
        fmt["claude_md_removed_ok"] = rebuilt["messages"] == plain_log[0]["messages"]
    out = os.path.join(os.path.expanduser(out_root), version)
    os.makedirs(out, exist_ok=True)
    for name, payload in (("stage1", stage1), ("stage2", stage2), ("format", fmt)):
        with open(os.path.join(out, name + ".json"), "w") as f:
            json.dump(payload, f, indent=2)
    return out
