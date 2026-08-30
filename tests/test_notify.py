from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import TYPE_CHECKING, Any

import pytest
from harness import add_task, clear_commands

from pi_task.db import RunRecord, RunSource, RunStatus
from pi_task.notify import (
    NotifyConfig,
    build_notification,
    load_notify_config,
    maybe_notify_run,
    parse_trigger_choice,
    save_notify_config,
    should_notify,
)

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable, Iterator


def test_should_notify_success_only_matches_succeeded() -> None:
    config = NotifyConfig(
        base_url="https://ntfy.example",
        topic="pi-task",
        token=None,
        on_success=True,
        on_attention=False,
    )
    assert should_notify(config, "succeeded") is True
    assert should_notify(config, "failed") is False
    assert should_notify(config, "timed_out") is False
    assert should_notify(config, "cancelled") is False
    assert should_notify(config, "skipped") is False
    assert should_notify(config, "running") is False


def test_should_notify_attention_matches_all_non_success_terminals() -> None:
    config = NotifyConfig(
        base_url="https://ntfy.example",
        topic="pi-task",
        token=None,
        on_success=False,
        on_attention=True,
    )
    assert should_notify(config, "succeeded") is False
    assert should_notify(config, "failed") is True
    assert should_notify(config, "timed_out") is True
    assert should_notify(config, "cancelled") is True
    assert should_notify(config, "skipped") is True
    assert should_notify(config, "running") is False


def test_fail_remains_an_alias_for_attention() -> None:
    assert parse_trigger_choice("attention") == (False, True)
    assert parse_trigger_choice("fail") == (False, True)


def test_build_notification_success_uses_task_name_and_check_tag() -> None:
    payload = build_notification(
        task_name="Daily review",
        task_id="daily-review",
        status="succeeded",
        source="scheduled",
        duration_ms=12_400,
        error=None,
    )
    assert payload.title == "Daily review: Completed"
    assert payload.tags == ("white_check_mark",)
    assert "Status: succeeded" in payload.body
    assert "Source: scheduled" in payload.body
    assert "Duration: 12.4s" in payload.body
    assert "Error:" not in payload.body


def test_build_notification_timeout_falls_back_to_task_id_and_includes_error() -> None:
    payload = build_notification(
        task_name=None,
        task_id="backup-photos",
        status="timed_out",
        source="manual",
        duration_ms=600_000,
        error="timed out after 600 seconds",
    )
    assert payload.title == "backup-photos: Timed out"
    assert payload.tags == ("x",)
    assert "Status: timed_out" in payload.body
    assert "Source: manual" in payload.body
    assert "Duration: 600.0s" in payload.body
    assert "Error: timed out after 600 seconds" in payload.body


def test_save_and_load_notify_config_round_trip(
    run_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", run_env["XDG_CONFIG_HOME"])
    config = NotifyConfig(
        base_url="https://ntfy.example",
        topic="homelab",
        token="secret-token",
        on_success=True,
        on_attention=True,
    )
    save_notify_config(config)
    loaded = load_notify_config()
    assert loaded == config
    saved = (Path(run_env["XDG_CONFIG_HOME"]) / "pi-task/notify.toml").read_text()
    assert "on_attention = true" in saved
    assert "on_fail" not in saved


def test_load_notify_config_missing_returns_none(
    run_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", run_env["XDG_CONFIG_HOME"])
    assert load_notify_config() is None


def test_load_notify_config_accepts_legacy_on_fail(
    run_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", run_env["XDG_CONFIG_HOME"])
    path = Path(run_env["XDG_CONFIG_HOME"]) / "pi-task/notify.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        'version = 1\nbase_url = "https://ntfy.example"\ntopic = "pi-task"\n'
        "on_success = false\non_fail = true\n"
    )

    loaded = load_notify_config()
    assert loaded is not None
    assert loaded.on_attention is True


class _NtfyHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]]

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode()
        type(self).requests.append(
            {
                "path": self.path,
                "headers": {key: value for key, value in self.headers.items()},
                "body": body,
            }
        )
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"id":"ok"}')


@pytest.fixture
def ntfy_server() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    _NtfyHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NtfyHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}", _NtfyHandler.requests
    finally:
        server.shutdown()
        thread.join(timeout=2)


def _terminal_record(
    *,
    status: RunStatus = "succeeded",
    source: RunSource = "scheduled",
    name: str | None = "Daily review",
    error: str | None = None,
    duration_ms: int = 1500,
    result_outcome: str | None = None,
    result_summary: str | None = None,
    result_reporting: bool = False,
) -> RunRecord:
    snapshot = {
        "task_id": "daily-review",
        "name": name,
        "working_directory": "/tmp/project",
        "prompt_kind": "inline",
        "prompt": "hi",
        "schedule_kind": "calendar",
        "schedule": "daily",
        "catch_up": True,
        "model": "acme/rocket",
        "thinking": "high",
        "timeout_seconds": 1200,
        "trust": "deny",
        "paused": False,
        "result_reporting": result_reporting,
    }
    return RunRecord(
        id="run-1",
        task_id="daily-review",
        source=source,
        status=status,
        started_at="2026-01-01T00:00:00.000Z",
        finished_at="2026-01-01T00:00:01.500Z",
        duration_ms=duration_ms,
        session_id=None,
        session_path=None,
        session_name="pi-task:daily-review:run-1",
        prompt_hash="abc",
        snapshot_json=json.dumps(snapshot),
        model="acme/rocket",
        thinking="high",
        input_tokens=None,
        output_tokens=None,
        cache_read_tokens=None,
        cache_write_tokens=None,
        cost_total=None,
        error=error,
        result_outcome=result_outcome,
        result_summary=result_summary,
    )


@pytest.mark.parametrize(
    ("outcome", "title"),
    [
        ("succeeded", "Daily review: Succeeded"),
        ("partial", "Daily review: Partially completed"),
        ("blocked", "Daily review: Blocked"),
        ("failed", "Daily review: Failed"),
        ("unknown", "Daily review: Result unknown"),
    ],
)
def test_structured_result_controls_notification_title_and_summary(
    outcome: str,
    title: str,
) -> None:
    record = _terminal_record(
        result_reporting=True,
        result_outcome=outcome,
        result_summary="Concrete result details.",
    )
    from pi_task.notify import notification_payload_for_run

    payload = notification_payload_for_run(record)
    assert payload.title == title
    assert "Result: Concrete result details." in payload.body


@pytest.mark.parametrize(
    ("status", "title"),
    [
        ("failed", "Daily review: Failed"),
        ("timed_out", "Daily review: Timed out"),
        ("cancelled", "Daily review: Cancelled"),
        ("skipped", "Daily review: Skipped"),
    ],
)
def test_operational_status_takes_precedence_over_structured_result(
    status: RunStatus,
    title: str,
) -> None:
    from pi_task.notify import notification_payload_for_run

    record = _terminal_record(
        status=status,
        result_reporting=True,
        result_outcome="succeeded",
        result_summary="Claimed success.",
    )
    assert notification_payload_for_run(record).title == title


@pytest.mark.parametrize("outcome", ["partial", "blocked", "failed", "unknown"])
def test_attention_trigger_matches_non_success_structured_results(outcome: str) -> None:
    config = NotifyConfig(
        base_url="https://ntfy.example",
        topic="pi-task",
        token=None,
        on_success=False,
        on_attention=True,
    )
    assert should_notify(config, "succeeded", result_outcome=outcome) is True
    assert should_notify(config, "succeeded", result_outcome="succeeded") is False


def test_maybe_notify_run_posts_when_trigger_matches(
    run_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    ntfy_server: tuple[str, list[dict[str, Any]]],
) -> None:
    base_url, requests = ntfy_server
    monkeypatch.setenv("XDG_CONFIG_HOME", run_env["XDG_CONFIG_HOME"])
    save_notify_config(
        NotifyConfig(
            base_url=base_url,
            topic="pi-task",
            token="tok-123",
            on_success=True,
            on_attention=False,
        )
    )
    maybe_notify_run(_terminal_record(status="succeeded"))
    assert len(requests) == 1
    posted = requests[0]
    assert posted["path"] == "/pi-task"
    assert posted["headers"].get("Title") == "Daily review: Completed"
    assert posted["headers"].get("Tags") == "white_check_mark"
    assert posted["headers"].get("Authorization") == "Bearer tok-123"
    assert "Status: succeeded" in posted["body"]


def test_maybe_notify_run_skips_when_trigger_disabled(
    run_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    ntfy_server: tuple[str, list[dict[str, Any]]],
) -> None:
    base_url, requests = ntfy_server
    monkeypatch.setenv("XDG_CONFIG_HOME", run_env["XDG_CONFIG_HOME"])
    save_notify_config(
        NotifyConfig(
            base_url=base_url,
            topic="pi-task",
            token=None,
            on_success=False,
            on_attention=True,
        )
    )
    maybe_notify_run(_terminal_record(status="succeeded"))
    assert requests == []


def test_maybe_notify_run_swallows_publish_errors(
    run_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", run_env["XDG_CONFIG_HOME"])
    save_notify_config(
        NotifyConfig(
            base_url="http://127.0.0.1:1",
            topic="pi-task",
            token=None,
            on_success=True,
            on_attention=True,
        )
    )
    maybe_notify_run(_terminal_record(status="succeeded"))
    err = capsys.readouterr().err
    assert "notification" in err.lower()


def test_notify_cli_show_redacts_token(
    run_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", run_env["XDG_CONFIG_HOME"])
    # Ensure child process sees the same config home.
    run_env = {**run_env, "XDG_CONFIG_HOME": run_env["XDG_CONFIG_HOME"]}
    save_notify_config(
        NotifyConfig(
            base_url="https://ntfy.example",
            topic="homelab",
            token="super-secret",
            on_success=False,
            on_attention=True,
        )
    )
    shown = run_cli("notify", "--show", env=run_env)
    assert shown.returncode == 0, shown.stdout + shown.stderr
    assert "https://ntfy.example" in shown.stdout
    assert "homelab" in shown.stdout
    assert "attention" in shown.stdout.lower()
    assert "super-secret" not in shown.stdout
    assert "redacted" in shown.stdout.lower()


def test_notify_cli_noninteractive_saves_without_test(
    run_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", run_env["XDG_CONFIG_HOME"])
    result = run_cli(
        "notify",
        "--url",
        "https://ntfy.example",
        "--topic",
        "pi-task",
        "--token",
        "abc",
        "--on",
        "both",
        "--no-test",
        env=run_env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    loaded = load_notify_config()
    assert loaded is not None
    assert loaded.base_url == "https://ntfy.example"
    assert loaded.topic == "pi-task"
    assert loaded.token == "abc"
    assert loaded.on_success is True
    assert loaded.on_attention is True


def test_scheduled_run_sends_notification_when_configured(
    run_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
    ntfy_server: tuple[str, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url, requests = ntfy_server
    monkeypatch.setenv("XDG_CONFIG_HOME", run_env["XDG_CONFIG_HOME"])
    add_task(run_env, "daily-review", cli=run_cli)
    save_notify_config(
        NotifyConfig(
            base_url=base_url,
            topic="pi-task",
            token=None,
            on_success=True,
            on_attention=True,
        )
    )
    clear_commands(run_env)
    executed = run_cli("_run-scheduled", "daily-review", env=run_env)
    assert executed.returncode == 0, executed.stdout + executed.stderr
    assert len(requests) == 1
    assert requests[0]["headers"].get("Title") == "Daily review: Completed"
    assert "Status: succeeded" in requests[0]["body"]


def test_scheduled_partial_result_sends_attention_notification(
    run_env: dict[str, str],
    run_cli: Callable[..., subprocess.CompletedProcess[str]],
    ntfy_server: tuple[str, list[dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_url, requests = ntfy_server
    monkeypatch.setenv("XDG_CONFIG_HOME", run_env["XDG_CONFIG_HOME"])
    add_task(run_env, "daily-review", result_reporting=True, cli=run_cli)
    save_notify_config(
        NotifyConfig(
            base_url=base_url,
            topic="pi-task",
            token=None,
            on_success=False,
            on_attention=True,
        )
    )
    result_text = (
        "Some work remains.\n<pi-task-result>\n"
        '{"outcome":"partial","summary":"Updated config; restart remains."}\n'
        "</pi-task-result>"
    )
    executed = run_cli(
        "_run-scheduled",
        "daily-review",
        env={**run_env, "FAKE_ASSISTANT_TEXT": result_text},
    )

    assert executed.returncode == 0, executed.stdout + executed.stderr
    assert len(requests) == 1
    assert requests[0]["headers"].get("Title") == "Daily review: Partially completed"
    assert "Result: Updated config; restart remains." in requests[0]["body"]
