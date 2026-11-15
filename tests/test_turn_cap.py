"""Turn budgets and moderation use isolated roots; no live agents or home-bus writes."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import bus
import mcp_server
from scripts import channel_stats


class TurnCapTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_BUS_")}
        env.update(AGENT_BUS_ROOT=str(self.root))
        self.env = patch.dict(os.environ, env, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def config(self, cap=4):
        return bus.configure_channel(self.root, "ch", cap, "moderator")

    def send(self, agent="a", harness="codex", kind="message", **kwargs):
        return bus.send(self.root, "ch", agent, harness, "work", kind=kind, **kwargs)

    def cli(self, *args):
        return subprocess.run([sys.executable, str(REPO / "bus.py"), *args],
                              capture_output=True, text=True, timeout=30,
                              cwd=self.root, env=os.environ)

    def test_refusal_at_cap_keeps_gapless_ids_and_one_receipt(self):
        self.config(2)
        self.assertEqual([self.send()["seq"] for _ in range(2)], [1, 2])
        marker = self.root / "ch" / "CAPPED.json"
        receipt = marker.read_bytes(), marker.stat().st_mtime_ns
        for _ in range(2):
            with self.assertRaisesRegex(bus.TurnCapError,
                                       "turn cap 2 reached on ch; a moderator must close or raise it"):
                self.send()
        self.assertEqual((marker.read_bytes(), marker.stat().st_mtime_ns), receipt)
        self.assertEqual(bus._existing_seqs(self.root / "ch" / "messages"), [1, 2])
        self.assertEqual(list((self.root / "ch" / "tmp").iterdir()), [])

    def test_all_seven_kinds_count_but_human_done_and_phase_do_not(self):
        self.config(10)
        for kind in bus.SEND_KINDS:
            self.send(kind=kind)
            self.send("operator", "human", kind)
        bus.post_done(self.root, "ch", "a", "codex", "PASS", "phase", phase="one")
        bus.post_done(self.root, "ch", "a", "codex", "PASS", "finished")
        st = bus.status(self.root, "ch")
        self.assertEqual((st["turns_used"], st["cap"], st["capped"]), (7, 10, False))
        self.assertTrue(st["complete"])

    def test_done_phase_and_human_sends_cannot_bypass_cap(self):
        self.config(1)
        self.send()
        for phase in (None, "one"):
            with self.assertRaises(bus.TurnCapError):
                bus.post_done(self.root, "ch", "a", "codex", "PASS", "finished", phase=phase)
        with self.assertRaises(bus.TurnCapError):
            self.send("operator", "human")
        self.assertFalse(bus.status(self.root, "ch")["complete"])
        self.assertFalse((self.root / "ch" / "VERDICT.json").exists())
        self.assertFalse((self.root / "ch" / "done" / "a.json").exists())

    def test_human_close_at_cap_writes_final_verdict(self):
        self.config(1)
        self.send()
        result = bus.close_channel(self.root, "ch", "operator", "human", "FAIL", "cap")
        self.assertEqual(result["closed_by"], "operator")
        self.assertEqual(result["reason"], "turn_cap")
        self.assertEqual(json.loads((self.root / "ch" / "VERDICT.json").read_text()), result)
        self.assertTrue(bus.status(self.root, "ch")["complete"])
        self.assertEqual(bus.status(self.root, "ch")["verdict"], "FAIL")
        with self.assertRaisesRegex(bus.BusError, "closed"):
            self.send()
        with self.assertRaisesRegex(bus.BusError, "closed"):
            bus.raise_cap(self.root, "ch", "operator", "human", 10)
        with self.assertRaisesRegex(bus.BusError, "closed"):
            bus.configure_channel(self.root, "ch", 20, agent="operator", harness="human")

    def test_named_moderator_closes_before_cap_and_cannot_overwrite(self):
        self.config()
        result = bus.close_channel(self.root, "ch", "moderator", "claude", "PASS", "reviewed")
        self.assertEqual(result["reason"], "moderator")
        with self.assertRaisesRegex(bus.BusError, "closed"):
            bus.close_channel(self.root, "ch", "moderator", "claude", "FAIL", "changed")
        self.assertEqual(bus.status(self.root, "ch")["verdict"], "PASS")

    def test_inflight_done_cannot_overwrite_moderator_close(self):
        self.config()
        original_send = bus.send

        def send_then_close(*args, **kwargs):
            result = original_send(*args, **kwargs)
            bus.close_channel(self.root, "ch", "moderator", "codex", "FAIL", "reviewed")
            return result

        with patch.object(bus, "send", side_effect=send_then_close):
            result = bus.post_done(self.root, "ch", "a", "codex", "PASS", "finished")
        self.assertEqual(result["verdict"], "FAIL")
        record = json.loads((self.root / "ch" / "VERDICT.json").read_text())
        self.assertEqual((record["verdict"], record["closed_by"]), ("FAIL", "moderator"))

    def test_concurrent_raises_preserve_every_increment(self):
        self.config(4)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: bus.raise_cap(self.root, "ch", "moderator", "codex", 1),
                          range(8)))
        self.assertEqual(bus.status(self.root, "ch")["cap"], 12)

    def test_non_moderator_cannot_close_raise_or_reconfigure(self):
        self.config(1)
        self.send()
        for action in (
            lambda: bus.close_channel(self.root, "ch", "a", "codex", "PASS", "done"),
            lambda: bus.raise_cap(self.root, "ch", "a", "codex", 10),
            lambda: bus.configure_channel(self.root, "ch", 100, "a", "a", "codex"),
            lambda: bus.configure_channel(self.root, "ch", 100),
        ):
            with self.assertRaisesRegex(bus.BusError, "only a human or the configured moderator"):
                action()
        self.assertEqual(bus.channel_config(self.root / "ch")["max_turns"], 1)
        self.assertFalse((self.root / "ch" / "VERDICT.json").exists())

    def test_raise_cap_reopens_budget_retaining_receipt_and_sequence(self):
        self.config(1)
        self.send()
        marker = (self.root / "ch" / "CAPPED.json").read_bytes()
        result = bus.raise_cap(self.root, "ch", "moderator", "claude", 2)
        self.assertEqual((result["cap"], result["turns_used"], result["capped"]), (3, 1, False))
        self.assertEqual([self.send()["seq"] for _ in range(2)], [2, 3])
        with self.assertRaises(bus.TurnCapError):
            self.send()
        self.assertEqual((self.root / "ch" / "CAPPED.json").read_bytes(), marker)
        bus.raise_cap(self.root, "ch", "operator", "human", 1)
        self.assertEqual(self.send()["seq"], 4)

    def test_sweep_and_forced_wake_are_suppressed_at_cap(self):
        self.config(1)
        bus.register_wake(self.root, "ch", "b", "codex", "echo resumed")
        with patch.object(bus.subprocess, "Popen") as launch:
            rec = self.send(wake=True)
            self.assertFalse(rec["woken"][0]["woke"])
            self.assertEqual(bus.sweep_wakes(self.root, idle=0, cooldown=0), [])
            self.assertFalse(bus.wake_agent(self.root, "ch", "b", cooldown=0)["woke"])
            launch.assert_not_called()
        bus.raise_cap(self.root, "ch", "operator", "human", 2)
        with patch.object(bus, "_launch_wake_locked", return_value=({"woke": True}, None)) as launch:
            self.assertTrue(bus.wake_agent(self.root, "ch", "b")["woke"])
            launch.assert_called_once()

    def test_closed_channel_suppresses_wakes_before_cap(self):
        self.config()
        bus.register_wake(self.root, "ch", "b", "codex", "echo resumed")
        self.send()
        bus.close_channel(self.root, "ch", "moderator", "codex", "PASS", "reviewed")
        with patch.object(bus.subprocess, "Popen") as launch:
            self.assertEqual(bus.sweep_wakes(self.root, idle=0, cooldown=0), [])
            self.assertFalse(bus.wake_agent(self.root, "ch", "b")["woke"])
            launch.assert_not_called()

    def test_old_channel_without_config_gets_default_read_only(self):
        ch = bus._channel_dir(self.root, "ch", create=True)
        bus._message_path(ch / "messages", 1).write_text(json.dumps({
            "seq": 1, "agent": "a", "harness": "codex", "kind": "message", "ts": bus._now_iso()
        }))
        st = bus.status(self.root, "ch")
        self.assertEqual((st["cap"], st["turns_used"]), (bus.DEFAULT_MAX_TURNS, 1))
        self.assertFalse((ch / "config.json").exists())
        with self.assertRaises(bus.BusError):
            bus.configure_channel(self.root, "ch", 500, "a")
        self.send()
        self.assertEqual(bus.channel_config(ch)["max_turns"], bus.DEFAULT_MAX_TURNS)

    def test_env_default_is_frozen_after_first_send(self):
        with patch.dict(os.environ, AGENT_BUS_MAX_TURNS="1"):
            self.send()
        with patch.dict(os.environ, AGENT_BUS_MAX_TURNS="500"):
            with self.assertRaises(bus.TurnCapError):
                self.send()
        self.assertEqual(bus.status(self.root, "ch")["cap"], 1)

    def test_invalid_caps_and_secret_config_are_refused(self):
        for cap in (0, -1, True, "many", 1.5):
            with self.subTest(cap=cap), self.assertRaises(bus.BusError):
                self.config(cap)
        with self.assertRaisesRegex(bus.BusError, "secret"):
            bus.configure_channel(self.root, "ch", 4, "ghp_" + "x" * 30)
        self.config()
        for by in (0, -1, True, "many"):
            with self.subTest(by=by), self.assertRaises(bus.BusError):
                bus.raise_cap(self.root, "ch", "operator", "human", by)
        with self.assertRaisesRegex(bus.BusError, "secret"):
            bus.close_channel(self.root, "ch", "operator", "human", "FAIL", "ghp_" + "x" * 30)
        with patch.dict(os.environ, AGENT_BUS_MAX_TURNS="invalid"):
            with self.assertRaises(bus.BusError):
                bus.configure_channel(self.root, "new")

    def test_corrupt_config_fails_closed(self):
        self.config()
        (self.root / "ch" / "config.json").write_text("not json")
        with self.assertRaisesRegex(bus.BusError, "invalid channel config"):
            self.send()
        self.assertEqual(bus._existing_seqs(self.root / "ch" / "messages"), [])

    def test_cli_demo_root_alias_exit_code_and_close(self):
        result = self.cli("config", "-c", "ch", "--max-turns", "4", "--moderator", "moderator")
        self.assertEqual(result.returncode, 0, result.stderr)
        for i in range(5):
            result = self.cli("send", "-c", "ch", "--as", "a", "--harness", "codex", "--body", "work")
            self.assertEqual(result.returncode, 0 if i < 4 else 4, result.stderr)
        self.assertIn("turn cap 4 reached on ch; a moderator must close or raise it", result.stderr)
        result = self.cli("close", "-c", "ch", "--as", "operator", "--harness", "human",
                          "--verdict", "FAIL", "--summary", "cap")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["closed_by"], "operator")

    def test_cli_raise_and_config_authority(self):
        self.config(1)
        self.send()
        refused = self.cli("config", "-c", "ch", "--max-turns", "99", "--moderator", "a",
                           "--as", "a", "--harness", "codex")
        self.assertEqual(refused.returncode, 2)
        raised = self.cli("raise-cap", "-c", "ch", "--by", "2",
                          "--as", "moderator", "--harness", "codex")
        self.assertEqual(raised.returncode, 0, raised.stderr)
        self.assertEqual(json.loads(raised.stdout)["cap"], 3)

    def test_mcp_returns_cap_error_and_status_fields(self):
        self.config(1)
        with patch.dict(os.environ, AGENT_BUS_AGENT="a", AGENT_BUS_HARNESS="codex"):
            mcp_server.call_tool("bus_send", {"channel": "ch", "body": "work"})
            output = io.StringIO()
            with redirect_stdout(output):
                mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "bus_send",
                                              "arguments": {"channel": "ch", "body": "more"}}})
            result = json.loads(output.getvalue())["result"]
            self.assertTrue(result["isError"])
            self.assertIn("turn cap 1 reached on ch", result["content"][0]["text"])
            st = mcp_server.call_tool("bus_status", {"channel": "ch"})
            self.assertEqual((st["turns_used"], st["cap"], st["capped"]), (1, 1, True))

    def test_concurrent_sends_cannot_overshoot(self):
        self.config(4)

        def send_or_refuse(n):
            try:
                return self.send(agent=f"a{n}")["seq"]
            except bus.TurnCapError:
                return None

        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(send_or_refuse, range(12)))
        self.assertEqual(sorted(r for r in results if r is not None), [1, 2, 3, 4])
        self.assertEqual(results.count(None), 8)

    def test_concurrent_processes_cannot_overshoot(self):
        self.config(4)
        procs = [subprocess.Popen([sys.executable, str(REPO / "bus.py"), "send", "-c", "ch",
                                   "--as", f"a{n}", "--harness", "codex", "--body", "work"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  cwd=self.root, env=os.environ) for n in range(12)]
        for proc in procs:
            proc.communicate(timeout=30)
        self.assertEqual(sorted(p.returncode for p in procs), [0] * 4 + [4] * 8)
        self.assertEqual(bus._existing_seqs(self.root / "ch" / "messages"), [1, 2, 3, 4])

    def test_stats_are_read_only_and_suggestion_uses_counted_real_turns(self):
        self.config()
        self.send()
        self.send("operator", "human")
        bus.post_done(self.root, "ch", "a", "codex", "PASS", "finished")
        for _ in range(6):
            bus.send(self.root, "crew-demo", "a", "codex", "fixture")
        (self.root / "ch" / "messages" / "bad.json").write_text("not json")
        before = {p.relative_to(self.root): (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in self.root.rglob("*") if p.is_file()}
        rows = channel_stats.scan(self.root)
        after = {p.relative_to(self.root): (p.read_bytes(), p.stat().st_mtime_ns)
                 for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(rows[0]["turns"], 1)
        self.assertEqual(rows[0]["state"], "done")
        self.assertEqual(rows[0]["kinds"], {"done": 1, "message": 2})
        self.assertEqual(rows[0]["agents"], {"a": 2, "operator": 1})
        self.assertEqual(rows[0]["skipped"], 1)
        self.assertTrue(rows[1]["fixture"])
        self.assertEqual(channel_stats.suggested_cap(rows), 10)
        self.assertEqual(channel_stats.suggested_cap([{"turns": 112, "fixture": False}]), 230)
        self.assertEqual(channel_stats.suggested_cap([]), 10)


if __name__ == "__main__":
    unittest.main()
