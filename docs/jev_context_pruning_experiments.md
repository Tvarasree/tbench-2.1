# JEV-Based Context Pruning

*Implementation flows, experiment results, and lessons learned*

Purpose: document the sequence of JEV/XOR experiments to reduce coding-agent context usage, how each approach worked, what the reported measurements showed, and what the next implementation direction should address.

# Executive summary

- Experiment 1 — XOR-assisted reads: JEV selected useful portions of file reads, but ran on only a minority of read events. The run solved 25/36 attempts versus 22/36 for CLI, while total tokens and average session time increased.

- Experiment 2 — Intent + read-result keep/prune decisions: JEV judged whether a read result should remain in future context. Trial counts matched no_xor_main; task-level resolution was one instance higher and mean agent time lower, but main-model tokens were higher and JEV used 814,188 tokens.

- Experiment 3 — Higher confidence threshold: raising the prune-application threshold from 0.60 to 0.75 produced no useful conclusion because only a few sessions were affected.

- Experiment 4 — More assistant context + file lifecycle: adding assistant-message context and file-read lifecycle evidence coincided with 3.86% lower Kimi-k3 usage and 14.8% lower mean execution time, while trial accuracy remained equal; unique task instances resolved fell from 12/12 to 10/12.

- Cross-experiment lesson: local pruning is not enough. Net value depends on invocation coverage, decision quality, inference overhead, downstream agent behavior, and how often results would be replayed.

# How to read the results

These experiments use different run folders and comparisons; do not treat them as one continuous controlled series. Pass@3 results use 36 attempts across 12 instances, while some measurements are averages over measured sessions. Main-model token usage and separate JEV usage must be combined when estimating total inference cost. Figures below are reproduced from the supplied notes; unspecified details are marked as not provided.

# Experiment 1 — XOR-enabled reads (Terminal-Bench 2.1)

Goal: use XOR/JEV to reduce the amount of file-read content retained for the main model while preserving task-solving quality.

## Implementation flow

**Main LLM requests a file read**

↓

Read tool returns file content

↓

XOR/JEV evaluates eligible read events (15 of 78 reads)

↓

JEV selects relevant portions; selected projection is used in context

↓

**Main LLM continues solving with the resulting context**

JEV reduced tokens for reads where it ran, but invocation coverage was only about 19%, so the local reduction did not translate into lower total usage.

| Metric | Normal CLI | XOR enabled | Difference / interpretation |
| --- | --- | --- | --- |
| Solved attempts (pass@3) | 22/36 | 25/36 | +3 successful attempts in this sample. |
| Total tokens | 17.53M | 20.93M | +19% overall for XOR. |
| Average time/session | 495s | 715s | +220s average for XOR. |
| Read calls | 112 | 71 | 41 fewer read calls. |
| Bash calls | 787 | 840 | 53 more Bash calls. |
| Write calls | 25 | 49 | 24 more write calls. |
| JEV-triggered reads | — | 15/78 (~19%) | JEV used on a minority of reads. |
| Candidate tokens on JEV reads | 40,196 | 26,651 | 33.7% reduction on evaluated reads. |

## Observed behavior

- XOR made 48 more file-viewing Bash commands and 26 more testing commands, suggesting the agent sometimes used Bash to inspect more content after JEV returned a partial file.

- XOR also made 24 more write calls.

## Interpretation

This experiment showed that selective read projection can reduce an individual read result. It did not show an overall token or latency win: total tokens rose 19% and average time rose from 495s to 715s. Increased Bash activity is an important side effect to monitor.

# Experiment 2 — JEV decides whether to prune a read

Goal: provide JEV with user intent and the read result, then let it decide whether the result should remain in future context.

## Implementation flow

**Main LLM reads a file and receives the full result**

↓

Build JEV request from user intent + read result

↓

JEV returns keep or prune with confidence

↓

Runtime validates action and confidence threshold

↓

**Apply accepted prune to future model-visible context; preserve original history**

This is post-consumption context management: the main model first receives the read normally, then JEV decides whether it needs to remain in future context.

| Metric | xor_read_good_v1 | no_xor_main |
| --- | --- | --- |
| Trial accuracy | 23/36 = 63.89% | 23/36 = 63.89% |
| Trials resolved | 23 | 23 |
| Task-level resolved | 11/12 = 91.67% | 10/12 = 83.33% |
| Average agent time/session | 761.48s | 926.99s |
| Main-model tokens | 24,220,182 | 20,919,439 |
| Average tokens/measured session | 712,358.29 | 597,698.26 |

## JEV decision accounting

| Metric | Value |
| --- | --- |
| Total XOR decision points | 118 |
| Actual JEV calls | 90 |
| Deterministic non-JEV decisions | 28 |
| Raw JEV keep decisions | 29 |
| Raw JEV prune decisions | 61 |
| Applied JEV prunes | 60 |
| Suppressed JEV prunes | 1 |
| Additional deterministic prunes | 2 |
| Total applied prunes | 62 |
| Characters removed by JEV prunes | 208,276 |
| Characters removed including deterministic prunes | 213,859 |
| Separate JEV token usage | 814,188 |

Of 90 JEV calls, 61 returned prune and 29 returned keep. Sixty prune decisions were applied; one was suppressed because its confidence did not pass the runtime threshold.

## Interpretation

- Trial accuracy was unchanged; task-level resolution was 11/12 for XOR versus 10/12 for the baseline.

- Average agent time was lower in xor_read_good_v1 (761.48s versus 926.99s).

- Main-model token usage was about 3.30M higher, with another 814,188 tokens consumed by JEV. The reported pruning therefore did not yield an overall token-cost win in this comparison.

- Characters removed and prune counts alone are insufficient; account for inference overhead, future replay frequency, agent behavior, and task outcomes.

# Experiment 3 — Raise confidence threshold from 0.60 to 0.75

Goal: make pruning more conservative by requiring higher confidence before applying a JEV prune.

## Implementation flow

**JEV evaluates a read candidate and returns action + confidence**

↓

Runtime compares confidence with the application threshold

↓

At 0.75, only higher-confidence prune decisions are applied

↓

**Lower-confidence prune decisions are suppressed; original result remains**

Reported outcome: no useful result was obtained because only a few sessions were affected. The supplied notes do not include enough quantitative data to estimate a reliable impact on tokens, time, or quality.

## Interpretation and limitation

- A threshold-only change is hard to evaluate when few sessions have decisions in the affected confidence range.

- Do not conclude that 0.75 is better or worse; the evidence was insufficient.

- A follow-up should report candidate counts, confidence distribution, prunes applied/suppressed, task outcomes, main-model tokens, JEV tokens, and latency on the same sessions.

# Experiment 4 — More assistant context + file lifecycle evidence

Goal: give JEV more evidence than user intent and the read result alone, including assistant-message context and lifecycle information indicating whether/how a file was read.

## Implementation flow

**Main LLM reads a file and receives the full result**

↓

Collect user task + candidate read + relevant assistant messages

↓

Add file lifecycle evidence and related later activity

↓

JEV evaluates whether the read result should remain in future context

↓

**Runtime applies accepted decision to the future context projection**

| Metric | CLI | XOR-CLI | Difference / interpretation |
| --- | --- | --- | --- |
| Accuracy | 69.44% | 69.44% | Equal reported trial accuracy. |
| Unique instances resolved | 12/12 | 10/12 | XOR resolved two fewer unique instances. |
| Average execution time/session | 761.99s | 649.39s | XOR 14.8% lower. |
| Total Kimi-k3 usage | 23.67M | 22.76M | XOR lower by 912.64K tokens (3.86%). |
| Average tokens/session | 657.52K | 632.17K | XOR lower by about 25.35K. |

## JEV pruning reported

| Metric | Result |
| --- | --- |
| JEV calls across 36 sessions | 81 |
| Prune decisions | 49 |
| Keep decisions | 32 |
| Reported token reduction attributed to JEV pruning | 381.74K |

The supplied summary attributes 381.74K tokens to good pruning where files were not reread. The provided table does not include a complete JEV-overhead accounting for this comparison, so treat 381.74K as attributed reduction, not automatically as net savings.

## Interpretation

- Adding assistant-message context and file lifecycle evidence coincided with lower reported main-model token usage and lower average execution time.

- Trial accuracy remained 69.44%, but XOR resolved 10/12 unique instances compared with 12/12 for CLI. This is promising for efficiency but does not show that all task-level outcomes were preserved.

- The comparison cannot isolate the effect of the added context unless other run conditions were held constant.

# Cross-experiment comparison

| Experiment | Decision/context approach | Reported outcome | Main lesson |
| --- | --- | --- | --- |
| 1. XOR read selection | Select portions of reads; JEV on 15/78 reads | 25/36 vs 22/36; tokens +19%; time 715s vs 495s | Local reduction did not produce an overall win; coverage and downstream Bash behavior mattered. |
| 2. Intent + read decision | JEV chooses keep/prune using intent and read result | 23/36 both; 11/12 vs 10/12 task-level; time lower; main tokens higher; JEV 814K | Measure decision quality and overhead together. |
| 3. Threshold 0.75 | More conservative confidence gate | Too few affected sessions for useful conclusion | Insufficient sample; no threshold conclusion. |
| 4. Assistant context + lifecycle | JEV sees assistant context and file lifecycle | Accuracy equal; tokens −3.86%; time −14.8%; unique instances 10/12 vs 12/12 | Promising efficiency signal, but task coverage and net cost need controlled evaluation. |

# What the experiments collectively show

1. The initial XOR approach reduced selected read content, but limited invocation and compensating Bash behavior made overall cost worse.

2. JEV keep/prune decisions based on task intent are a more direct formulation, but JEV inference overhead can overwhelm savings.

3. Raising the confidence threshold alone produced insufficient evidence to justify a policy change.

4. Adding assistant context and file lifecycle evidence coincided with a better token/time result, but unique task coverage was lower.

5. A successful system must optimize net cost and task quality—not just characters removed or the number of prunes.

# Recommended next implementation direction

Preserve the post-consumption principle—let the main LLM receive the full result first—while extending evaluation beyond file reads to accumulated Bash results. Bash appears to be a substantial opportunity, but candidate selection should be value-based rather than gated by a fixed character count.

6. Collect candidates after the main LLM has consumed them; start with Bash results while reusing existing projection and persistence infrastructure.

7. Give JEV three actions: keep, prune, or summarize. Verify the deployed JEV contract supports the third action before implementation.

8. For summarize, make a separate LLM call using the exact same model and provider configuration as the active main session. JEV selects the action; it does not generate summary text.

9. Use 4,000 characters only as a prioritization heuristic, never as a hard eligibility gate. Small redundant results may be prunable; large critical results may need to be kept.

10. Apply transformations only to future model-visible context. Preserve original durable results and persist decisions/summaries so replay does not repeat inference.

11. Fail open to keep if JEV or summarization fails, confidence is insufficient, or relevance is uncertain.

12. Measure net token savings after JEV and summarization overhead, alongside task success, latency, repeated reads/commands, and future context requests.

# Evaluation checklist for the next experiment

- Same benchmark instances, trials, model configuration, and harness version across baseline and treatment.

- Report trial-level and task-level resolution separately.

- Separate main-model input/output/total tokens from JEV and summarization tokens.

- Distinguish total end-to-end time from agent-only time.

- Count candidate results, JEV calls, keep/prune/summarize choices, applied actions, and fallbacks.

- Estimate future replay savings rather than only immediate character reduction.

- Track later reads or Bash calls that repeat or recover previously pruned information.

- Report confidence distribution and reasons for suppressed decisions.

- Verify original history and replay correctness.

- Use enough sessions to assess the policy rather than only a few affected sessions.

# Data caveats

- The four experiments use different comparisons/run folders and should not be treated as a controlled ablation without confirmation that run conditions match.

- Experiment 2's XOR main-model token count excludes separate JEV usage in the supplied table; JEV usage is reported separately.

- Experiment 4's 381.74K tokens are attributed to good pruning, but all overhead figures needed to establish net savings were not supplied.

- Experiment 3's exact sample size and numerical metrics were not supplied.

- Experiment 1's 40,196 → 26,651 reduction applies only to reads where JEV was invoked, not all reads or total session tokens.
