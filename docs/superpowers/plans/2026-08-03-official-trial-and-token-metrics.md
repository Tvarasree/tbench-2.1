# Official Trial and Token Metrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve `Solved x/N` while adding official trial accuracy, solved-task repeatability, and successful-trial-only token averages.

**Architecture:** Extract the embedded result aggregation into a stdlib Python CLI so formulas can be unit-tested with synthetic Harbor trial trees. Keep token aggregation in `analysis/token_usage.py`; expose new values through the existing token headline without changing execution, output paths, or fallback behavior.

**Tech Stack:** Bash, Python 3 stdlib, `unittest`, Harbor trial artifacts.

## Global Constraints

- Do not run Git commands.
- Keep `metrics.main` and all existing result/token fields backward compatible.
- Planned trial denominator is selected tasks multiplied by configured attempts.
- Missing/error/no-grade trials count as failed official trials.
- Token accounting remains non-fatal and missing telemetry is never treated as zero in averages.

---

### Task 1: Testable result metrics

**Files:**
- Create: `analysis/results.py`
- Create: `analysis/test_results.py`
- Modify: `run.sh`

**Interfaces:**
- Consumes: Harbor run directory, selected task count, configured attempts, token report path, agent/model/dataset metadata.
- Produces: the existing standardized results JSON plus additive trial-accuracy and solved-task-repeatability fields.

- [ ] Write synthetic trial-tree tests proving the unchanged main metric, `successful_trials / planned_trials`, missing-attempt failures, and equal-weight solved-task repeatability.
- [ ] Run `python3 -m unittest analysis.test_results -v` and confirm failures because `analysis/results.py` does not exist.
- [ ] Implement `build_results(...)` and a CLI writer with atomic output replacement.
- [ ] Run the focused tests and confirm they pass.
- [ ] Replace only the embedded aggregation invocation in `run.sh`; preserve its exit handling and fallback trap.

### Task 2: Successful-trial token averages

**Files:**
- Modify: `analysis/test_token_usage.py`
- Modify: `analysis/token_usage.py`

**Interfaces:**
- Consumes: existing attempt records and pricing configuration.
- Produces: additive fields under `aggregates.cost_per_success` for successful-trial counts, coverage, token totals/average, and priced cost totals/average.

- [ ] Add failing tests with multiple successful trials, failed trials, missing telemetry, priced and unpriced modes.
- [ ] Run the focused aggregate tests and verify the expected missing-field failures.
- [ ] Implement successful-trial filtering and measured/priceable denominators without changing existing calculations.
- [ ] Extend Markdown/HTML summaries with both token representations and coverage.
- [ ] Run token tests and confirm they pass.

### Task 3: Headline propagation and regression verification

**Files:**
- Modify: `run.sh`
- Modify: `README.md`

**Interfaces:**
- Consumes: new token aggregation fields.
- Produces: dashboard-visible additive token fields and documented metric definitions.

- [ ] Add successful-trial token fields to the defensive token headline reader.
- [ ] Document main versus official trial accuracy and both token averages.
- [ ] Run `python3 -m unittest discover -s analysis -p 'test_*.py' -v`.
- [ ] Run `bash -n run.sh`.
- [ ] Run a synthetic result-generation CLI smoke test and inspect the output formulas.
