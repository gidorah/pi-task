from __future__ import annotations

import json
import os
import sys
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlparse, urlunparse

from pi_task.db import RunRecord, RunSource, RunStatus

TriggerChoice = Literal["success", "fail", "both", "none"]


class NotifyError(Exception):
    pass


@dataclass(frozen=True)
class NotifyConfig:
    base_url: str
    topic: str
    token: str | None
    on_success: bool
    on_fail: bool


@dataclass(frozen=True)
class NotificationPayload:
    title: str
    body: str
    tags: tuple[str, ...]


def _config_home() -> Path:
    value = os.environ.get("XDG_CONFIG_HOME")
    return Path(value).expanduser() if value else Path.home() / ".config"


def notify_config_path() -> Path:
    return _config_home() / "pi-task" / "notify.toml"


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def normalize_base_url(url: str) -> str:
    cleaned = url.strip().rstrip("/")
    if not cleaned:
        raise NotifyError("base URL must not be empty")
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise NotifyError("base URL must be an absolute http(s) URL")
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", ""))


def normalize_topic(topic: str) -> str:
    cleaned = topic.strip().strip("/")
    if not cleaned:
        raise NotifyError("topic must not be empty")
    if any(ch.isspace() for ch in cleaned):
        raise NotifyError("topic must not contain whitespace")
    return cleaned


def parse_trigger_choice(value: str) -> tuple[bool, bool]:
    choice = value.strip().lower()
    if choice == "success":
        return True, False
    if choice == "fail":
        return False, True
    if choice == "both":
        return True, True
    if choice in {"none", "off", "neither"}:
        return False, False
    raise NotifyError("triggers must be one of: success, fail, both, none")


def trigger_choice(config: NotifyConfig) -> TriggerChoice:
    if config.on_success and config.on_fail:
        return "both"
    if config.on_success:
        return "success"
    if config.on_fail:
        return "fail"
    return "none"


def validate_config(config: NotifyConfig) -> NotifyConfig:
    return NotifyConfig(
        base_url=normalize_base_url(config.base_url),
        topic=normalize_topic(config.topic),
        token=(config.token.strip() or None) if config.token is not None else None,
        on_success=config.on_success,
        on_fail=config.on_fail,
    )


def serialize_notify_config(config: NotifyConfig) -> str:
    validated = validate_config(config)
    lines = [
        "version = 1",
        f"base_url = {_toml_string(validated.base_url)}",
        f"topic = {_toml_string(validated.topic)}",
    ]
    if validated.token is not None:
        lines.append(f"token = {_toml_string(validated.token)}")
    lines.extend(
        [
            f"on_success = {'true' if validated.on_success else 'false'}",
            f"on_fail = {'true' if validated.on_fail else 'false'}",
            "",
        ]
    )
    return "\n".join(lines)


def save_notify_config(config: NotifyConfig) -> NotifyConfig:
    validated = validate_config(config)
    path = notify_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialize_notify_config(validated), encoding="utf-8")
    return validated


def load_notify_config() -> NotifyConfig | None:
    path = notify_config_path()
    if not path.is_file():
        return None
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
        token = data.get("token")
        if token is not None and not isinstance(token, str):
            raise NotifyError("token must be a string")
        on_success = data.get("on_success", False)
        on_fail = data.get("on_fail", False)
        if not isinstance(on_success, bool) or not isinstance(on_fail, bool):
            raise NotifyError("on_success and on_fail must be booleans")
        return validate_config(
            NotifyConfig(
                base_url=str(data["base_url"]),
                topic=str(data["topic"]),
                token=token,
                on_success=on_success,
                on_fail=on_fail,
            )
        )
    except NotifyError:
        raise
    except (KeyError, OSError, TypeError, ValueError, tomllib.TOMLDecodeError) as error:
        raise NotifyError(f"could not read notify config: {error}") from error


def format_config_for_display(config: NotifyConfig) -> str:
    token_display = "redacted" if config.token else "(not set)"
    lines = [
        f"Base URL: {config.base_url}",
        f"Topic: {config.topic}",
        f"Token: {token_display}",
        f"Triggers: {trigger_choice(config)}",
        f"  on_success: {config.on_success}",
        f"  on_fail: {config.on_fail}",
    ]
    return "\n".join(lines)


def should_notify(config: NotifyConfig, status: RunStatus) -> bool:
    if status == "running":
        return False
    if status == "succeeded":
        return config.on_success
    return config.on_fail


def build_notification(
    *,
    task_name: str | None,
    task_id: str,
    status: RunStatus,
    source: RunSource,
    duration_ms: int | None,
    error: str | None,
) -> NotificationPayload:
    label = (task_name or "").strip() or task_id
    succeeded = status == "succeeded"
    title = f"{label}: {'Succeeded' if succeeded else 'Failed'}"
    duration = "unknown" if duration_ms is None else f"{duration_ms / 1000:.1f}s"
    lines = [
        f"Status: {status}",
        f"Source: {source}",
        f"Duration: {duration}",
    ]
    if error:
        lines.append(f"Error: {error}")
    return NotificationPayload(
        title=title,
        body="\n".join(lines),
        tags=("white_check_mark",) if succeeded else ("x",),
    )


def publish_notification(config: NotifyConfig, payload: NotificationPayload) -> None:
    validated = validate_config(config)
    url = f"{validated.base_url}/{quote(validated.topic, safe='')}"
    headers = {
        "Title": payload.title,
        "Tags": ",".join(payload.tags),
        "Content-Type": "text/plain; charset=utf-8",
    }
    if validated.token:
        headers["Authorization"] = f"Bearer {validated.token}"
    request = urllib.request.Request(
        url,
        data=payload.body.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        detail = body.strip() or error.reason
        raise NotifyError(f"ntfy publish failed: HTTP {error.code} {detail}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise NotifyError(f"ntfy publish failed: {error}") from error


def notification_payload_for_run(record: RunRecord) -> NotificationPayload:
    task_name: str | None = None
    try:
        snapshot = json.loads(record.snapshot_json)
        raw_name = snapshot.get("name")
        if isinstance(raw_name, str):
            task_name = raw_name
    except TypeError, ValueError, json.JSONDecodeError:
        task_name = None
    return build_notification(
        task_name=task_name,
        task_id=record.task_id,
        status=record.status,
        source=record.source,
        duration_ms=record.duration_ms,
        error=record.error,
    )


def maybe_notify_run(record: RunRecord) -> None:
    """Best-effort Notification for one terminal Run. Never raises."""
    if record.status == "running":
        return
    try:
        config = load_notify_config()
        if config is None or not should_notify(config, record.status):
            return
        payload = notification_payload_for_run(record)
        publish_notification(config, payload)
    except Exception as error:
        print(f"notification failed: {error}", file=sys.stderr, flush=True)
