# Sequential Wi-Fi AUTO batches

Run All Ready and Start Auto Suite use the complete catalog of the current
AP/STA and 2.4/5 GHz subgroup, regardless of table filters, selected checkboxes,
or previous results. Run Selected uses explicitly selected catalog entries.
The runtime reserves Wi-Fi execution throughout discovery, preparation, tests,
and final restoration; opening another subgroup cannot start a concurrent TC.

## Queue and results

Initial environment discovery precedes per-test readiness evaluation. Only
READY and READY_WITH_RECONFIG enter an immutable queue. Other entries are
recorded as SKIPPED with their prerequisite reason, without an attempt folder
or Execution View. Before every queued TC, the runtime refreshes discovery and
rechecks readiness; a newly blocked TC is skipped, not failed. Initially
blocked entries never enter the queue after preparation changes the environment.
Preflight evaluation errors are isolated to the affected TC. An unavailable
original-profile snapshot blocks only disruptive tests needing that snapshot;
it does not globally gate unrelated read-only cases.

Each collector has new buffers, counters and timestamps. Completion signals
advance the queue only after collector cleanup. PASS, FAIL, ERROR, MEASUREMENT
ERROR and qualitative NEEDS REVIEW do not invalidate unrelated cases. Skipped
preflight entries do not overwrite a previously completed test result.

Batch evidence is `batch_<id>/batch.json`, separate from TC attempt evidence.
It records membership, skips/reasons, actual attempt paths, counters, and the
original non-secret network snapshot. Completion is a summary (COMPLETED,
STOPPED, ABORTED or an infrastructure ERROR), never an aggregate DUT PASS/FAIL.
The persistent batch banner shows current TC/phase, both elapsed clocks, X/N,
result counts and remaining tests. Hover it for per-entry reasons.

## Preparation, recovery, cleanup and restoration

The original DUT/client NetworkManager connection UUIDs, interfaces, association,
SSID/BSSID, band/mode, IP/route and NetworkManager state are captured before
preparation. If safe, an eligible AP reconfiguration case can prepare one
app-owned subgroup profile clone; compatible eligible cases reuse it. Original
profiles are not modified. Each production collector verifies mode/band and
association, plus required Wi-Fi routes, immediately before measurement.

Disruptive cases retain existing per-test alternate-control/reconnect prerequisites.
When verified alternate control is needed, the existing shared SSH manager
uses it until batch network restoration finishes, then reconnects to its original
endpoint. No additional persistent manager is created; Dashboard/core classes
are unchanged.

Transport loss enters RECOVERING and attempts bounded reconnection using that
same manager/authentication. Successful recovery retries only a read-only command
and continues the existing collector/attempt. Known read-only RF loops and
idempotent, PID/start/ownership-checked iperf cleanup can also safely retry.
An uncertain configuration command is
not replayed and produces infrastructure ERROR, not DUT FAIL. Unrecoverable
control loss aborts the remaining queue, recording SKIPPED rather than failures.

Stop Auto Suite requests cancellation of the current measurement, waits for
collector-owned cleanup, restores original profiles, deletes only the batch-owned
clone and cancels remaining entries. Restoration verifies connection UUIDs and
reports errors without changing completed DUT results. Existing listener cleanup
checks PID/start identity and ownership; unrelated user processes are never killed.

## Validation

Focused coverage is in `tests/test_wifi_batch.py`, including real-collector stop,
owned iperf cleanup, original-state restoration, same-attempt recovery, uncertain
command handling, scope independent of filters and stable criteria during timer
updates. Full pytest and source verification are required alongside these tests.
Hardware batch qualification requires the running application's shared Dashboard
connection; controlled mocks/offscreen UI tests are not live DUT evidence.
