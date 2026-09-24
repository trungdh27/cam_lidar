# Wi-Fi AUTO tester workflow

The existing catalogs, collectors, source criteria, evidence files, shared Dashboard SSH and history remain in use.

## Setup and start

Pre-Test contains one Environment Readiness table. Its detected values come from the runtime discovery snapshot. Missing remote control is shown as the actionable root blocker; dependent unknown values remain inspectable under Show technical dependencies. Unresolved Setup displays only missing inputs used by the selected case (or relevant catalog when no case is selected). Discoverable interfaces, addresses and tools have no editable form. Optional/project limits remain under Advanced / Optional thresholds.

AUTO Detail shows critical checks and readiness, with source material available under Source Details. Resolve Setup refreshes discovery and offers Dashboard, saved-profile, physical-position and contextual setup actions. Production refreshes on entry to Pre-Test/AUTO, before preflight, and after execution/restoration.

A blocked preflight creates no attempt or evidence attempt directory and leaves the tester in Detail/Setup Required. A real attempt is allocated only after preflight passes. Setup readiness (`READY`, `READY_WITH_RECONFIG`, `READY_WITH_RECONNECT`, `BLOCKED`) is separate from execution and final result. Legacy blocked archives stay in history but open setup rather than a pretend execution.

Temporary automatic band configuration is supported for AP runtime inspection (C001) with explicit disruptive authorization, an AP profile, target-band capability and verified non-Wi-Fi management. The runner clones the profile, applies source/project settings to the clone, restores the original and deletes the clone. Traffic/recovery tests retain their existing association and bidirectional Wi-Fi-route gates. Explicit source-required channels 6/36 remain acceptance requirements.

## Execution and stop

The execution header shows start time, elapsed time, active phase, actual measurement-step progress and STOP. Endurance shows its real scheduled duration; tests without measurement counts show a phase instead of an invented percentage. TCP directions use the existing command plan's real run count (currently one per direction); no extra runs or thresholds are introduced.

Live measurements precede compact critical checks. A vertical splitter gives approximately 45% to measurements/checks and 55% to resizable diagnostics; adjustments persist within the runtime session. AUTO diagnostics contain Measurements and Technical Log only. Events hide command dumps and show important phase/measurement/warning/error information. Full Output preserves grouped command evidence and existing secret-safe display/search behavior. AUTO has no import controls.

STOP cancels measurement work, terminates owned local process groups and waits for existing shielded restoration/cleanup. Iperf listener startup is shielded until ownership identity is captured, so cancellation can clean the app-owned PID even during startup. Existing listeners are never killed. Client recovery restores saved profiles and removes isolated negative-authentication profiles. Stopped STA association/authentication restores the previous DUT profile. Collected raw output, including partial local command output, and structured evidence are retained.

A successful safe stop records execution/result `STOPPED`, not acceptance FAIL. A restoration/cleanup failure records ERROR and guidance in Technical Log. Pending live measurements stay RUNNING rather than showing premature missing-collector errors. Completion records execution `COMPLETED` with a separate PASS/FAIL/NEEDS REVIEW result, freezes duration and offers Run Again/Open Test Folder. Result override remains available as a compact optional review control.

Back preserves subgroup, filters, scroll and checkbox selection, and leaves the runner active. The list banner returns to the owning execution view, even from a different subgroup.

## Validation

Coverage includes preflight directory isolation, source retention, contextual setup/actions, no AUTO documentation/evidence/import tabs, live phase/time/layout, safe listener/client/STA restoration, stopped evidence, completion, and navigation continuity. Visual QA uses Qt offscreen screenshots with simulated discovery/measurements. No physical DUT runs are claimed by those smoke checks.
