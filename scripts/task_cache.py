#!/usr/bin/env python3
"""Validate and describe Harbor's immutable package-task cache."""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
from dataclasses import dataclass
from typing import Any

_AUTO_YAML = object()
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_ENTRIES = ("task.toml", "instruction.md", "environment", "tests")


class CacheValidationError(ValueError):
    """Selected task packages are absent, ambiguous, or incomplete."""


@dataclass(frozen=True)
class CacheReport:
    task_count: int
    cache_bytes: int
    manifest: dict[str, dict[str, str]]


def _strip_comment(value: str) -> str:
    quote: str | None = None
    for index, char in enumerate(value):
        if char in "'\"":
            quote = None if quote == char else char if quote is None else quote
        elif char == "#" and quote is None:
            return value[:index].rstrip()
    return value.strip()


def _scalar(value: str) -> Any:
    value = _strip_comment(value).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value in {"null", "Null", "NULL", "~"}:
        return None
    try:
        return int(value)
    except ValueError:
        return value


def _minimal_yaml(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for line_number, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        body = raw.strip()
        if ":" not in body:
            raise ValueError(f"unsupported YAML at line {line_number}: {body}")
        key, value = body.split(":", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"empty YAML key at line {line_number}")
        while stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        value = _strip_comment(value).strip()
        if value:
            parent[key] = _scalar(value)
        else:
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
    return root


def load_config(
    path: pathlib.Path, *, yaml_module: Any = _AUTO_YAML
) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if yaml_module is _AUTO_YAML:
        try:
            import yaml as yaml_module  # type: ignore[no-redef]
        except ImportError:
            yaml_module = None
    if yaml_module is None:
        return _minimal_yaml(text)
    loaded = yaml_module.safe_load(text)
    if not isinstance(loaded, dict):
        raise ValueError(f"configuration is not a mapping: {path}")
    return loaded


def config_value(config: dict[str, Any], dotted_key: str) -> Any:
    current: Any = config
    for part in dotted_key.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(dotted_key)
        current = current[part]
    return current


def _dir_size(path: pathlib.Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _missing_entries(task_dir: pathlib.Path) -> list[str]:
    missing: list[str] = []
    for name in _REQUIRED_ENTRIES:
        entry = task_dir / name
        valid = entry.is_file() if "." in name else entry.is_dir()
        if not valid:
            missing.append(name)
    return missing


def validate_selected_tasks(
    *, cache_root: pathlib.Path, org: str, selected_tasks: list[str]
) -> CacheReport:
    package_root = cache_root / "packages" / org
    manifest: dict[str, dict[str, str]] = {}
    errors: list[str] = []

    for task_name in selected_tasks:
        task_root = package_root / task_name
        digest_dirs = sorted(
            path
            for path in task_root.iterdir()
            if path.is_dir() and _DIGEST_RE.fullmatch(path.name)
        ) if task_root.is_dir() else []
        usable: list[pathlib.Path] = []
        invalid: list[tuple[pathlib.Path, list[str]]] = []
        for digest_dir in digest_dirs:
            missing = _missing_entries(digest_dir)
            if missing:
                invalid.append((digest_dir, missing))
            else:
                usable.append(digest_dir)

        if len(usable) > 1:
            errors.append(f"{task_name}: multiple usable digests")
            continue
        if not usable:
            if invalid:
                missing = ", ".join(invalid[0][1])
                errors.append(f"{task_name}: missing required entries: {missing}")
            else:
                errors.append(f"{task_name}: no usable digest directory")
            continue

        task_dir = usable[0].resolve()
        manifest[f"{org}/{task_name}"] = {
            "digest": task_dir.name,
            "path": str(task_dir),
        }

    if errors:
        raise CacheValidationError("; ".join(errors))

    return CacheReport(
        task_count=len(manifest),
        cache_bytes=_dir_size(cache_root),
        manifest=manifest,
    )


def write_manifest(path: pathlib.Path, report: CacheReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {
                "task_count": report.task_count,
                "cache_bytes": report.cache_bytes,
                "tasks": report.manifest,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    config_parser = commands.add_parser("config-value")
    config_parser.add_argument("--config", required=True, type=pathlib.Path)
    config_parser.add_argument("--key", required=True)

    preflight_parser = commands.add_parser("preflight")
    preflight_parser.add_argument("--cache-root", required=True, type=pathlib.Path)
    preflight_parser.add_argument("--org", required=True)
    preflight_parser.add_argument("--tasks-file", required=True, type=pathlib.Path)
    preflight_parser.add_argument("--manifest", required=True, type=pathlib.Path)
    args = parser.parse_args()

    if args.command == "config-value":
        print(config_value(load_config(args.config), args.key))
        return 0

    selected = [
        line.strip()
        for line in args.tasks_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    report = validate_selected_tasks(
        cache_root=args.cache_root,
        org=args.org,
        selected_tasks=selected,
    )
    write_manifest(args.manifest, report)
    print(
        f"validated {report.task_count} task packages "
        f"({report.cache_bytes / 1024 / 1024:.2f} MiB) -> {args.manifest}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
