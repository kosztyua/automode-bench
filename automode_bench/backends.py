"""Send one request to a model backend; return text, usage, seconds, error. Never raises."""
import json
import time
import urllib.error
import urllib.request

BACKENDS = ("anthropic", "openai")


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


def _body_snippet(e: urllib.error.HTTPError) -> str:
    try:
        return e.read().decode("utf-8", "replace")[:200]
    except Exception:
        return ""


def _result(text: str, usage: dict, seconds: float, error: str | None) -> dict:
    return {"text": text, "usage": usage, "seconds": seconds, "error": error}
