#!/usr/bin/env python3
"""Contract tests for deterministic Terminal-Bench package-cache preflight."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import task_cache  # noqa: E402


def make_task(cache_root: pathlib.Path, name: str, digest: str = "a" * 64) -> pathlib.Path:
    task_dir = cache_root / "packages" / "terminal-bench" / name / digest
    task_dir.mkdir(parents=True)
    (task_dir / "task.toml").write_text("version = '1.0'\n", encoding="utf-8")
    (task_dir / "instruction.md").write_text("Do the task.\n", encoding="utf-8")
    (task_dir / "environment").mkdir()
    (task_dir / "tests").mkdir()
    return task_dir


class ConfigFallbackTest(unittest.TestCase):
    def test_nested_config_values_are_available_without_pyyaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = pathlib.Path(tmp) / "config.yaml"
            config.write_text(
                """
dataset:
  name: terminal-bench/terminal-bench-2-1
  harbor_cache_subdir: tasks # inline comment
  tarball:
    gcs_uri: gs://bucket/tasks.tar.zst
    sha256_gcs_uri: gs://bucket/tasks.tar.zst.sha256
""".lstrip(),
                encoding="utf-8",
            )

            values = task_cache.load_config(config, yaml_module=None)

        self.assertEqual(
            task_cache.config_value(values, "dataset.tarball.gcs_uri"),
            "gs://bucket/tasks.tar.zst",
        )
        self.assertEqual(
            task_cache.config_value(values, "dataset.harbor_cache_subdir"), "tasks"
        )

    def test_config_value_cli_supports_shell_helpers_without_pyyaml_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = pathlib.Path(tmp) / "config.yaml"
            config.write_text(
                "dataset:\n  tarball:\n    gcs_uri: gs://bucket/tasks.tar.zst\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "task_cache.py"),
                    "config-value",
                    "--config",
                    str(config),
                    "--key",
                    "dataset.tarball.gcs_uri",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "gs://bucket/tasks.tar.zst")

    def test_dataset_fetch_uses_the_stdlib_config_helper(self) -> None:
        fetch_script = (SCRIPTS_DIR / "fetch_dataset_tarball.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('task_cache.py" config-value', fetch_script)
        self.assertNotIn("import yaml", fetch_script)


class CacheValidationTest(unittest.TestCase):
    def test_valid_selected_tasks_produce_digest_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "tasks"
            alpha = make_task(root, "alpha", "1" * 64)
            beta = make_task(root, "beta", "2" * 64)

            report = task_cache.validate_selected_tasks(
                cache_root=root,
                org="terminal-bench",
                selected_tasks=["alpha", "beta"],
            )

        self.assertEqual(report.task_count, 2)
        self.assertEqual(report.manifest["terminal-bench/alpha"]["digest"], "1" * 64)
        self.assertEqual(
            report.manifest["terminal-bench/alpha"]["path"], str(alpha.resolve())
        )
        self.assertEqual(
            report.manifest["terminal-bench/beta"]["path"], str(beta.resolve())
        )

    def test_missing_required_entry_fails_before_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "tasks"
            task_dir = make_task(root, "alpha")
            (task_dir / "instruction.md").unlink()

            with self.assertRaisesRegex(
                task_cache.CacheValidationError, "alpha.*instruction.md"
            ):
                task_cache.validate_selected_tasks(
                    cache_root=root,
                    org="terminal-bench",
                    selected_tasks=["alpha"],
                )

    def test_multiple_usable_digests_are_rejected_as_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "tasks"
            make_task(root, "alpha", "1" * 64)
            make_task(root, "alpha", "2" * 64)

            with self.assertRaisesRegex(
                task_cache.CacheValidationError, "alpha.*multiple usable digests"
            ):
                task_cache.validate_selected_tasks(
                    cache_root=root,
                    org="terminal-bench",
                    selected_tasks=["alpha"],
                )

    def test_manifest_write_is_atomic_and_contains_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "tasks"
            make_task(root, "alpha")
            output = pathlib.Path(tmp) / "output" / "task_cache_manifest.json"
            report = task_cache.validate_selected_tasks(
                cache_root=root,
                org="terminal-bench",
                selected_tasks=["alpha"],
            )

            task_cache.write_manifest(output, report)
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(payload["task_count"], 1)
        self.assertGreater(payload["cache_bytes"], 0)
        self.assertIn("terminal-bench/alpha", payload["tasks"])
        self.assertFalse(output.with_suffix(output.suffix + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
