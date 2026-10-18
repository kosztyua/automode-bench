"""Load a captured Claude Code classifier prompt and build per-case requests."""
import copy
import json
import os

PLACEHOLDER = "AUTOMODE_BENCH_CLAUDE_MD_PLACEHOLDER"


def capture_dir(version: str | None = None) -> str:
    """~/.automode-bench/<version>; with None, the most recently modified subdir."""
    base = os.path.join(os.path.expanduser("~"), ".automode-bench")
    if version is not None:
        return os.path.join(base, version)
    subs = (
        [os.path.join(base, d) for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))]
        if os.path.isdir(base)
        else []
    )
    if not subs:
        raise FileNotFoundError(f"no capture found under {base}; run capture first")
    return max(subs, key=os.path.getmtime)


def load_capture(path: str) -> dict:
    """Read stage1/stage2/format from a capture dir."""
    out = {}
    for key in ("stage1", "stage2", "format"):
        with open(os.path.join(path, key + ".json")) as f:
            out[key] = json.load(f)
    return out


def build_request(
    stage: dict, lines: list[str], claude_md: str | None, model: str
) -> dict:
    """Copy a captured request with the transcript, CLAUDE.md slot, and model replaced."""
    req = copy.deepcopy(stage)
    req["model"] = model
    req.pop("metadata", None)
    blocks = req["messages"][-1]["content"]
    start = next(i for i, b in enumerate(blocks) if b["text"].startswith("<transcript>"))
    end = next(i for i, b in enumerate(blocks) if b["text"].startswith("</transcript>"))
    injected = [{"type": "text", "text": "<transcript>\n"}] + [
        {"type": "text", "text": line + "\n"} for line in lines
    ]
    req["messages"][-1]["content"] = blocks[:start] + injected + blocks[end:]
    for msg in req["messages"]:
        if claude_md is None:
            msg["content"] = [b for b in msg["content"] if PLACEHOLDER not in b["text"]]
        else:
            for b in msg["content"]:
                if PLACEHOLDER in b["text"]:
                    b["text"] = b["text"].replace(PLACEHOLDER, claude_md)
    req["messages"] = [m for m in req["messages"] if m["content"]]
    return req
