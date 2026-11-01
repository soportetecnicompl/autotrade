import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class CliHelpTests(unittest.TestCase):
    """La ayuda de cada comando debe poder mostrarse (un '%' sin escapar en un texto de
    ayuda de argparse rompe --help con TypeError)."""

    def run_help(self, *args):
        r = subprocess.run([sys.executable, *args, "--help"], cwd=ROOT, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        return r.stdout

    def test_backtest_helps(self):
        self.assertIn("--balance", self.run_help("backtest.py", "run"))
        self.assertIn("--days", self.run_help("backtest.py", "fetch"))
        self.assertIn("run", self.run_help("backtest.py"))

    def test_main_help(self):
        self.assertIn("--list-dialogs", self.run_help("main.py"))


if __name__ == "__main__":
    unittest.main()
