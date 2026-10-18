"""Case loading and validation: JSONL parsing, schema checks, safe hostnames."""
import ipaddress
import json
import re
from collections import Counter

CATEGORIES: tuple[str, ...] = (
    "destructive", "exfiltration", "credentials", "persistence", "supply-chain",
    "cloud-infra", "git-history", "scope-creep", "relayed-injection",
    "obfuscation", "consent", "benign",
)
LABELS = ("block", "allow", "judgment")
READ_ONLY = ("Read", "Grep", "Glob")

_OK_HOSTS = {
    "localhost", "example.com", "example.net", "example.org",
    "github.com", "api.github.com", "pypi.org", "registry.npmjs.org",
}
_OK_SUFFIXES = (".example", ".invalid", ".test")
_OK_NETS = tuple(
    ipaddress.ip_network(n)
    for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "127.0.0.0/8")
)

_URL_HOST = re.compile(r"[a-z][a-z0-9+.-]*://([\w.-]+)", re.I)
_USER_HOST = re.compile(r"[\w+-]+@([\w-]+(?:\.[\w-]+)+)")
_CMD_HOST = re.compile(
    r"\b(?:nc|ssh|scp|curl|wget|ping|telnet)\s+([\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,})\b")
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def load_cases(paths: list[str]) -> list[dict]:
    """Read JSONL case files, skipping blank lines; adds _file/_line to each case."""
    cases = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    case = json.loads(line)
                except json.JSONDecodeError as e:
                    raise ValueError(f"{path}:{n}: {e.msg}") from None
                if not isinstance(case, dict):
                    raise ValueError(f"{path}:{n}: expected a JSON object")
                case["_file"] = path
                case["_line"] = n
                cases.append(case)
    return cases


def hosts_in(text: str) -> set[str]:
    """Hosts referenced in text: URL hosts, user@host, bare host after net commands, IPv4."""
    hosts = (set(_URL_HOST.findall(text)) | set(_USER_HOST.findall(text))
             | set(_CMD_HOST.findall(text)))
    for ip in _IPV4.findall(text):
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            continue
        hosts.add(ip)
    return hosts


def host_ok(host: str) -> bool:
    """True for reserved test hosts, the benign allowlist, and documentation/loopback IPs."""
    h = host.lower().strip(".")
    if h in _OK_HOSTS or h.endswith(_OK_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return any(ip in net for net in _OK_NETS)


def _key(case: dict) -> str:
    cid = case.get("id")
    if isinstance(cid, str) and cid:
        return cid
    return f"{case.get('_file', '?')}:{case.get('_line', '?')}"


def _check(case: dict, dup: bool, by_id: dict[str, dict]) -> list[str]:
    problems = []

    def add(msg: str) -> None:
        problems.append(f"{_key(case)}: {msg}")

    cid = case.get("id")
    if not (isinstance(cid, str) and cid):
        add("missing id")
    elif dup:
        add("duplicate id")
    if case.get("category") not in CATEGORIES:
        add(f"unknown category {case.get('category')!r}")
    if case.get("label") not in LABELS:
        add(f"bad label {case.get('label')!r}")
    user = case.get("user")
    if not (isinstance(user, list) and user
            and all(isinstance(u, str) and u.strip() for u in user)):
        add("user must be a non-empty list of messages")
    actions = case.get("actions")
    if not (isinstance(actions, list) and actions):
        add("empty actions")
    elif not all(isinstance(a, dict) and a.get("tool") and "input" in a for a in actions):
        add("every action needs tool and input")
    elif actions[-1]["tool"] in READ_ONLY:
        add(f"last action is read-only ({actions[-1]['tool']})")
    pair = case.get("pair")
    if pair:
        twin = by_id.get(pair)
        if twin is None:
            add(f"pair {pair!r} not found")
        elif twin.get("label") == case.get("label"):
            add(f"pair {pair!r} has the same label")
    text = json.dumps({k: v for k, v in case.items() if not k.startswith("_")},
                      ensure_ascii=False)
    for host in sorted(hosts_in(text)):
        if not host_ok(host):
            add(f"non-reserved host {host!r}")
    return problems


def validate(cases: list[dict]) -> list[str]:
    """Every schema/pair/hostname problem in the set, each prefixed with the case id."""
    ids = [c["id"] for c in cases if isinstance(c.get("id"), str) and c["id"]]
    counts = Counter(ids)
    by_id = {c["id"]: c for c in cases if c.get("id") in counts}
    return [p for c in cases
            for p in _check(c, counts.get(c.get("id"), 0) > 1, by_id)]
