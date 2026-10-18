"""Case → classifier transcript lines, Claude Code's format and exclusions."""
import json

from automode_bench.cases import READ_ONLY


def _dumps(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False)


def render(case: dict, read_only: tuple[str, ...] = READ_ONLY) -> list[str]:
    """One {"user": msg} line per user message, then {tool: rendered} per non-read-only action."""
    lines = [_dumps({"user": msg}) for msg in case.get("user", [])]
    for action in case.get("actions", []):
        if action["tool"] in read_only:
            continue
        tool_input = action["input"]
        rendered = tool_input["command"] if action["tool"] == "Bash" else tool_input
        lines.append(_dumps({action["tool"]: rendered}))
    return lines
