"""argparse CLI: capture, run, report, cases."""
import argparse
import glob
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

from automode_bench import backends, capture as capture_mod, cases as cases_mod
from automode_bench import prompts, report, runner


def _load_cases(pattern: str) -> tuple[list[str], list[dict], list[str]]:
    """(paths, cases, problems); problems non-empty means nothing should proceed."""
    paths = sorted(glob.glob(pattern))
    if not paths:
        return [], [], [f"no cases matched {pattern!r}"]
    try:
        loaded = cases_mod.load_cases(paths)
    except ValueError as e:
        return paths, [], [str(e)]
    if not loaded:
        return paths, [], [f"no cases matched {pattern!r}"]
    return paths, loaded, cases_mod.validate(loaded)


def _print_problems(problems: list[str]) -> None:
    for p in problems:
        print(p, file=sys.stderr)


def _cmd_capture(args: argparse.Namespace) -> int:
    try:
        out = capture_mod.run_capture(args.out_root, args.claude, args.timeout)
    except (RuntimeError, OSError) as e:
        print(str(e), file=sys.stderr)
        return 2
    print(out)
    with open(os.path.join(out, "format.json"), encoding="utf-8") as f:
        fmt = json.load(f)
    for check in ("render_ok", "claude_md_removed_ok"):
        if fmt.get(check) is False:
            print(f"WARNING: {check} is false — Claude Code format may have drifted",
                  file=sys.stderr)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    if args.think and args.backend != "anthropic":
        print("--think is only supported with --backend anthropic", file=sys.stderr)
        return 2
    try:
        cap = prompts.load_capture(args.capture or prompts.capture_dir())
        if not cap["format"].get("claude_code"):
            raise ValueError("capture format.json lacks claude_code; run capture again")
    except (OSError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 2
    if any(cap["format"].get(c) is False
           for c in ("render_ok", "claude_md_removed_ok")):
        print("WARNING: capture format checks failed; run capture again", file=sys.stderr)
    paths, cases, problems = _load_cases(args.cases)
    if problems:
        _print_problems(problems)
        return 2
    key = os.environ.get(args.key_env) or "local"

    def call_fn(request: dict) -> dict:
        if args.think:
            request["thinking"] = {"type": "enabled",
                                   "budget_tokens": min(4096, request["max_tokens"] // 2)}
        return backends.call(args.backend, args.url, request, key)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = args.out or os.path.join(
        "results", f"{ts}-{args.backend}-{re.sub(r'[^A-Za-z0-9._-]', '-', args.model)}.jsonl")
    header = {
        "backend": args.backend,
        "url_host": urlparse(args.url).hostname or "",
        "model": args.model,
        "claude_code": cap["format"]["claude_code"],
        "cases_hash": _cases_hash(paths),
        "started": ts,
        "flags": {"conc": args.conc, "think": args.think, "cases": args.cases},
    }
    rows = runner.run(cases, cap, call_fn, args.model, args.conc)
    runner.write_results(out, header, rows)
    print(runner.summary(rows))
    print(f"\nwrote {len(rows)} rows to {out}")
    return 0


def _cases_hash(paths: list[str]) -> str:
    h = hashlib.sha256()
    for p in paths:
        with open(p, "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:12]


def _cmd_report(args: argparse.Namespace) -> int:
    cases = None
    if args.cases is not None:
        _, cases, problems = _load_cases(args.cases)
        if problems:
            _print_problems(problems)
            return 2
    try:
        text = report.render(args.files, cases)
    except (OSError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 2
    print(text)
    return 0


def _cmd_cases(args: argparse.Namespace) -> int:
    _, cases, problems = _load_cases(args.cases)
    if problems:
        _print_problems(problems)
        return 2
    print(f"{len(cases)} cases")
    for cat in cases_mod.CATEGORIES:
        cs = [c for c in cases if c.get("category") == cat]
        if cs:
            print(f"  {cat}: " + " ".join(
                f"{lab} {sum(1 for c in cs if c.get('label') == lab)}"
                for lab in cases_mod.LABELS))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m automode_bench",
                                description="Score a model as Claude Code's auto-mode classifier.")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("capture", help="capture this Claude Code version's classifier prompts")
    c.add_argument("--claude", default="claude", help="claude binary to drive")
    c.add_argument("--timeout", type=int, default=120)
    c.add_argument("--out-root", default="~/.automode-bench")
    c.set_defaults(func=_cmd_capture)

    r = sub.add_parser("run", help="judge every case with a model, write results JSONL")
    r.add_argument("--backend", choices=("anthropic", "openai", "claude-cli"), required=True)
    r.add_argument("--url", default="http://localhost:8000")
    r.add_argument("--model", required=True)
    r.add_argument("--conc", type=int, default=4)
    r.add_argument("--cases", default="cases/*.jsonl")
    r.add_argument("--key-env", default="AUTOMODE_BENCH_KEY",
                   help="env var holding the API key (falls back to 'local')")
    r.add_argument("--capture", default=None,
                   help="capture dir (default: newest under ~/.automode-bench)")
    r.add_argument("--out", default=None)
    r.add_argument("--think", action="store_true", help="add thinking to the request")
    r.set_defaults(func=_cmd_run)

    rep = sub.add_parser("report", help="Markdown comparison of results files")
    rep.add_argument("files", nargs="+")
    rep.add_argument("--cases", default=None, help="case glob, enables the pairs section")
    rep.set_defaults(func=_cmd_report)

    ca = sub.add_parser("cases", help="validate the case set and print counts")
    ca.add_argument("--cases", default="cases/*.jsonl")
    ca.set_defaults(func=_cmd_cases)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
