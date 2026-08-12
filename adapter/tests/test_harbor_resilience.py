from __future__ import annotations

import asyncio
import pathlib
import tempfile
import unittest
from dataclasses import dataclass
from unittest import mock

from tb_harbor_compat import install, runtime


@dataclass(frozen=True)
class FakeTaskId:
    org: str
    name: str
    ref: str | None


def make_cached_task(root: pathlib.Path, digest: str = "a" * 64) -> pathlib.Path:
    path = root / "terminal-bench" / "alpha" / digest
    path.mkdir(parents=True)
    (path / "task.toml").write_text("version = '1.0'\n", encoding="utf-8")
    (path / "instruction.md").write_text("task\n", encoding="utf-8")
    (path / "environment").mkdir()
    (path / "tests").mkdir()
    return path


class LocalFirstResolutionTest(unittest.IsolatedAsyncioTestCase):
    async def test_pinned_digest_cache_hit_never_calls_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            make_cached_task(root)
            calls = 0

            async def registry_resolve(_task_id: FakeTaskId):
                nonlocal calls
                calls += 1
                raise AssertionError("registry must not be called")

            resolved = await runtime.resolve_local_first(
                FakeTaskId("terminal-bench", "alpha", f"sha256:{'a' * 64}"),
                package_cache_dir=root,
                registry_resolve=registry_resolve,
            )

        self.assertEqual(resolved.content_hash, "a" * 64)
        self.assertEqual(calls, 0)

    async def test_repeated_attempts_reuse_cache_without_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            make_cached_task(root)
            calls = 0

            async def registry_resolve(_task_id: FakeTaskId):
                nonlocal calls
                calls += 1
                raise AssertionError("registry must not be called")

            for _ in range(5):
                await runtime.resolve_local_first(
                    FakeTaskId("terminal-bench", "alpha", f"sha256:{'a' * 64}"),
                    package_cache_dir=root,
                    registry_resolve=registry_resolve,
                )

        self.assertEqual(calls, 0)

    async def test_corrupt_exact_digest_fails_without_registry_or_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            path = make_cached_task(root)
            (path / "task.toml").unlink()
            calls = 0

            async def registry_resolve(_task_id: FakeTaskId):
                nonlocal calls
                calls += 1

            with self.assertRaises(runtime.CacheIntegrityError):
                await runtime.resolve_local_first(
                    FakeTaskId("terminal-bench", "alpha", f"sha256:{'a' * 64}"),
                    package_cache_dir=root,
                    registry_resolve=registry_resolve,
                )

        self.assertEqual(calls, 0)

    async def test_malformed_sha256_reference_cannot_escape_cache_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            make_cached_task(root)
            (root / "terminal-bench" / "ignored").mkdir()
            calls = 0

            async def registry_resolve(_task_id: FakeTaskId):
                nonlocal calls
                calls += 1
                return "registry-result"

            resolved = await runtime.resolve_local_first(
                FakeTaskId(
                    "terminal-bench",
                    "ignored",
                    f"sha256:../alpha/{'a' * 64}",
                ),
                package_cache_dir=root,
                registry_resolve=registry_resolve,
            )

        self.assertEqual(resolved, "registry-result")
        self.assertEqual(calls, 1)


class RegistryRetryTest(unittest.IsolatedAsyncioTestCase):
    async def test_null_version_error_is_retried_then_succeeds(self) -> None:
        attempts = 0
        sleeps: list[float] = []

        async def operation():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise ValueError("Task version not found: terminal-bench/alpha")
            return "resolved"

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        result = await runtime.retry_registry(operation, sleep=fake_sleep)

        self.assertEqual(result, "resolved")
        self.assertEqual(attempts, 3)
        self.assertEqual(sleeps, [1.0, 2.0])

    async def test_retry_exhaustion_uses_exact_backoff_schedule(self) -> None:
        attempts = 0
        sleeps: list[float] = []

        async def operation():
            nonlocal attempts
            attempts += 1
            raise ValueError("Task version not found: terminal-bench/alpha")

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        with self.assertRaisesRegex(ValueError, "Task version not found"):
            await runtime.retry_registry(operation, sleep=fake_sleep)

        self.assertEqual(attempts, 5)
        self.assertEqual(sleeps, [1.0, 2.0, 4.0, 8.0])

    async def test_invalid_input_is_not_retried(self) -> None:
        attempts = 0

        async def operation():
            nonlocal attempts
            attempts += 1
            raise ValueError("Malformed package identifier")

        with self.assertRaisesRegex(ValueError, "Malformed package"):
            await runtime.retry_registry(operation)

        self.assertEqual(attempts, 1)

    async def test_dataset_tag_miss_is_retried(self) -> None:
        attempts = 0

        async def operation():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ValueError(
                    "Tag 'latest' not found for dataset "
                    "'terminal-bench/terminal-bench-2-1'"
                )
            return "dataset"

        result = await runtime.retry_registry(operation, sleep=lambda _: asyncio.sleep(0))

        self.assertEqual(result, "dataset")
        self.assertEqual(attempts, 2)


class FailureIsolationTest(unittest.IsolatedAsyncioTestCase):
    async def test_one_failure_does_not_cancel_queued_siblings(self) -> None:
        completed: list[str] = []

        async def succeed(name: str, delay: float) -> str:
            await asyncio.sleep(delay)
            completed.append(name)
            return name

        async def fail() -> str:
            await asyncio.sleep(0)
            raise ValueError("task resolution failed")

        with self.assertRaisesRegex(ExceptionGroup, "isolated trial failures"):
            await runtime.gather_trial_results(
                [succeed("first", 0.01), fail(), succeed("last", 0.02)]
            )

        self.assertEqual(completed, ["first", "last"])

    async def test_outer_cancellation_still_cancels_children(self) -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def child() -> None:
            try:
                started.set()
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        parent = asyncio.create_task(runtime.gather_trial_results([child()]))
        await started.wait()
        parent.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await parent
        self.assertTrue(cancelled.is_set())


class HarborSourcePatchTest(unittest.TestCase):
    def test_job_taskgroup_block_is_replaced_and_patch_is_idempotent(self) -> None:
        source = (
            "        coros = self._trial_queue.submit_batch(self._remaining_trial_configs)\n"
            "\n"
            "        async with asyncio.TaskGroup() as tg:\n"
            "            tasks = [tg.create_task(coro) for coro in coros]\n"
            "\n"
            "        return [t.result() for t in tasks]\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "job.py"
            path.write_text(source, encoding="utf-8")

            self.assertTrue(install.patch_job_source(path))
            patched_once = path.read_text(encoding="utf-8")
            self.assertFalse(install.patch_job_source(path))
            patched_twice = path.read_text(encoding="utf-8")

        self.assertEqual(patched_once, patched_twice)
        self.assertIn("gather_trial_results", patched_once)
        self.assertNotIn("asyncio.TaskGroup", patched_once)

    def test_unknown_harbor_source_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "job.py"
            path.write_text("async def changed_upstream(): pass\n", encoding="utf-8")
            with self.assertRaisesRegex(install.PatchCompatibilityError, "0.13.1"):
                install.patch_job_source(path)

    def test_unknown_harbor_version_fails_before_source_patch(self) -> None:
        with mock.patch.object(
            install.importlib.metadata, "version", return_value="0.14.0"
        ):
            with self.assertRaisesRegex(
                install.PatchCompatibilityError,
                "Expected harbor 0.13.1, got 0.14.0",
            ):
                install.patch_installed_harbor()


if __name__ == "__main__":
    unittest.main()
