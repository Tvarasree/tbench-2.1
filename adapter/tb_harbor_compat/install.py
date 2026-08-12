"""Install the exact-source Harbor 0.13.1 failure-isolation patch."""
from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import pathlib
import tempfile

SUPPORTED_HARBOR_VERSION = "0.13.1"
_MARKER = "from tb_harbor_compat.runtime import gather_trial_results"
_ORIGINAL = """        coros = self._trial_queue.submit_batch(self._remaining_trial_configs)

        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(coro) for coro in coros]

        return [t.result() for t in tasks]
"""
_REPLACEMENT = """        coros = self._trial_queue.submit_batch(self._remaining_trial_configs)

        from tb_harbor_compat.runtime import gather_trial_results

        return await gather_trial_results(coros)
"""


class PatchCompatibilityError(RuntimeError):
    """The pinned Harbor source no longer matches the verified patch target."""


def patch_job_source(path: pathlib.Path) -> bool:
    source = path.read_text(encoding="utf-8")
    if _MARKER in source:
        return False
    if _ORIGINAL not in source:
        raise PatchCompatibilityError(
            f"Harbor {SUPPORTED_HARBOR_VERSION} job source does not match the "
            "verified failure-isolation patch target"
        )
    patched = source.replace(_ORIGINAL, _REPLACEMENT, 1)
    fd, temporary_name = tempfile.mkstemp(prefix=path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(patched)
        os.replace(temporary_name, path)
    except BaseException:
        pathlib.Path(temporary_name).unlink(missing_ok=True)
        raise
    return True


def patch_installed_harbor() -> pathlib.Path:
    version = importlib.metadata.version("harbor")
    if version != SUPPORTED_HARBOR_VERSION:
        raise PatchCompatibilityError(
            f"Expected harbor {SUPPORTED_HARBOR_VERSION}, got {version}"
        )
    spec = importlib.util.find_spec("harbor.job")
    if spec is None or spec.origin is None:
        raise PatchCompatibilityError("Cannot locate installed Harbor job.py")
    path = pathlib.Path(spec.origin)
    patch_job_source(path)
    return path

