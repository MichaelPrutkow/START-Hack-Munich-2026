"""Historical evaluations must use their own source revision, including split modules."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import eval_cases as E
import runner as R
from claim_to_fame import pricing, prompts

SOURCE = Path(R.__file__).resolve().parent


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.multiple(E, ROOT=self.root, EVAL=self.root / "out"))
        self.git("init", "-q")

    def git(self, *args):
        return subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
             "-c", "user.name=Offline tests", "-c", "user.email=tests@example.invalid", *args],
            cwd=self.root, check=True, capture_output=True, text=True,
        ).stdout

    def commit(self):
        self.git("add", "runner.py")
        if (self.root / "claim_to_fame").exists():
            self.git("add", "claim_to_fame")
        self.git("commit", "-qm", "Synthetic historical version")

    def test_legacy_single_file_runner_still_loads(self):
        (self.root / "runner.py").write_text("def build_prompt(*args):\n    return 'legacy prompt'\n")
        self.commit()
        old = E.load_old("HEAD")
        self.assertEqual(old.build_prompt("", "", {}), "legacy prompt")
        self.assertIs(sys.modules["claim_to_fame.pricing"], pricing)

    def test_snapshot_uses_historical_modules_and_restores_current_imports(self):
        shutil.copyfile(SOURCE / "runner.py", self.root / "runner.py")
        package = self.root / "claim_to_fame"
        package.mkdir()
        for src in (SOURCE / "claim_to_fame").glob("*.py"):
            shutil.copyfile(src, package / src.name)
        p = package / "prompts.py"
        p.write_text(p.read_text().replace("You review insurance claims.", "Snapshot-specific prompt."))
        p = package / "pricing.py"
        p.write_text(p.read_text().replace("a = a_mult * base", "a = 0.5 * base"))
        self.commit()
        # A working-tree edit must not leak into the selected Git revision either.
        (package / "prompts.py").write_text("raise RuntimeError('working tree was imported')\n")
        saved_path = sys.path[:]
        old = E.load_old("HEAD")
        self.assertTrue(old.build_prompt("", "", {})[0]["content"].startswith("Snapshot-specific prompt."))
        self.assertEqual(old.price(1, 100, None, None, 50)[0], 50)
        self.assertEqual(R.build_prompt("", "", {})[0]["content"], prompts.SYSTEM)
        self.assertIs(sys.modules["claim_to_fame.pricing"], pricing)
        self.assertIs(sys.modules["claim_to_fame.prompts"], prompts)
        self.assertEqual(sys.path, saved_path)

    def test_import_failure_restores_current_modules_and_path(self):
        package = self.root / "claim_to_fame"
        package.mkdir()
        (package / "__init__.py").write_text("")
        (self.root / "runner.py").write_text("import claim_to_fame\nraise RuntimeError('broken snapshot')\n")
        self.commit()
        saved_path = sys.path[:]
        with self.assertRaisesRegex(RuntimeError, "broken snapshot"):
            E.load_old("HEAD")
        self.assertIs(sys.modules["claim_to_fame.pricing"], pricing)
        self.assertIs(sys.modules["claim_to_fame.prompts"], prompts)
        self.assertEqual(sys.path, saved_path)
