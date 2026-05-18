#!/usr/bin/env python3
"""
pull_tb_images.py — PER-RUN: make the selected terminal-bench task images
available locally from GAR, under the exact name harbor expects.

WHY THIS WORKS
--------------
harbor's prebuilt compose template is literally:

    services:
      main:
        image: ${PREBUILT_IMAGE_NAME}      # == task.toml [environment].docker_image
        command: [ "sh", "-c", "sleep infinity" ]

There is NO `pull_policy`, so Docker Compose defaults to `missing` — it only
pulls when the image is absent locally. So if we:

    docker pull  <gar_ref>                       (from Artifact Registry)
    docker tag   <gar_ref>  <source_ref>         (the Docker Hub name harbor wants)

then harbor finds <source_ref> locally and NEVER contacts Docker Hub. This is
the exact mechanism swe-bench's docker_build.py uses (pull from GAR, present
under the expected name), adapted to harbor. The host Docker daemon caches
images across trials/runs (DooD), so each image is really pulled at most once
per VM.

INTEGRITY / ROBUSTNESS
----------------------
* Platform-pinned pulls (default linux/amd64 from config) so we never
  materialise an emulated/arch-wrong image.
* If scripts/seeded_images.json exists, the GAR ref and expected image id come
  from that verified manifest; the local image id is checked against it and a
  mismatch is logged loudly (stale seed) without silently using bad content.
* GAR auth is refreshed from the GCP metadata server before pulls (single
  flight, min-interval) — long sweeps outlive the ~60-min token written by
  setup.sh, exactly the failure docker_build.py works around.
* Idempotent: if <source_ref> is already present locally it is reused.
* Shells out to the `docker` CLI (not docker-py) for the same credHelper
  reason documented in seed_gar_images.py.

EXIT CODE
    0  every selected task's image is present locally as its source_ref
    1  one or more are missing (run.sh decides whether to proceed/fallback)
    2  bad invocation / config

USAGE (run.sh calls this; also usable by hand)
    python3 scripts/pull_tb_images.py --all
    python3 scripts/pull_tb_images.py --task regex-log --task chess-best-move
    python3 scripts/pull_tb_images.py --tasks-file /tmp/selected.txt
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
CONFIG_PATH = REPO_DIR / "config.yaml"

_DOCKER_IMAGE_RE = re.compile(
    r'^\s*docker_image\s*=\s*["\']([^"\']+)["\']\s*$', re.MULTILINE
)
_METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/token"
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def _load_config() -> dict:
    text = CONFIG_PATH.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text) or {}
    except Exception:
        cfg: dict = {"gar": {}, "dataset": {}}

        def grab(k: str) -> Optional[str]:
            m = re.search(rf'^\s*{re.escape(k)}\s*:\s*(.+?)\s*$', text, re.M)
            return m.group(1).strip().strip('"').strip("'") if m else None

        cfg["gar"]["registry_url"] = grab("registry_url")
        cfg["gar"]["image_prefix"] = grab("image_prefix") or "tbench-"
        cfg["gar"]["platform"] = grab("platform") or "linux/amd64"
        cfg["gar"]["manifest_path"] = grab("manifest_path") or "scripts/seeded_images.json"
        cfg["dataset"]["harbor_cache_subdir"] = grab("harbor_cache_subdir") or "tasks"
        return cfg


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("pull_tb")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s", "%H:%M:%S"))
    logger.addHandler(h)
    return logger


# ---------------------------------------------------------------------------
# docker helpers (CLI, not docker-py)
# ---------------------------------------------------------------------------
def _run(cmd: list[str], timeout: int = 1800) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, env=os.environ.copy())


def _image_exists_local(ref: str) -> bool:
    return _run(["docker", "image", "inspect", ref], 60).returncode == 0


def _diff_ids(ref: str) -> Optional[list[str]]:
    """Uncompressed-layer (diff) digests of the locally-present image — the
    same content identity seed_gar_images.py records. Robust to multi-arch /
    attested sources (the opaque `.Id` is not). Returns None if unreadable."""
    cp = _run(
        ["docker", "image", "inspect", ref,
         "--format", "{{join .RootFS.Layers \",\"}}"],
        60,
    )
    if cp.returncode != 0:
        return None
    layers = [x for x in cp.stdout.strip().split(",") if x]
    return layers or None


class GarAuth:
    """Single-flight GAR re-auth via the GCP metadata server. Mirrors the
    token refresh in swe-bench docker_build.py (tokens expire ~60 min)."""

    def __init__(self, logger: logging.Logger, min_interval: int = 1500):
        self._logger = logger
        self._min_interval = min_interval
        self._last = 0.0
        self._lock = threading.Lock()
        self._host = "https://us-central1-docker.pkg.dev"

    def refresh(self, force: bool = False) -> None:
        with self._lock:
            now = time.time()
            if not force and (now - self._last) < self._min_interval:
                return
            try:
                meta = _run(
                    ["curl", "-sf", "-H", "Metadata-Flavor: Google",
                     _METADATA_TOKEN_URL], 5)
                if meta.returncode != 0 or not meta.stdout.strip():
                    return  # not on a GCP VM, or no SA — rely on existing auth
                tok = json.loads(meta.stdout).get("access_token", "")
                if not tok:
                    return
                login = subprocess.run(
                    ["docker", "login", "-u", "oauth2accesstoken",
                     "--password-stdin", self._host],
                    input=tok, capture_output=True, text=True,
                    env=os.environ.copy(), timeout=15)
                if login.returncode == 0:
                    self._last = now
                    self._logger.info("Refreshed GAR auth via metadata token")
                else:
                    self._logger.warning(
                        "GAR re-login failed: %s",
                        (login.stderr or login.stdout).strip()[:160])
            except Exception as e:  # noqa: BLE001
                self._logger.warning("GAR auth refresh skipped: %s", e)


# ---------------------------------------------------------------------------
# Task discovery + GAR ref
# ---------------------------------------------------------------------------
@dataclass
class Item:
    task: str
    source_ref: str
    gar_ref: str
    # Content identity from the seed manifest (schema 2): amd64 diff_ids.
    expected_diff_ids: Optional[list[str]] = None


def _sanitize(name: str) -> str:
    return name.replace("/", "-", 1)


def _build_gar_ref(source_ref: str, registry_url: str, prefix: str) -> str:
    last = source_ref.rsplit("/", 1)[-1]
    if ":" in last:
        repo, tag = source_ref.rsplit(":", 1)
    else:
        repo, tag = source_ref, "latest"
    s = re.sub(r"[^A-Za-z0-9._/-]", "-", _sanitize(repo)).lower()
    return f"{registry_url.rstrip('/')}/{prefix}{s}:{tag}"


def discover(cfg: dict, logger: logging.Logger) -> dict[str, Item]:
    gar = cfg.get("gar", {}) or {}
    ds = cfg.get("dataset", {}) or {}
    registry = gar.get("registry_url")
    prefix = gar.get("image_prefix", "tbench-")
    if not registry:
        logger.error("config.yaml gar.registry_url missing")
        return {}

    # Prefer the verified seed manifest if it exists (authoritative gar_ref +
    # expected image id). Fall back to scanning task.toml otherwise.
    manifest_path = REPO_DIR / gar.get("manifest_path", "scripts/seeded_images.json")
    manifest: dict = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8")).get("images", {})
            logger.info("Loaded seed manifest (%d entries) from %s",
                        len(manifest), manifest_path)
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not read manifest %s: %s", manifest_path, e)

    out: dict[str, Item] = {}
    cache_root = Path.home() / ".cache" / "harbor" / ds.get("harbor_cache_subdir", "tasks")
    if cache_root.exists():
        for toml_path in cache_root.rglob("task.toml"):
            try:
                m = _DOCKER_IMAGE_RE.search(toml_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not m:
                continue
            task = toml_path.parent.parent.name
            src = m.group(1).strip()
            if task in out:
                continue
            out[task] = Item(
                task=task,
                source_ref=src,
                gar_ref=_build_gar_ref(src, registry, prefix),
            )

    # Overlay manifest authority (covers tasks even if cache scan missed them).
    for task, entry in manifest.items():
        src = entry.get("source")
        gref = entry.get("gar_ref")
        if not src or not gref:
            continue
        # schema 2 stores diff_ids; tolerate old/absent format (None ->
        # continuity check skipped, pull+retag still happens correctly).
        diff = entry.get("diff_ids") or None
        out[task] = Item(task=task, source_ref=src, gar_ref=gref,
                          expected_diff_ids=diff)
    return out


# ---------------------------------------------------------------------------
# Pull one
# ---------------------------------------------------------------------------
@dataclass
class PullOutcome:
    task: str
    ok: bool
    skipped: bool = False
    error: Optional[str] = None


def pull_one(it: Item, platform: str, auth: GarAuth,
             force: bool, logger: logging.Logger) -> PullOutcome:
    try:
        # Idempotent: already present under the harbor-expected name?
        if not force and _image_exists_local(it.source_ref):
            if it.expected_diff_ids:
                got = _diff_ids(it.source_ref)
                if got and got != it.expected_diff_ids:
                    logger.warning(
                        "%s: local %s diff_ids != manifest — re-pulling from GAR",
                        it.task, it.source_ref)
                else:
                    logger.info("%s: present locally (verified) — skip", it.task)
                    return PullOutcome(it.task, True, skipped=True)
            else:
                logger.info("%s: present locally — skip", it.task)
                return PullOutcome(it.task, True, skipped=True)

        auth.refresh()  # single-flight; cheap if recently refreshed

        logger.info("%s: pulling %s", it.task, it.gar_ref)
        cp = _run(["docker", "pull", "--platform", platform, it.gar_ref], 2400)
        if cp.returncode != 0:
            # One forced re-auth + retry (token may have just expired).
            auth.refresh(force=True)
            cp = _run(["docker", "pull", "--platform", platform, it.gar_ref], 2400)
        if cp.returncode != 0:
            return PullOutcome(
                it.task, False,
                error=f"GAR pull failed: {(cp.stderr or cp.stdout).strip()[:240]}")

        # Optional integrity continuity check vs the verified manifest
        # (content diff_ids — same anchor seed_gar_images.py records).
        if it.expected_diff_ids:
            got = _diff_ids(it.gar_ref)
            if got and got != it.expected_diff_ids:
                logger.warning(
                    "%s: pulled diff_ids != manifest (stale seed?) — using anyway",
                    it.task)

        # Present it under the exact name harbor's compose expects.
        tag = _run(["docker", "tag", it.gar_ref, it.source_ref], 60)
        if tag.returncode != 0:
            return PullOutcome(
                it.task, False,
                error=f"retag failed: {(tag.stderr or tag.stdout).strip()[:160]}")

        if not _image_exists_local(it.source_ref):
            return PullOutcome(it.task, False,
                               error="post-retag image still not present")
        logger.info("%s: ready as %s", it.task, it.source_ref)
        return PullOutcome(it.task, True)

    except subprocess.TimeoutExpired as e:
        return PullOutcome(it.task, False, error=f"timeout: {e}")
    except Exception as e:  # noqa: BLE001
        return PullOutcome(it.task, False,
                           error=f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Pull selected terminal-bench task images from GAR and "
                    "present them under the harbor-expected name.")
    ap.add_argument("--task", action="append", default=[],
                    help="Task name (repeatable).")
    ap.add_argument("--tasks-file",
                    help="File with one task name per line.")
    ap.add_argument("--all", action="store_true",
                    help="All tasks found in the harbor cache / manifest.")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--platform", default=None,
                    help="Override config gar.platform (e.g. linux/amd64).")
    ap.add_argument("--force", action="store_true",
                    help="Re-pull + retag even if present locally.")
    args = ap.parse_args()

    logger = setup_logging()
    cfg = _load_config()
    platform = args.platform or (cfg.get("gar", {}) or {}).get("platform", "linux/amd64")

    catalog = discover(cfg, logger)
    if not catalog:
        logger.error("No task→image mapping found (empty cache and no manifest).")
        return 2

    # Resolve the selection.
    wanted: list[str] = list(args.task)
    if args.tasks_file:
        try:
            wanted += [ln.strip() for ln in Path(args.tasks_file).read_text().splitlines()
                       if ln.strip() and not ln.strip().startswith("#")]
        except Exception as e:  # noqa: BLE001
            logger.error("Could not read --tasks-file %s: %s", args.tasks_file, e)
            return 2
    if args.all:
        wanted = sorted(catalog.keys())
    wanted = sorted(dict.fromkeys(wanted))  # de-dup, stable

    if not wanted:
        logger.error("No tasks selected. Pass --all, --task, or --tasks-file.")
        return 2

    items, missing_meta = [], []
    for t in wanted:
        if t in catalog:
            items.append(catalog[t])
        else:
            missing_meta.append(t)
    if missing_meta:
        logger.error("No image mapping for %d task(s): %s",
                     len(missing_meta), ", ".join(missing_meta))

    if not _run(["docker", "version"], 30).returncode == 0:
        logger.error("Docker daemon not reachable — cannot pull images.")
        return 2

    auth = GarAuth(logger)
    auth.refresh(force=True)  # prime auth once up front

    logger.info("Pulling %d task image(s) from GAR (platform=%s, conc=%d)",
                len(items), platform, args.concurrency)

    results: list[PullOutcome] = []
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        futs = {ex.submit(pull_one, it, platform, auth, args.force, logger): it
                for it in items}
        for fut in as_completed(futs):
            results.append(fut.result())

    ok = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]

    logger.info("=" * 64)
    logger.info("pull summary: %d ok (%d cached) | %d failed | %d unmapped",
                len(ok), sum(1 for r in ok if r.skipped),
                len(failed), len(missing_meta))
    for r in failed:
        logger.error("  FAILED %s: %s", r.task, r.error)
    logger.info("=" * 64)

    return 0 if (not failed and not missing_meta) else 1


if __name__ == "__main__":
    sys.exit(main())
