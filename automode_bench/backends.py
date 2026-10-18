"""Send one request to a model backend; return text, usage, seconds, error. Never raises."""
import json
import time
import urllib.error
import urllib.request

BACKENDS = ("anthropic", "openai")


def call(
    backend: str, url: str, request: dict, key: str = "local", timeout: float = 300
) -> dict:
    """POST request to the backend and parse the reply; problems go to `error`, text = ""."""
    if backend not in BACKENDS:
        return _result("", {}, 0.0, f"unknown backend: {backend}")
    target = url.rstrip("/") + ("/v1/messages" if backend == "anthropic" else "/v1/chat/completions")
    if backend == "anthropic":
        body = request
        headers = {
            "content-type": "application/json",
            "x-api-key": key,
            "authorization": f"Bearer {key}",
            "anthropic-version": "2023-06-01",
        }
    else:
        body, headers = _openai_body(request), {"content-type": "application/json"}
    start = time.time()
    try:
        req = urllib.request.Request(target, data=json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return _result("", {}, time.time() - start, f"HTTP {e.code}: {e.reason}")
    except Exception as e:
        return _result("", {}, time.time() - start, f"{type(e).__name__}: {e}")
    if backend == "anthropic":
        text = "".join(
            b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text"
        )
    else:
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            return _result("", {}, time.time() - start, "unexpected response shape")
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
    return body


def _result(text: str, usage: dict, seconds: float, error: str | None) -> dict:
    return {"text": text, "usage": usage, "seconds": seconds, "error": error}
