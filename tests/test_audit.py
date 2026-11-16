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
