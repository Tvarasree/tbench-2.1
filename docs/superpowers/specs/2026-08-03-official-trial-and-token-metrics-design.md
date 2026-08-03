# Official Trial and Token Metrics Design

## Goal

Keep the dashboard's existing `Solved x/N` main metric while adding official-style
trial accuracy, solved-task repeatability, and an isolated successful-trial token
average without changing benchmark execution or failure handling.

## Metric contract

### Main metric: solved tasks

The main metric remains unchanged: a task is solved when at least one observed
trial has reward `>= 1`. Its value is the number of solved tasks, and
`solve_rate_pct` remains `solved_tasks / selected_tasks * 100`.

### Secondary metric: trial accuracy

Add these secondary fields:

- `successful_trials`: number of trials with reward `> 0`, matching the official
  leaderboard formula. Terminal-Bench 2.1 rewards are binary, so this normally
  equals the existing `reward >= 1` solved count.
- `planned_trials`: `selected_tasks * configured_attempts`.
- `trial_accuracy_pct`: `successful_trials / planned_trials * 100`.

Missing trial directories, malformed rewards, no-grade trials, and errored trials
occupy their planned denominator slot and therefore count as failures. This matches
the Terminal-Bench leaderboard's treatment of errored trials as reward zero while
preserving the existing no-grade diagnostics.

### Additional metric: solved-task repeatability

For each solved task, calculate `passed_attempts / configured_attempts`. Average
those task-level rates with equal weight per solved task. Publish:

- `solved_task_trial_success_pct`
- `solved_task_trial_success`: numerator/denominator details and the per-task rates

Completely unsolved tasks are excluded. Missing attempts for a solved task remain
failures through the configured-attempt denominator. If no task is solved, the
percentage is `null` rather than zero because the population is empty.

### Token metrics

Preserve the existing whole-run metrics exactly:

- `tokens_per_solve_including_failed_retries` remains total measured run tokens
  divided by tasks solved at least once.
- The corresponding priced cost metric remains unchanged.

Add successful-trial-only metrics:

- `successful_trials`: successful trial count regardless of telemetry coverage.
- `measured_successful_trials`: successful trials with token measurements.
- `tokens_successful_trials`: measured tokens summed only across successful trials.
- `avg_tokens_per_successful_trial`: the preceding sum divided by measured
  successful trials.
- `priceable_successful_trials`, `cost_usd_successful_trials`, and
  `avg_cost_usd_per_successful_trial` use the same rule for custom-priced records.

Failed attempts are excluded from this new average because each trial is isolated.
Unmeasured successful trials are reported in coverage but are not treated as zero
tokens. Existing all-run totals and lower-bound coverage warnings remain unchanged.

## Implementation boundaries

Move the embedded result aggregation from `run.sh` into a small stdlib-only Python
module. The shell runner will invoke that module with the same paths and metadata.
This makes the existing output contract and the new formulas directly testable
without launching Harbor. Token aggregation stays in `analysis/token_usage.py`.

All new JSON fields are additive. Existing field names, main metric shape, output
paths, token files, non-fatal token accounting behavior, and fallback result writes
remain intact.

## Verification

Use synthetic trial directories to test:

- mixed pass/fail attempts across solved and unsolved tasks;
- missing and malformed trial rewards in the planned denominator;
- no solved tasks;
- successful trials with full, missing, and partial token telemetry;
- priced and unpriced runs;
- preservation of existing token metrics and result fields.

Run focused unit tests first, then the complete analysis test suite, shell syntax
validation, and a synthetic end-to-end result generation smoke test.
