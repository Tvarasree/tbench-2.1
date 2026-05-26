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
| Custom agent | `xyne_harbor_agent.agent:XyneCliAgent` — uploads the prebuilt `xyne-linux-{arch}` binary into the task container and runs `xyne prompt` |
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
                    "total": T, "solve_rate_pct": R },
    "additional": { "agent": "...", "model": "...", "per_task": { ... }, ... }
} }
```

A task counts as **solved** if `verifier/reward.txt >= 1` in **≥1** of its
`--attempts` trials (pass@k).

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
./run.sh my-run-id --task regex-log --no-gar               # skip GAR, let harbor use Docker Hub
./run.sh --help
```

Selection is mutually exclusive: `--task`, `--range START-END` (0-indexed
inclusive), `--limit N`, `--all`. Unknown `--flags` are forwarded to
`harbor run` verbatim.

---

## Repo layout

```
terminal-bench/
├── config.yaml                 # single source of truth
├── setup.sh                    # Batch-VM provisioning (idempotent)
├── run.sh                      # eval-runner-contract entrypoint + results
├── requirements.txt            # helper-script deps (PyYAML)
├── adapter/                    # the xyne-cli harbor agent (unchanged)
│   └── xyne_harbor_agent/agent.py
├── scripts/
│   ├── seed_gar_images.py      # ONE-TIME: task images → GAR (digest-verified)
│   ├── make_dataset_tarball.sh # ONE-TIME: ~/.cache/harbor/tasks → GCS
│   ├── pull_tb_images.py       # per-run: GAR → local (harbor-expected name)
│   └── fetch_dataset_tarball.sh# setup.sh: GCS → ~/.cache/harbor (verified)
├── binaries -> ../xyne-cli/binaries   # local dev symlink; setup.sh replaces it
│                                      # with a real dir + npm-fetched binaries
│                                      # on a fresh VM
└── runs/                       # harbor job outputs
```

## Notes

* **Why GAR, not Docker Hub at run time:** avoids rate limits, makes runs
  reproducible/offline, and lets us push correct-arch images once (the
  amd64-only-image / QEMU issue is sidestepped because Batch VMs are x86_64
  and we pin `linux/amd64`).
* **harbor prebuilt hook:** `docker-compose-prebuilt.yaml` has no
  `pull_policy`, so Compose default `missing` reuses a locally-present image —
  that's why retagging the GAR image to the `task.toml` name works.
* **Adapter unchanged:** all Batch wiring lives in `setup.sh` / `run.sh` /
  `scripts/`; the proven `xyne_harbor_agent` is untouched.
