from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, cast


@dataclass
class UsageTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_total: float | None = None

    def add_message_usage(self, usage: dict[str, Any]) -> None:
        self.input_tokens += int(usage.get("input") or 0)
        self.output_tokens += int(usage.get("output") or 0)
        self.cache_read_tokens += int(usage.get("cacheRead") or 0)
        self.cache_write_tokens += int(usage.get("cacheWrite") or 0)
        cost = usage.get("cost")
        if isinstance(cost, dict) and cost.get("total") is not None:
            amount = float(cost["total"])
            self.cost_total = amount if self.cost_total is None else self.cost_total + amount
        elif usage.get("totalCost") is not None:
            amount = float(usage["totalCost"])
            self.cost_total = amount if self.cost_total is None else self.cost_total + amount


@dataclass
class StreamObservation:
    session_id: str | None = None
    session_timestamp: str | None = None
    session_cwd: str | None = None
    final_stop_reason: str | None = None
    saw_assistant: bool = False
    malformed_line: bool = False
    usage: UsageTotals = field(default_factory=UsageTotals)


def consume_event_line(observation: StreamObservation, line: str) -> None:
    text = line.strip()
    if not text:
        return
    try:
        event = json.loads(text)
    except json.JSONDecodeError:
        observation.malformed_line = True
        return
    if not isinstance(event, dict):
        observation.malformed_line = True
        return

    event_type = event.get("type")
    if event_type == "session":
        session_id = event.get("id")
        timestamp = event.get("timestamp")
        cwd = event.get("cwd")
        if isinstance(session_id, str):
            observation.session_id = session_id
        if isinstance(timestamp, str):
            observation.session_timestamp = timestamp
        if isinstance(cwd, str):
            observation.session_cwd = cwd
        return

    event_type = event.get("type")
    if event_type == "message_end":
        message = event.get("message")
        if isinstance(message, dict):
            _observe_assistant(observation, message, count_usage=True)
        return
    if event_type == "turn_end":
        message = event.get("message")
        if isinstance(message, dict):
            # turn_end repeats the assistant message; keep stop reason only.
            _observe_assistant(observation, message, count_usage=False)
        return
    if event_type == "agent_end":
        messages = event.get("messages")
        if not isinstance(messages, list):
            return
        # agent_end reprints the full message list; use it only as a fallback.
        if observation.saw_assistant:
            for item in reversed(messages):
                if isinstance(item, dict):
                    message = cast("dict[str, Any]", item)
                    if message.get("role") == "assistant":
                        _observe_assistant(observation, message, count_usage=False)
                        break
            return
        for item in messages:
            if isinstance(item, dict):
                _observe_assistant(observation, cast("dict[str, Any]", item), count_usage=True)
        return


def _observe_assistant(
    observation: StreamObservation,
    message: dict[str, Any],
    *,
    count_usage: bool,
) -> None:
    if message.get("role") != "assistant":
        return
    observation.saw_assistant = True
    stop_reason = message.get("stopReason")
    if isinstance(stop_reason, str):
        observation.final_stop_reason = stop_reason
    if count_usage:
        usage = message.get("usage")
        if isinstance(usage, dict):
            observation.usage.add_message_usage(usage)


def classify_run_status(
    *,
    process_exit_code: int | None,
    timed_out: bool,
    observation: StreamObservation,
) -> str:
    if timed_out:
        return "timed_out"
    if process_exit_code is None:
        return "failed"
    if process_exit_code != 0:
        return "failed"
    if observation.malformed_line:
        return "failed"
    if not observation.saw_assistant or observation.final_stop_reason is None:
        return "failed"
    if observation.final_stop_reason == "stop":
        return "succeeded"
    return "failed"
