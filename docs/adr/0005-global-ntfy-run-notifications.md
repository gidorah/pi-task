# Global best-effort Run Notifications via ntfy

Run completion should be pushable to the operator's phone without coupling delivery to Run status. One machine-global ntfy config stores the base URL, topic, optional token, and `success` / `attention` triggers. Publish is best-effort after the Run is terminal. Delivery failures are logged without retries and never change Run status.

Tasks may opt into structured Result reporting. pi-task appends a short contract to the resolved prompt and stores the agent-reported `outcome` and `summary`. The allowed Results are `succeeded`, `partial`, `blocked`, `failed`, and `unknown`. Missing or malformed reports become `unknown`. This is cooperative agent self-reporting, not verification.

Operational status takes precedence in Notification titles. An operationally successful Run uses its Result title when reporting is enabled, or `Completed` when reporting is disabled. `success` matches clean completion without reporting and a reported `succeeded` Result. `attention` matches every operational non-success plus `partial`, `blocked`, `failed`, and `unknown` Results. The CLI accepts `fail` as a compatibility alias for `attention`.

Semantic Results do not change Run status, wrapper exit code, or systemd service health. Per-task Notification overrides, source filters, independent evaluators, and alternate backends remain deferred.
