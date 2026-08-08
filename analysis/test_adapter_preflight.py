#!/usr/bin/env python3
"""Functional checks for selected-agent preflight behavior in run.sh."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class AdapterPreflightTest(unittest.TestCase):
    def test_broken_selected_pi_adapter_fails_before_harbor(self) -> None:
        """Catches launching benchmark trials after the Pi import has failed."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            output_dir = root / "output"
            bin_dir.mkdir()
            fake_uv = bin_dir / "uv"
            fake_uv.write_text("#!/usr/bin/env bash\nexit 42\n")
            fake_uv.chmod(fake_uv.stat().st_mode | stat.S_IXUSR)

            env = os.environ.copy()
            env.update(
                {
                    "EVAL_RUNNER_OUTPUT_DIR": str(output_dir),
                    "HOME": str(root),
                    "PATH": f"{bin_dir}:/usr/bin:/bin",
                    "XYNE_API_KEY": "",
                }
            )
            completed = subprocess.run(
                ["bash", "run.sh", "preflight-test", "--agent", "pi"],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 1)
            self.assertIn(
                "Selected agent adapter cannot import: pi_harbor_agent.agent",
                completed.stderr,
            )
            result = json.loads(
                (output_dir / "preflight-test_results.json").read_text()
            )
            self.assertEqual(result["metrics"]["additional"]["status"], "no-results")
            self.assertNotIn("harbor run", completed.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
