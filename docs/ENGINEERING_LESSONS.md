# Engineering Lessons

**This document is curated engineering guidance, not automatically verified
knowledge from every task.** It is written and edited by people reviewing what
the tests and the failure reports actually showed. Nothing here is generated
from a task's experience row, nothing here is injected into a planner prompt,
and a failed or partial experience can never promote itself into a lesson.

The distinction matters because the codebase records two different kinds of
memory:

| Record | What it is | May guide a plan? |
| --- | --- | --- |
| `learning.db` experience row | A log of one task: actions, verdict, outcome, grade evidence, partial-progress evidence | No |
| `lessons.json` lesson | A short statement a named person signed off, with `verified_by` | Yes, when its tags match |

Promotion from the first to the second is a human act. `lessons.json` entries
require `verified: true` and a non-empty `verified_by`; the loader enforces the
same `MAX_LESSONS = 3`, `MAX_TOTAL_CHARS = 300` and `MAX_TEXT_CHARS = 200`
limits regardless of who wrote the file.

Labels used below:

- **[Verified]** behaviour a test in this repository proves.
- **[Observed]** a real failure seen while running the code.
- **[Implemented]** the correction that shipped, with where it lives.
- **[Limitation]** what the correction does *not* cover.

## 1. A returned result is not a verdict

**[Observed]** A coding loop returned success and the project stayed in
`BUILDING` indefinitely: the completion handler deliberately skipped
verification and only wrote a message. A second path recorded an experience as
`success` as soon as the loop stopped, before the completion gate judged it, so
a run that wrote nothing was later retrieved as a positive lesson.

**[Implemented]** `giscode/projects/build_verification.py` re-runs the project's
own suite through the existing verifier and records one explicit outcome
(`VERIFIED`, `TESTS_FAILED`, `NO_TESTS`, `COMPLETED_NO_CHANGES`, `UNAVAILABLE`,
`TIMEOUT`, `CANCELLED`, `ERROR`). `COMPLETE` is reachable only from a real pass.
`ExperienceRecorder._reconcile_outcome` downgrades an optimistic outcome to
`failed` once the final verdict is known.

**[Verified]** `tests/test_build_verification.py`,
`tests/test_project_build_lifecycle.py`,
`tests/test_experience_outcome_integrity.py`.

**[Limitation]** A project with no tests can never reach `COMPLETE`; it ends
`FAILED` and is retryable. That is deliberate, but it means "no tests" and
"tests failed" look the same to anyone reading only the status.

## 2. Terminal means terminal, retryable means explicit

**[Observed]** `FAILED` was listed in `TERMINAL_STATES` with no exits, and
`APPROVED` had no path back to `REVIEW`. A failed build could not be retried,
and a build that never started left its project stuck in an approval state.

**[Implemented]** Terminal states are `{COMPLETE, CANCELLED}`.
`RETRYABLE_STATES = {FAILED, APPROVED}` exit only to `REVIEW`, through
`ProjectStore.retry()`, and relaunching goes back through the normal approval
path. Retry is explicit; nothing retries itself.

**[Verified]** `tests/test_project_build_lifecycle.py`,
`tests/test_project_store.py`.

**[Limitation]** `Project.is_terminal` no longer includes `FAILED`. Any external
consumer that relied on that meaning must be updated.

## 3. Evidence is written before the undo

**[Observed]** Restoring a file to its pre-edit content removes the version the
task produced. On a failed suite that version is not automatically worthless -
one edit of several may be the right one - and it was being destroyed.

**[Implemented]** `giscode/gisai/recovery_store.py` writes the produced bytes and
a unified diff *before* `safe_rollback` removes them; `partial_replay.py` offers
those edits back one at a time, bounded by `MAX_REPLAY_EDITS = 3` and
`MAX_REPLAY_VERIFICATION_RUNS = 2`, inside the task's existing verification
budget. An edit is kept only on a strict improvement with no new failure.

**[Verified]** `tests/test_gisai_safe_rollback.py`,
`tests/test_verification_identity_evidence.py`,
`tests/test_quarantine_recovery.py`.

**[Limitation]** Replay compares failing-test *identities*. A verdict whose
failing tests cannot be identified rejects every edit rather than guessing.

## 4. A passing replay is not a successful task

**[Observed]** Replay can end with the suite passing after re-applying one edit,
which is exactly the moment a report is tempted to call the task fixed.

**[Implemented]** The task stays `FAILED`. `ReplayResult.verified` is named for
what it is, and its report line says the change is not recorded as verified.
Stage 4 stores that fact as `partial_evidence` and withholds the grade.

**[Verified]** `tests/test_partial_evidence_learning.py`.

**[Limitation]** The evidence records what replay *observed*; it does not re-run
the suite to confirm the final tree after the user's later edits.

## 5. Grade A must be unearnable by partial work

**[Observed]** Grading keys off structured facts in `verification`. Any new
"progress" field is a new way to accidentally satisfy a check.

**[Implemented]** `grade_experience` gained a single negative check,
`not_partial_progress`; `ExperienceRecorder.record_final_grade` does not call
`set_grade` at all for a record carrying `partial_evidence`, so `outcome_grade`
stays `NULL` instead of holding a best-effort grade. A successful replay of one
edit is never read as the task's verdict.

**[Verified]** `tests/test_graded_learning.py`,
`tests/test_partial_evidence_learning.py`.

**[Limitation]** Grade B remains reserved and unimplemented: it needs the first
verification attempt of the repair loop, which is still not kept.

## 6. Reference artifacts, never copy them

**[Observed]** An experience row outlives the run. Copying source into it would
persist whatever the source contained - including secrets - and would grow
without bound.

**[Implemented]** `partial_evidence` stores paths (`recovery_patch_dir`,
`edits_kept`, `edits_discarded`), bounded lists (`MAX_LIST_ITEMS = 20`) and
redacted reasons; the bytes stay in the recovery directory. `learning.db` gains
one additive nullable column, `partial_evidence`, through the same
`PRAGMA table_info` + `ALTER TABLE ADD COLUMN` pattern as the grade columns.
Old rows are not rewritten, backfilled or deleted.

**[Verified]** `tests/test_partial_evidence_learning.py`,
`tests/test_learning_store_migration.py`.

**[Limitation]** A recovery directory the user later deletes leaves a row
pointing at a path that no longer exists. The row is still honest: it says where
the evidence *was*, not that it is still there.

## 7. Serialize the shutdown against the worker

**[Observed]** A Windows access violation (`0xC0000005`) inside
`ProjectStore.get_project` while a launcher worker was still running: the
fixture closed the SQLite connection underneath it.

**[Implemented]** `ProjectStore.close()` takes the same lock its queries use, and
`build_in_flight()` stays true through the completion callback, so a caller
never sees "idle" while the worker is still writing.

**[Verified]** `tests/test_project_build_lifecycle.py` (deterministic
finishing-window tests).

**[Limitation]** This protects one process. Two processes opening the same
profile still rely on SQLite's own locking.

## 8. Report the reason, not a synonym for it

**[Observed]** "It changed" was printed for a missing version record and for a
failed restore, both of which are different facts. A single cause sentence was
false for at least one file in a partial rollback.

**[Implemented]** `describe_conflict` maps each reason to its own sentence, and a
partial rollback names every kept file with its own reason
(`safe_rollback.RollbackOutcome.conflicts`, `real_work.py`).

**[Verified]** `tests/test_rollback_reporting.py`,
`tests/test_gisai_safe_rollback.py`.

**[Limitation]** The wording is only as precise as the reason recorded at the
failure site; an unknown reason still falls back to a generic sentence.
