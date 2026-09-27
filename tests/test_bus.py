"""agent-bus tests: no live model needed. Run: python3 -m unittest discover -s tests"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import bus  # noqa: E402


class BusCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def send(self, agent, body, channel="ch", **kw):
        return bus.send(self.root, channel, agent, f"{agent}-harness", body, **kw)


class SendTests(BusCase):
    def test_ids_are_monotonic_and_gapless(self):
        seqs = [self.send("alpha", f"m{i}")["seq"] for i in range(3)]
        self.assertEqual(seqs, [1, 2, 3])
        first = self.send("beta", "hello")
        self.assertEqual(first["seq"], 4)
        self.assertEqual(first["id"], "ch:00000004")
        self.assertEqual(first["harness"], "beta-harness")

    def test_concurrent_senders_never_share_or_skip_ids(self):
        errors = []

        def worker(name):
            try:
                for i in range(15):
                    self.send(name, f"{name} {i}")
            except Exception as err:  # pragma: no cover - failure detail
                errors.append(err)

        threads = [threading.Thread(target=worker, args=(f"agent{n}",)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        msgs = bus.recv(self.root, "ch", "observer", include_own=True, after=0)
        self.assertEqual([m["seq"] for m in msgs], list(range(1, 61)))

    def test_empty_body_and_bad_names_are_refused(self):
        with self.assertRaises(bus.BusError):
            self.send("alpha", "   ")
        with self.assertRaises(bus.BusError):
            bus.send(self.root, "ch", "", "h", "x")
        with self.assertRaises(bus.BusError):
            bus.send(self.root, "../evil", "alpha", "h", "x")
        with self.assertRaises(bus.BusError):
            bus.send(self.root, "ch", "alpha", "h", "x", kind="weird")

    def test_secret_tripwire_blocks_and_can_be_overridden(self):
        token = "ghp_" + "a" * 36
        with self.assertRaises(bus.BusError):
            self.send("alpha", f"use {token} please")
        with self.assertRaises(bus.BusError):
            self.send("alpha", "-----BEGIN RSA PRIVATE KEY-----")
        rec = self.send("alpha", f"use {token} please", allow_secret_like=True)
        self.assertEqual(rec["seq"], 1)


class RecvTests(BusCase):
    def test_recv_hides_own_and_dedupes(self):
        self.send("alpha", "from alpha")
        self.send("beta", "from beta")
        got = bus.recv(self.root, "ch", "alpha")
        self.assertEqual([m["body"] for m in got], ["from beta"])
        self.assertEqual(bus.recv(self.root, "ch", "alpha"), [])  # de-duped

    def test_own_messages_are_consumed_silently(self):
        self.send("alpha", "mine")
        self.assertEqual(bus.recv(self.root, "ch", "alpha"), [])
        self.send("beta", "yours")
        got = bus.recv(self.root, "ch", "alpha")
        self.assertEqual([m["body"] for m in got], ["yours"])

    def test_each_agent_has_an_independent_cursor(self):
        self.send("alpha", "hello")
        self.assertEqual(len(bus.recv(self.root, "ch", "beta")), 1)
        self.assertEqual(len(bus.recv(self.root, "ch", "gamma")), 1)
        self.assertEqual(bus.recv(self.root, "ch", "beta"), [])

    def test_peek_does_not_advance_the_cursor(self):
        self.send("alpha", "hello")
        self.assertEqual(len(bus.recv(self.root, "ch", "beta", peek=True)), 1)
        self.assertEqual(len(bus.recv(self.root, "ch", "beta")), 1)

    def test_after_reads_history_without_moving_the_cursor(self):
        self.send("alpha", "one")
        self.send("beta", "two")
        bus.recv(self.root, "ch", "beta")  # advance beta's cursor to the end
        replay = bus.recv(self.root, "ch", "beta", after=0, include_own=True)
        self.assertEqual([m["body"] for m in replay], ["one", "two"])
        self.assertEqual(bus.recv(self.root, "ch", "beta"), [])  # cursor untouched

    def test_recv_does_not_advance_cursor_past_a_gap(self):
        # Claiming is max+1, so a hole should only appear if listing races a
        # concurrent send. Recv must still stop at the first missing seq.
        self.send("alpha", "one")
        later = {
            "seq": 3, "id": "ch:00000003", "ts": bus._now_iso(),
            "channel": "ch", "agent": "beta", "harness": "h",
            "kind": "message", "body": "three",
        }
        (self.root / "ch" / "messages" / "00000003.json").write_text(
            json.dumps(later) + "\n", encoding="utf-8")
        got = bus.recv(self.root, "ch", "gamma")
        self.assertEqual([m["seq"] for m in got], [1])
        (self.root / "ch" / "messages" / "00000002.json").write_text(
            json.dumps({**later, "seq": 2, "id": "ch:00000002", "body": "two"})
            + "\n", encoding="utf-8")
        got = bus.recv(self.root, "ch", "gamma")
        self.assertEqual([(m["seq"], m["body"]) for m in got],
                         [(2, "two"), (3, "three")])

    def test_wait_returns_when_a_message_arrives(self):
        def later():
            time.sleep(0.2)
            self.send("beta", "here")

        t = threading.Thread(target=later)
        t.start()
        got = bus.recv(self.root, "ch", "alpha", wait=True, timeout=5, poll=0.05)
        t.join()
        self.assertEqual([m["body"] for m in got], ["here"])

    def test_wait_times_out_with_an_empty_list(self):
        start = time.monotonic()
        got = bus.recv(self.root, "ch", "alpha", wait=True, timeout=0.3, poll=0.05)
        self.assertEqual(got, [])
        self.assertLess(time.monotonic() - start, 2.0)


class DoneTests(BusCase):
    def test_complete_needs_every_participant(self):
        self.send("alpha", "work")
        self.send("beta", "reply")
        first = bus.post_done(self.root, "ch", "alpha", "h1", "pass", "did the thing")
        self.assertFalse(first["complete"])
        self.assertFalse((self.root / "ch" / "VERDICT.json").exists())
        second = bus.post_done(self.root, "ch", "beta", "h2", "PASS", "confirmed")
        self.assertTrue(second["complete"])
        self.assertEqual(second["verdict"], "PASS")
        verdict = json.loads((self.root / "ch" / "VERDICT.json").read_text(encoding="utf-8"))
        self.assertTrue(verdict["complete"])
        self.assertEqual(verdict["verdict"], "PASS")
        self.assertEqual(len(verdict["participants"]), 2)

    def test_done_is_visible_to_the_peer_as_a_message(self):
        self.send("alpha", "work")
        bus.recv(self.root, "ch", "beta")
        bus.post_done(self.root, "ch", "alpha", "h1", "PASS", "wrapped up")
        got = bus.recv(self.root, "ch", "beta")
        self.assertEqual([m["kind"] for m in got], ["done"])
        self.assertIn("DONE PASS", got[0]["body"])

    def test_one_fail_makes_the_shared_verdict_fail(self):
        self.send("alpha", "work")
        self.send("beta", "reply")
        bus.post_done(self.root, "ch", "alpha", "h1", "PASS", "fine by me")
        result = bus.post_done(self.root, "ch", "beta", "h2", "FAIL", "tests still red")
        self.assertTrue(result["complete"])
        self.assertEqual(result["verdict"], "FAIL")

    def test_done_validates_verdict_and_summary(self):
        with self.assertRaises(bus.BusError):
            bus.post_done(self.root, "ch", "alpha", "h1", "MAYBE", "hmm")
        with self.assertRaises(bus.BusError):
            bus.post_done(self.root, "ch", "alpha", "h1", "PASS", "  ")

    def test_status_counts_and_participants(self):
        self.send("alpha", "one")
        self.send("beta", "two")
        self.send("alpha", "three")
        st = bus.status(self.root, "ch")
        self.assertEqual(st["message_count"], 3)
        self.assertEqual(st["last_seq"], 3)
        self.assertEqual([p["agent"] for p in st["participants"]], ["alpha", "beta"])
        self.assertEqual(st["participants"][0]["messages"], 2)
        self.assertFalse(st["complete"])
        self.assertIsNone(st["verdict"])

    def test_new_participant_drops_a_stale_verdict(self):
        self.send("alpha", "work")
        self.send("beta", "reply")
        bus.post_done(self.root, "ch", "alpha", "h1", "PASS", "a")
        bus.post_done(self.root, "ch", "beta", "h2", "PASS", "b")
        self.assertTrue((self.root / "ch" / "VERDICT.json").exists())
        self.send("alpha", "still chatting")
        self.assertTrue((self.root / "ch" / "VERDICT.json").exists())
        self.assertTrue(bus.status(self.root, "ch")["complete"])
        self.send("gamma", "third joins")
        self.assertFalse(bus.status(self.root, "ch")["complete"])
        self.assertFalse((self.root / "ch" / "VERDICT.json").exists())
        self.assertFalse(bus.list_channels(self.root)[0]["complete"])


class WakeTests(BusCase):
    """Two fake agents; beta's wake command appends one line to a file."""

    def setUp(self):
        super().setUp()
        self.woke = self.root / "woke.txt"
        self.cmd = f"printf 'woke %s %s\\n' \"$AGENT_BUS_AGENT\" \"$AGENT_BUS_CHANNEL\" >> {self.woke}"

    def woke_lines(self):
        return self.woke.read_text().splitlines() if self.woke.exists() else []

    def wait_for_lines(self, n, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.woke_lines()) >= n:
                return self.woke_lines()
            time.sleep(0.05)
        return self.woke_lines()

    def sweep(self, **kw):
        kw.setdefault("cooldown", 120)
        kw.setdefault("idle", 60)
        return bus.sweep_wakes(self.root, wait=True, **kw)

    def log_lines(self):
        path = self.root / "ch" / "wake" / "log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_register_send_sweep_wakes_once_and_cooldown_holds(self):
        reg = bus.register_wake(self.root, "ch", "beta", "beta-harness", self.cmd)
        self.assertEqual(reg["last_wake_seq"], 0)
        sent = self.send("alpha", "are you there?")
        self.assertEqual(sent["wake_registered"], ["beta"])
        self.assertNotIn("woken", sent)

        launched = self.sweep()
        self.assertEqual([(w["agent"], w["seq"], w["exit_code"]) for w in launched],
                         [("beta", 1, 0)])
        self.assertEqual(self.woke_lines(), ["woke beta ch"])  # identity comes from the registration
        events = self.log_lines()
        self.assertEqual([e["event"] for e in events], ["launch", "exit"])
        self.assertEqual(events[1]["seq"], 1)
        self.assertEqual(events[1]["exit_code"], 0)
        self.assertEqual(events[0]["command"], self.cmd)

        self.send("alpha", "still there?")
        self.assertEqual(self.sweep(), [])  # cooldown holds
        self.assertEqual(self.woke_lines(), ["woke beta ch"])
        self.assertEqual(self.sweep(), [])  # and a second sweep does not re-fire for the same seq

        self.send("alpha", "third")
        launched = self.sweep(cooldown=0)  # cooldown over, a newer message: wake again
        self.assertEqual([w["seq"] for w in launched], [3])
        self.assertEqual(len(self.woke_lines()), 2)
        self.assertEqual(self.sweep(cooldown=0), [])  # once per seq, even with no cooldown

    def test_done_clears_the_registration_unless_kept(self):
        bus.register_wake(self.root, "ch", "beta", "beta-harness", self.cmd)
        self.send("beta", "hello")
        self.assertEqual(bus.status(self.root, "ch")["wakes"], ["beta"])
        result = bus.post_done(self.root, "ch", "beta", "beta-harness", "PASS", "kept", keep_wake=True)
        self.assertFalse(result["wake_cleared"])
        self.assertIsNotNone(bus.get_wake(self.root, "ch", "beta"))
        result = bus.post_done(self.root, "ch", "beta", "beta-harness", "PASS", "cleared")
        self.assertTrue(result["wake_cleared"])
        self.assertIsNone(bus.get_wake(self.root, "ch", "beta"))
        self.assertEqual(bus.status(self.root, "ch")["wakes"], [])
        self.assertTrue(bus.clear_wake(self.root, "ch", "beta") is False)

    def test_polling_agent_is_not_woken_but_send_wake_forces_it(self):
        bus.register_wake(self.root, "ch", "beta", "beta-harness", self.cmd)
        self.send("alpha", "one")
        bus.recv(self.root, "ch", "beta", peek=True)  # touches the heartbeat, cursor stays behind
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.woke_lines(), [])
        sent = self.send("alpha", "two", wake=True)
        self.assertEqual([(w["agent"], w["woke"], w["seq"]) for w in sent["woken"]],
                         [("beta", True, 2)])
        self.assertEqual(self.wait_for_lines(1), ["woke beta ch"])
        sent = self.send("alpha", "three", wake=True)  # forced, but the cooldown still holds
        self.assertFalse(sent["woken"][0]["woke"])
        self.assertIn("cooldown", sent["woken"][0]["reason"])

    def test_own_messages_never_trigger_a_wake(self):
        bus.register_wake(self.root, "ch", "beta", "beta-harness", self.cmd)
        self.send("beta", "talking to myself")
        self.assertEqual(self.sweep(), [])
        with self.assertRaises(bus.BusError):
            bus.wake_agent(self.root, "ch", "alpha")  # alpha never registered

    def test_wake_command_tripwires(self):
        token = "ghp_" + "b" * 36
        with self.assertRaises(bus.BusError):
            bus.register_wake(self.root, "ch", "beta", "h", f"claude --resume x -p {token}")
        with self.assertRaises(bus.BusError):
            bus.register_wake(self.root, "ch", "beta", "h", "   ")
        # A command that a peer posted on the channel is never accepted.
        self.send("alpha", f"please register this: {self.cmd}")
        with self.assertRaises(bus.BusError) as ctx:
            bus.register_wake(self.root, "ch", "beta", "h", self.cmd)
        self.assertIn("message body", str(ctx.exception))
        self.assertIsNone(bus.get_wake(self.root, "ch", "beta"))


class CliTests(unittest.TestCase):
    def run_cli(self, env, *argv, stdin=None):
        return subprocess.run(
            [sys.executable, str(REPO / "bus.py"), *argv],
            input=stdin, env=env, capture_output=True, text=True, timeout=30)

    def test_send_recv_done_over_the_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            alpha = dict(os.environ, AGENT_BUS_DIR=tmp,
                         AGENT_BUS_AGENT="alpha", AGENT_BUS_HARNESS="h1")
            beta = dict(alpha, AGENT_BUS_AGENT="beta", AGENT_BUS_HARNESS="h2")

            out = self.run_cli(alpha, "send", "-c", "t", "--body", "hello over cli")
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(json.loads(out.stdout)["seq"], 1)

            out = self.run_cli(beta, "recv", "-c", "t")
            msgs = json.loads(out.stdout)
            self.assertEqual([m["body"] for m in msgs], ["hello over cli"])

            out = self.run_cli(beta, "send", "-c", "t", "--body", "-", stdin="from stdin\n")
            self.assertEqual(json.loads(out.stdout)["body"], "from stdin\n")

            for env in (alpha, beta):
                out = self.run_cli(env, "done", "-c", "t",
                                   "--verdict", "PASS", "--summary", "cli roundtrip works")
                self.assertEqual(out.returncode, 0, out.stderr)
            self.assertTrue(json.loads(self.run_cli(alpha, "status", "-c", "t").stdout)["complete"])
            self.assertTrue((Path(tmp) / "t" / "VERDICT.json").exists())

    def test_concurrent_processes_never_share_or_skip_ids(self):
        # Threads under the GIL barely contend at the os.link call; real
        # processes exercise the cross-process race the hard link defends.
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, AGENT_BUS_DIR=tmp, AGENT_BUS_HARNESS="h")
            procs = [
                subprocess.Popen(
                    [sys.executable, str(REPO / "bus.py"), "send", "-c", "race",
                     "-a", f"proc{n}", "--body", f"from process {n}"],
                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for n in range(12)
            ]
            for p in procs:
                _, err = p.communicate(timeout=60)
                self.assertEqual(p.returncode, 0, err)
            msgs = bus.recv(Path(tmp), "race", "observer", include_own=True, after=0)
            self.assertEqual([m["seq"] for m in msgs], list(range(1, 13)))
            self.assertEqual(len({m["agent"] for m in msgs}), 12)

    def test_join_send_watch_wake_once_over_the_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            woke = Path(tmp) / "woke.txt"
            alpha = dict(os.environ, AGENT_BUS_DIR=tmp,
                         AGENT_BUS_AGENT="alpha", AGENT_BUS_HARNESS="h1")
            beta = dict(alpha, AGENT_BUS_AGENT="beta", AGENT_BUS_HARNESS="h2")
            out = self.run_cli(beta, "join", "-c", "t", "--wake", f"echo resumed >> {woke}")
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(json.loads(out.stdout)["harness"], "h2")
            out = self.run_cli(alpha, "send", "-c", "t", "--body", "ping")
            self.assertEqual(json.loads(out.stdout)["wake_registered"], ["beta"])
            out = self.run_cli(alpha, "watch-wake", "--once")
            self.assertEqual(out.returncode, 0, out.stderr)
            launched = json.loads(out.stdout)
            self.assertEqual([(w["agent"], w["exit_code"]) for w in launched], [("beta", 0)])
            self.assertEqual(woke.read_text(), "resumed\n")
            out = self.run_cli(alpha, "watch-wake", "--once")
            self.assertEqual(json.loads(out.stdout), [])  # cooldown
            out = self.run_cli(alpha, "wake", "-c", "t", "beta")
            self.assertFalse(json.loads(out.stdout)["woke"])
            out = self.run_cli(beta, "join", "-c", "t", "--clear")
            self.assertTrue(json.loads(out.stdout)["cleared"])
            out = self.run_cli(alpha, "wake", "-c", "t", "beta")
            self.assertEqual(out.returncode, 2)
            self.assertIn("no wake registration", out.stderr)

    def test_missing_identity_is_a_clean_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith("AGENT_BUS_")}
            env["AGENT_BUS_DIR"] = tmp
            out = self.run_cli(env, "send", "-c", "t", "--body", "x")
            self.assertEqual(out.returncode, 2)
            self.assertIn("identity required", out.stderr)


class McpTests(unittest.TestCase):
    def test_initialize_list_call_over_stdio(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, AGENT_BUS_DIR=tmp,
                       AGENT_BUS_AGENT="mcp-a", AGENT_BUS_HARNESS="mcp-h")
            requests = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                            "clientInfo": {"name": "test", "version": "0"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "bus_send", "arguments": {"channel": "m", "body": "over mcp"}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                 "params": {"name": "bus_recv",
                            "arguments": {"channel": "m", "include_own": True, "after": 0}}},
                {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                 "params": {"name": "bus_done", "arguments": {"channel": "m", "verdict": "MAYBE",
                                                              "summary": "bad verdict"}}},
                {"jsonrpc": "2.0", "id": 6, "method": "nonsense/method"},
            ]
            payload = "".join(json.dumps(r) + "\n" for r in requests)
            proc = subprocess.run([sys.executable, str(REPO / "mcp_server.py")],
                                  input=payload, env=env, capture_output=True,
                                  text=True, timeout=30)
            replies = {}
            for line in proc.stdout.splitlines():
                if line.strip():
                    msg = json.loads(line)
                    replies[msg["id"]] = msg

            self.assertEqual(replies[1]["result"]["protocolVersion"], "2025-06-18")
            self.assertEqual(replies[1]["result"]["serverInfo"]["name"], "agent-bus")
            names = [t["name"] for t in replies[2]["result"]["tools"]]
            self.assertEqual(names, ["bus_send", "bus_recv", "bus_done",
                                     "bus_status", "bus_channels",
                                     "bus_register_wake", "bus_wake"])
            sent = json.loads(replies[3]["result"]["content"][0]["text"])
            self.assertEqual(sent["seq"], 1)
            self.assertFalse(replies[3]["result"]["isError"])
            got = json.loads(replies[4]["result"]["content"][0]["text"])
            self.assertEqual([m["body"] for m in got], ["over mcp"])
            self.assertTrue(replies[5]["result"]["isError"])
            self.assertIn("verdict", replies[5]["result"]["content"][0]["text"])
            self.assertEqual(replies[6]["error"]["code"], -32601)
            self.assertNotIn(None, replies)  # the notification got no reply


if __name__ == "__main__":
    unittest.main()
