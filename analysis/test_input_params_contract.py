#!/usr/bin/env python3
"""Contract tests for dashboard-provided eval inputs."""

from __future__ import annotations

import json
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class InputParamsContractTest(unittest.TestCase):
    def test_token_price_names_are_canonical_and_legacy_flags_still_work(self) -> None:
        schema = json.loads((ROOT / "input_params.json").read_text())
        names = {field["name"] for field in schema["fields"]}

        self.assertIn("input_token_price", names)
        self.assertIn("output_token_price", names)
        self.assertNotIn("price_input", names)
        self.assertNotIn("price_output", names)
        self.assertNotIn("price_cached", names)
        self.assertNotIn("price_cache_write", names)

        source = (ROOT / "run.sh").read_text()
        self.assertIn("--input-token-price|--price-input)", source)
        self.assertIn("--output-token-price|--price-output)", source)
        self.assertIn("--price-cached|--price-cache-write)", source)
        self.assertNotIn('TOKEN_PRICE_FLAGS+=(--price-cached', source)
        self.assertNotIn('TOKEN_PRICE_FLAGS+=(--price-cache-write', source)

    def test_pi_uses_the_custom_grid_adapter(self) -> None:
        run_source = (ROOT / "run.sh").read_text()
        config_source = (ROOT / "config.yaml").read_text()

        self.assertIn("pi_harbor_agent.agent:PiGridAgent", run_source)
        self.assertIn('PI_GRID_BASE_URL=${BASE_URL}', run_source)
        self.assertNotIn("pi|aider)", run_source)
        self.assertIn('model_format: "juspay/{MODEL}"', config_source)


if __name__ == "__main__":
    unittest.main()
