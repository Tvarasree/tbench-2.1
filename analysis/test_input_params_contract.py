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

    def test_schema_only_uses_keys_the_dashboard_understands(self) -> None:
        """The dashboard silently DROPS unknown schema keys.

        `InputParamField` (eval-dashboard-backend/src/evals/dto.rs) has no
        `deny_unknown_fields`, so a misspelled key — e.g. a top-level `pattern`
        instead of `constraints.regex` — is accepted at eval-creation time and
        then validates nothing, forever, with no error anywhere. Evals are
        authored by pasting raw JSON into the dashboard, so a typo here reaches
        the `evals.input_params` column verbatim.
        """
        field_keys = {
            "name", "label", "type", "required", "default", "options", "constraints",
        }
        constraint_keys = {
            "min_length", "max_length", "regex", "min", "max", "start", "end",
        }
        types = {"text", "email", "password", "number", "select", "boolean", "time"}

        schema = json.loads((ROOT / "input_params.json").read_text())
        for field in schema["fields"]:
            name = field["name"]
            self.assertLessEqual(
                set(field), field_keys,
                f"field '{name}' has keys the dashboard ignores: "
                f"{sorted(set(field) - field_keys)}",
            )
            self.assertIn(field["type"], types, f"field '{name}' has an unknown type")
            constraints = field.get("constraints", {})
            self.assertLessEqual(
                set(constraints), constraint_keys,
                f"field '{name}' has constraints the dashboard ignores: "
                f"{sorted(set(constraints) - constraint_keys)}",
            )

    def test_task_selectors_are_declared_and_wired(self) -> None:
        """`tasks` must reach run.sh and must beat `range` deterministically.

        The runner submits every populated field, so `--range` arrives even when
        the operator picked names; precedence cannot depend on flag order.
        """
        schema = json.loads((ROOT / "input_params.json").read_text())
        by_name = {field["name"]: field for field in schema["fields"]}

        self.assertIn("tasks", by_name)
        self.assertIn("range", by_name)
        # Both select tasks, so neither may be required or the other is unusable.
        self.assertFalse(by_name["tasks"].get("required", False))
        self.assertFalse(by_name["range"].get("required", False))

        source = (ROOT / "run.sh").read_text()
        self.assertIn("--tasks)", source)
        self.assertIn('SELECTOR_KIND="tasks"', source)
        # tasks wins regardless of the order the runner emits flags in.
        self.assertIn('if [ -n "${TASKS_LIST//[[:space:],]/}" ]; then', source)

    def test_pi_uses_the_custom_grid_adapter(self) -> None:
        run_source = (ROOT / "run.sh").read_text()
        config_source = (ROOT / "config.yaml").read_text()

        self.assertIn("pi_harbor_agent.agent:PiGridAgent", run_source)
        self.assertIn('PI_GRID_BASE_URL=${BASE_URL}', run_source)
        self.assertNotIn("pi|aider)", run_source)
        self.assertIn('model_format: "juspay/{MODEL}"', config_source)


if __name__ == "__main__":
    unittest.main()
