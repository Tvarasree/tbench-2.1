#!/usr/bin/env python3
"""
seed_gar_images.py — ONE-TIME terminal-bench image mirror into Google Artifact
Registry.

WHAT IT DOES
------------
Every harbor terminal-bench task declares a prebuilt image in its task.toml:

    [environment]
    docker_image = "alexgshaw/<task>:<tag>"

harbor pulls that image instead of building the Dockerfile. To make Batch-VM
runs fast, reproducible, and independent of Docker Hub rate limits / arch
mismatch, we mirror every such image into GAR exactly once with this script,
then pull from GAR on every run (scripts/pull_tb_images.py).

GAR naming mirrors swe-bench's rule (replace the FIRST '/' with '-') plus a
configurable prefix:

    src : alexgshaw/adaptive-rejection-sampler:20251031
    gar : us-central1-docker.pkg.dev/xyne-dev-461113/eval-dashboard/
          tbench-alexgshaw-adaptive-rejection-sampler:20251031

INTEGRITY GUARANTEES (why you can trust what lands in GAR)
----------------------------------------------------------
1. Platform-pinned pulls (`docker pull --platform linux/amd64`): we never push
   an emulated or wrong-arch image. Batch VMs are x86_64, so amd64 is correct.
2. Content verification: after push we DELETE the local GAR-tagged image,
   RE-PULL it from GAR, and assert its CONTENT identity
   ('<os>/<arch> <RootFS.Layers diff_ids>') equals the source's. diff_ids are
   uncompressed-layer digests, so equality ⇒ byte-identical extracted
   filesystem ⇒ no corruption — and this is robust when the source is a
   multi-arch / attested OCI index (whose top-level digest legitimately
   differs from the single-arch amd64 copy that lands in GAR; the opaque
   `.Id` is NOT a safe anchor there). Any real layer tamper still fails loud.
   An image is only recorded as seeded if the diff_ids match.
3. Self-healing idempotency: an image already in GAR but not yet recorded
   with a content identity (e.g. seeded by an older run) is re-verified
   (source vs GAR diff_ids) and recorded WITHOUT a re-push.
4. Atomic, incremental manifest: verified entries are flushed to
   scripts/seeded_images.json (temp file + os.replace) after each success, so
   an interrupted run never loses or half-writes verified state.
5. No partial pushes: if the source pull fails or its identity can't be read,
   nothing is tagged or pushed for that task.
6. Loud failure: the process exits non-zero if ANY task failed or failed
   verification, and prints an explicit per-task failure list.

It shells out to the `docker` CLI (NOT docker-py): docker-py 7.x silently
swallows credHelper/auth-stream errors and reports them as ImageNotFound —
the exact failure mode swe-bench's docker_build.py documents and avoids.

USAGE
-----
    # one-time, from a host with `docker`, disk headroom, and GAR push creds:
    python3 scripts/seed_gar_images.py                 # all 89 tasks
    python3 scripts/seed_gar_images.py --dry-run       # plan only, no pull/push
    python3 scripts/seed_gar_images.py --task regex-log
    python3 scripts/seed_gar_images.py --limit 5 --concurrency 2
    python3 scripts/seed_gar_images.py --force         # re-seed even if present

PREREQUISITES (you handle these; the script verifies and fails clearly)
    * `docker` CLI present and the daemon reachable.
    * Authenticated to push to the GAR repo in config.yaml, e.g.:
          gcloud auth configure-docker us-central1-docker.pkg.dev
      or  gcloud auth print-access-token | docker login -u oauth2accesstoken \
              --password-stdin https://us-central1-docker.pkg.dev
    * The harbor task packages cached locally (setup.sh / `harbor download`).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Paths / config
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
CONFIG_PATH = REPO_DIR / "config.yaml"
LOG_DIR = SCRIPT_DIR / "logs" / "gar_seed"

_DOCKER_IMAGE_RE = re.compile(
    r'^\s*docker_image\s*=\s*["\']([^"\']+)["\']\s*$', re.MULTILINE
)


def _load_config() -> dict:
    """Load config.yaml. PyYAML if available, else a tiny targeted fallback."""
    text = CONFIG_PATH.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text) or {}
    except Exception:
        # Minimal fallback: pull just the gar.* and dataset.* leaves we need so
        # the script still works before `pip install -r requirements.txt`.
        cfg: dict = {"gar": {}, "dataset": {}}

        def _grab(key: str) -> Optional[str]:
            m = re.search(rf'^\s*{re.escape(key)}\s*:\s*(.+?)\s*$', text, re.M)
            if not m:
                return None
            return m.group(1).strip().strip('"').strip("'")

        cfg["gar"]["registry_url"] = _grab("registry_url")
        cfg["gar"]["image_prefix"] = _grab("image_prefix") or "tbench-"
        cfg["gar"]["platform"] = _grab("platform") or "linux/amd64"
        cfg["gar"]["manifest_path"] = _grab("manifest_path") or "scripts/seeded_images.json"
        cfg["dataset"]["harbor_cache_subdir"] = _grab("harbor_cache_subdir") or "tasks"
        cfg["dataset"]["name"] = _grab("name") or "terminal-bench/terminal-bench-2-1"
        return cfg


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOG_DIR / f"seed_{ts}.log"

    logger = logging.getLogger("gar_seed")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s", "%Y-%m-%d %H:%M:%S"))

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s", "%H:%M:%S"))

    logger.addHandler(fh)
    logger.addHandler(ch)
    logger.info("Log file: %s", log_file)
    return logger


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class TaskImage:
    task: str
    source_ref: str          # alexgshaw/<task>:<tag>
    gar_ref: str             # <registry>/tbench-alexgshaw-<task>:<tag>
    task_toml: Path


@dataclass
class SeedResult:
    task: str
    source_ref: str
    gar_ref: str
    status: str = "pending"          # seeded | skipped | failed
    already_present: bool = False
    source_image_id: Optional[str] = None
    verified_image_id: Optional[str] = None
    duration_s: float = 0.0
    error: Optional[str] = None
    detail: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# docker helpers (CLI, not docker-py — see module docstring)
# ---------------------------------------------------------------------------
def _run(cmd: list[str], timeout: int = 1200) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
        env=os.environ.copy(),
    )


def _docker_available(logger: logging.Logger) -> bool:
    try:
        cp = _run(["docker", "version", "--format", "{{.Server.Version}}"], 30)
    except FileNotFoundError:
        logger.error("`docker` CLI not found on PATH.")
        return False
    except Exception as e:  # noqa: BLE001
        logger.error("docker version failed: %s", e)
        return False
    if cp.returncode != 0:
        logger.error("Docker daemon not reachable: %s",
                     (cp.stderr or cp.stdout).strip())
        return False
    logger.info("Docker daemon reachable (server %s)", cp.stdout.strip())
    return True


def _amd64_identity(ref: str) -> Optional[str]:
    """Content identity of the locally-present image: '<os>/<arch> <diff_ids>'.

    `.RootFS.Layers` are the UNCOMPRESSED layer (diff) digests — identical iff
    the extracted filesystem is byte-identical, regardless of how the registry
    serialized the manifest (single image vs OCI index w/ attestations) or
    recompressed blobs. This is the correct integrity anchor for mirroring:
    immune to index-vs-manifest digest differences, but any real layer tamper
    changes a diff_id. The opaque top-level `.Id` is NOT safe here — for a
    multi-arch source it's the index digest, for the single-arch GAR copy it's
    the amd64 manifest digest (different values, identical content).
    """
    cp = _run(
        ["docker", "image", "inspect", ref,
         "--format", "{{.Os}}/{{.Architecture}} {{join .RootFS.Layers \",\"}}"],
        60,
    )
    if cp.returncode != 0:
        return None
    out = cp.stdout.strip()
    # A valid identity must carry at least one diff_id; reject a degenerate
    # (layerless) result so we never record or match on an empty identity.
    return out if (out and "sha256:" in out) else None


def _remote_exists(ref: str) -> bool:
    """True iff the ref resolves in the remote registry (no local copy needed)."""
    cp = _run(["docker", "manifest", "inspect", ref], 120)
    return cp.returncode == 0


def _rmi(ref: str) -> None:
    _run(["docker", "image", "rm", "-f", ref], 60)


# ---------------------------------------------------------------------------
# GAR ref construction
# ---------------------------------------------------------------------------
def _sanitize(name: str) -> str:
    """swe-bench rule: replace ONLY the first '/' with '-', then make the rest
    a valid repository path. Tag is handled separately."""
    return name.replace("/", "-", 1)


def build_gar_ref(source_ref: str, registry_url: str, prefix: str) -> str:
    if ":" in source_ref.rsplit("/", 1)[-1]:
        repo, tag = source_ref.rsplit(":", 1)
    else:
        repo, tag = source_ref, "latest"
    sanitized = _sanitize(repo)              # alexgshaw-<task>
    sanitized = re.sub(r"[^A-Za-z0-9._/-]", "-", sanitized).lower()
    return f"{registry_url.rstrip('/')}/{prefix}{sanitized}:{tag}"


# ---------------------------------------------------------------------------
# Task discovery
# ---------------------------------------------------------------------------
def discover_task_images(
    cache_root: Path, registry_url: str, prefix: str, logger: logging.Logger
) -> list[TaskImage]:
    """Walk ~/.cache/harbor/tasks/packages/<org>/<task>/<hash>/task.toml and
    extract docker_image. Robust to the content-addressable hash subdir."""
    if not cache_root.exists():
        logger.error("Harbor task cache not found: %s", cache_root)
        return []

    out: list[TaskImage] = []
    seen: set[str] = set()
    for toml_path in sorted(cache_root.rglob("task.toml")):
        try:
            text = toml_path.read_text(encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not read %s: %s", toml_path, e)
            continue
        m = _DOCKER_IMAGE_RE.search(text)
        if not m:
            continue
        source_ref = m.group(1).strip()
        # Task name = the package dir under the org dir (parent of the hash dir).
        # .../packages/terminal-bench/<task>/<hash>/task.toml
        task = toml_path.parent.parent.name
        key = f"{task}|{source_ref}"
        if key in seen:
            continue
        seen.add(key)
        out.append(TaskImage(
            task=task,
            source_ref=source_ref,
            gar_ref=build_gar_ref(source_ref, registry_url, prefix),
            task_toml=toml_path,
        ))
    return out


# ---------------------------------------------------------------------------
# Core: seed one image with full integrity verification
# ---------------------------------------------------------------------------
def seed_one(
    ti: TaskImage,
    platform: str,
    force: bool,
    dry_run: bool,
    manifest: "Manifest",
    logger: logging.Logger,
) -> SeedResult:
    """Mirror one task image into GAR and verify it by CONTENT identity.

    Idempotency has three states:
      * already in GAR AND recorded+verified in the manifest -> skip (no I/O)
      * already in GAR but NOT recorded (e.g. pushed by an earlier run that
        failed the old too-strict check) -> VERIFY-ONLY: pull source + GAR,
        compare diff_ids, record. No re-push.
      * not in GAR (or --force) -> pull -> tag -> push -> re-pull -> verify.

    Verification compares '<os>/<arch> <diff_ids>' (see _amd64_identity), so a
    multi-arch / attested source and its single-arch amd64 GAR copy compare
    equal iff the extracted filesystem is byte-identical — while any real layer
    tamper still fails loudly.
    """
    r = SeedResult(task=ti.task, source_ref=ti.source_ref, gar_ref=ti.gar_ref)
    t0 = time.time()
    pfx = "[DRY-RUN] " if dry_run else ""
    logger.info("%s%s : %s -> %s", pfx, ti.task, ti.source_ref, ti.gar_ref)

    def _record_identity(ident: str) -> None:
        r.verified_image_id = ident
        os_arch, _, diff = ident.partition(" ")
        r.detail["os_arch"] = os_arch
        r.detail["diff_ids"] = [d for d in diff.split(",") if d]

    try:
        # 0. Dry-run is pure resolution: never touch docker or any registry.
        if dry_run:
            r.status = "skipped"
            r.detail["plan"] = "would pull, retag, push, verify"
            r.duration_s = time.time() - t0
            return r

        in_gar = _remote_exists(ti.gar_ref)

        # 1a. Already in GAR AND already recorded+verified -> nothing to do.
        if in_gar and not force and manifest.is_verified(ti.task):
            r.status = "skipped"
            r.already_present = True
            r.duration_s = time.time() - t0
            logger.info("   already in GAR and recorded — skipping")
            return r

        # 2. Pull source, platform-pinned (never trust an arch-wrong image).
        logger.info("   pulling source (%s) …", platform)
        cp = _run(["docker", "pull", "--platform", platform, ti.source_ref], 1800)
        if cp.returncode != 0:
            r.status = "failed"
            r.error = f"source pull failed: {(cp.stderr or cp.stdout).strip()[:300]}"
            logger.error("   %s", r.error)
            return r

        src_ident = _amd64_identity(ti.source_ref)
        if not src_ident:
            r.status = "failed"
            r.error = "could not read source content identity after pull"
            logger.error("   %s", r.error)
            return r
        r.source_image_id = src_ident
        logger.info("   source identity: %s", src_ident)

        verify_only = in_gar and not force
        if not verify_only:
            # 3. Retag -> GAR.
            cp = _run(["docker", "tag", ti.source_ref, ti.gar_ref], 60)
            if cp.returncode != 0:
                r.status = "failed"
                r.error = f"docker tag failed: {(cp.stderr or cp.stdout).strip()[:200]}"
                logger.error("   %s", r.error)
                return r
            # 4. Push to GAR.
            logger.info("   pushing to GAR …")
            cp = _run(["docker", "push", ti.gar_ref], 2400)
            if cp.returncode != 0:
                r.status = "failed"
                r.error = f"push failed: {(cp.stderr or cp.stdout).strip()[:300]}"
                logger.error("   %s", r.error)
                return r
        else:
            logger.info("   already in GAR — verify-only (no re-push)")

        # 5. Verify by content: drop any local GAR tag, re-pull from GAR,
        #    compare the os/arch + diff_id identity. Equal ⇒ byte-identical
        #    filesystem ⇒ no corruption (robust to index-vs-manifest digests).
        _rmi(ti.gar_ref)
        cp = _run(["docker", "pull", "--platform", platform, ti.gar_ref], 1800)
        if cp.returncode != 0:
            r.status = "failed"
            r.error = f"verify re-pull failed: {(cp.stderr or cp.stdout).strip()[:300]}"
            logger.error("   %s", r.error)
            return r
        gar_ident = _amd64_identity(ti.gar_ref)
        if not gar_ident or gar_ident != src_ident:
            r.status = "failed"
            r.error = (f"INTEGRITY MISMATCH (content): source [{src_ident}] != "
                       f"GAR [{gar_ident}] — NOT recording as seeded")
            logger.error("   %s", r.error)
            return r

        _record_identity(gar_ident)
        r.status = "seeded"
        r.duration_s = time.time() - t0
        logger.info("   verified OK (content identity matches%s) in %.1fs",
                    ", existing GAR image" if verify_only else "", r.duration_s)
        return r

    except subprocess.TimeoutExpired as e:
        r.status = "failed"
        r.error = f"timeout: {e}"
        logger.error("   %s", r.error)
        return r
    except Exception as e:  # noqa: BLE001
        r.status = "failed"
        r.error = f"unexpected: {type(e).__name__}: {e}"
        logger.error("   %s", r.error)
        return r
    finally:
        r.duration_s = time.time() - t0


# ---------------------------------------------------------------------------
# Manifest (atomic, incremental)
# ---------------------------------------------------------------------------
class Manifest:
    def __init__(self, path: Path, dataset_name: str):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict = {
            "schema": 2,   # 2 = content identity (os_arch + diff_ids)
            "dataset": dataset_name,
            "updated_at": None,
            "images": {},
        }
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(existing, dict) and "images" in existing:
                    self._data = existing
                    # Adopt existing entries but stamp the CURRENT writer
                    # schema — once a re-run records diff_ids, the file is
                    # schema-2 in substance, so the field must not stay 1.
                    self._data["schema"] = 2
                    self._data.setdefault("dataset", dataset_name)
            except Exception:
                pass  # corrupt/old manifest: start fresh, will be overwritten

    def is_verified(self, task: str) -> bool:
        """True iff this task is already recorded WITH a content identity
        (os_arch + diff_ids). Old-format entries lacking diff_ids return False
        so a re-run self-heals them via the verify-only path."""
        with self._lock:
            e = self._data.get("images", {}).get(task)
            return bool(e and e.get("diff_ids"))

    def record(self, r: SeedResult) -> None:
        if r.status != "seeded":
            return
        with self._lock:
            self._data["images"][r.task] = {
                "source": r.source_ref,
                "gar_ref": r.gar_ref,
                # Content identity — what pull_tb_images.py checks continuity
                # against. `image_id` kept for human/debug readability.
                "image_id": r.verified_image_id,
                "os_arch": r.detail.get("os_arch"),
                "diff_ids": r.detail.get("diff_ids", []),
                "seeded_at": datetime.now(timezone.utc).isoformat(),
            }
            self._data["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._flush_locked()

    def _flush_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".seeded_", suffix=".json.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, sort_keys=True)
            os.replace(tmp, self.path)   # atomic
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description="One-time mirror of terminal-bench task images into GAR "
                    "(digest-verified).")
    ap.add_argument("--task", help="Seed only this task name.")
    ap.add_argument("--limit", type=int, help="Seed only the first N tasks.")
    ap.add_argument("--concurrency", type=int, default=3,
                    help="Parallel workers (default 3; pushes are heavy).")
    ap.add_argument("--force", action="store_true",
                    help="Re-seed even if the GAR ref already exists.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Resolve + plan only; no pull/tag/push.")
    args = ap.parse_args()

    logger = setup_logging()
    cfg = _load_config()
    gar = cfg.get("gar", {}) or {}
    ds = cfg.get("dataset", {}) or {}

    registry_url = gar.get("registry_url")
    prefix = gar.get("image_prefix", "tbench-")
    platform = gar.get("platform", "linux/amd64")
    manifest_path = REPO_DIR / gar.get("manifest_path", "scripts/seeded_images.json")
    cache_subdir = ds.get("harbor_cache_subdir", "tasks")
    dataset_name = ds.get("name", "terminal-bench/terminal-bench-2-1")

    if not registry_url:
        logger.error("config.yaml gar.registry_url is missing — aborting.")
        return 2

    logger.info("=" * 70)
    logger.info("terminal-bench → GAR seeding")
    logger.info("  registry : %s", registry_url)
    logger.info("  prefix   : %s", prefix)
    logger.info("  platform : %s", platform)
    logger.info("  manifest : %s", manifest_path)
    logger.info("  mode     : %s", "DRY-RUN" if args.dry_run else "LIVE")
    logger.info("=" * 70)

    if not args.dry_run and not _docker_available(logger):
        return 2

    cache_root = Path.home() / ".cache" / "harbor" / cache_subdir
    images = discover_task_images(cache_root, registry_url, prefix, logger)
    if not images:
        logger.error("No task images discovered under %s. Run setup.sh / "
                     "`harbor download %s --cache` first.",
                     cache_root, dataset_name)
        return 2

    if args.task:
        images = [i for i in images if i.task == args.task]
        if not images:
            logger.error("Task %r not found in cache.", args.task)
            return 2
    if args.limit:
        images = images[: args.limit]

    logger.info("Discovered %d task image(s) to process.", len(images))

    manifest = Manifest(manifest_path, dataset_name)
    results: list[SeedResult] = []

    workers = max(1, args.concurrency)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(seed_one, ti, platform, args.force, args.dry_run,
                      manifest, logger): ti
            for ti in images
        }
        done = 0
        for fut in as_completed(futs):
            r = fut.result()
            results.append(r)
            manifest.record(r)
            done += 1
            logger.info("progress: %d/%d (%s: %s)",
                        done, len(images), r.task, r.status)

    # Summary
    seeded = [r for r in results if r.status == "seeded"]
    skipped = [r for r in results if r.status == "skipped"]
    failed = [r for r in results if r.status == "failed"]

    logger.info("=" * 70)
    logger.info("SUMMARY: %d seeded | %d skipped | %d failed (of %d)",
                len(seeded), len(skipped), len(failed), len(results))
    if failed:
        logger.error("FAILED tasks (NOT in manifest):")
        for r in failed:
            logger.error("  - %s : %s", r.task, r.error)
    logger.info("Manifest: %s", manifest_path)
    logger.info("=" * 70)

    # Loud failure: any failure ⇒ non-zero so the caller gets clear assurance.
    if failed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
