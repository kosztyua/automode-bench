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


def _block_text(block: dict, where: str) -> str | None:
    """Text of a text block, None for other block types, ValueError if malformed."""
    if block.get("type") != "text":
        return None
    try:
        return block["text"]
    except KeyError:
        raise ValueError(f"capture format drift: {where} text block lacks 'text': {block!r}") from None


def _find_marker(blocks: list, marker: str) -> int:
    for i, b in enumerate(blocks):
        text = _block_text(b, f"block {i} of last message")
        if text is not None and text.startswith(marker):
            return i
    raise ValueError(f"capture format drift: no block starting with {marker!r} in last message")


def build_request(
    stage: dict, lines: list[str], claude_md: str | None, model: str
) -> dict:
    """Copy a captured request with the transcript, CLAUDE.md slot, and model replaced."""
    if not lines:
        raise ValueError("build_request needs at least one transcript line")
    req = copy.deepcopy(stage)
    req["model"] = model
    req.pop("metadata", None)
    blocks = req["messages"][-1]["content"]
    start = _find_marker(blocks, "<transcript>")
    end = _find_marker(blocks, "</transcript>")
    between = blocks[start + 1 : end]
    if not between:
        raise ValueError("capture format drift: no transcript blocks between the markers")
    # block A = history (all but the last line), block B = the action under review;
    # cache_control is copied from the captured blocks (absent if the capture had none).
    injected = []
    if len(lines) > 1:
        block = {"type": "text", "text": "".join(line + "\n" for line in lines[:-1])}
        if between[0].get("cache_control") is not None:
            block["cache_control"] = between[0]["cache_control"]
        injected.append(block)
    block = {"type": "text", "text": lines[-1] + "\n"}
    if between[-1].get("cache_control") is not None:
        block["cache_control"] = between[-1]["cache_control"]
    injected.append(block)
    req["messages"][-1]["content"] = blocks[: start + 1] + injected + blocks[end:]
    for msg in req["messages"]:
        if claude_md is None:
            msg["content"] = [
                b for b in msg["content"] if PLACEHOLDER not in (_block_text(b, "block") or "")
            ]
        else:
            for b in msg["content"]:
                text = _block_text(b, "block")
                if text is not None and "  " + PLACEHOLDER in text:
                    indented = "\n".join("  " + l for l in claude_md.split("\n"))
                    b["text"] = text.replace("  " + PLACEHOLDER, indented)
    req["messages"] = [m for m in req["messages"] if m["content"]]
    return req
