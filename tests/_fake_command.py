#!/usr/bin/env python3
import json
import os
import signal
import sys
import time
from contextlib import suppress
from pathlib import Path

name = Path(sys.argv[0]).name
with Path(os.environ["FAKE_COMMAND_LOG"]).open("a") as log:
    print(json.dumps([name, *sys.argv[1:]]), file=log)

if name == "pi":
    if "--list-models" in sys.argv:
        print(os.environ.get("FAKE_MODELS", "provider model context\nacme rocket 128K"))
        raise SystemExit(0)
    if "--session" in sys.argv:
        raise SystemExit(0)

    # One-shot JSON mode: create a normal Pi session file and emit lifecycle events.
    cwd = Path.cwd().resolve()
    agent_dir = Path(os.environ["PI_CODING_AGENT_DIR"])
    session_id = os.environ.get("FAKE_SESSION_ID", "019f0000-1111-2222-3333-444455556666")
    timestamp = os.environ.get("FAKE_SESSION_TIMESTAMP", "2030-01-07T09:00:00.000Z")
    safe = "--" + str(cwd).lstrip("/").replace("/", "-").replace("\\", "-").replace(":", "-") + "--"
    session_dir = agent_dir / "sessions" / safe
    session_dir.mkdir(parents=True, exist_ok=True)
    file_ts = timestamp.replace(":", "-").replace(".", "-")
    session_path = session_dir / f"{file_ts}_{session_id}.jsonl"
    header = {
        "type": "session",
        "version": 3,
        "id": session_id,
        "timestamp": timestamp,
        "cwd": str(cwd),
    }
    assistant = {
        "role": "assistant",
        "content": [{"type": "text", "text": "done"}],
        "provider": "acme",
        "model": "rocket",
        "usage": {
            "input": 11,
            "output": 7,
            "cacheRead": 3,
            "cacheWrite": 1,
            "totalTokens": 22,
            "cost": {
                "input": 0.01,
                "output": 0.02,
                "cacheRead": 0.0,
                "cacheWrite": 0.0,
                "total": 0.03,
            },
        },
        "stopReason": os.environ.get("FAKE_STOP_REASON", "stop"),
        "timestamp": 1,
    }
    user_message = {
        "type": "message",
        "message": {
            "role": "user",
            "content": "hi",
            "timestamp": 1,
        },
    }
    partial_only = os.environ.get("FAKE_PI_PARTIAL_SESSION") == "1"
    session_body = json.dumps(header) + "\n" + json.dumps(user_message) + "\n"
    if not partial_only:
        session_body += json.dumps({"type": "message", "message": assistant}) + "\n"
    session_path.write_text(session_body)
    print(json.dumps(header), flush=True)
    print(json.dumps({"type": "agent_start"}), flush=True)
    if noise := os.environ.get("FAKE_PI_STDOUT_NOISE"):
        print(noise, flush=True)
    if not partial_only:
        print(json.dumps({"type": "message_end", "message": assistant}), flush=True)
        print(json.dumps({"type": "agent_end", "messages": [assistant]}), flush=True)
    if os.environ.get("FAKE_PI_STDERR"):
        print(os.environ["FAKE_PI_STDERR"], file=sys.stderr)
    sleep_for = float(os.environ.get("FAKE_PI_SLEEP", "0"))
    if sleep_for > 0:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        time.sleep(sleep_for)
    raise SystemExit(int(os.environ.get("FAKE_PI_EXIT", "0")))

if name == "systemd-analyze":
    print("  Original form: " + sys.argv[-1])
    print("Normalized form: *-*-* 09:00:00")
    print("    Next elapse: Mon 2030-01-07 09:00:00 UTC")
    print("   Iteration #2: Tue 2030-01-08 09:00:00 UTC")
    print("   Iteration #3: Wed 2030-01-09 09:00:00 UTC")
    raise SystemExit(0)
if name == "systemctl":
    if "show-environment" in sys.argv:
        environment = {
            "PATH": os.environ["FAKE_MANAGER_PATH"],
            "FAKE_COMMAND_LOG": os.environ["FAKE_COMMAND_LOG"],
            "FAKE_MODELS": os.environ.get(
                "FAKE_MODELS",
                "provider model context\nacme rocket 128K",
            ),
        }
        if "--output=json" in sys.argv:
            print(json.dumps(environment))
        else:
            for key, value in environment.items():
                print(f"{key}={value}")
        raise SystemExit(0)
    if "is-enabled" in sys.argv:
        print("enabled")
        raise SystemExit(0)
    if "is-active" in sys.argv:
        print("active")
        raise SystemExit(0)
    if "stop" in sys.argv:
        # Tests can point cancel at a live wrapper PID started outside systemd.
        # Wait for exit the way real systemctl stop waits for the unit.
        stop_pid = os.environ.get("FAKE_SYSTEMCTL_STOP_PID")
        if stop_pid:
            try:
                pid = int(stop_pid)
                stop_signal = (
                    signal.SIGKILL
                    if os.environ.get("FAKE_SYSTEMCTL_STOP_SIGNAL") == "KILL"
                    else signal.SIGTERM
                )
                os.kill(pid, stop_signal)
                for _ in range(400):
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.05)
                else:
                    with suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGKILL)
            except ProcessLookupError, ValueError:
                pass
        raise SystemExit(0)
    raise SystemExit(0)
if name == "systemd-run":
    import subprocess

    args = sys.argv[1:]
    exit_code = int(os.environ.get("FAKE_SYSTEMD_RUN_EXIT", "0"))
    if exit_code:
        if error := os.environ.get("FAKE_SYSTEMD_RUN_STDERR"):
            print(error, file=sys.stderr)
        raise SystemExit(exit_code)
    wait = "--wait" in args
    command: list[str] = []
    for index, arg in enumerate(args):
        if arg == "--":
            command = args[index + 1 :]
            break
        if not arg.startswith("-"):
            command = args[index:]
            break
    if wait and command:
        raise SystemExit(subprocess.call(command))
    raise SystemExit(0)
if name == "journalctl":
    # Record the exact query so tests can assert invocation-scoped selection.
    empty = os.environ.get("FAKE_JOURNAL_EMPTY") == "1"
    exit_code = int(os.environ.get("FAKE_JOURNAL_EXIT", "0"))
    output = os.environ.get("FAKE_JOURNAL_OUTPUT", "")
    if empty:
        raise SystemExit(0)
    if output:
        print(output, end="" if output.endswith("\n") else "\n")
    raise SystemExit(exit_code)
raise SystemExit(64)
