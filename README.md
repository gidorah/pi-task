# pi-task

Schedule local [Pi](https://pi.dev) agent prompts with systemd user timers.

> **Status:** Early development. The CLI is not yet usable.

## Goals

- Calendar and interval schedules
- Per-task model and thinking-level selection
- Persistent, resumable Pi sessions
- Pause, resume, and independent manual runs
- Local execution through systemd user services
- Task and run management through a friendly CLI

## Platform

pi-task targets Linux systems with systemd and requires Python 3.14, Pi, and a systemd user manager.

## License

[MIT](LICENSE)
