#!/usr/bin/env python3
"""Process-cleanup regression tests for the dashboard heartbeat."""
from __future__ import annotations

import os
import pathlib
import shlex
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
HEARTBEAT_SCRIPT = ROOT / "scripts" / "heartbeat.sh"


class HeartbeatCleanupTest(unittest.TestCase):
    def test_stopping_heartbeat_reaps_active_sleep(self) -> None:
        """Stopping the Bash loop must not orphan its current sleep process."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            sleep_pid_file = tmp_path / "sleep.pid"
            fake_sleep = tmp_path / "sleep"
            fake_sleep.write_text(
                "#!/usr/bin/env python3\n"
                "import os, pathlib, time\n"
                "pathlib.Path(os.environ['HEARTBEAT_SLEEP_PID_FILE']).write_text(str(os.getpid()))\n"
                "time.sleep(60)\n"
            )
            fake_sleep.chmod(0o755)

            env = os.environ.copy()
            env["PATH"] = f"{tmp_path}:{env['PATH']}"
            env["HEARTBEAT_SLEEP_PID_FILE"] = str(sleep_pid_file)
            command = f"""
source {shlex.quote(str(HEARTBEAT_SCRIPT))}
RUN_DIR={shlex.quote(str(tmp_path / 'run'))}
EXPECTED_TRIALS=1
mkdir -p "$RUN_DIR"
heartbeat & heartbeat_pid=$!
for _ in $(seq 1 100); do
  [ -s "$HEARTBEAT_SLEEP_PID_FILE" ] && break
  /bin/sleep 0.01
done
[ -s "$HEARTBEAT_SLEEP_PID_FILE" ] || exit 20
sleep_pid=$(cat "$HEARTBEAT_SLEEP_PID_FILE")
kill "$heartbeat_pid"
wait "$heartbeat_pid"
if kill -0 "$sleep_pid" 2>/dev/null; then
  kill "$sleep_pid" 2>/dev/null || true
  exit 21
fi
"""
            result = subprocess.run(
                ["bash", "-c", command],
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(
                result.returncode,
                0,
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
