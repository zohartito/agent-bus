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
