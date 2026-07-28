# Global best-effort Run Notifications via ntfy

Run completion should be pushable to the operator's phone without coupling delivery to Run success. v1 uses one machine-global ntfy config (base URL, topic, optional token) and binary triggers: Success means status `succeeded`; Fail means any other terminal status. Publish is best-effort after the Run is already terminal—failures are logged only, with no retries or effect on Run status. Per-task overrides, source filters, and alternate backends are deferred.
