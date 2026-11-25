"""agent-bus tests: no live model needed. Run: python3 -m unittest discover -s tests"""

import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
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

    def test_an_explicit_wake_works_on_an_empty_channel_once(self):
        bus.register_wake(self.root, "ch", "beta", "beta-harness", self.cmd)
        first = bus.wake_agent(self.root, "ch", "beta", cooldown=0, wait=True)
        self.assertEqual((first["seq"], first["woke"], first["exit_code"]), (0, True, 0))
        self.assertEqual(self.woke_lines(), ["woke beta ch"])
        again = bus.wake_agent(self.root, "ch", "beta", cooldown=0)
        self.assertFalse(again["woke"])
        self.assertIn("already woke for seq 0", again["reason"])

    def test_concurrent_wakes_launch_once_per_seq(self):
        bus.register_wake(self.root, "ch", "beta", "beta-harness", self.cmd)
        self.send("alpha", "one")
        gate = threading.Barrier(6)
        results = []

        def racer():
            gate.wait()
            results.append(bus.wake_agent(self.root, "ch", "beta", seq=1, cooldown=0))

        threads = [threading.Thread(target=racer) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(r["woke"] for r in results), 1)
        self.assertEqual(self.wait_for_lines(1), ["woke beta ch"])

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
    def run_cli(self, env, *argv, stdin=None, cwd=None):
        # A neutral cwd, so no .agent-bus.toml above the repo leaks an identity in.
        return subprocess.run(
            [sys.executable, str(REPO / "bus.py"), *argv],
            input=stdin, env=env, capture_output=True, text=True, timeout=30,
            cwd=cwd or tempfile.gettempdir())

    def test_send_recv_done_over_the_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            alpha = dict(os.environ, AGENT_BUS_DIR=tmp,
                         AGENT_BUS_AGENT="alpha", AGENT_BUS_HARNESS="h1")
            beta = dict(alpha, AGENT_BUS_AGENT="beta", AGENT_BUS_HARNESS="h2")

            out = self.run_cli(alpha, "send", "-c", "t", "--body", "hello over cli", "--json")
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(json.loads(out.stdout)["seq"], 1)

            out = self.run_cli(beta, "recv", "-c", "t")
            msgs = json.loads(out.stdout)
            self.assertEqual([m["body"] for m in msgs], ["hello over cli"])

            out = self.run_cli(beta, "send", "-c", "t", "--body", "-", "--json",
                               stdin="from stdin\n")
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
            out = self.run_cli(alpha, "send", "-c", "t", "--body", "ping", "--json")
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
                                     "bus_register_wake", "bus_wake", "bus_verify_refs"])
            sent = json.loads(replies[3]["result"]["content"][0]["text"])
            self.assertEqual(sent["seq"], 1)
            self.assertFalse(replies[3]["result"]["isError"])
            got = json.loads(replies[4]["result"]["content"][0]["text"])
            self.assertEqual([m["body"] for m in got], ["over mcp"])
            self.assertTrue(replies[5]["result"]["isError"])
            self.assertIn("verdict", replies[5]["result"]["content"][0]["text"])
            self.assertEqual(replies[6]["error"]["code"], -32601)
            self.assertNotIn(None, replies)  # the notification got no reply

    def test_kinds_refs_phases_over_mcp(self):
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / "part.step"
            part.write_bytes(b"v1")
            env = dict(os.environ, AGENT_BUS_DIR=tmp, AGENT_BUS_CHANNEL="m",
                       AGENT_BUS_AGENT="mcp-a", AGENT_BUS_HARNESS="mcp-h")
            calls = [
                ("bus_send", {"body": "x", "kind": "done"}),
                ("bus_send", {"body": "which fillet?", "kind": "question", "for": "zohar",
                              "refs": [str(part)]}),
                ("bus_recv", {"kind": "question", "include_own": True}),
                ("bus_done", {"verdict": "PASS", "summary": "A ok", "phase": "A"}),
                ("bus_verify_refs", {"seq": 1}),
                ("bus_done", {"verdict": "PASS", "summary": "empty phase", "phase": ""}),
                ("bus_status", {}),
            ]
            payload = "".join(
                json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                            "params": {"name": name, "arguments": args}}) + "\n"
                for i, (name, args) in enumerate(calls, start=1))
            proc = subprocess.run([sys.executable, str(REPO / "mcp_server.py")],
                                  input=payload, env=env, capture_output=True,
                                  text=True, timeout=30)
            replies = {msg["id"]: msg["result"] for msg in map(json.loads, proc.stdout.splitlines())}
            self.assertTrue(replies[1]["isError"])  # done/phase only through bus_done
            sent = json.loads(replies[2]["content"][0]["text"])
            self.assertEqual((sent["seq"], sent["for"], sent["refs"][0]["sha256"]),
                             (1, "zohar", sha(b"v1")))
            got = json.loads(replies[3]["content"][0]["text"])
            self.assertEqual([m["kind"] for m in got], ["question"])
            self.assertTrue(json.loads(replies[4]["content"][0]["text"])["phase_complete"])
            self.assertEqual(json.loads(replies[5]["content"][0]["text"])[0]["status"], "unchanged")
            self.assertTrue(replies[6]["isError"])  # an empty phase is refused, not a final DONE
            st = json.loads(replies[7]["content"][0]["text"])
            self.assertEqual([p["done"] for p in st["participants"]], [None])
            self.assertTrue(Path(st["root"]).is_absolute())

    def test_a_relative_root_is_made_absolute(self):
        self.assertTrue(bus.resolve_root("rel/bus").is_absolute())


def wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LivenessTests(BusCase):
    def test_status_reports_heartbeat_age_and_silence(self):
        self.send("alpha", "hi")
        bus.recv(self.root, "ch", "beta")  # beta polls but never posts
        st = bus.status(self.root, "ch")
        alpha = st["participants"][0]
        self.assertIsNone(alpha["heartbeat_age_s"])
        self.assertFalse(alpha["watching"])
        self.assertFalse(alpha["silent"])  # it just sent
        self.assertEqual([r["agent"] for r in st["readers"]], ["beta"])
        self.assertLess(st["readers"][0]["heartbeat_age_s"], 5)
        self.assertFalse(st["readers"][0]["silent"])
        time.sleep(0.01)
        self.assertTrue(bus.status(self.root, "ch", silent_after=0)["participants"][0]["silent"])
        old = time.time() - 3600
        os.utime(self.root / "ch" / "heartbeats" / "beta", (old, old))
        beta = bus.status(self.root, "ch")["readers"][0]
        self.assertTrue(beta["silent"])
        self.assertGreater(beta["heartbeat_age_s"], 3000)


class WatchLockTests(BusCase):
    """A real `bus.py watch` in another process holds beta's lock."""

    def start_watch(self, agent="beta"):
        env = dict(os.environ, AGENT_BUS_DIR=str(self.root))
        proc = subprocess.Popen(
            [sys.executable, str(REPO / "bus.py"), "watch", "-c", "ch", "--as", agent,
             "--timeout", "30"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=tempfile.gettempdir())
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.communicate()))
        lock = self.root / "ch" / "watchers" / f"{agent}.json"
        self.assertTrue(wait_until(lock.exists), "watch never took its lock")
        return proc, lock

    def test_a_second_reader_is_refused_while_a_watch_is_live(self):
        proc, lock = self.start_watch()
        beta = bus.status(self.root, "ch")["readers"][0]
        self.assertEqual((beta["agent"], beta["watching"], beta["watch_pid"]),
                         ("beta", True, proc.pid))
        self.assertFalse(beta["silent"])
        with self.assertRaises(bus.BusError) as ctx:
            bus.recv(self.root, "ch", "beta")
        self.assertIn("silently ack", str(ctx.exception))
        with self.assertRaises(bus.BusError):
            bus.recv(self.root, "ch", "beta", wait=True, timeout=0.1)
        self.assertEqual(bus.recv(self.root, "ch", "beta", peek=True), [])  # never acks: allowed
        proc.terminate()  # SIGTERM clears the lock on the way out
        proc.communicate(timeout=10)
        self.assertFalse(lock.exists())
        self.assertFalse(bus.status(self.root, "ch")["readers"][0]["watching"])
        self.send("alpha", "now")
        self.assertEqual([m["body"] for m in bus.recv(self.root, "ch", "beta")], ["now"])

    def test_force_takes_over_and_the_stray_stops_without_acking(self):
        proc, _ = self.start_watch()
        bus.recv(self.root, "ch", "beta", wait=True, timeout=0.2, poll=0.05, force=True)
        _, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("taken over", err)
        self.send("alpha", "after the takeover")
        self.assertEqual([m["body"] for m in bus.recv(self.root, "ch", "beta")],
                         ["after the takeover"])

    def test_a_nonblocking_force_read_also_stops_the_old_watcher(self):
        proc, _ = self.start_watch()
        self.assertEqual(bus.recv(self.root, "ch", "beta", force=True), [])
        _, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("taken over", err)
        self.assertFalse((self.root / "ch" / "watchers" / "beta.stop").exists())

    def test_a_killed_watcher_leaves_no_lock(self):
        proc, info = self.start_watch()
        proc.kill()  # SIGKILL: no cleanup runs, the info file stays behind
        proc.communicate(timeout=10)
        self.assertTrue(info.exists())
        self.send("alpha", "hello")
        self.assertEqual(len(bus.recv(self.root, "ch", "beta")), 1)
        self.assertFalse(bus.status(self.root, "ch")["readers"][0]["watching"])


class RefTests(BusCase):
    def test_refs_pin_hashes_and_verify_reports_drift(self):
        part = self.root / "part.step"
        part.write_bytes(b"v1")
        notes = self.root / "notes.md"
        notes.write_bytes(b"keep")
        rec = self.send("alpha", "review these",
                        refs=[str(part), f"{notes}@{sha(b'keep')[:12]}"])
        self.assertEqual(rec["refs"][0],
                         {"path": str(part.resolve()), "sha256": sha(b"v1"), "size": 2})
        self.assertEqual(rec["refs"][1]["sha256"], sha(b"keep"))
        report = bus.verify_refs(self.root, "ch", rec["seq"])
        self.assertEqual([r["status"] for r in report], ["unchanged", "unchanged"])
        part.write_bytes(b"v2")
        notes.unlink()
        report = bus.verify_refs(self.root, "ch", rec["seq"])
        self.assertEqual([r["status"] for r in report], ["changed", "missing"])
        self.assertEqual(report[0]["current_sha256"], sha(b"v2"))
        self.assertEqual(bus.verify_refs(self.root, "ch", self.send("alpha", "no refs")["seq"]), [])

    def test_a_stale_pin_or_a_missing_file_is_refused(self):
        part = self.root / "part.step"
        part.write_bytes(b"v1")
        with self.assertRaises(bus.BusError):
            self.send("alpha", "x", refs=[f"{part}@{'0' * 64}"])
        with self.assertRaises(bus.BusError):
            self.send("alpha", "x", refs=[str(self.root / "nope.step")])
        with self.assertRaises(bus.BusError):
            bus.verify_refs(self.root, "ch", 1)  # nothing was sent


class AuditTests(BusCase):
    def snapshot(self):
        return {p: (p.stat().st_mtime_ns, p.read_bytes())
                for p in self.root.rglob("*") if p.is_file()}

    def kinds(self, report):
        return sorted(((v["seq"], v["kind"]) for v in report["violations"]),
                      key=lambda pair: (pair[0] or 0, pair[1]))

    def test_a_clean_concurrent_channel_passes_and_the_audit_writes_nothing(self):
        threads = [threading.Thread(target=lambda n=n: [self.send(f"agent{n}", f"m{i}")
                                                        for i in range(15)])
                   for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        bus.post_done(self.root, "ch", "agent0", "agent0-harness", "PASS", "ok")
        before = self.snapshot()
        report = bus.audit_channel(self.root, "ch")
        self.assertEqual((report["ok"], report["last_seq"], report["violations"]), (True, 61, []))
        self.assertEqual(self.snapshot(), before)

    def test_a_hole_reports_missing_and_orphaned_seqs_and_status_shows_them(self):
        for i in range(4):
            self.send("alpha", f"m{i}")
        (self.root / "ch" / "messages" / "00000002.json").unlink()
        report = bus.audit_channel(self.root, "ch")
        self.assertFalse(report["ok"])
        self.assertEqual(self.kinds(report), [(2, "missing"), (3, "orphaned"), (4, "orphaned")])
        st = bus.status(self.root, "ch")
        self.assertEqual((st["message_count"], st["unreachable_seqs"]), (1, [3, 4]))

    def test_corrupt_mismatched_and_two_harness_records_are_each_named_by_seq(self):
        for i in range(4):
            self.send("alpha", f"m{i}")
        msgs = self.root / "ch" / "messages"
        (msgs / "00000002.json").write_text("{not json", encoding="utf-8")
        rec = json.loads((msgs / "00000003.json").read_text(encoding="utf-8"))
        (msgs / "00000003.json").write_text(json.dumps(dict(rec, seq=9, id="ch:00000009")),
                                            encoding="utf-8")
        rec = json.loads((msgs / "00000004.json").read_text(encoding="utf-8"))
        (msgs / "00000004.json").write_text(json.dumps(dict(rec, harness="other")),
                                            encoding="utf-8")
        (self.root / "ch" / "done" / "ghost.json").write_text(
            json.dumps({"agent": "ghost", "harness": "h"}), encoding="utf-8")
        report = bus.audit_channel(self.root, "ch")
        self.assertEqual(self.kinds(report),
                         [(None, "done_without_post"), (2, "unparseable"), (3, "id_mismatch"),
                          (3, "seq_mismatch"), (4, "harness_mismatch")])


class KindTests(BusCase):
    def test_kinds_and_filtered_views_never_move_the_cursor(self):
        self.send("alpha", "the plan", kind="plan")
        self.send("alpha", "fillet 2 or 3 mm?", kind="question")
        self.send("alpha", "chatter")
        got = bus.recv(self.root, "ch", "beta", kinds=["question"])
        self.assertEqual([m["kind"] for m in got], ["question"])
        self.assertEqual(len(bus.recv(self.root, "ch", "beta", kinds=["question"])), 1)
        self.assertEqual([m["kind"] for m in bus.recv(self.root, "ch", "beta")],
                         ["plan", "question", "message"])
        with self.assertRaises(bus.BusError):
            bus.recv(self.root, "ch", "beta", kinds=["gossip"])

    def test_phase_verdicts_leave_the_channel_open(self):
        self.send("alpha", "work")
        self.send("beta", "reply")
        first = bus.post_done(self.root, "ch", "alpha", "h1", "PASS", "A modelled", phase="A")
        self.assertFalse(first["phase_complete"])
        second = bus.post_done(self.root, "ch", "beta", "h2", "pass", "A reviewed", phase="A")
        self.assertTrue(second["phase_complete"])
        self.assertEqual(second["phase_verdict"], "PASS")
        st = bus.status(self.root, "ch")
        self.assertFalse(st["complete"])
        self.assertFalse((self.root / "ch" / "VERDICT.json").exists())
        self.assertEqual(sorted(st["phases"]["A"]["verdicts"]), ["alpha", "beta"])
        got = bus.recv(self.root, "ch", "gamma", kinds=["phase"])
        self.assertEqual([(m["agent"], m["phase"], m["verdict"]) for m in got],
                         [("alpha", "A", "PASS"), ("beta", "A", "PASS")])
        bus.post_done(self.root, "ch", "beta", "h2", "FAIL", "B rejected", phase="B")
        bus.post_done(self.root, "ch", "alpha", "h1", "PASS", "B fine by me", phase="B")
        self.assertEqual(bus.status(self.root, "ch")["phases"]["B"]["verdict"], "FAIL")
        with self.assertRaises(bus.BusError):
            bus.post_done(self.root, "ch", "alpha", "h1", "PASS", "x", phase="../A")


class HumanTests(BusCase):
    def test_a_human_never_blocks_the_verdict_but_a_human_fail_counts(self):
        self.send("alpha", "work")
        self.send("beta", "reply")
        bus.send(self.root, "ch", "zohar", "human", "use 3 mm", kind="answer")
        bus.post_done(self.root, "ch", "alpha", "h1", "PASS", "modelled")
        result = bus.post_done(self.root, "ch", "beta", "h2", "PASS", "reviewed")
        self.assertTrue(result["complete"])
        self.assertEqual(result["verdict"], "PASS")
        zohar = [p for p in bus.status(self.root, "ch")["participants"] if p["agent"] == "zohar"]
        self.assertTrue(zohar[0]["human"])
        bus.post_done(self.root, "ch", "zohar", "human", "FAIL", "wrong material")
        self.assertEqual(bus.status(self.root, "ch")["verdict"], "FAIL")

    def test_a_message_for_someone_runs_their_notify_hook(self):
        out = self.root / "notified.txt"
        cmd = ('printf "%s|%s|%s|%s|%s\\n" "$AGENT_BUS_AGENT" "$AGENT_BUS_MSG_FROM" '
               f'"$AGENT_BUS_MSG_SEQ" "$AGENT_BUS_MSG_KIND" "$AGENT_BUS_MSG_BODY" >> {out}')
        bus.register_notify(self.root, "ch", "zohar", "human", cmd)
        sent = self.send("alpha", "fillet 2 or 3 mm?", kind="question", for_agent="zohar")
        self.assertEqual(sent["for"], "zohar")
        self.assertTrue(sent["notified"]["notified"])
        self.assertTrue(wait_until(out.exists))
        self.assertTrue(wait_until(lambda: out.read_text().endswith("\n")))
        self.assertEqual(out.read_text(), "zohar|alpha|1|question|fillet 2 or 3 mm?\n")
        sent = self.send("alpha", "and you?", kind="question", for_agent="beta")
        self.assertFalse(sent["notified"]["notified"])
        self.assertEqual(bus.status(self.root, "ch")["notify"], ["zohar"])
        self.assertTrue(bus.clear_notify(self.root, "ch", "zohar"))
        with self.assertRaises(bus.BusError):
            bus.register_notify(self.root, "ch", "zohar", "human", "   ")
        self.send("alpha", f"register: {cmd}")
        with self.assertRaises(bus.BusError) as ctx:  # never a command a peer posted
            bus.register_notify(self.root, "ch", "zohar", "human", cmd)
        self.assertIn("message body", str(ctx.exception))


class SpawnTests(BusCase):
    def fake(self, name, script):
        bindir = self.root / "bin"
        bindir.mkdir(exist_ok=True)
        path = bindir / name
        path.write_text("#!/bin/sh\n" + script)
        path.chmod(0o755)
        return path

    def stop(self, pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for proc in bus._DETACHED:  # reap our own child so its pid really dies
            if proc.pid == pid:
                proc.wait()

    def test_codex_spawn_captures_its_session_id_and_registers_the_resume(self):
        seen = self.root / "env.txt"
        sid = "0199aaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        codex = self.fake("codex", f'echo "$AGENT_BUS_AGENT $AGENT_BUS_HARNESS $AGENT_BUS_CHANNEL" '
                                   f'> {seen}\necho "session id: {sid}" >&2\nexec sleep 30\n')
        argv = [str(codex), "exec", "-m", "gpt-6-astra", "-c", 'model_reasoning_effort="high"',
                "--dangerously-bypass-approvals-and-sandbox", "model the case"]
        rec = bus.spawn(self.root, "ch", "codex", None, argv, cwd=str(self.root), id_timeout=10)
        self.addCleanup(self.stop, rec["pid"])
        self.assertEqual((rec["harness"], rec["session_id"], rec["running"]),
                         ("codex-cli", sid, True))
        self.assertTrue(rec["wake_registered"])
        expected = shlex.join([str(codex), "exec", "resume", "-m", "gpt-6-astra",
                               "-c", 'model_reasoning_effort="high"',
                               "--dangerously-bypass-approvals-and-sandbox", sid,
                               "read agent-bus channel ch and continue"])
        self.assertEqual(bus.get_wake(self.root, "ch", "codex")["command"],
                         f"cd {shlex.quote(str(self.root.resolve()))} && {expected}")
        self.assertNotEqual(os.getsid(rec["pid"]), os.getsid(0))  # its own session
        self.assertTrue(wait_until(lambda: seen.exists() and seen.read_text()))
        self.assertEqual(seen.read_text(), "codex codex-cli ch\n")
        reader = bus.status(self.root, "ch")["readers"][0]
        self.assertEqual((reader["agent"], reader["spawn_alive"]), ("codex", True))
        with self.assertRaises(bus.BusError):
            bus.spawn(self.root, "ch", "codex", None, argv, cwd=str(self.root))
        self.stop(rec["pid"])
        self.assertFalse(bus.status(self.root, "ch")["readers"][0]["spawn_alive"])

    def test_claude_spawn_gets_a_session_id_injected(self):
        seen = self.root / "argv.txt"
        claude = self.fake("claude", f'echo "$@" > {seen}\nexec sleep 30\n')
        rec = bus.spawn(self.root, "ch", "claude", None,
                        [str(claude), "--model", "opus", "-p", "review it"], cwd=str(self.root))
        self.addCleanup(self.stop, rec["pid"])
        sid = rec["session_id"]
        uuid.UUID(sid)
        self.assertTrue(wait_until(lambda: seen.exists() and seen.read_text()))
        self.assertEqual(seen.read_text(), f"--session-id {sid} --model opus -p review it\n")
        self.assertTrue(bus.get_wake(self.root, "ch", "claude")["command"].endswith(
            shlex.join([str(claude), "--resume", sid, "--model", "opus", "-p",
                        "read agent-bus channel ch and continue"])))

    def test_no_session_id_means_no_wake_and_the_exit_is_reported(self):
        codex = self.fake("codex", "echo starting\nexit 3\n")
        rec = bus.spawn(self.root, "ch", "codex", None, [str(codex), "exec", "x"],
                        cwd=str(self.root), id_timeout=5)
        self.assertEqual((rec["wake_registered"], rec["running"], rec["exit_code"]),
                         (False, False, 3))
        self.assertIn("no session id", rec["wake_reason"])
        self.assertIsNone(bus.get_wake(self.root, "ch", "codex"))

    def test_respawn_without_a_wake_drops_the_old_sessions_wake(self):
        sid = "0199aaaa-0000-0000-0000-000000000000"
        codex = self.fake("codex", f'echo "session id: {sid}" >&2\nexit 0\n')
        first = bus.spawn(self.root, "ch", "codex", None, [str(codex), "exec", "x"],
                          cwd=str(self.root), id_timeout=5)
        self.assertTrue(first["wake_registered"])
        self.stop(first["pid"])
        again = bus.spawn(self.root, "ch", "codex", None, [str(codex), "exec", "x"],
                          cwd=str(self.root), register=False)
        self.assertTrue(again["wake_cleared"])
        self.assertIsNone(bus.get_wake(self.root, "ch", "codex"))
        # An operator's own registration is not the last spawn's, so it stays.
        bus.register_wake(self.root, "ch", "codex", "codex-cli", "echo mine")
        self.stop(again["pid"])
        third = bus.spawn(self.root, "ch", "codex", None, [str(codex), "exec", "x"],
                          cwd=str(self.root), register=False)
        self.assertNotIn("wake_cleared", third)
        self.assertEqual(bus.get_wake(self.root, "ch", "codex")["command"], "echo mine")
        self.stop(third["pid"])

    def test_an_exited_peer_is_dead_even_before_anyone_reaps_it(self):
        codex = self.fake("codex", 'echo "session id: 0199bbbb-0000-0000-0000-000000000000" >&2\n'
                                   'sleep 1\n')
        rec = bus.spawn(self.root, "ch", "codex", None, [str(codex), "exec", "x"],
                        cwd=str(self.root), id_timeout=5)
        self.assertTrue(rec["started"])
        self.assertTrue(wait_until(
            lambda: not bus.status(self.root, "ch")["readers"][0]["spawn_alive"]))
        again = bus.spawn(self.root, "ch", "codex", None, [str(codex), "exec", "x"],
                          cwd=str(self.root), id_timeout=5)  # not "already running"
        self.stop(again["pid"])

    def test_a_reused_pid_is_not_the_spawned_peer(self):
        me = {"pid": os.getpid(), "started": bus._proc_started(os.getpid())}
        self.assertTrue(bus._spawn_alive(me))
        self.assertFalse(bus._spawn_alive(dict(me, started="Thu Jan  1 00:00:00 1970")))

    def test_a_spawn_in_progress_refuses_a_second(self):
        (self.root / "ch" / "spawn").mkdir(parents=True)
        (self.root / "ch" / "messages").mkdir()
        with bus._flock(self.root / "ch" / "spawn" / "codex.lock"):
            with self.assertRaises(bus.BusError) as ctx:
                bus.spawn(self.root, "ch", "codex", None, ["codex", "exec", "x"])
        self.assertIn("in progress", str(ctx.exception))

    def test_resume_flags_stop_at_the_prompt_separator(self):
        cmd = bus.resume_command("codex", "sid", "ch", "/w",
                                 ["codex", "exec", "-m", "gpt-x", "--", "--model=not-a-flag"])
        self.assertEqual(cmd, "cd /w && codex exec resume -m gpt-x sid "
                              "'read agent-bus channel ch and continue'")

    def test_spawn_refuses_a_secret_on_the_command_line(self):
        with self.assertRaises(bus.BusError):
            bus.spawn(self.root, "ch", "codex", None, ["codex", "exec", "sk-" + "a" * 30])
        self.assertFalse((self.root / "ch" / "spawn" / "codex.json").exists())


class CliFeatureTests(unittest.TestCase):
    run_cli = CliTests.run_cli

    def test_quiet_send_body_only_and_reserved_kinds(self):
        with tempfile.TemporaryDirectory() as tmp:
            alpha = dict(os.environ, AGENT_BUS_DIR=tmp,
                         AGENT_BUS_AGENT="alpha", AGENT_BUS_HARNESS="h1")
            beta = dict(alpha, AGENT_BUS_AGENT="beta", AGENT_BUS_HARNESS="h2")
            out = self.run_cli(alpha, "send", "-c", "t", "--body", "hello")
            self.assertEqual((out.returncode, out.stdout), (0, "1 t:00000001\n"))
            out = self.run_cli(alpha, "send", "-c", "t", "--kind", "question",
                               "--body", "two\nlines", "--json")
            self.assertEqual(json.loads(out.stdout)["kind"], "question")
            out = self.run_cli(beta, "recv", "-c", "t", "--kind", "question", "--body-only")
            self.assertEqual(out.stdout, "two\nlines\n")
            out = self.run_cli(beta, "watch", "-c", "t", "--timeout", "1", "--body-only")
            self.assertEqual(out.stdout, "hello\ntwo\nlines\n")
            out = self.run_cli(beta, "recv", "-c", "t", "--body-only")
            self.assertEqual((out.returncode, out.stdout), (0, ""))
            out = self.run_cli(alpha, "send", "-c", "t", "--kind", "done", "--body", "x")
            self.assertEqual(out.returncode, 2)
            self.assertIn("invalid choice", out.stderr)

    def test_send_ref_then_verify_refs_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, AGENT_BUS_DIR=tmp,
                       AGENT_BUS_AGENT="alpha", AGENT_BUS_HARNESS="h1")
            part = Path(tmp) / "part.step"
            part.write_bytes(b"v1")
            out = self.run_cli(env, "send", "-c", "t", "--body", "review", "--ref", str(part))
            self.assertEqual(out.returncode, 0, out.stderr)
            out = self.run_cli(env, "verify-refs", "-c", "t", "1")
            self.assertEqual((out.returncode, json.loads(out.stdout)[0]["status"]), (0, "unchanged"))
            part.write_bytes(b"v2")
            out = self.run_cli(env, "verify-refs", "-c", "t", "1")
            self.assertEqual((out.returncode, json.loads(out.stdout)[0]["status"]), (1, "changed"))

    def test_verify_channel_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, AGENT_BUS_DIR=tmp,
                       AGENT_BUS_AGENT="alpha", AGENT_BUS_HARNESS="h1")
            for body in ("one", "two"):
                self.run_cli(env, "send", "-c", "t", "--body", body)
            out = self.run_cli(env, "verify-channel", "-c", "t")
            self.assertEqual((out.returncode, json.loads(out.stdout)["ok"]), (0, True), out.stderr)
            (Path(tmp) / "t" / "messages" / "00000001.json").unlink()
            out = self.run_cli(env, "verify-channel", "-c", "t")
            self.assertEqual((out.returncode, json.loads(out.stdout)["violations"][0]["kind"]),
                             (1, "missing"))

    def test_config_file_supplies_identity_and_flags_and_env_override_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            work = project / "sub"
            work.mkdir(parents=True)
            (project / ".agent-bus.toml").write_text(
                'agent = "alpha"\nharness = "h1"\nchannel = "cfg"\nroot = "bus"\n')
            env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_BUS_")}
            out = self.run_cli(env, "send", "--body", "from config", cwd=work)
            self.assertEqual((out.returncode, out.stdout), (0, "1 cfg:00000001\n"), out.stderr)
            self.run_cli(env, "send", "--as", "beta", "--body", "flag wins", cwd=work)
            self.run_cli(dict(env, AGENT_BUS_AGENT="gamma"), "send", "--body", "env wins", cwd=work)
            msgs = bus.recv(project / "bus", "cfg", "observer", after=0)
            self.assertEqual([(m["agent"], m["harness"]) for m in msgs],
                             [("alpha", "h1"), ("beta", "h1"), ("gamma", "h1")])
            (project / ".agent-bus.toml").write_text('agent = "alpha"\nnickname = "x"\n')
            out = self.run_cli(env, "send", "--body", "x", cwd=work)
            self.assertEqual(out.returncode, 2)
            self.assertIn("unknown keys", out.stderr)

    def test_spawn_over_the_cli_outlives_the_spawner(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex = Path(tmp) / "codex"
            codex.write_text('#!/bin/sh\necho "session id: 0199cccc-dddd" >&2\nexec sleep 30\n')
            codex.chmod(0o755)
            env = dict(os.environ, AGENT_BUS_DIR=tmp)
            out = self.run_cli(env, "spawn", "-c", "t", "--agent", "codex", "--id-timeout", "10",
                               "--", str(codex), "exec", "go")
            self.assertEqual(out.returncode, 0, out.stderr)
            rec = json.loads(out.stdout)
            try:
                self.assertEqual((rec["session_id"], rec["wake_registered"]),
                                 ("0199cccc-dddd", True))
                self.assertTrue(bus._pid_alive(rec["pid"]))  # the CLI is gone, the peer is not
                out = self.run_cli(env, "status", "-c", "t")
                self.assertTrue(json.loads(out.stdout)["readers"][0]["spawn_alive"])
            finally:
                os.kill(rec["pid"], signal.SIGKILL)
            self.assertTrue(wait_until(lambda: not bus._pid_alive(rec["pid"])))


class PidAliveTests(unittest.TestCase):
    @unittest.skipUnless(Path("/proc/self/stat").exists(), "zombie state is read from Linux /proc")
    def test_unreaped_exited_process_is_not_alive(self):
        child = os.fork()
        if child == 0:
            os._exit(0)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:  # wait until the child has exited but is not yet reaped
                state = Path(f"/proc/{child}/stat").read_text().rsplit(")", 1)[1].split()[0]
                if state == "Z":
                    break
                time.sleep(0.01)
            os.kill(child, 0)  # a zombie still answers the signal probe
            self.assertFalse(bus._pid_alive(child))
        finally:
            os.waitpid(child, 0)


if __name__ == "__main__":
    unittest.main()
