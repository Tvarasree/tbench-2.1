#!/usr/bin/env bash
# Dashboard progress heartbeat. Sourced by run.sh and kept separate so its
# process-cleanup behavior can be regression-tested without running Harbor.

heartbeat() {
  local interval=30 summary_every=10 tick=0 sleep_pid=""
  local t name state reward done_n run_n now
  local -A seen

  # A background Bash function waiting on an external `sleep` does not kill
  # that child when only the function PID receives TERM. Kill and reap the
  # active sleep before the heartbeat shell exits, otherwise eval-runner sees
  # an orphan in run.sh's process group and fails artifact finalization.
  trap 'trap - TERM INT; if [ -n "${sleep_pid:-}" ]; then kill "$sleep_pid" 2>/dev/null; wait "$sleep_pid" 2>/dev/null; fi; exit 0' TERM INT

  while :; do
    sleep "$interval" & sleep_pid=$!
    wait "$sleep_pid" || return 0
    sleep_pid=""
    tick=$((tick + 1))
    done_n=0; run_n=0
    now="$(date +%H:%M:%S)"
    for t in "$RUN_DIR"/*/; do
      [ -d "$t" ] || continue
      name="$(basename "$t")"
      if [ -f "${t}result.json" ]; then
        state="done"; done_n=$((done_n + 1))
      else
        state="running"; run_n=$((run_n + 1))
      fi
      if [ "${seen[$name]:-}" != "$state" ]; then
        if [ "$state" = "done" ]; then
          reward=""
          [ -f "${t}verifier/reward.txt" ] && reward="$(tr -d '\n' < "${t}verifier/reward.txt" 2>/dev/null)"
          echo "    [hb ${now}] ${name}: finished  reward=${reward:-<none>}"
        else
          echo "    [hb ${now}] ${name}: started"
        fi
        seen[$name]="$state"
      fi
    done
    if [ $((tick % summary_every)) -eq 0 ]; then
      echo "    [hb ${now}] progress: ${done_n}/${EXPECTED_TRIALS} done, ${run_n} running, elapsed $((tick * interval / 60))m"
    fi
  done
}
