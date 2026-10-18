"""Send one request to a model backend; return text, usage, seconds, error. Never raises."""
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

BACKENDS = ("anthropic", "openai", "claude-cli")


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise IOError(f"redirect refused: {code} -> {newurl}")


# ponytail: timeout is per socket operation, not a total deadline for the call
_OPENER = urllib.request.build_opener(_NoRedirects)


def call(
    backend: str, url: str, request: dict, key: str = "local", timeout: float = 300
) -> dict:
    """POST request to the backend and parse the reply; problems go to `error`, text = ""."""
    if backend not in BACKENDS:
        return _result("", {}, 0.0, f"unknown backend: {backend}")
    start = time.time()
    if backend == "claude-cli":
        return _claude_cli(request, timeout, start)
    try:
        target = url.rstrip("/") + ("/v1/messages" if backend == "anthropic" else "/v1/chat/completions")
        if backend == "anthropic":
            body = request
            headers = {
                "content-type": "application/json",
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
            }
        else:
            body = _openai_body(request)
            headers = {"content-type": "application/json", "authorization": f"Bearer {key}"}
        req = urllib.request.Request(target, data=json.dumps(body).encode(), headers=headers)
        with _OPENER.open(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        if backend == "anthropic":
            text = "".join(
                b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text"
            )
        else:
            text = data["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        return _result("", {}, time.time() - start, f"HTTP {e.code}: {_body_snippet(e)}")
    except Exception as e:
        return _result("", {}, time.time() - start, f"{type(e).__name__}: {e}")
    if not isinstance(text, str) or not text:
        return _result("", {}, time.time() - start, "empty reply")
    return _result(text, data.get("usage") or {}, time.time() - start, None)


def _claude_cli(request: dict, timeout: float, start: float) -> dict:
    """Run `claude -p` with the captured prompt; user's login env inherited, cwd a temp dir."""
    try:
        # A system prompt starting with Claude Code's billing-header line makes the CLI silently
        # use its default prompt instead (verified 2.1.302), so that block is dropped.
        system = "\n".join(b["text"] for b in request.get("system") or []
                           if not b["text"].startswith("x-anthropic-billing-header"))
        # Messages are separate turns in the API; keep them apart in the single CLI prompt.
        prompt = "\n\n".join(
            "".join(b["text"] for b in m.get("content") or []) for m in request.get("messages") or []
        )
        with tempfile.TemporaryDirectory(prefix="automode-bench-cli-") as cwd:
            sys_file = os.path.join(cwd, "system_prompt.txt")
            with open(sys_file, "w", encoding="utf-8") as f:
                f.write(system)
            proc = subprocess.run(
                [
                    "claude", "-p",
                    "--model", request["model"],
                    "--system-prompt-file", sys_file,
                    "--tools", "",
                    "--output-format", "json",
                    "--setting-sources", "",
                ],
                input=prompt, capture_output=True, text=True, cwd=cwd, timeout=timeout,
                # the judge must not see the user's own CLAUDE.md (verified 2.1.302)
                env={**os.environ, "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1"},
            )
    except subprocess.TimeoutExpired:
        return _result("", {}, time.time() - start, f"claude timed out after {timeout}s")
    except FileNotFoundError:
        return _result("", {}, time.time() - start, "claude not found on PATH")
    except Exception as e:
        return _result("", {}, time.time() - start, f"{type(e).__name__}: {e}")
    if proc.returncode != 0:
        # a model refusal exits 1 with empty stderr; the reason is in the JSON on stdout
        detail = proc.stderr[:200].strip()
        try:
            detail = detail or f"stop_reason {json.loads(proc.stdout)['stop_reason']}"
        except Exception:
            detail = detail or proc.stdout[:200].strip()
        return _result("", {}, time.time() - start, f"exit {proc.returncode}: {detail}")
    try:
        data = json.loads(proc.stdout)
        text = data["result"]
    except Exception:
        return _result("", {}, time.time() - start, f"bad CLI output: {proc.stdout[:200]}")
    if data.get("is_error"):
        return _result("", {}, time.time() - start, f"is_error: {str(text)[:200]}")
    if not isinstance(text, str) or not text:
        return _result("", {}, time.time() - start, "empty reply")
    return _result(text, data.get("usage") or {}, time.time() - start, None)


def _openai_body(request: dict) -> dict:
    """Convert an Anthropic-shaped request to a chat-completions body."""
    body = {
        "model": request["model"],
        "max_tokens": request["max_tokens"],
        "temperature": 0,
        "messages": [
            {"role": "system", "content": "\n".join(b["text"] for b in request.get("system") or [])}
        ]
        + [
            # blocks already end with \n (captured format); "" join keeps bytes stable
            {"role": m["role"], "content": "".join(b["text"] for b in m["content"])}
            for m in request.get("messages") or []
        ],
    }
    if request.get("stop_sequences"):
        body["stop"] = request["stop_sequences"]
    if request.get("reasoning_effort"):
        body["reasoning_effort"] = request["reasoning_effort"]
    return body


def _body_snippet(e: urllib.error.HTTPError) -> str:
    try:
        return e.read().decode("utf-8", "replace")[:200]
    except Exception:
        return ""


def _result(text: str, usage: dict, seconds: float, error: str | None) -> dict:
    return {"text": text, "usage": usage, "seconds": seconds, "error": error}
