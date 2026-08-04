# Terminal-Bench Always-On Token Usage HTML Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate a useful `token_usage.html` for priced and unpriced Terminal-Bench runs while preserving existing priced calculations and exposing additive token round-efficiency and waste metrics.

**Architecture:** Keep the existing aggregation and single self-contained HTML renderer. Add two token-native aggregate fields, then make the renderer select cost-first or token-first labels, values, columns, and chart inputs from `meta["priced"]`; upstream billed cost remains secondary telemetry only.

**Tech Stack:** Python 3 standard library, `unittest`, Bash syntax validation, self-contained HTML/CSS.

## Global Constraints

- All changes stay inside `terminal-bench-v2-agentic`.
- Do not change `xyne-eval-ops-dashboard`, the eval runner, Harbor execution, benchmark selection, or dashboard UI code.
- Do not run Git commands or create branches, worktrees, commits, or pushes.
- Custom pricing still requires both input and output prices; partial pricing remains token-only.
- Existing pricing, cache-rate, coverage, solve, successful-trial, winning-attempt, outcome, task, and waste formulas remain unchanged.
- Existing JSON fields and CSV/Markdown schemas remain unchanged; only `by_round[*].tokens_per_solve` and `waste.tokens_wasted_pct` are additive JSON fields.
- Upstream billed cost is secondary telemetry and never drives charts or efficiency calculations.
- `run.sh` execution, argument handling, report ordering, failure isolation, results aggregation, and exit behavior remain unchanged.
- Token accounting remains non-fatal.

---

## File map

- Modify `analysis/token_usage.py`: additive aggregate metrics, mode-aware HTML, always-on emission, script documentation and completion output.
- Modify `analysis/test_token_usage.py`: aggregation, unpriced/partial-priced HTML, priced-regression, and defensive rendering tests.
- Modify `run.sh`: operator-facing comments and HTML label only.
- Reference `docs/superpowers/specs/2026-08-04-always-on-token-usage-html-design.md`: approved behavior contract.

### Task 1: Persist token-native round efficiency and waste percentage

**Files:**
- Modify: `analysis/token_usage.py:492-608`
- Test: `analysis/test_token_usage.py:477-493`

**Interfaces:**
- Consumes: existing round bucket fields `n_total_tokens` and `solves`; existing waste values `tokens_total` and `tokens_winning`.
- Produces: `by_round[str].tokens_per_solve: float | None` and `waste.tokens_wasted_pct: float | None`.

- [ ] **Step 1: Extend the aggregate tests with exact token formulas**

Add these assertions to the existing round and waste tests:

```python
def test_waste_is_everything_not_attributable_to_a_solve(self) -> None:
    agg = self._run()
    waste = agg["waste"]
    self.assertEqual(waste["tokens_attributable_to_a_solve"], 200)
    self.assertEqual(waste["tokens_wasted"], 1000)
    self.assertAlmostEqual(waste["tokens_wasted_pct"], 83.33, places=2)
    self.assertAlmostEqual(waste["cost_usd_wasted"], 0.001)
    self.assertAlmostEqual(waste["wasted_pct"], 83.33, places=2)

def test_by_round_tracks_solves_and_cost_per_solve(self) -> None:
    agg = self._run()
    rounds = agg["by_round"]
    self.assertEqual(rounds["1"]["attempts"], 2)
    self.assertEqual(rounds["1"]["solves"], 0)
    self.assertIsNone(rounds["1"]["tokens_per_solve"])
    self.assertIsNone(rounds["1"]["cost_usd_per_solve"])
    self.assertEqual(rounds["2"]["solves"], 1)
    self.assertEqual(rounds["2"]["tokens_per_solve"], 600.0)
    self.assertAlmostEqual(rounds["2"]["cost_usd_per_solve"], 0.0006)
```

Add a zero-total edge case:

```python
def test_zero_token_total_has_no_waste_percentage(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = pathlib.Path(tmp)
        make_trial(run_dir, "zero__1", reward=0.0, tokens=(0, 0, 0))
        waste = tu.aggregate(
            tu.load_attempts(run_dir), tu.Pricing(None, None, None)
        )["waste"]

    self.assertEqual(waste["tokens_wasted"], 0)
    self.assertIsNone(waste["tokens_wasted_pct"])
```

- [ ] **Step 2: Run the focused tests and confirm the new keys fail**

Run:

```bash
python3 -m unittest \
  analysis.test_token_usage.AggregateTest.test_waste_is_everything_not_attributable_to_a_solve \
  analysis.test_token_usage.AggregateTest.test_by_round_tracks_solves_and_cost_per_solve \
  analysis.test_token_usage.AggregateTest.test_zero_token_total_has_no_waste_percentage
```

Expected: failures for missing `tokens_per_solve` and `tokens_wasted_pct`.

- [ ] **Step 3: Add the two aggregate fields without changing priced formulas**

In the existing by-round finalization loop, add:

```python
solves = bucket.get("solves", 0)
bucket["tokens_per_solve"] = (
    round(bucket["n_total_tokens"] / solves, 1) if solves else None
)
bucket["cost_usd_per_solve"] = (
    round(bucket["cost_usd_priced"] / solves, 6)
    if solves and bucket["priceable_attempts"]
    else None
)
```

In the returned waste dictionary, add:

```python
"tokens_wasted_pct": (
    round(100.0 * (tokens_total - tokens_winning) / tokens_total, 2)
    if tokens_total
    else None
),
```

- [ ] **Step 4: Re-run the focused tests**

Run the command from Step 2. Expected: all three tests pass.

### Task 2: Make HTML emission unconditional for non-empty runs

**Files:**
- Modify: `analysis/token_usage.py:1-45,1214-1232`
- Test: `analysis/test_token_usage.py:535-600`

**Interfaces:**
- Consumes: existing `report` from `build_report()` and `meta.priced`.
- Produces: `token_usage.html` alongside JSON, CSV, and Markdown whenever `attempts` is non-empty.

- [ ] **Step 1: Replace the priced-only emitter expectation**

Rename `test_unpriced_run_emits_json_csv_md_but_no_html` to
`test_unpriced_run_emits_all_reports` and assert:

```python
for name in (
    "token_usage.json",
    "token_usage.csv",
    "token_usage.md",
    "token_usage.html",
):
    self.assertTrue((out / name).is_file(), name)

html = (out / "token_usage.html").read_text(encoding="utf-8")
self.assertIn("Terminal-Bench Token Usage", html)
```

Add a partial-pricing test that invokes `tu.main()` with only
`--price-input 3` and asserts that HTML exists, `meta.priced` is false, and the
pricing note states that the report is not priced.

- [ ] **Step 2: Run both new emission tests and confirm failure**

Run:

```bash
python3 -m unittest \
  analysis.test_token_usage.EmittersTest.test_unpriced_run_emits_all_reports \
  analysis.test_token_usage.EmittersTest.test_partial_pricing_emits_token_only_html
```

Expected: unpriced and partial-priced invocations do not yet create HTML.

- [ ] **Step 3: Remove the emission gate and update script-local wording**

Change the output documentation to describe HTML as an always-generated visual
report for non-empty runs. Replace:

```python
if pricing.enabled:
    write_html(out_dir / "token_usage.html", report)
```

with:

```python
write_html(out_dir / "token_usage.html", report)
```

Make the completion message always list `json,csv,md,html`; retain the existing
priced/unpriced summary branch and non-fatal top-level exception handling.

- [ ] **Step 4: Run the focused tests**

Run the command from Step 2. Expected: both tests pass and create all four report
files.

### Task 3: Render token-only HTML without disturbing priced HTML

**Files:**
- Modify: `analysis/token_usage.py:789-1160`
- Test: `analysis/test_token_usage.py:569-715`

**Interfaces:**
- Consumes: `report["meta"]["priced"]`, existing cost/token aggregate fields,
  `by_round[*].tokens_per_solve`, and `waste.tokens_wasted_pct`.
- Produces: one self-contained mode-aware HTML document from `write_html(path, report)`.

- [ ] **Step 1: Add token-only renderer assertions before implementation**

In the unpriced emitter test, assert the primary metric and conditional labels:

```python
self.assertIn("Tokens per solved task, including all trials", html)
self.assertIn("600 tokens per solved task", html)
self.assertIn("Token-only report", html)
self.assertIn("Including failed retries", html)
self.assertIn("Winning attempt only", html)
self.assertIn("Average successful trial", html)
self.assertIn("Tokens/solve", html)
self.assertIn("66.67% of measured tokens", html)
self.assertNotIn("Custom-priced cost", html)
self.assertNotIn("Avg cost/trial", html)
self.assertNotIn("Cost/solve", html)
```

Use a fixture with upstream billed cost and compare the generated success-bar
widths against the same fixture without billed cost. Assert both documents have
identical `data-chart="success"` width values and that only the billed fixture
contains:

```html
Upstream billed cost (secondary telemetry)
```

Update the zero-bar safety test to run once with `priced=True` and once with
`priced=False`.

- [ ] **Step 2: Run the token-only renderer tests and confirm semantic failures**

Run:

```bash
python3 -m unittest \
  analysis.test_token_usage.EmittersTest.test_unpriced_run_emits_all_reports \
  analysis.test_token_usage.EmittersTest.test_upstream_cost_does_not_drive_unpriced_charts \
  analysis.test_token_usage.EmittersTest.test_html_escapes_artifact_labels_and_handles_zero_bars
```

Expected: HTML exists after Task 2 but remains cost-first and fails token-only
labels, values, columns, or bar assertions.

- [ ] **Step 3: Introduce one mode switch and token formatting helpers**

At the start of `write_html()`, define:

```python
priced = bool(meta.get("priced"))

def tokens(value: int | float | None) -> str:
    formatted = number(value)
    return "&mdash;" if value is None else f"{formatted} tokens"
```

Keep `usd()`, `number()`, `width()`, and escaping behavior intact. Build
conditional HTML fragments from `priced`; do not create a second template.

- [ ] **Step 4: Switch success cards and round charts to the active primary unit**

For each success scenario, retain both token and cost fields but select:

```python
primary_value = cost if priced else token_count
primary_text = usd(cost) if priced else tokens(token_count)
secondary_text = tokens(token_count) if priced else ""
```

Normalize success bars with costs in priced mode and tokens in token-only mode.
For rounds, select `cost_usd_per_solve` in priced mode and `tokens_per_solve` in
token-only mode. A null per-solve value must render as unavailable and a
zero-width bar.

- [ ] **Step 5: Make cards, tables, and waste content mode-aware**

Implement these conditional fragments in the single template:

```python
outcome_cost_headers = (
    '<th class="n">Priced cost</th><th class="n">Avg cost/trial</th>'
    if priced else ""
)
outcome_cost_cells = (
    f'<td class="n">{usd(bucket["cost_usd_priced"])}</td>'
    f'<td class="n">{usd(bucket["avg_cost_usd_per_attempt"])}</td>'
    if priced else ""
)
```

Apply the same pattern to round and per-task custom-cost columns. Preserve token
columns in both modes. Show billed cost only in an explicitly secondary card or
column and never pass it to `width()`.

In token-only waste cards, lead with `tokens_attributable_to_a_solve` and
`tokens_wasted`, and show `tokens_wasted_pct`. In priced mode, preserve the
existing cost-first cards and token footers.

- [ ] **Step 6: Preserve priced output through exact regression assertions**

Run:

```bash
python3 -m unittest analysis.test_token_usage.EmittersTest.test_priced_run_emits_html_too
```

Expected: all existing exact priced values and row assertions pass. If markup
must move to support conditional columns, update only structure-dependent test
slicing while retaining every existing expected numeric value and heading.

- [ ] **Step 7: Run all HTML-focused tests**

Run:

```bash
python3 -m unittest analysis.test_token_usage.EmittersTest
```

Expected: all emitter tests pass in priced, unpriced, partial-priced, zero-value,
unsafe-label, and missing-billed-cost cases.

### Task 4: Align runner-facing wording without changing execution

**Files:**
- Modify: `run.sh:538-555,614-616`

**Interfaces:**
- Consumes: existing token report invocation and file-existence check.
- Produces: accurate operator logs only; no control-flow changes.

- [ ] **Step 1: Update comments and status labels**

Change the comment describing outputs from `token_usage.{json,csv,md}` plus
priced-only HTML to `token_usage.{json,csv,md,html}` for every non-empty run.
Keep the no-price log message but state that counts and token-only HTML will be
reported. Change:

```bash
echo "  priced report: ${OUTPUT_DIR}/token_usage.html"
```

to:

```bash
echo "  HTML report: ${OUTPUT_DIR}/token_usage.html"
```

Do not change the Python command, price flags, `|| log_warn`, report order, or
final exit status.

- [ ] **Step 2: Validate shell syntax**

Run:

```bash
bash -n run.sh
```

Expected: exit code 0 with no output.

### Task 5: Full regression and representative report verification

**Files:**
- Verify: `analysis/token_usage.py`
- Verify: `analysis/test_token_usage.py`
- Verify: `run.sh`
- Update: repository-root `../WORK_DONE_AUGUST.md` after successful verification

**Interfaces:**
- Consumes: completed implementation and synthetic trial fixtures.
- Produces: evidence that priced and token-only output differ only in the
  presentation/pricing layer while token totals remain identical.

- [ ] **Step 1: Run syntax and complete analysis tests**

Run:

```bash
python3 -m py_compile analysis/token_usage.py analysis/test_token_usage.py
python3 -m unittest analysis.test_token_usage
bash -n run.sh
```

Expected: all commands exit 0.

- [ ] **Step 2: Generate representative priced and unpriced HTML from one fixture**

Create one explicit synthetic run directory:

```bash
FIXTURE_ROOT="$(mktemp -d /private/tmp/terminal-token-html-fixture.XXXXXX)"
export FIXTURE_ROOT
python3 -c 'import os, pathlib; from analysis.test_token_usage import make_trial; run = pathlib.Path(os.environ["FIXTURE_ROOT"]) / "run"; run.mkdir(); make_trial(run, "alpha__1", reward=0.0, tokens=(100, 0, 100)); make_trial(run, "alpha__2", reward=1.0, tokens=(100, 0, 100)); make_trial(run, "beta__1", reward=0.0, tokens=(300, 0, 100)); make_trial(run, "beta__2", reward=0.0, tokens=(300, 0, 100))'
```

Use that same run directory for two invocations:

```bash
python3 analysis/token_usage.py \
  --run-dir "$FIXTURE_ROOT/run" \
  --out-dir "$FIXTURE_ROOT/unpriced" \
  --eval-run-id terminal-unpriced \
  --agent xyne-cli --model private-large --attempts 3

python3 analysis/token_usage.py \
  --run-dir "$FIXTURE_ROOT/run" \
  --out-dir "$FIXTURE_ROOT/priced" \
  --eval-run-id terminal-priced \
  --agent xyne-cli --model private-large --attempts 3 \
  --price-input 3 --price-output 15 --price-cached 0.3
```

- [ ] **Step 3: Compare machine-readable totals**

Load both generated `token_usage.json` files and assert that token totals,
coverage, `tokens_per_solve`, and `tokens_wasted_pct` match, while only the
priced report has non-null custom-priced fields.

- [ ] **Step 4: Inspect desktop and narrow-width HTML**

Open or render both HTML files and confirm:

- priced report remains cost-first with token equivalents;
- unpriced report is token-first and has no misleading custom-priced sections;
- billed cost is secondary only;
- all three Terminal-Bench success scenarios remain visible;
- tables scroll rather than overflow at narrow width;
- lower-bound and no-solve states remain readable.

- [ ] **Step 5: Record the completed 4 August work item**

Append one condensed bullet under `## 4 August` in `../WORK_DONE_AUGUST.md`
covering always-on Terminal-Bench HTML, token-first unpriced rendering, the two
additive metrics, priced-regression preservation, and verification results.
