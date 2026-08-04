# Terminal-Bench Always-On Token Usage HTML

## Goal

Generate `token_usage.html` for every Terminal-Bench run that has trial records,
including runs without custom token prices. Preserve the existing priced report
and use measured tokens as the primary unit when complete custom pricing is not
available.

## Scope and blast radius

All changes remain inside the `terminal-bench-v2-agentic` repository.

Production behavior changes are concentrated in `analysis/token_usage.py`:

- remove the priced-only HTML emission gate;
- make the existing HTML renderer choose a priced or token-only presentation;
- update the script's output description and completion message.

`analysis/test_token_usage.py` receives coverage for both modes. `run.sh` may
receive wording or status-output corrections so it accurately describes the
always-generated HTML file, but its argument parsing, Harbor invocation, report
ordering, non-fatal accounting behavior, results aggregation, and exit behavior
must not change.

There are no changes to `xyne-eval-ops-dashboard`, the eval runner, benchmark
selection, agent execution, Harbor, or dashboard UI code.

## Existing behavior to preserve

The following contracts remain unchanged:

- custom pricing requires both input and output prices;
- partial pricing is never applied;
- cache-read and cache-write pricing use their supplied rates or retain the
  existing input-rate fallback;
- priced totals use only priceable attempts;
- upstream billed cost remains separate from custom-priced cost;
- measurement coverage and lower-bound warnings retain their current meaning;
- solved-task, successful-trial, winning-attempt, outcome, round, task, and waste
  formulas remain unchanged;
- existing JSON fields and the CSV and Markdown schemas remain unchanged;
- two additive JSON metrics are introduced for token-only round efficiency and
  token waste percentage;
- token accounting remains non-fatal to the benchmark run;
- an empty run directory continues to produce no reports because there are no
  trial records to render.

## Report modes

The renderer selects its mode from the existing `meta.priced` value. It uses a
single HTML template with mode-aware labels, values, columns, and chart inputs.
Separate priced and unpriced templates are intentionally avoided to prevent the
two reports from drifting.

### Priced mode

When both input and output prices are supplied:

- custom-priced cost remains the primary economic unit;
- token values remain visible as the secondary equivalent;
- success and round comparison bars continue to use custom-priced values;
- priced cost columns and cache-pricing context remain visible;
- upstream billed cost remains secondary telemetry.

The current priced calculations and visible values must not change.

### Token-only mode

When complete custom pricing is unavailable:

- the HTML file is still generated;
- measured tokens become the primary unit in every headline, comparison card,
  chart, and efficiency view;
- custom-priced cards and columns are omitted instead of being filled with em
  dashes or presented as zero;
- a partial price list does not alter any value or chart;
- upstream billed cost may be shown when available, but only as explicitly
  labelled secondary telemetry.

Upstream billed cost must never be used as a fallback for custom pricing, chart
normalization, success efficiency, round efficiency, or waste attribution.

## Terminal-Bench metric semantics

Terminal-Bench runs every configured trial even when an earlier trial solved the
task. The HTML therefore preserves three intentionally distinct success views:

1. **Including failed retries**: all measured run tokens, including later trials
   and trials for unsolved tasks, divided by tasks solved at least once.
2. **Winning attempt only**: the first successful trial's tokens for each solved
   task, divided by solved tasks.
3. **Average successful trial**: measured tokens from every independently
   successful trial, divided by measured successful trials.

These definitions are not replaced with the stop-after-success semantics used by
SWE Auto Eval.

## HTML presentation contract

### Header and hero

The header retains run, agent, model, attempt count, generation time, agent
version, pricing note, and coverage warning.

The hero shows:

- priced mode: custom-priced cost per solved task including failed retries, with
  the token equivalent;
- token-only mode: tokens per solved task including failed retries.

Both modes retain solved-task and trial context. No-solve values render as
unavailable rather than zero.

### Summary and token composition

Both modes show total measured tokens, tasks solved, successful trials,
measurement coverage, and the input/cache-read/cache-write/output composition.

Priced mode additionally leads with custom-priced total cost. Token-only mode
does not display an empty custom-cost KPI. When upstream billed cost exists, it
is displayed in a secondary, explicitly labelled location with no implication of
complete coverage or cross-agent comparability.

### Success economics

All three existing success scenarios remain visible.

- priced mode displays custom-priced cost as the primary value and tokens as the
  secondary value; bars normalize by custom-priced cost;
- token-only mode displays tokens as the primary value; bars normalize by tokens.

Successful-trial measurement and priceable coverage text remains accurate for
the selected mode.

### Outcome breakdown

Outcome volume bars remain token-based in both modes. Exact input, cache-read,
cache-write, output, total, and average-token values remain in the table.

- priced mode retains custom-priced and billed-cost columns;
- token-only mode omits custom-priced columns;
- billed-cost telemetry is included only as secondary information when present.

### Attempt-round efficiency

Each round aggregate adds:

- `tokens_per_solve`

It is calculated as:

```text
tokens per solve = round total measured tokens / successful trials in that round
```

A round with no successful trial has no per-solve value.

- priced mode continues to use the existing custom-priced cost per solve;
- token-only mode uses the derived tokens per solve.

The HTML consumes this persisted value in token-only mode. Existing round fields
and priced calculations remain unchanged.

### Attribution and waste

The existing attribution definition remains unchanged:

```text
attributable tokens = first winning-attempt tokens for solved tasks
non-attributable tokens = all measured run tokens - attributable tokens
```

The waste aggregate adds:

- `tokens_wasted_pct`

It is calculated as:

```text
tokens wasted percentage = non-attributable tokens / all measured run tokens * 100
```

When the measured token total is zero, the percentage is unavailable rather than
zero. Priced mode displays existing custom-priced attribution, waste, and
percentage with token equivalents. Token-only mode displays attributable and
non-attributable tokens with the additive token waste percentage. Existing waste
fields and priced calculations remain unchanged.

### Per-task table

Both modes show all-attempt tokens and first-winning-attempt tokens. Priced mode
also retains custom-priced columns. Token-only mode omits those columns rather
than rendering unavailable monetary placeholders.

## Output behavior

When trial records exist, the script writes all four files regardless of pricing:

- `token_usage.json`
- `token_usage.csv`
- `token_usage.md`
- `token_usage.html`

The script's completion message lists HTML in both modes. `run.sh` may update its
operator-facing wording from "priced report" to a neutral HTML-report label, but
continues to check for the file and treats token reporting as non-fatal.

## Defensive behavior

- HTML-escape all metadata and artifact-derived labels.
- Zero or missing chart maxima produce zero-width bars without division errors.
- Missing primary values render as unavailable, not zero.
- Missing billed-cost telemetry is omitted or rendered as unavailable only in a
  clearly secondary position.
- A reported upstream cost of exactly zero remains distinguishable from missing
  telemetry.
- Incomplete token coverage retains the prominent lower-bound warning.
- Token-only output must not contain misleading custom-priced headings or claim
  that upstream cost represents the full run.
- The HTML remains self-contained, offline, responsive, and compatible with
  light and dark color schemes.

## Verification

Tests must prove that:

- an unpriced invocation emits JSON, CSV, Markdown, and HTML;
- partial pricing still produces token-only HTML;
- token-only hero, success cards, success bars, round bars, outcome tables,
  waste section, and per-task table use token values;
- `by_round[*].tokens_per_solve` is persisted with a null value for rounds with
  no successful trials;
- `waste.tokens_wasted_pct` is persisted and remains null when total measured
  tokens are zero;
- custom-priced placeholders and headings are absent from token-only sections;
- upstream billed cost remains secondary and does not affect bar widths;
- priced HTML preserves current calculated values, labels, columns, and chart
  inputs;
- zero solves, zero token totals, missing billed cost, incomplete coverage, and
  unsafe artifact strings render safely;
- existing parser, aggregation, JSON fields, CSV, Markdown, and priced-report
  tests keep passing;
- `run.sh` passes shell syntax validation if its status wording changes.

A representative priced and unpriced report must be generated after tests and
inspected to confirm the mode differences without changing underlying token
totals.
