# Create one persistent Pi session per run

Every scheduled or manual run creates a fresh, named, persistent Pi session instead of continuing a task-owned session. Isolation prevents concurrent writers and unbounded shared context while preserving exact history; continuation is a deliberate interactive action after the run has finished.
