#!/usr/bin/env python3
"""Static setup contracts for the pinned Harbor resilience layer."""
from __future__ import annotations

import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class HarborSetupContractTest(unittest.TestCase):
    def test_setup_installs_source_patch_and_runtime_hook_fail_closed(self) -> None:
        setup = (ROOT / "setup.sh").read_text(encoding="utf-8")
        self.assertIn("patch_harbor_resilience()", setup)
        self.assertIn("patch_installed_harbor", setup)
        self.assertIn("tb_harbor_resilience.pth", setup)
        self.assertIn("tb_harbor_compat.runtime", setup)
        self.assertIn("patch_harbor_resilience", setup.split("setup_harbor()", 1)[1])
        resilience_body = setup.split("patch_harbor_resilience()", 1)[1].split(
            "\n}\n", 1
        )[0]
        self.assertIn("die ", resilience_body)

    def test_adapter_distribution_includes_compatibility_package(self) -> None:
        pyproject = (ROOT / "adapter" / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"tb_harbor_compat*"', pyproject)


if __name__ == "__main__":
    unittest.main()
