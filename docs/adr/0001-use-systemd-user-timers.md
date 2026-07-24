# Use systemd user timers as the scheduling backend

pi-task delegates durable local scheduling and process supervision to systemd user timers instead of operating an application daemon or workflow platform. This keeps the product small and uses the host's existing reliability, logging, wake-up, and lingering behavior, while accepting that systemd may suppress an overlapping activation without producing a run record.

## Considered Options

- systemd user timers with a thin task-oriented CLI and wrapper
- an APScheduler daemon backed by SQLite
- a self-hosted workflow platform such as Windmill
