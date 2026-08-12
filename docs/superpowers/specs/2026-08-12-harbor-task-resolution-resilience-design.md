# Harbor Task Resolution Resilience

## Goal

Prevent a transient Harbor Hub task-resolution failure from cancelling an
in-progress Terminal-Bench run. Resolve pinned task digests from the existing
local package cache, retry registry work before model execution, isolate trial
creation failures, resume incomplete jobs, and never report a partial job as a
successful benchmark.

## Scope and blast radius

All production and test changes remain inside `terminal-bench-v2-agentic`.

The implementation may change:

- the Harbor 0.13.1 compatibility patch installed by `setup.sh`;
- dataset-cache restoration and validation helpers;
- `run.sh` supervision, recovery, completion validation, and final status;
- standardized result metadata describing completion and recovery;
- focused unit and fault-injection tests;
- Terminal-Bench operator documentation.

There are no changes to `xyne-eval-ops-dashboard`, the eval runner, dashboard
UI, benchmark tasks, agents, model prompts, verifier behavior, reward formulas,
configured concurrency, or configured attempts.

Harbor remains pinned to `0.13.1`. Compatibility changes must verify that the
target Harbor version and expected source structure match before activation.
An unknown Harbor version or failed patch must stop setup instead of silently
running without the protections.

## Incident being addressed

Run `e1775a2e-e453-40c4-a5fa-95a8abdf6563` stopped at 379 of 445 planned
trials. Four earlier attempts of `gpt2-codegolf` resolved the same pinned digest
successfully. The fifth attempt called Harbor Hub again, received no task row,
and raised `ValueError: Task version not found` before the trial was created.
The exception escaped Harbor's `asyncio.TaskGroup`, cancelled eight active
siblings, and prevented 66 queued trials from starting.

The existing runner then preserved partial reports and exited zero, so the
outer eval service treated an incomplete benchmark as successful.

## Storage contract

The resilience work reuses the task-package cache that setup already populates
under `~/.cache/harbor/tasks/packages`. It does not create one package copy per
attempt and does not duplicate Docker images.

The current Terminal-Bench 2.1 cache was measured from the configured GCS
object:

- compressed archive: 50,288,097 bytes (47.96 MiB);
- extracted cache: 59,508 KiB (58.11 MiB);
- 89 task packages and 946 files;
- largest individual package: approximately 12.1 MiB;
- temporary peak while retaining both archive and extraction: approximately
  106 MiB.

This is negligible relative to the 250 GB VM disk. Container images remain the
dominant storage consumer and retain their existing GAR/Docker cache behavior.

## Cache restoration and preflight

Dataset restoration must be deterministic and complete before agent compute.

1. Restore the configured GCS task archive and verify its SHA-256 sidecar.
2. Fall back to `harbor download` only when the archive path is unavailable or
   invalid.
3. Fix configuration parsing so absence of PyYAML does not erase configured
   nested values and accidentally force the live-download fallback.
4. Validate every selected task before invoking `harbor run`.
5. For each selected task, require one usable digest directory containing at
   least `task.toml`, `instruction.md`, `environment/`, and `tests/`.
6. Emit a run-local cache manifest mapping package task name to digest and
   absolute local directory.
7. Log selected package count, extracted cache size, free disk space, and
   validation outcome.

Missing, ambiguous, or malformed selected packages fail before task images or
model calls are started. Extra unselected packages are harmless.

## Local-first package resolution

For a `PackageTaskId` carrying an immutable `sha256:<digest>` reference, the
Harbor compatibility layer must compute the standard package-cache path and
validate it before making a registry request:

```text
~/.cache/harbor/tasks/packages/<org>/<task>/<digest>
```

When the directory is valid, Harbor returns a cached download result directly.
It must not call `RegistryDB.resolve_task_version`, Supabase storage, or task
download telemetry for that trial.

Mutable references such as `latest`, aliases, and missing digest directories
still require registry resolution. A cache entry that exists but lacks required
task files is treated as corrupt, not as a hit.

The dataset package may still require Harbor Hub once while constructing the
initial pinned job configuration. That work happens before agent execution and
is covered by registry retry. After the job has pinned task digests and passed
cache preflight, trial execution must not require Harbor Hub.

## Registry retry

Registry retry is restricted to pre-agent infrastructure work. It must not
repeat an agent or verifier execution.

Use five total attempts with exponential delays of 1, 2, 4, and 8 seconds.
Retry:

- transport and connection failures;
- temporary server/API failures;
- a null registry response;
- `Task version not found` for a pinned or previously known package reference.

Do not retry invalid local input, malformed package identifiers, unsupported
reference formats, or cache integrity failures.

Harbor's generic trial `--max-retries` remains zero. Retrying whole trials can
repeat model calls and alter benchmark cost or semantics. Registry retry and
job recovery provide infrastructure robustness without changing trial
fairness.

## Failure isolation

One trial coroutine failure must not cancel unrelated active or queued trials.
The Harbor compatibility change replaces fail-fast collection with isolated
collection equivalent to `asyncio.gather(..., return_exceptions=True)`.

Successful trials continue to write their normal directories and
`result.json`. Failed trial creation remains absent or incomplete and is
eligible for resume. After all unaffected trials finish, Harbor returns a
nonzero systemic error containing the failed trial identities.

Operator cancellation and termination signals retain normal cancellation
semantics; failure isolation must not make a deliberately cancelled job ignore
shutdown.

## Automatic resumability

`run.sh` supervises the initial Harbor invocation and the same job directory.
If Harbor exits nonzero or completion validation finds missing trials, the
runner invokes Harbor's existing job-resume command against that directory.

Recovery behavior:

- completed trials with valid configuration and `result.json` are reused;
- directories without `result.json` are discarded by Harbor and rerun;
- `CancelledError` trials are removed and rerun using Harbor's default resume
  filter;
- the same job configuration, task digests, agent, model, concurrency, timeout
  multiplier, and attempt count are preserved;
- at most two automatic resume cycles run after the initial invocation;
- each invocation's exit code and before/after completion counts are recorded.

Resume must never rerun a valid completed trial. A recovery cycle that makes no
progress stops immediately instead of looping.

Recovery is primarily same-VM and same-invocation. The synced job directory
retains Harbor's `config.json`, `lock.json`, root result, and completed trial
results so an operator can also resume the directory manually in a compatible
environment.

## Completion gate and final exit status

Reward zero and normal no-grade agent/verifier outcomes remain completed
trials. Completion concerns execution records, not benchmark success.

Before reporting success, the runner must establish:

```text
planned trials == selected tasks * configured attempts
completed trial result files == planned trials
pending trials == 0
running trials == 0
cancelled trials == 0
final Harbor/resume invocation exited 0
```

For the full 89-task, five-attempt run, both planned and completed counts must
equal 445.

Reports and artifacts are finalized regardless of the gate result. After
finalization:

- complete jobs exit zero;
- incomplete jobs exit nonzero;
- systemic agent health failures retain their existing nonzero behavior.

The standardized results add recovery and completion metadata without changing
existing metric names or formulas:

- final status (`complete` or `incomplete`);
- planned, completed, pending, running, and cancelled counts;
- initial Harbor exit code;
- ordered invocation exit codes;
- automatic resume count;
- recovered trial count;
- remaining incomplete trial identities or a bounded sample plus artifact
  pointer.

Partial metrics remain available for diagnosis but must be labelled incomplete.

## Compatibility implementation

The Harbor changes are maintained as a focused, importable compatibility unit
inside the adapter distribution and activated from Harbor's environment during
setup. Any unavoidable source-level patch must:

- target exact Harbor 0.13.1 source snippets;
- be idempotent;
- fail closed on source mismatch;
- print an explicit activation message;
- expose enough functions to test with fake cache and registry clients;
- remain separate from Xyne, Pi, and token-accounting behavior.

The existing DooD transfer-mode compatibility remains intact and independently
gated by `TB_HARBOR_UNMOUNTED=1`.

## Verification

Tests must prove:

- all 89 selected packages pass preflight using the existing archive layout;
- missing `task.toml`, instruction, environment, or tests fails preflight;
- a pinned digest cache hit performs zero registry calls;
- five attempts use the same local package without registry access;
- a cache miss retries a transient null response and then succeeds;
- registry retry exhausts after five attempts with the expected 1/2/4/8-second
  schedule, using mocked sleep;
- invalid input and corrupt cache entries are not retried;
- one trial-creation exception does not cancel active or queued siblings;
- external cancellation still propagates;
- resume reuses completed trials and reruns only missing or cancelled trials;
- a no-progress recovery cycle stops;
- a successful recovery reaches the exact planned count and exits zero;
- an incomplete recovery still writes reports and exits nonzero;
- reward-zero, timeout, and no-grade completed trials do not fail the completion
  gate solely because they did not pass;
- result metadata exposes completion and recovery details without changing
  existing score formulas;
- Harbor version/source mismatch fails setup;
- existing DooD, agent, token-usage, results, heartbeat, shell syntax, and
  preflight tests continue to pass.

A fault-injection smoke test must warm the selected package cache, deny Harbor
Hub access after job creation, and complete a small multi-attempt run. A second
smoke test must inject one trial-creation failure and demonstrate that sibling
trials finish and only the failed trial is recovered.
