#!/bin/bash

set -eu

log_level_arg=()

if [[ -n "${EKM_LOG_LEVEL:-}" ]]; then
  log_level_args=(--log-level "$EKM_LOG_LEVEL")
fi

shutdown() {
  kill -TERM "$watcher_pid" "$aggregator_pid" 2>/dev/null || true
}

run_aggregate_loop() {
  local interval=$((60 * 60 * 24))
  local started_at=$(date +%s)

  while true; do
    sleep $((interval - ($(date +%s) - started_at) % interval))
    ekm-meter-watcher --aggregate "${log_level_args[@]}" || true
  done
}

ekm-meter-watcher "${log_level_args[@]}" &
watcher_pid=$!

run_aggregate_loop &
aggregator_pid=$!

trap shutdown INT TERM

set +e
wait -n "$watcher_pid" "$aggregator_pid"
status=$?
shutdown
wait "$watcher_pid" "$aggregator_pid" 2>/dev/null
exit "$status"
