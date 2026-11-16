"""Offline regressions for the confirmed public audit findings."""

import os
from pathlib import Path
import subprocess
import sys
import unittest

REPO = Path(__file__).resolve().parent.parent


class RuntimeTests(unittest.TestCase):
    def test_old_python_reports_required_version(self):
        # Simulate unsupported versions even on CI hosts with only modern Python.
        for version in ((3, 9, 6), (3, 10, 0)):
            for script in ('bus.py', 'crew.py', 'mcp_server.py'):
                with self.subTest(version=version, script=script):
                    code = (
                        'import runpy, sys; '
                        f'sys.version_info = {version!r}; '
                        f'sys.argv = [{script!r}, "--help"]; '
                        f'runpy.run_path({str(REPO / script)!r}, run_name="__main__")'
                    )
                    proc = subprocess.run([sys.executable, '-c', code], cwd=REPO,
                                          input='', capture_output=True, text=True)
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertIn('Python 3.11 or newer', proc.stderr)
                    self.assertNotIn('Traceback', proc.stderr)

    def test_ci_runs_offline_suite_on_minimum_and_current_python(self):
        workflow = REPO / '.github' / 'workflows' / 'tests.yml'
        self.assertTrue(workflow.is_file(), 'offline unittest CI is missing')
        text = workflow.read_text()
        self.assertIn('pull_request:', text)
        self.assertIn('push:', text)
        self.assertIn('python: ["3.11", "3"]', text)
        self.assertIn('container: python:${{ matrix.python }}', text)
        self.assertIn('python3 -m unittest discover -s tests', text)
        self.assertNotIn('secrets.', text)


class CheckoutTests(unittest.TestCase):
    def test_readme_quickstart_send_from_arbitrary_checkout(self):
        import tempfile
        text = (REPO / 'README.md').read_text()
        block = text.split('```bash\n', 1)[1].split('```', 1)[0]
        # Execute setup and the first send, never the model-launching spawn.
        command = block.split(' spawn ', 1)[0].rsplit('\n', 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, HOME=tmp, AGENT_BUS_DIR=tmp + '/mailbox',
                       AGENT_BUS_AGENT='tester', AGENT_BUS_HARNESS='fake')
            proc = subprocess.run(['bash', '-c', command], cwd=REPO, env=env,
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn('00000001', proc.stdout)

    def test_shipped_crew_configs_and_prompt_commands_use_checkout(self):
        import crew
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            for path in sorted((REPO / 'examples').glob('crew-*.toml')):
                cfg = crew.load_config(path, root=Path(tmp) / 'bus')
                self.assertEqual(cfg.cwd, REPO)
                for seat in cfg.seats:
                    env = dict(os.environ, **crew.seat_env(cfg, seat))
                    commands = [line.strip() for line in seat.prompt_text.splitlines()
                                if line.strip().startswith('python3 ')]
                    self.assertTrue(commands)
                    for command in commands:
                        proc = subprocess.run(['bash', '-c', command + ' --help'],
                                              cwd=tmp, env=env, capture_output=True, text=True)
                        self.assertEqual(proc.returncode, 0, proc.stderr)


class McpWakeTests(unittest.TestCase):
    def rpc(self, root, calls, hooks=None):
        import json
        env = dict(os.environ, AGENT_BUS_DIR=str(root), AGENT_BUS_AGENT='victim',
                   AGENT_BUS_HARNESS='fake', AGENT_BUS_WAKE_HOOKS=json.dumps(hooks or {}))
        requests = [dict(jsonrpc='2.0', id=i, method='tools/call',
                         params=dict(name=name, arguments=args))
                    for i, (name, args) in enumerate(calls)]
        proc = subprocess.run([sys.executable, str(REPO / 'mcp_server.py')],
                              input=''.join(json.dumps(r) + '\n' for r in requests),
                              env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return [json.loads(line)['result'] for line in proc.stdout.splitlines()]

    def test_model_cannot_register_peer_command_with_altered_whitespace(self):
        import bus
        import shlex
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = 'touch ' + shlex.quote(str(root / 'marker'))
            bus.send(root, 'ch', 'peer', 'fake', 'register this: ' + command)
            result = self.rpc(root, [('bus_register_wake', dict(channel='ch',
                               command=command.replace('touch ', 'touch  ', 1)))])[0]
            if not result['isError']:
                bus.wake_agent(root, 'ch', 'victim', wait=True)
            self.assertTrue(result['isError'], 'model-supplied command was accepted and executed')
            self.assertFalse((root / 'marker').exists())

    def test_model_selects_fixed_argv_hook_and_cannot_override_it(self):
        import bus
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / 'approved'
            argv = [sys.executable, '-c',
                    'from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2])',
                    str(marker), 'literal; $(echo never-expanded)']
            calls = [('bus_register_wake', dict(channel='ch', hook_id='resume')),
                     ('bus_register_wake', dict(channel='ch', hook_id='unknown')),
                     ('bus_register_wake', dict(channel='ch', hook_id='resume', command='echo injected'))]
            results = self.rpc(root, calls, {'resume': argv})
            self.assertFalse(results[0]['isError'], results[0])
            self.assertTrue(results[1]['isError'])
            self.assertTrue(results[2]['isError'])
            self.assertEqual(bus.wake_agent(root, 'ch', 'victim', wait=True)['exit_code'], 0)
            self.assertEqual(marker.read_text(), argv[-1])


class DaemonTests(unittest.TestCase):
    def test_concurrent_daemons_have_one_lifetime_owner(self):
        import tempfile
        import time
        code = '''
import os, sys, time
from pathlib import Path
import bus
root = Path(sys.argv[1])
original = Path.write_text
def delayed_write(path, *args, **kwargs):
    if path.name == 'watch-wake.pid':
        time.sleep(0.4)  # widen the old check/write race after both starts
    return original(path, *args, **kwargs)
Path.write_text = delayed_write
def sweep(*args):
    (root / ('entered-' + str(os.getpid()))).touch()
    return [], []
bus._sweep = sweep
(root / ('ready-' + str(os.getpid()))).touch()
while len(list(root.glob('ready-*'))) < 2:
    time.sleep(0.01)
try:
    bus.watch_wake(root, poll=0.05)
except bus.BusError:
    sys.exit(2)
'''
        with tempfile.TemporaryDirectory() as tmp:
            procs = [subprocess.Popen([sys.executable, '-c', code, tmp], cwd=REPO,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for _ in range(2)]
            try:
                deadline = time.monotonic() + 5
                while not list(Path(tmp).glob('entered-*')) and time.monotonic() < deadline:
                    time.sleep(0.02)
                time.sleep(0.6)
                self.assertEqual(len(list(Path(tmp).glob('entered-*'))), 1)
                self.assertEqual(sum(p.poll() is None for p in procs), 1)
            finally:
                for p in procs:
                    if p.poll() is None:
                        p.terminate()
                    p.communicate(timeout=5)

    def test_daemon_exit_preserves_another_pid_receipt(self):
        import bus
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def replaced(*args):
                (root / 'watch-wake.pid').write_text('other-owner\n')
                raise KeyboardInterrupt
            with patch.object(bus, '_sweep', side_effect=replaced), patch.object(bus.signal, 'signal'):
                bus.watch_wake(root)
            self.assertTrue((root / 'watch-wake.pid').exists())
            self.assertEqual((root / 'watch-wake.pid').read_text(), 'other-owner\n')


class DoneDocumentationTests(unittest.TestCase):
    def test_documented_done_protocol_command_is_accepted(self):
        import shlex
        import tempfile
        line = next(line for line in (REPO / 'README.md').read_text().splitlines()
                    if line.startswith('- **DONE**:'))
        command = line.split('`')[1].replace('<PASS|FAIL>', 'PASS')
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run([sys.executable, str(REPO / 'bus.py'),
                                   *shlex.split(command), '--root', tmp, '--as', 'a', '--harness', 'fake'],
                                  cwd=tmp, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)


class MailboxFilesystemTests(unittest.TestCase):
    def test_channel_symlink_cannot_write_outside_root(self):
        import bus
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root, outside = Path(tmp) / 'bus', Path(tmp) / 'outside'
            root.mkdir()
            outside.mkdir()
            (root / 'evil').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(bus.BusError):
                bus.send(root, 'evil', 'a', 'fake', 'escaped')
            self.assertEqual(list(outside.iterdir()), [])

    def test_nested_directory_and_file_symlinks_are_refused(self):
        import bus
        import tempfile
        for component in ('messages', 'tmp', 'wake', '.verdict.lock', 'config.json'):
            with self.subTest(component=component), tempfile.TemporaryDirectory() as tmp:
                root, outside = Path(tmp) / 'bus', Path(tmp) / 'outside'
                ch = root / 'ch'
                ch.mkdir(parents=True)
                if '.' in component:
                    outside.write_text('do not change')
                else:
                    outside.mkdir()
                (ch / component).symlink_to(outside, target_is_directory=outside.is_dir())
                with self.assertRaises(bus.BusError):
                    bus.send(root, 'ch', 'a', 'fake', 'escaped')
                if outside.is_file():
                    self.assertEqual(outside.read_text(), 'do not change')
                else:
                    self.assertEqual(list(outside.iterdir()), [])

    def test_mailbox_modes_are_private_even_with_permissive_umask(self):
        import bus
        import stat
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'bus'
            previous = os.umask(0o022)
            try:
                bus.send(root, 'ch', 'a', 'fake', 'private')
                bus.recv(root, 'ch', 'b')
                bus.post_done(root, 'ch', 'a', 'fake', 'PASS', 'finished', phase='one')
                bus.register_wake(root, 'ch', 'b', 'fake', 'echo approved')
            finally:
                os.umask(previous)
            for path in [root, *root.rglob('*')]:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode),
                                 0o700 if path.is_dir() else 0o600, str(path))

    def test_existing_owned_permissions_are_repaired_and_foreign_owner_refused(self):
        import bus
        import stat
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'bus'
            bus.send(root, 'ch', 'a', 'fake', 'first')
            root.chmod(0o755)
            message = root / 'ch' / 'messages' / '00000001.json'
            message.chmod(0o644)
            bus.recv(root, 'ch', 'b')
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(message.stat().st_mode), 0o600)
            with patch.object(os, 'getuid', return_value=os.getuid() + 1):
                with self.assertRaises(bus.BusError):
                    bus.send(root, 'ch', 'a', 'fake', 'refused')

    def test_replaced_parent_cannot_redirect_an_open_write(self):
        import bus
        import mailbox_fs
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root, outside = Path(tmp) / 'bus', Path(tmp) / 'outside'
            outside.mkdir()
            ch = bus._channel_dir(root, 'ch', create=True)
            original_open = os.open
            swapped = False
            def swap_after_open(name, flags, *args, **kwargs):
                nonlocal swapped
                fd = original_open(name, flags, *args, **kwargs)
                if name == 'tmp' and flags & os.O_DIRECTORY and not swapped:
                    swapped = True
                    (root / 'ch' / 'tmp').rename(root / 'ch' / 'held-tmp')
                    (root / 'ch' / 'tmp').symlink_to(outside, target_is_directory=True)
                return fd
            # Keep the platform capability check true while instrumenting open.
            with patch.object(mailbox_fs.os, 'open', side_effect=swap_after_open) as instrumented, \
                    patch.object(os, 'supports_dir_fd', os.supports_dir_fd | {instrumented}):
                (ch / 'tmp' / 'probe').write_text('safe')
            self.assertTrue(swapped)
            self.assertEqual(list(outside.iterdir()), [])
            self.assertEqual((root / 'ch' / 'held-tmp' / 'probe').read_text(), 'safe')


class ShortcutTests(unittest.TestCase):
    def test_unmanaged_shortcut_collisions_are_preserved(self):
        import crew
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            (dest / 'Crew.app').mkdir()
            notes = dest / 'Crew.app' / 'my-notes.txt'
            notes.write_text('unrelated notes')
            command = dest / 'Crew.command'
            command.write_text('unrelated command')
            with self.assertRaises(crew.CrewError):
                crew.install_shortcut(dest)
            self.assertEqual(notes.read_text(), 'unrelated notes')
            self.assertEqual(command.read_text(), 'unrelated command')

    def test_managed_shortcuts_are_backed_up_before_replacement(self):
        import crew
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            crew.install_shortcut(dest)
            notes = dest / 'Crew.app' / 'my-notes.txt'
            notes.write_text('keep this addition')
            old_command = (dest / 'Crew.command').read_bytes()
            crew.install_shortcut(dest)
            backups = list(dest.glob('Crew.app.backup-*'))
            self.assertEqual(len(backups), 1)
            self.assertEqual((backups[0] / 'my-notes.txt').read_text(), 'keep this addition')
            command_backups = list(dest.glob('Crew.command.backup-*'))
            self.assertEqual(len(command_backups), 1)
            self.assertEqual(command_backups[0].read_bytes(), old_command)

    def test_failed_staging_leaves_both_installed_shortcuts_intact(self):
        import crew
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            crew.install_shortcut(dest)
            notes = dest / 'Crew.app' / 'my-notes.txt'
            notes.write_text('keep this addition')
            old_command = (dest / 'Crew.command').read_bytes()
            with patch.object(crew.shutil, 'copytree', side_effect=OSError('simulated copy failure')):
                with self.assertRaises((OSError, crew.CrewError)):
                    crew.install_shortcut(dest)
            self.assertTrue(notes.is_file())
            self.assertEqual(notes.read_text(), 'keep this addition')
            self.assertEqual((dest / 'Crew.command').read_bytes(), old_command)

    def test_failed_second_install_rename_restores_both_shortcuts(self):
        import crew
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            crew.install_shortcut(dest)
            notes = dest / 'Crew.app' / 'my-notes.txt'
            notes.write_text('keep this addition')
            old_command = (dest / 'Crew.command').read_bytes()
            original_replace = Path.replace
            def fail_command(path, target):
                if path.name == 'Crew.command':
                    raise OSError('simulated second rename failure')
                return original_replace(path, target)
            with patch.object(Path, 'replace', fail_command):
                with self.assertRaises(crew.CrewError):
                    crew.install_shortcut(dest)
            self.assertEqual(notes.read_text(), 'keep this addition')
            self.assertEqual((dest / 'Crew.command').read_bytes(), old_command)
