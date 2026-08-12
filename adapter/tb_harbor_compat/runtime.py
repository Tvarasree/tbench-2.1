"""Runtime resilience hooks for Harbor 0.13.1 package tasks."""
from __future__ import annotations

import asyncio
import importlib.metadata
import pathlib
import re
import sys
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, TypeVar

SUPPORTED_HARBOR_VERSION = "0.13.1"
REGISTRY_ATTEMPTS = 5
REGISTRY_DELAYS = (1.0, 2.0, 4.0, 8.0)
_REQUIRED_PACKAGE_ENTRIES = ("task.toml", "instruction.md", "environment", "tests")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_T = TypeVar("_T")


class CacheIntegrityError(ValueError):
    """An exact immutable cache entry exists but is incomplete."""


@dataclass(frozen=True)
class LocalResolvedPackage:
    id: str
    archive_path: str
    content_hash: str


def _missing_package_entries(path: pathlib.Path) -> list[str]:
    missing: list[str] = []
    for name in _REQUIRED_PACKAGE_ENTRIES:
        entry = path / name
        valid = entry.is_file() if "." in name else entry.is_dir()
        if not valid:
            missing.append(name)
    return missing


def is_retryable_registry_error(exc: BaseException) -> bool:
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    if type(exc).__module__.startswith("httpx") and type(exc).__name__.endswith(
        "RequestError"
    ):
        return True
    if isinstance(exc, ValueError):
        message = str(exc).lower()
        return any(
            marker in message
            for marker in (
                "task version not found",
                "dataset version not found",
                "not found for dataset",
                "error getting dataset",
                "no versions available",
            )
        )
    if type(exc).__name__ == "APIError":
        code = str(getattr(exc, "code", ""))
        status = getattr(exc, "status_code", None)
        return code in {"PGRST301", "PGRST302", "PGRST303"} or (
            isinstance(status, int) and status >= 500
        )
    return False


async def retry_registry(
    operation: Callable[[], Awaitable[_T]],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> _T:
    for attempt in range(REGISTRY_ATTEMPTS):
        try:
            return await operation()
        except BaseException as exc:
            if not is_retryable_registry_error(exc) or attempt == REGISTRY_ATTEMPTS - 1:
                raise
            await sleep(REGISTRY_DELAYS[attempt])
    raise RuntimeError("registry retry loop exhausted without a result")


async def resolve_local_first(
    task_id: Any,
    *,
    package_cache_dir: pathlib.Path,
    registry_resolve: Callable[[Any], Awaitable[_T]],
) -> LocalResolvedPackage | _T:
    ref = getattr(task_id, "ref", None)
    if isinstance(ref, str) and ref.startswith("sha256:"):
        digest = ref.removeprefix("sha256:")
        if _DIGEST_RE.fullmatch(digest):
            local_path = (
                package_cache_dir / str(task_id.org) / str(task_id.name) / digest
            )
            if local_path.exists():
                missing = _missing_package_entries(local_path)
                if missing:
                    raise CacheIntegrityError(
                        f"Corrupt cached package {task_id.org}/{task_id.name}@{ref}: "
                        f"missing {', '.join(missing)}"
                    )
                return LocalResolvedPackage(
                    id=f"local-cache:{task_id.org}/{task_id.name}@{ref}",
                    archive_path="",
                    content_hash=digest,
                )

    return await retry_registry(lambda: registry_resolve(task_id))


async def gather_trial_results(
    coroutines: list[Coroutine[Any, Any, _T]],
) -> list[_T]:
    results = await asyncio.gather(*coroutines, return_exceptions=True)
    failures = [result for result in results if isinstance(result, BaseException)]
    if failures:
        cancellations = [
            failure
            for failure in failures
            if isinstance(failure, asyncio.CancelledError)
        ]
        if cancellations:
            raise cancellations[0]
        raise ExceptionGroup("isolated trial failures", failures)
    return list(results)  # type: ignore[arg-type]


def _check_version() -> None:
    version = importlib.metadata.version("harbor")
    if version != SUPPORTED_HARBOR_VERSION:
        raise RuntimeError(
            f"tb_harbor_compat supports harbor {SUPPORTED_HARBOR_VERSION}, got {version}"
        )


def activate() -> None:
    """Install idempotent local-first and registry-retry runtime hooks."""
    _check_version()
    from harbor.constants import PACKAGE_CACHE_DIR
    from harbor.registry.client.package import PackageDatasetClient
    from harbor.tasks.client import TaskClient

    if not getattr(TaskClient._resolve_package_version, "_tb_resilient", False):
        original_task_resolve = TaskClient._resolve_package_version

        async def resilient_task_resolve(self, task_id):
            return await resolve_local_first(
                task_id,
                package_cache_dir=PACKAGE_CACHE_DIR,
                registry_resolve=lambda value: original_task_resolve(self, value),
            )

        resilient_task_resolve._tb_resilient = True  # type: ignore[attr-defined]
        TaskClient._resolve_package_version = resilient_task_resolve

    if not getattr(PackageDatasetClient._get_dataset_metadata, "_tb_resilient", False):
        original_dataset_resolve = PackageDatasetClient._get_dataset_metadata

        async def resilient_dataset_resolve(self, name):
            return await retry_registry(lambda: original_dataset_resolve(self, name))

        resilient_dataset_resolve._tb_resilient = True  # type: ignore[attr-defined]
        PackageDatasetClient._get_dataset_metadata = resilient_dataset_resolve

    print(
        "[tb-harbor-resilience] local-first package cache + 5-attempt registry retry active",
        file=sys.stderr,
    )
