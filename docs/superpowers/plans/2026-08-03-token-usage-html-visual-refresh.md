# Terminal-Bench Token Usage HTML Visual Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the plain Terminal-Bench priced HTML report with a responsive SWE-style visual dashboard while preserving every existing token and cost metric.

**Architecture:** Keep `aggregate()` and the JSON/CSV/Markdown emitters unchanged. Replace only `write_html()` presentation code, deriving normalized inline-CSS bars from the existing report dictionary and retaining exact values in tables. Generate a checked sample from synthetic Harbor artifacts through the public `load_attempts()` → `build_report()` → `write_html()` path.

**Tech Stack:** Python 3 standard library, `unittest`, static HTML5, inline responsive CSS.

## Global Constraints

- Do not change Harbor execution or result aggregation.
- Do not rename, remove, or recompute fields in the report dictionary.
- Preserve retry-inclusive, winning-attempt-only, and successful-trial-only token and cost metrics.
- Preserve input, cache-read, cache-write, output, total, billed, priced, waste, coverage, quality, outcome, round, and task metrics.
- Keep HTML self-contained: no JavaScript, fonts, external assets, or network requests.
- Keep the current priced-only HTML emission behavior.
- Missing prices render as an em dash and missing telemetry is never zero-filled.
- Do not run Git commands.

---

### Task 1: Lock the visual and metric contract with emitter tests

**Files:**
- Modify: `analysis/test_token_usage.py`

**Interfaces:**
- Consumes: `token_usage.write_html(path: pathlib.Path, report: dict) -> None`
- Produces: regression coverage for the HTML structure and preserved metrics

- [ ] **Step 1: Extend the priced emitter test**

Assert that generated HTML contains stable structural hooks and all three success-economics labels:

```python
html = (out_dir / "token_usage.html").read_text(encoding="utf-8")
self.assertIn('class="stack"', html)
self.assertIn('class="bars"', html)
self.assertIn("Including failed retries", html)
self.assertIn("Winning attempt only", html)
self.assertIn("Average successful trial", html)
self.assertIn("Cache read", html)
self.assertIn("Cache write", html)
```

- [ ] **Step 2: Add a defensive rendering test**

Construct a report containing an artifact-derived task name such as
`<script>alert(1)</script>`, zero tokens, and missing costs. Call `write_html()`
and assert the raw script tag is absent, the escaped label is present, and bars
render without an exception.

- [ ] **Step 3: Run the focused tests and confirm failure**

Run:

```bash
python3 -m unittest analysis.test_token_usage.EmittersTest -v
```

Expected: failure because the existing report has no stacked or horizontal bar
structure and does not expose all preserved metrics visually.

### Task 2: Implement the self-contained visual dashboard

**Files:**
- Modify: `analysis/token_usage.py`
- Test: `analysis/test_token_usage.py`

**Interfaces:**
- Consumes: the unchanged dictionary returned by `build_report()`
- Produces: `write_html(path: pathlib.Path, report: dict) -> None`

- [ ] **Step 1: Add local rendering helpers inside `write_html()`**

Use HTML escaping for labels, safe money/integer formatting, and a width helper
that returns `0.0` when the section maximum is zero:

```python
def width(value: float, peak: float) -> float:
    return 0.0 if peak <= 0 else min(100.0, 100.0 * value / peak)
```

- [ ] **Step 2: Build visual fragments from existing aggregates**

Create:

- an outcome token-share stack with legend;
- outcome horizontal bars and the existing exact table;
- attempt-round cost-per-solve bars and the existing exact table;
- success-economics cards for retry-inclusive, winning-attempt-only, and
  successful-trial-only metrics;
- waste and token-composition displays;
- the existing per-task table.

All task names, outcome names, round labels, metadata, and notes must pass through
the escape helper.

- [ ] **Step 3: Replace the HTML/CSS template**

Use the approved light/dark responsive design with hero, KPI cards, stacked bars,
horizontal bars, tables, lower-bound warning, methodology, and caveats. Keep all
CSS inline and add no JavaScript.

- [ ] **Step 4: Run focused tests**

Run:

```bash
python3 -m unittest analysis.test_token_usage.EmittersTest -v
```

Expected: all emitter tests pass.

- [ ] **Step 5: Run the complete analysis suite and syntax checks**

Run:

```bash
python3 -m unittest discover -s analysis -p 'test_*.py' -v
bash -n run.sh
python3 -m py_compile analysis/token_usage.py analysis/test_token_usage.py
```

Expected: all tests and checks pass.

### Task 3: Generate and inspect a representative sample

**Files:**
- Create: `examples/token_usage_sample.html`

**Interfaces:**
- Consumes: `load_attempts()`, `Pricing`, `build_report()`, and `write_html()`
- Produces: a browser-openable example of the final priced report

- [ ] **Step 1: Build synthetic Harbor trial artifacts**

Create a temporary run with solved, unsolved, no-grade, measured, and unmeasured
attempts across multiple rounds. Include input, cache-read/cache-write, output,
billed cost, and custom prices so every visual section has representative data.

- [ ] **Step 2: Generate the sample through production functions**

Run the synthetic artifacts through:

```python
attempts = load_attempts(run_dir, "xyne-cli")
report = build_report(attempts, Pricing(1.4, 4.4, 0.26, 1.75), meta)
write_html(sample_path, report)
```

- [ ] **Step 3: Validate the generated artifact**

Assert that the sample starts with `<!doctype html>`, contains no external
`<script>` or stylesheet references, includes all success-economics sections,
and has non-zero bar widths.

- [ ] **Step 4: Visually inspect the sample**

Render or open `examples/token_usage_sample.html` and verify responsive layout,
legible colors, aligned tables, and visible values in both chart labels and exact
tables.
