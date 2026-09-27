"""crew tests: config parse and command construction. No live agents, no tmux."""

import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(REPO))

import crew  # noqa: E402


class CrewCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        (self.dir / "prompts").mkdir()
        (self.dir / "prompts" / "orch.md").write_text("you are orch\n", encoding="utf-8")
        (self.dir / "prompts" / "peer.md").write_text("you are peer\n", encoding="utf-8")

    def write_toml(self, body: str, name="crew.toml") -> Path:
        path = self.dir / name
        path.write_text(body, encoding="utf-8")
        return path

    def two_seat(self, **kw) -> str:
        channel = kw.get("channel", "radar")
        cwd = kw.get("cwd", str(self.dir))
        h1 = kw.get("h1", "cursor-agent")
        h2 = kw.get("h2", "codex-cli")
        extra1 = kw.get("extra1", 'flags = ["--force"]\nmodel = "cursor-grok-4.6-xhigh-fast"')
        extra2 = kw.get("extra2", 'model = "gpt-5.6-sol"')
        return f"""
channel = "{channel}"
cwd     = "{cwd}"

[[seat]]
name    = "orch"
harness = "{h1}"
{extra1}
prompt  = "prompts/orch.md"

[[seat]]
name    = "codex"
harness = "{h2}"
{extra2}
prompt  = "prompts/peer.md"
"""


class ParseTests(CrewCase):
    def test_parses_two_seat_config(self):
        cfg = crew.load_config(self.write_toml(self.two_seat()))
        self.assertEqual(cfg.channel, "radar")
        self.assertEqual(cfg.cwd, self.dir.resolve())
        self.assertEqual([s.name for s in cfg.seats], ["orch", "codex"])
        self.assertEqual(cfg.seats[0].harness, "cursor-agent")
        self.assertEqual(cfg.seats[0].flags, ["--force"])
        self.assertEqual(cfg.seats[0].model, "cursor-grok-4.6-xhigh-fast")
        self.assertEqual(cfg.seats[0].prompt_text, "you are orch")
        self.assertEqual(cfg.seats[1].harness, "codex-cli")

    def test_expands_home_in_cwd(self):
        cfg = crew.load_config(self.write_toml(self.two_seat(cwd="~/projects/agent-bus")))
        self.assertEqual(cfg.cwd, Path("~/projects/agent-bus").expanduser().resolve())

    def test_rejects_unknown_harness(self):
        body = self.two_seat(h1="not-a-harness")
        with self.assertRaises(crew.CrewError) as ctx:
            crew.load_config(self.write_toml(body))
        self.assertIn("unknown harness", str(ctx.exception))

    def test_rejects_duplicate_seat_names(self):
        body = self.two_seat().replace('name    = "codex"', 'name    = "orch"')
        with self.assertRaises(crew.CrewError):
            crew.load_config(self.write_toml(body))

    def test_rejects_missing_prompt_file(self):
        body = self.two_seat().replace("prompts/peer.md", "prompts/missing.md")
        with self.assertRaises(crew.CrewError) as ctx:
            crew.load_config(self.write_toml(body))
        self.assertIn("prompt", str(ctx.exception).lower())

    def test_rejects_headless_flag_on_seat(self):
        body = self.two_seat(extra1='flags = ["--force", "-p"]\nmodel = "x"')
        with self.assertRaises(crew.CrewError) as ctx:
            crew.load_config(self.write_toml(body))
        self.assertIn("headless", str(ctx.exception).lower())

    def test_rejects_secret_like_prompt(self):
        (self.dir / "prompts" / "orch.md").write_text(
            "use ghp_" + "a" * 36 + " please\n", encoding="utf-8")
        with self.assertRaises(crew.CrewError) as ctx:
            crew.load_config(self.write_toml(self.two_seat()))
        self.assertIn("secret", str(ctx.exception).lower())


class CommandTests(CrewCase):
    def test_cursor_agent_is_interactive_with_positional_prompt(self):
        cfg = crew.load_config(self.write_toml(self.two_seat()))
        argv = crew.seat_argv(cfg.seats[0])
        self.assertEqual(argv[0], "cursor-agent")
        self.assertIn("--model", argv)
        self.assertTrue(any("cursor-grok-4.6-xhigh-fast" in str(a) for a in argv))
        self.assertIn("--force", argv)
        self.assertEqual(argv[-1], "you are orch")
        self.assertNotIn("-p", argv)
        self.assertNotIn("--print", argv)

    def test_claude_is_interactive_with_positional_prompt(self):
        cfg = crew.load_config(self.write_toml(self.two_seat(h1="claude", extra1='model = "sonnet"')))
        argv = crew.seat_argv(cfg.seats[0])
        self.assertEqual(argv[0], "claude")
        self.assertEqual(argv[-1], "you are orch")
        self.assertNotIn("-p", argv)

    def test_codex_is_interactive_with_positional_prompt(self):
        cfg = crew.load_config(self.write_toml(self.two_seat()))
        argv = crew.seat_argv(cfg.seats[1])
        self.assertEqual(argv[0], "codex")
        self.assertIn("--model", argv)
        self.assertEqual(argv[-1], "you are peer")
        self.assertNotIn("exec", argv)

    def test_kimi_cannot_take_prompt_on_argv_so_send_keys(self):
        cfg = crew.load_config(self.write_toml(self.two_seat(h2="kimi", extra2='model = "kimi-k2"')))
        seat = cfg.seats[1]
        argv = crew.seat_argv(seat)
        self.assertEqual(argv[0], "kimi")
        self.assertIn("--model", argv)
        self.assertNotIn("you are peer", argv)
        self.assertNotIn("-p", argv)
        self.assertNotIn("--prompt", argv)
        self.assertEqual(crew.prompt_delivery(seat), "send-keys")
        self.assertEqual(crew.prompt_delivery(cfg.seats[0]), "positional")

    def test_seat_env_exports_bus_identity(self):
        cfg = crew.load_config(self.write_toml(self.two_seat()))
        env = crew.seat_env(cfg, cfg.seats[0])
        self.assertEqual(env["AGENT_BUS_AGENT"], "orch")
        self.assertEqual(env["AGENT_BUS_HARNESS"], "cursor-agent")
        self.assertEqual(env["AGENT_BUS_CHANNEL"], "radar")

    def test_observer_uses_recv_peek_after_zero(self):
        cfg = crew.load_config(self.write_toml(self.two_seat()))
        argv = crew.observer_argv(cfg)
        self.assertEqual(argv[0], sys.executable)
        self.assertTrue(argv[1].endswith("bus.py"))
        self.assertEqual(argv[2], "recv")
        self.assertIn("-c", argv)
        self.assertIn("radar", argv)
        self.assertIn("--peek", argv)
        self.assertIn("--after", argv)
        self.assertIn("0", argv)
        self.assertIn("--include-own", argv)
        self.assertNotIn("watch", argv)

    def test_dry_run_prints_exact_commands_and_not_send_keys_for_native(self):
        cfg = crew.load_config(self.write_toml(self.two_seat()))
        text = crew.format_plan(crew.build_plan(cfg))
        self.assertIn("tmux new-session", text)
        self.assertIn("cursor-agent", text)
        self.assertIn("--force", text)
        self.assertIn("you are orch", text)
        self.assertIn("codex", text)
        self.assertIn("AGENT_BUS_AGENT=orch", text)
        self.assertIn("AGENT_BUS_AGENT=codex", text)
        self.assertIn("recv", text)
        self.assertIn("--peek", text)
        self.assertNotIn("send-keys", text)
        self.assertNotIn("cursor-agent -p", text)
        self.assertNotIn("codex exec", text)

    def test_dry_run_includes_send_keys_only_for_kimi(self):
        cfg = crew.load_config(self.write_toml(self.two_seat(h2="kimi", extra2='model = "kimi-k2"')))
        text = crew.format_plan(crew.build_plan(cfg))
        self.assertIn("send-keys", text)
        self.assertIn("you are peer", text)
        self.assertNotIn("kimi -p", text)

    def test_missing_tmux_message(self):
        with self.assertRaises(crew.CrewError) as ctx:
            crew.require_tmux(which=lambda _name: None)
        self.assertIn("tmux", str(ctx.exception).lower())


class CliTests(CrewCase):
    def test_example_dry_run_exits_zero_and_prints_tmux(self):
        proc = __import__("subprocess").run(
            [sys.executable, str(REPO / "crew.py"), "up", "--dry-run",
             str(REPO / "examples" / "crew-two-seat.toml")],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("tmux new-session", proc.stdout)
        self.assertIn("cursor-agent", proc.stdout)
        self.assertIn("codex", proc.stdout)
        self.assertIn("recv", proc.stdout)
        self.assertNotIn("send-keys", proc.stdout)

    def test_parent_dry_run_flag_does_not_launch(self):
        proc = __import__("subprocess").run(
            [sys.executable, str(REPO / "crew.py"), "--dry-run", "up",
             str(REPO / "examples" / "crew-two-seat.toml")],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("tmux new-session", proc.stdout)
        self.assertNotIn("is up", proc.stderr)

    def test_desk_dry_run_prints_holds_not_harnesses(self):
        proc = __import__("subprocess").run(
            [sys.executable, str(REPO / "crew.py"), "desk", "--dry-run",
             str(REPO / "examples" / "crew-four.toml")],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("_hold", proc.stdout)
        self.assertIn("_board", proc.stdout)
        self.assertNotIn("cursor-agent --model", proc.stdout)


class DeskTests(CrewCase):
    def four_seat(self) -> Path:
        (self.dir / "prompts" / "cursor.md").write_text("you are cursor\n", encoding="utf-8")
        (self.dir / "prompts" / "claude.md").write_text("you are claude\n", encoding="utf-8")
        body = f"""
channel = "four"
cwd     = "{self.dir}"

[[seat]]
name    = "orch"
harness = "cursor-agent"
model   = "cursor-grok-4.6-xhigh-fast"
flags   = ["--force"]
prompt  = "prompts/orch.md"

[[seat]]
name    = "cursor"
harness = "cursor-agent"
model   = "cursor-grok-4.6-xhigh-fast"
flags   = ["--force"]
prompt  = "prompts/cursor.md"

[[seat]]
name    = "claude"
harness = "claude"
model   = "sonnet"
prompt  = "prompts/claude.md"

[[seat]]
name    = "codex"
harness = "codex-cli"
model   = "gpt-5.6-sol"
prompt  = "prompts/peer.md"
"""
        return self.write_toml(body, "four.toml")

    def test_desk_actions_are_human_load_buttons(self):
        cfg = crew.load_config(self.four_seat())
        actions = crew.desk_actions(cfg)
        ids = [a["id"] for a in actions]
        self.assertEqual(ids[:5], ["load:orch", "load:cursor", "load:claude",
                                   "load:codex", "load-all"])
        self.assertIn("quit", ids)
        labels = " ".join(a["label"].lower() for a in actions)
        self.assertIn("load orch", labels)
        self.assertIn("cursor", labels)
        self.assertIn("claude", labels)
        self.assertIn("codex", labels)
        self.assertIn("add-one", ids)
        self.assertIn("add-four", ids)

    def test_balanced_size_controls_can_grow_a_team_by_one_or_four(self):
        cfg = crew.load_config(self.four_seat())
        with mock.patch.object(crew, "_live_add_pane") as live_add:
            added = crew._add_balanced(cfg, 4)
        self.assertEqual(len(cfg.seats), 8)
        self.assertEqual(len(added), 4)
        self.assertEqual(live_add.call_count, 4)
        counts = {harness: 0 for harness in crew.ADDABLE}
        for seat in cfg.seats:
            if seat.harness in counts:
                counts[seat.harness] += 1
        self.assertLessEqual(max(counts.values()) - min(counts.values()), 1)

    def test_desk_load_opens_harness_without_sending_its_prompt(self):
        cfg = crew.load_config(self.four_seat())
        argv = crew.respawn_argv(cfg, cfg.seats[0], "%12")
        self.assertEqual(argv[:3], ["tmux", "respawn-pane", "-k"])
        self.assertIn("%12", argv)
        self.assertIn("cursor-agent", argv)
        self.assertNotIn("you are orch", argv)
        self.assertNotIn("-p", argv)
        self.assertNotIn("exec", argv)

    def test_load_action_does_not_send_keys_or_start_a_conversation(self):
        cfg = crew.load_config(self.four_seat())
        with mock.patch.object(crew, "_read_panes", return_value={"orch": "%12"}), \
                mock.patch.object(crew, "_run") as run, \
                mock.patch.object(crew, "_style_pane") as style, \
                mock.patch.object(crew.subprocess, "run") as subprocess_run:
            result = crew.do_action(cfg, "load:orch")
        self.assertEqual(result, "loaded")
        self.assertNotIn("you are orch", run.call_args.args[0])
        style.assert_called_once()
        subprocess_run.assert_not_called()

    def test_desk_plan_opens_empty_panes_and_a_control_bar(self):
        cfg = crew.load_config(self.four_seat())
        text = crew.format_plan(crew.build_desk_plan(cfg))
        self.assertIn("_hold", text)
        self.assertIn("_board", text)
        self.assertIn("split-window", text)
        self.assertIn("-x 180", text)
        self.assertIn(f"-l {crew.BOARD_HEIGHT}", text)
        self.assertIn("window-size latest", text)
        self.assertIn("aggressive-resize on", text)
        self.assertIn("attach-session -d", text)
        self.assertNotIn("cursor-agent --model", text)
        self.assertNotIn("codex --model", text)
        for line in text.splitlines():
            if "split-window" in line:
                self.assertIn(f"-t ={cfg.session}:", line)

    def test_example_four_seat_config_is_two_cursors_claude_codex(self):
        path = REPO / "examples" / "crew-four.toml"
        cfg = crew.load_config(path)
        self.assertEqual([s.name for s in cfg.seats], ["orch", "Scout", "Claude", "Codex"])
        self.assertEqual([s.harness for s in cfg.seats],
                         ["cursor-agent", "cursor-agent", "claude", "codex-cli"])

    def test_pane_title_marks_the_orchestrator(self):
        cfg = crew.load_config(self.four_seat())
        self.assertTrue(crew.pane_title(cfg.seats[0]).startswith("ORCH"))
        self.assertIn("cursor", crew.pane_title(cfg.seats[0]))
        self.assertFalse(crew.pane_title(cfg.seats[1]).startswith("ORCH"))
        self.assertIn("cursor", crew.pane_title(cfg.seats[1]))

    def test_add_and_cycle_and_remove_seats(self):
        cfg = crew.load_config(self.four_seat())
        added = crew.add_seat(cfg, "claude")
        self.assertEqual(added.harness, "claude")
        self.assertEqual(added.role, "worker")
        self.assertEqual(len(cfg.seats), 5)
        crew.cycle_harness(cfg, added.name)
        self.assertEqual(added.harness, "codex-cli")
        crew.remove_seat(cfg, added.name)
        self.assertEqual(len(cfg.seats), 4)
        with self.assertRaises(crew.CrewError):
            crew.remove_seat(cfg, "orch")

    def test_desk_plan_forces_a_dark_truecolor_window(self):
        cfg = crew.load_config(self.four_seat())
        text = crew.format_plan(crew.build_desk_plan(cfg))
        self.assertIn("window-style", text)
        self.assertIn("#1a1a1a", text)
        self.assertIn("RGB", text)
        self.assertIn("COLORFGBG", text)

    def test_desk_plan_keeps_mouse_clicks_out_of_agent_prompts(self):
        cfg = crew.load_config(self.four_seat())
        text = crew.format_plan(crew.build_desk_plan(cfg))
        self.assertIn("MouseDown1Pane", text)
        self.assertIn("MouseDrag1Pane", text)
        self.assertIn("@crew-board", text)
        self.assertIn("@crew-seat", text)

    def test_fresh_desk_root_preserves_runs_but_does_not_replay_them(self):
        root = self.dir / "bus"
        fresh = crew._fresh_desk_root(root)
        self.assertEqual(fresh.parent.parent, root)
        self.assertEqual(fresh.parent.name, "crew-runs")
        self.assertNotEqual(fresh, root)

    def test_agent_pane_survives_harness_exit_and_has_live_identity(self):
        cfg = crew.load_config(self.four_seat())
        with mock.patch.object(crew.subprocess, "run") as run:
            crew._style_pane("%9", cfg.seats[0], waiting=True)
        commands = [call.args[0] for call in run.call_args_list]
        rendered = "\n".join(" ".join(command) for command in commands)
        self.assertIn("@crew-seat orch", rendered)
        self.assertIn("remain-on-exit on", rendered)
        self.assertIn("Agent closed", rendered)
        for call in run.call_args_list:
            self.assertIs(call.kwargs["stderr"], crew.subprocess.DEVNULL)

    def test_stale_pane_registry_is_repaired_from_live_pane_tags(self):
        cfg = crew.load_config(self.four_seat(), root=self.dir / "root")
        crew._write_panes(cfg, {"orch": "%0", "cursor": "%1"})
        live = [
            {"pane_id": "%7", "seat": "orch", "board": "", "dead": "0",
             "title": "ORCH"},
            {"pane_id": "%8", "seat": "cursor", "board": "", "dead": "1",
             "title": "Cursor"},
            {"pane_id": "%9", "seat": "", "board": "1", "dead": "0",
             "title": "CREW"},
        ]
        with mock.patch.object(crew, "_live_pane_records", return_value=live):
            panes = crew._read_panes(cfg)
        self.assertEqual(panes, {"orch": "%7", "cursor": "%8"})
        self.assertEqual(json.loads(crew.panes_path(cfg).read_text()), panes)

    def test_live_add_splits_a_worker_pane_instead_of_the_control_board(self):
        cfg = crew.load_config(self.four_seat())
        seat = crew.add_seat(cfg, "claude", name="Reviewer")
        existing = {"orch": "%10", "cursor": "%11", "claude": "%12", "codex": "%13"}
        live = [
            {"pane_id": "%10", "seat": "orch", "board": "", "state": "empty",
             "dead": "0", "width": "100", "height": "30", "title": "ORCH"},
            {"pane_id": "%11", "seat": "cursor", "board": "", "state": "empty",
             "dead": "0", "width": "80", "height": "20", "title": "Cursor"},
            {"pane_id": "%12", "seat": "claude", "board": "", "state": "empty",
             "dead": "0", "width": "70", "height": "20", "title": "Claude"},
            {"pane_id": "%13", "seat": "codex", "board": "", "state": "empty",
             "dead": "0", "width": "60", "height": "20", "title": "Codex"},
            {"pane_id": "%20", "seat": "", "board": "1", "state": "",
             "dead": "0", "width": "180", "height": "13", "title": "CREW"},
        ]
        with mock.patch.object(crew, "_session_exists", return_value=True), \
                mock.patch.object(crew, "_read_panes", return_value=existing.copy()), \
                mock.patch.object(crew, "_live_pane_records", return_value=live), \
                mock.patch.object(crew.subprocess, "check_output", return_value="%14\n") as output, \
                mock.patch.object(crew, "_write_panes"), \
                mock.patch.object(crew, "_style_pane"):
            crew._live_add_pane(cfg, seat)
        command = output.call_args.args[0]
        self.assertIn("-h", command)
        self.assertEqual(command[command.index("-t") + 1], "%10")

    def test_desk_actions_include_add_provider_buttons(self):
        cfg = crew.load_config(self.four_seat())
        ids = [a["id"] for a in crew.desk_actions(cfg)]
        self.assertIn("add:cursor-agent", ids)
        self.assertIn("add:claude", ids)
        self.assertIn("add:codex-cli", ids)
        self.assertIn("remove-last", ids)

    def test_board_cards_name_seats_and_expose_effort_provider_delete(self):
        cfg = crew.load_config(self.four_seat())
        cards = crew.board_cards(cfg)
        self.assertEqual([c["name"] for c in cards], ["orch", "cursor", "claude", "codex"])
        self.assertEqual(cards[0]["label"], "Orch")
        self.assertIn("start", cards[0]["actions"])
        self.assertIn("effort", cards[0]["actions"])
        self.assertIn("provider", cards[0]["actions"])
        self.assertNotIn("delete", cards[0]["actions"])
        self.assertIn("delete", cards[1]["actions"])
        self.assertEqual(cards[0]["effort"], "xhigh")

    def test_claude_argv_includes_effort_flag(self):
        cfg = crew.load_config(self.four_seat())
        claude = cfg.seats[2]
        claude.effort = "max"
        argv = crew.seat_argv(claude)
        self.assertIn("--effort", argv)
        self.assertIn("max", argv)
        self.assertNotIn("-p", argv)

    def test_cursor_argv_puts_effort_on_the_model(self):
        cfg = crew.load_config(self.four_seat())
        orch = cfg.seats[0]
        orch.effort = "high"
        argv = crew.seat_argv(orch)
        model = argv[argv.index("--model") + 1]
        self.assertEqual(model, "cursor-grok-4.6-high-fast")

    def test_cursor_model_without_explicit_effort_is_not_mangled(self):
        cfg = crew.load_config(self.four_seat())
        orch = cfg.seats[0]
        orch.model = "auto"
        orch.effort = "high"
        argv = crew.seat_argv(orch)
        self.assertEqual(argv[argv.index("--model") + 1], "auto")

    def test_cycle_effort_and_named_add(self):
        cfg = crew.load_config(self.four_seat())
        crew.cycle_effort(cfg, "claude")
        self.assertNotEqual(cfg.seats[2].effort, "high")
        added = crew.add_seat(cfg, "cursor-agent", name="Scout", effort="max")
        self.assertEqual(added.name, "Scout")
        self.assertEqual(added.label, "Scout")
        self.assertEqual(added.effort, "max")


class LauncherTests(unittest.TestCase):
    def test_crew_app_opens_terminal_and_the_desk(self):
        exe = REPO / "Crew.app" / "Contents" / "MacOS" / "Crew"
        self.assertTrue(exe.is_file(), "Crew.app is the clickable starter")
        text = exe.read_text(encoding="utf-8")
        self.assertIn("crew.py desk", text)
        self.assertIn("Terminal", text)
        self.assertNotIn("tmux attach", text)

    def test_shortcut_copies_the_app_to_a_folder(self):
        dest = Path(tempfile.mkdtemp())
        installed = crew.install_shortcut(dest)
        self.assertEqual(installed, dest / "Crew.app")
        self.assertTrue((installed / "Contents" / "MacOS" / "Crew").is_file())
        self.assertIn("crew.py desk",
                      (installed / "Contents" / "MacOS" / "Crew").read_text(encoding="utf-8"))
        self.assertTrue((dest / "Crew.command").is_file())

    def test_launchers_find_the_repo_in_place_and_when_installed(self):
        dest = Path(tempfile.mkdtemp())
        installed = crew.install_shortcut(dest)
        fakebin = Path(tempfile.mkdtemp())
        (fakebin / "open").write_text('#!/bin/sh\necho "$@"\n')
        (fakebin / "open").chmod(0o755)
        env = dict(os.environ, PATH=f"{fakebin}:{os.environ['PATH']}")
        for exe in (REPO / "Crew.app" / "Contents" / "MacOS" / "Crew",
                    installed / "Contents" / "MacOS" / "Crew"):
            out = subprocess.run([str(exe)], env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(out.stdout, f"-a Terminal {REPO}/Open Crew.command\n", out.stderr)
        pinned = f"REPO={shlex.quote(str(REPO))}\n"
        self.assertIn(pinned, (dest / "Crew.command").read_text(encoding="utf-8"))
        self.assertTrue(os.access(dest / "Crew.command", os.X_OK))

    def test_shortcut_skips_the_quarantine_step_without_xattr(self):
        dest = Path(tempfile.mkdtemp())
        with mock.patch.object(crew.shutil, "which", return_value=None), \
                mock.patch.object(crew.subprocess, "run") as run:
            self.assertEqual(crew.install_shortcut(dest), dest / "Crew.app")
        run.assert_not_called()


class StatusWrapTests(CrewCase):
    def test_status_wraps_bus_status(self):
        root = self.dir / "busroot"
        bus_mod = __import__("bus")
        bus_mod.send(root, "radar", "orch", "cursor-agent", "hello from orch")
        cfg = crew.load_config(self.write_toml(self.two_seat()), root=root)
        st = crew.channel_status(cfg)
        self.assertEqual(st["channel"], "radar")
        self.assertEqual(st["message_count"], 1)
        self.assertFalse(st["complete"])
        self.assertIsNone(st["participants"][0]["done"])


if __name__ == "__main__":
    unittest.main()
