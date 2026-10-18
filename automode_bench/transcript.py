"""Case → classifier transcript lines, Claude Code's format and exclusions."""
import json

from automode_bench.cases import READ_ONLY


def _dumps(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def render(case: dict, read_only: tuple[str, ...] = READ_ONLY) -> list[str]:
    """One {"user":msg} line per user message, then one line per non-read-only action.

    Claude Code renders Edit as {file_path, adds, removes, removesTruncated},
    Write as "path: content", Bash as "command"; other tools keep their input."""
    lines = [_dumps({"user": msg}) for msg in case.get("user", [])]
    for action in case.get("actions", []):
        tool = action["tool"]
        if tool in read_only:
            continue
        tool_input = action["input"]
        if tool == "Bash":
            rendered = tool_input["command"]
        elif tool == "Edit":
            rendered = {
                "file_path": tool_input["file_path"],
                "adds": tool_input["new_string"],
                "removes": tool_input["old_string"],
                "removesTruncated": False,
            }
        elif tool == "Write":
            rendered = f'{tool_input["file_path"]}: {tool_input["content"]}'
        else:
            rendered = tool_input
        lines.append(_dumps({tool: rendered}))
    return lines
