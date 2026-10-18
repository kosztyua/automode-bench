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
# Input keys transcript.render reads for each tool.
_NEEDS = {"Bash": ("command",), "Edit": ("file_path", "old_string", "new_string"), "Write": ("file_path", "content")}

_OK_HOSTS = {
    "localhost", "example.com", "example.net", "example.org",
    "github.com", "api.github.com", "pypi.org", "registry.npmjs.org",
}
_OK_SUFFIXES = (".example", ".invalid", ".test")
_OK_NETS = tuple(
    ipaddress.ip_network(n)
    for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "127.0.0.0/8",
              "0.0.0.0/32", "::1/128", "2001:db8::/32")
)

_NET_CMDS = frozenset((
    "curl", "wget", "nc", "ncat", "netcat", "ssh", "scp", "sftp", "rsync",
    "ping", "telnet", "dig", "host", "nslookup", "ftp",
))
_SCHEME_HOST = re.compile(r"[a-z][a-z0-9+.-]*://(\[[^\]]+\]|[\w.-]+)", re.I)
_USER_HOST = re.compile(r"[\w.+-]+@(\[[^\]]+\]|[\w-]+(?:\.[\w-]+)+)")
_BRACKET = re.compile(r"\[([0-9a-fA-F:.]{3,})\]")
_DEV_TCP = re.compile(r"/dev/(?:tcp|udp)/([^/\s]+)/")
_SOCKET = re.compile(r"(?:create_connection|connect)\(\s*\(\s*['\"]([^'\"]+)['\"]")
_SEGMENT = re.compile(r"\|\||&&|\$\(|[;\n|`]")
_LABEL = re.compile(r"[A-Za-z]{2,}$")
# Secret-shaped strings: secret scanners (e.g. GitHub push protection) reject them even when fake.
_SECRET = re.compile(r"sk_(?:test|live)_[0-9A-Za-z]{16,}|npm_[0-9A-Za-z]{30,}|gh[pousr]_[0-9A-Za-z]{30,}"
                     r"|AKIA[0-9A-Z]{16}|xox[baprs]-[0-9A-Za-z-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
                     r"|AIza[0-9A-Za-z_-]{35}|glpat-[0-9A-Za-z_-]{20}")
_FILE_EXT = frozenset(
    "txt json sh py gz tgz tar zip log md pem key csv yml yaml conf html js toml ini cfg out bin"
    .split()
)


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


def _as_host(cand: str) -> str | None:
    """Normalize a candidate (strip brackets/quotes/user@/port/path) and keep it only if it is an IP or a dotted domain."""
    t = cand.strip("[]'\" ")
    if "@" in t:
        t = t.rsplit("@", 1)[1]
    t = t.split("/", 1)[0]
    if t.count(":") == 1:
        t = t.rsplit(":", 1)[0]
    t = t.lower().rstrip(".")
    if not t:
        return None
    try:
        ipaddress.ip_address(t)
        return t
    except ValueError:
        pass
    # ponytail: decimal/hex IP forms (ping 2130706433) are not detected.
    if "." in t and re.fullmatch(r"[\w.-]+", t) and _LABEL.search(t):
        return t
    return None


def hosts_in(text: str) -> set[str]:
    """Hosts referenced in text, found only in positions where a host can appear:
    URL/bracket/user@/dev-tcp/socket-call hosts and bare tokens of net-command segments.
    A candidate counts if it is an IP or has a dot with a letters-only last label."""
    hosts = set()
    for pat in (_SCHEME_HOST, _USER_HOST, _BRACKET, _DEV_TCP, _SOCKET):
        for cand in pat.findall(text):
            if (h := _as_host(cand)):
                hosts.add(h)
    for seg in _SEGMENT.split(text):
        toks = seg.split()
        if not toks or toks[0].strip("'\"").rsplit("/", 1)[-1] not in _NET_CMDS:
            continue
        for t in toks[1:]:
            if t.startswith(("-", "@", "~", ".", "/")) or "://" in t:
                continue
            head, _, last = t.rpartition(".")
            if head and last.lower() in _FILE_EXT:  # file name, not a host
                continue
            if (h := _as_host(t)):
                hosts.add(h)
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
    elif bad := [f"{a['tool']} needs {k}" for a in actions for k in _NEEDS.get(a["tool"], ())
                 if not isinstance(a["input"], dict) or not isinstance(a["input"].get(k), str)]:
        add("; ".join(bad))
    elif actions[-1]["tool"] in READ_ONLY:
        add(f"last action is read-only ({actions[-1]['tool']})")
    pair = case.get("pair")
    if pair:
        if not isinstance(pair, str):
            add(f"pair {pair!r} is not a string")
        elif case.get("label") == "judgment":
            add("judgment case may not have a pair")
        else:
            twin = by_id.get(pair)
            if twin is None:
                add(f"pair {pair!r} not found")
            elif {case.get("label"), twin.get("label")} != {"block", "allow"}:
                add(f"pair {pair!r} must be a block/allow twin")
    text = json.dumps({k: v for k, v in case.items() if not k.startswith("_")},
                      ensure_ascii=False)
    if "<transcript>" in text or "</transcript>" in text:
        add("case text contains a <transcript> marker")
    for m in sorted(set(_SECRET.findall(text))):
        add(f"secret-shaped string {m[:12]}... (use a short FAKE placeholder)")
    hosts = set()
    for value in _strings(case):
        hosts |= hosts_in(value)
    for host in sorted(hosts):
        if not host_ok(host):
            add(f"non-reserved host {host!r}")
    return problems


def _strings(obj):
    """Every string in a nested case structure (skipping _file/_line metadata keys)."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if not (isinstance(k, str) and k.startswith("_")):
                yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def validate(cases: list[dict]) -> list[str]:
    """Every schema/pair/hostname problem in the set, each prefixed with the case id."""
    ids = [c["id"] for c in cases if isinstance(c.get("id"), str) and c["id"]]
    counts = Counter(ids)
    by_id = {c["id"]: c for c in cases
             if isinstance(c.get("id"), str) and c["id"] in counts}
    return [p for c in cases
            for p in _check(c, isinstance(c.get("id"), str) and counts[c["id"]] > 1,
                            by_id)]
