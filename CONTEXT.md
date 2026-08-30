# Pi Task Scheduling

Pi Task Scheduling describes local, recurring Pi agent work and the records produced when that work is invoked.

## Language

**Task**:
A named configuration that combines one prompt, working directory, model, timeout, and recurring schedule.
_Avoid_: Job, routine

**Schedule**:
The calendar expression or elapsed interval that determines when a task is due.
_Avoid_: Period, cadence

**Run**:
One isolated invocation of a task, initiated either by its schedule or manually.
_Avoid_: Execution, job

**Session**:
The persistent Pi conversation created for exactly one run and available for interactive continuation after that run finishes.
_Avoid_: Run history, transcript

**Result**:
An optional agent-reported assessment of whether the Task's requested work succeeded, partially completed, was blocked, failed, or could not be assessed. A Result does not replace the Run's operational status.
_Avoid_: Run status, verified outcome

**Pause**:
A task state that suppresses future scheduled runs without cancelling an active run and without replaying occurrences missed while paused.
_Avoid_: Disable, stop

**Notification**:
A push alert about one terminal Run, sent only when that Run's status and optional Result match the operator's configured triggers.
_Avoid_: Alert, webhook, message
