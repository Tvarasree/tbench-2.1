# terminal-bench (with xyne-cli as the agent)

Runs [harbor](https://harborframework.com/)'s **terminal-bench** dataset using
**xyne-cli** as a custom agent — the same role `claude-code`, `opencode`,
`pi`, `aider` play in the standard harbor flow. Models are served by grid.ai
(juspay). This harness is wired to the **same Batch-VM / eval-runner contract
as `swe-auto-eval`**, so it drops into the existing eval dashboard pipeline.

> Framing: this is *terminal-bench, testing xyne-cli* (and pitting it against
> mainstream agents) — **not** a new benchmark.

---

## How it fits together

| Layer | What it is |
|---|---|
| Harness | `harbor` CLI, installed as an isolated `uv tool` |
| Dataset | `terminal-bench/terminal-bench-2-1` (89 tasks) cached under `~/.cache/harbor` |
| Task images | Each `task.toml` declares `[environment].docker_image`. We mirror all 89 into **Google Artifact Registry** once, and pull from GAR every run (no Docker Hub at run time). |
| Custom agent | `xyne_harbor_agent.agent:XyneCliAgent` — uploads the prebuilt `xyne-linux-{arch}` binary into the task container and runs `xyne prompt --yolo`. `--yolo` is mandatory: headless `xyne prompt` has no interactive approver, so without it every mutating tool call stalls at the permission gate and no task can be solved. |
| Native-engine agent | `xyne_native_harbor_agent.agent:XyneNativeCliAgent` (`--agent xyne-cli-native`) — the same CLI on its **native plugin-kernel engine**, via `XYNE_NATIVE_HARNESS=1`. See [Native harness](#native-harness-xyne-cli-native). |
| Other agents | harbor built-ins: `claude-code`, `opencode`, `pi`, `aider`, `goose`, `codex` |
| Models | grid.ai (juspay), default `private-large`; self-hosted/open-weights for the PoC sweep |
| Config | `config.yaml` — single source of truth (models, agents, GAR, GCS, defaults) |

The GAR + GCS "fetch/create once, pull every time" design mirrors
`swe-auto-eval` exactly (same registry, same metadata-token auth, same
`/var/lib/docker/gcloud-env.sh` hand-off between `setup.sh` and `run.sh`).

---

## Batch-VM contract (how the eval-runner drives it)

The eval-runner clones this repo and runs, with input-params already
translated from JSON (`_`→`-`, `true`→`--flag`):

```bash
bash setup.sh  [API_KEY] EVAL_RUN_ID [--flags ...]   # provisioning (args ignored)
bash run.sh    [API_KEY] EVAL_RUN_ID [--flags ...]   # the actual eval
```

* `API_KEY` is optional — prepended only when `grid_ai_api_key` is configured.
* `EVAL_RUN_ID` is the first non-key positional; results are written to
  `${EVAL_RUNNER_OUTPUT_DIR:-<repo>/output}/<EVAL_RUN_ID>_results.json`.
* `run.sh` **always** writes a standardized results file (even on partial or
  empty failure) so the runner submits metrics instead of marking FAILED:

```json
{ "metrics": {
    "main":       { "name": "Solved", "value": <#tasks solved> },
    "secondary":  { "solved": N, "unsolved": U, "no_grade": G,
                    "total": T, "solve_rate_pct": R,
                    "successful_trials": Z, "planned_trials": P,
                    "trial_accuracy_pct": A },
    "additional": { "agent": "...", "model": "...", "per_task": { ... }, ... }
} }
```

A task counts as **solved** if `verifier/reward.txt >= 1` in **≥1** of its
`--attempts` trials (pass@k).

The main metric remains solved tasks (`x/T`). `trial_accuracy_pct` is the
official-style individual-trial metric (`Z/P * 100`), where
`P = selected tasks * configured attempts`; missing, errored, malformed, and
no-grade trials retain their planned denominator slot and count as failures.
`metrics.additional.solved_task_trial_success_pct` averages each solved task's
`passed_attempts / configured attempts`, with completely unsolved tasks excluded.

---

## One-time prerequisites (you run these once)

These mirror how `swe-auto-eval` seeds GAR. They are **scripts only** — nothing
heavy runs automatically. Permissions/credentials are yours to provide; each
script verifies what it can and **fails loudly** rather than shipping anything
corrupt.

1. **Cache the dataset locally**, then snapshot it to GCS:
   ```bash
   harbor download terminal-bench/terminal-bench-2-1 --cache
   ./scripts/make_dataset_tarball.sh            # → gs://…/terminal-bench-2-1-tasks.tar.zst (+ .sha256)
   ```
2. **Mirror task images into GAR** (digest-verified — see integrity note):
   ```bash
   python3 scripts/seed_gar_images.py --dry-run # plan (no docker)
   python3 scripts/seed_gar_images.py           # pull→retag→push→VERIFY all 89
   ```
The xyne-cli linux binaries no longer need a separate one-time publish step —
they ship inside the `@xyne/xyne-cli` npm package (>= 0.1.1), and `setup.sh`
pulls the tarball straight from the npm registry on each VM.

Targets are all configured in `config.yaml`
(`gar.registry_url`, `dataset.tarball.*`, `xyne_binary.npm_package`).

### Integrity guarantee for `seed_gar_images.py`

You asked for assurance that nothing corrupt reaches the registry. The script:

* pulls every source image **platform-pinned** (`linux/amd64`) so an
  emulated/arch-wrong image is never pushed;
* after push, **deletes the local tag, re-pulls from GAR, and compares the
  content-addressed image config id** to the pre-push id — an image is only
  recorded as seeded if they match byte-for-byte;
* writes verified entries to `scripts/seeded_images.json` **atomically and
  incrementally** (temp file + rename) so an interrupted run never half-writes;
* never pushes when the pull or digest read failed (no partial state);
* **exits non-zero** and prints an explicit failure list if *anything* failed
  or failed verification.

It uses the `docker` CLI (not docker-py) for the same credHelper reason
documented in `swe-auto-eval`'s `docker_build.py`.

---

## On each Batch VM (automatic)

`setup.sh` (in order): misc pkgs + `zstd` → `docker-ce-cli` (DooD, pinned 24.x)
→ **gcloud + GAR auth** (tarball install under `/var/lib/docker`, metadata-token
`docker login`, persisted to `/var/lib/docker/gcloud-env.sh`) → Python 3.11+ →
`uv` → `harbor` + xyne adapter (one `uv tool` env) → helper deps → **dataset
restore** (`fetch_dataset_tarball.sh`, falls back to `harbor download`) →
**xyne binary fetch** (npm registry → `./binaries/`) → `.cli_paths.sh` → verify.

`run.sh`: sources `gcloud-env.sh` → parses the contract → selects tasks →
**pre-pulls the selected images from GAR** and retags them to the exact name
harbor's prebuilt compose expects (so harbor never touches Docker Hub) →
`harbor run` → aggregates trials → writes the standardized results JSON.

---

## Manual usage (local / debugging)

```bash
export XYNE_API_KEY=sk-...                  # grid.ai key

./run.sh --task regex-log                                  # back-compat: run id auto-synthesised
./run.sh my-run-id --task regex-log --agent xyne-cli --model private-large
./run.sh my-run-id --all --agent opencode --model glm-latest --attempts 3 --concurrency 10
./run.sh KEY my-run-id --range 0-9 --agent claude-code     # explicit API key positional
./run.sh my-run-id --tasks build-pmars,regex-log,dna-insert  # pick exact tasks by name
./run.sh my-run-id --task regex-log --no-gar               # skip GAR, let harbor use Docker Hub
./run.sh --help
```

Selection is mutually exclusive: `--task`, `--tasks A,B,C`, `--range START-END`
(0-indexed inclusive), `--limit N`, `--all`. Unknown `--flags` are forwarded to
`harbor run` verbatim.

`--tasks` takes any number of names, up to the whole dataset. Surrounding
whitespace is trimmed and duplicates are dropped; the selection is emitted in
dataset-sorted order, so trial ordering matches `--range`/`--limit`. Any name
not in the task cache aborts the run before a single image is pulled, and the
error lists every unknown name at once. Because the dashboard submits every form
field on each run, `--tasks` **takes precedence over `--range`/`--limit`
regardless of flag order**; leave the field empty to select by range.

---

## Token usage & cost

Every run writes `token_usage.{json,csv,md}` into the results dir (synced as
`repo/output/`), plus `token_usage.html` when prices were supplied. A headline
also lands in `metrics.additional.token_usage`, so the run page shows totals
without opening artifacts.

```bash
./run.sh my-run-id --all --price-input 3 --price-output 15 --price-cached 0.3 --price-cache-write 3.75
```

All four are **USD per 1,000,000 tokens** and optional:

* `--price-input` and `--price-output` are both required to price a run. One
  alone leaves it unpriced *with a stated reason* — a half-priced total is
  worse than no total.
* `--price-cached` is optional on top. Given, cache-read tokens bill at that
  rate; omitted, they bill at the input rate.
* `--price-cache-write` is also optional. Cache writes use this rate when
  supplied and otherwise use the input rate. Reads and writes remain separate
  in JSON, CSV, and Markdown output.
* Unpriced runs still report every available token count, and Harbor's as-billed
  `cost_usd` is emitted either way.

The report prefers each agent's native artifact, preserving fields Harbor
0.13.1 drops, then falls back to `result.json` (`AgentContext`). It supports
`xyne-cli`, `xyne-cli-native`, `claude-code`, `opencode`, `pi`, `aider`,
`goose`, and `codex`.
Every row records its source and measurement quality. Outcomes come from
`verifier/reward.txt`; one row is one **trial** = one attempt.

Two token-efficiency views are emitted together:

* The existing `tokens_per_solve_including_failed_retries` remains total
  measured run tokens divided by tasks solved at least once. It represents the
  whole evaluation spend needed to produce the observed solved-task count.
* `avg_tokens_per_successful_trial` sums only successful trials and divides by
  successful trials with measured telemetry. Failed trials are excluded because
  Harbor trials are isolated. Its successful/measured/priceable counts are
  included alongside it, and the priced equivalent uses only priceable
  successful trials.

**Coverage is a first-class output.** Killed trials can still lack a complete
artifact, so any total taken at < 100% coverage is a lower bound and is
labelled as one. Harbor 0.13.1's old Goose combined-total fallback is retained
as `total_only`: it counts toward token coverage but is never custom-priced as
input because its input/output split is unknown.

> **Known limit.** xyne's session JSONL has no terminal summary record, so the
> adapter sums per-message `usage` — the same way xyne computes its own totals.
> Subagent turns are only counted if pi persisted them into that session file.
> Reconcile against a finished run before trusting absolute xyne-cli numbers.
>
> `xyne-cli-native` is measured differently and does **not** share that limit:
> the native engine writes an append-only `llm_usage` ledger row per model step
> (exact provider counters, including tool-loop steps), which
> `xyne_native_harbor_agent.session_usage` sums directly. The embedded reader
> cannot read it — different record kind, different field names — which is why
> the two adapters do not share a parser.

Reporting is non-fatal and standalone-runnable over a finished job:

```bash
python3 analysis/token_usage.py --run-dir logs/<job-id> --out-dir /tmp/report \
  --price-input 3 --price-output 15
python3 -m unittest discover -s analysis -p 'test_*.py'   # stdlib-only test suite
```

---

## Repo layout

```
terminal-bench/
├── config.yaml                 # single source of truth
├── setup.sh                    # Batch-VM provisioning (idempotent)
├── run.sh                      # eval-runner-contract entrypoint + results
├── input_params.json           # reference copy of the dashboard's run form
├── requirements.txt            # helper-script deps (PyYAML)
├── adapter/                    # the xyne-cli harbor agents
│   ├── xyne_harbor_agent/      # embedded-Pi engine (default)
│   │   ├── agent.py            # install + `xyne prompt --yolo` + token capture
│   │   └── session_usage.py    # session-JSONL parser (harbor-free, unit-tested)
│   └── xyne_native_harbor_agent/   # native plugin-kernel engine
│       ├── agent.py            # same + XYNE_NATIVE_HARNESS=1, engine probe/verdict
│       └── session_usage.py    # llm_usage ledger parser + engine classifier
├── analysis/
│   ├── token_sources.py        # native parsers for all seven offered agents
│   ├── token_usage.py          # normalization, pricing, aggregation, emitters
│   ├── test_token_sources.py   # native artifact parser regressions
│   ├── test_token_usage.py     # end-to-end reporter regressions
│   ├── test_heartbeat.py       # background-process cleanup regression
│   └── report.py               # manual pass/fail taxonomy report (not run by run.sh)
├── scripts/
│   ├── heartbeat.sh            # progress loop with explicit child cleanup
│   ├── seed_gar_images.py      # ONE-TIME: task images → GAR (digest-verified)
│   ├── make_dataset_tarball.sh # ONE-TIME: ~/.cache/harbor/tasks → GCS
│   ├── pull_tb_images.py       # per-run: GAR → local (harbor-expected name)
│   └── fetch_dataset_tarball.sh# setup.sh: GCS → ~/.cache/harbor (verified)
├── binaries -> ../xyne-cli/binaries   # local dev symlink; setup.sh replaces it
│                                      # with a real dir + npm-fetched binaries
│                                      # on a fresh VM
├── binaries-native/            # setup.sh builds this from feat/native-harness
│                               # (gitignored; never fetched from npm)
└── runs/                       # harbor job outputs
```

## Native harness (`xyne-cli-native`)

`xyne-cli` ships two engines. The default is the embedded Pi runtime; the
**native plugin-kernel** engine is opt-in at runtime via `XYNE_NATIVE_HARNESS=1`.
`session-factory.ts` is the single branch point, and headless `xyne prompt` goes
through the same factory as the TUI, so the env var is the entire switch — there
is no separate binary mode or entrypoint.

```bash
./run.sh my-run-id --task regex-log --agent xyne-cli-native --model private-large
```

**Where the binary comes from.** The native engine lives on the unmerged
`feat/native-harness` branch and is **not on npm** — registry `latest` tracks
`master`, which has no kernel code. Building it on the VM was tried and
rejected: **that branch does not build from a clean checkout.** Three defects,
each still present at xyne-cli `4202023f` (rebuilt 2026-09-10):

1. `build:webpack-bundle` fails — webpack cannot resolve `@xyne/protocol`.
   `tsconfig.json` maps it via `paths` (tsc only); `webpack.config.cjs` has no
   matching alias and the root `package.json` does not declare it, so `bun install`
   never links it. Fix: `ln -s ../../packages/protocol node_modules/@xyne/protocol`.
2. The resulting binary **crashes at startup**, on Linux and macOS alike:
   `graceful-fs` (via `proper-lockfile`) monkey-patches the `fs` module object at
   import time, and Bun's compiled-binary `fs` namespace is non-extensible, so
   `Object.defineProperty` throws before `main()`. Fix: route `graceful-fs` to the
   builtin in `webpack.config.cjs`'s function-based externals —
   `if (request === 'graceful-fs') return callback(null, 'import node:fs');`
3. `npm install` fails with `EOVERRIDE` — `overrides["@babel/core"]` is `^7.29.7`
   while `devDependencies` pins exact `7.29.7` (bun's `exact = true` in
   `bunfig.toml` rewrote it). Master has caret in both, so this is
   branch-introduced. No CI builds a binary, which is why it went unnoticed.

**Why the binary must be rebuilt past `f7a41fb9`.** The first dry run
(2026-09-09, `d7d59564`) scored 0/5 with every trial making exactly ONE model
call and executing ZERO tools: `toolNames: []` in every `request_header`, and
kimi-k3 improvising `<|open|>call tool="bash"...` as plain prose. Cause was in
xyne-cli, not this harness — headless never selected a model, so
`resolveModelCapabilities` returned `undefined` and `advertisedToolList`
returned `[]`. xyne-cli fixed it in `f7a41fb9` ("the zero-tools-on-fresh-boot
fix": `seedDefaultModel()` at boot plus `input.model ?? llm.defaultModelId?.()`
in the loop). **Any binary built before `f7a41fb9` advertises no tools and
cannot solve a single task** — check `request_header.toolNames` in
`agent/sessions/**/*.jsonl` before trusting a run.

**Second dry run (2026-09-10, `c9134b90`) scored 40% (2/5).** Tools worked; the
losses were two xyne-cli engine caps, both hit deterministically: the native
loop's 32-step limit (`core-agent-loop` `maxSteps ?? 32`) cut off the
many-step tasks, and the 8192 default max-output-tokens
(`llm-pi-ai/models.ts`) truncated single-message file writes
(`finishReason: length`). xyne-cli lifted both in `4202023f` — `maxSteps ?? 200`
and `maxTokens ?? 32000`. The committed binary is built from `4202023f`; a
run's requests now carry `max_completion_tokens: 32000` (podman-verified).

So the binary is **built by hand and committed**, zstd-compressed to 33.2 MB (under
GitHub's 50 MB warning threshold; the raw 118 MB would exceed the 100 MB hard
limit). `setup.sh` only decompresses it — `zstd` is already a verified dependency.

To rebuild after the branch moves (**it will not build without the two patches
above**):

```bash
bun install --frozen-lockfile
ln -s ../../packages/protocol node_modules/@xyne/protocol      # defect 1
# apply the graceful-fs external in webpack.config.cjs          # defect 2
bun run build:protocol && bun run build:compile \
  && bun run build:webpack-bundle && bun run build:binary:prepare \
  && bun run build:binary:linux-x64
zstd -19 -T0 binaries/xyne-linux-x64 -o <repo>/binaries-native/xyne-linux-x64.zst
cp package.json <repo>/binaries-native/package.json
```

Then update `built_from_commit` in `config.yaml`. Verify before committing —
`podman run --rm -v $PWD/binaries:/b:ro debian:bookworm-slim /b/xyne-linux-x64 --version`
should print a version, not a `graceful-fs` stack trace.

**How a run proves it used the native engine.** A binary built before the kernel
landed treats `XYNE_NATIVE_HARNESS=1` as an unknown variable, runs the embedded
engine, and says nothing about it — a silent wrong-engine measurement. Three
independent checks close that:

| # | Check | When | On failure |
|---|---|---|---|
| 1 | Probe with a deliberately invalid `XYNE_NATIVE_PROFILE`; a kernel-capable binary rejects it by name before any model call (zero tokens, zero network) | `install()`, before credentials are written | **raises** — the task never runs |
| 2 | `[tb-native] XYNE_NATIVE_HARNESS=1 XYNE_NATIVE_PROFILE=standard` echoed into `/logs/agent/xyne.log` ahead of the turn | every trial | visible in the dashboard log viewer |
| 3 | Session-log header line: the two engines write deliberately incompatible headers (`xyne-native-session` vs `session`) | after the graded turn | error log + `verdict` in `<trial>/agent/engine.json` and `AgentContext.metadata` |

Check 3 is evidence produced *by* the graded run rather than an assertion about
it. `engine.json` reads:

```json
{ "expected": "native-plugin-kernel", "profile": "standard",
  "verdict": "native", "native_files": 1, "embedded_files": 0 }
```

A `verdict` of `embedded` means the flag did not take and that trial's numbers
describe the wrong runtime.

## Notes

* **Why GAR, not Docker Hub at run time:** avoids rate limits, makes runs
  reproducible/offline, and lets us push correct-arch images once (the
  amd64-only-image / QEMU issue is sidestepped because Batch VMs are x86_64
  and we pin `linux/amd64`).
* **harbor prebuilt hook:** `docker-compose-prebuilt.yaml` has no
  `pull_policy`, so Compose default `missing` reuses a locally-present image —
  that's why retagging the GAR image to the `task.toml` name works.
* **Adapter surface is deliberately small:** all Batch wiring lives in
  `setup.sh` / `run.sh` / `scripts/`. `xyne_harbor_agent` owns only two things
  beyond install/run — the `--yolo` flag (see above) and
  `populate_context_post_run`, which fills harbor's token fields from the
  session transcript so `xyne-cli` appears in the cost report like every
  built-in agent.
