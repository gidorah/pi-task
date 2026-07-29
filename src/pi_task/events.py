from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal, cast

TerminalRunStatus = Literal["succeeded", "failed", "timed_out", "cancelled"]

_UNEXPECTED_LINE_SAMPLE_LIMIT = 3
_UNEXPECTED_LINE_SAMPLE_LENGTH = 200


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
    unexpected_line_count: int = 0
    unexpected_line_samples: list[str] = field(default_factory=list)
    usage: UsageTotals = field(default_factory=UsageTotals)

    def observe_unexpected_line(self, text: str) -> None:
        self.unexpected_line_count += 1
        if len(self.unexpected_line_samples) < _UNEXPECTED_LINE_SAMPLE_LIMIT:
            self.unexpected_line_samples.append(text[:_UNEXPECTED_LINE_SAMPLE_LENGTH])


def unexpected_output_diagnostic(observation: StreamObservation) -> str | None:
    if observation.unexpected_line_count == 0:
        return None
    count = observation.unexpected_line_count
    noun = "line" if count == 1 else "lines"
    samples = ", ".join(
        json.dumps(sample, ensure_ascii=True) for sample in observation.unexpected_line_samples
    )
    return f"unexpected Pi stdout: {count} {noun}; samples: {samples}"


def consume_event_line(observation: StreamObservation, line: str) -> None:
    text = line.strip()
    if not text:
        return
    try:
        event = json.loads(text)
    except json.JSONDecodeError:
        observation.observe_unexpected_line(text)
        return
    if not isinstance(event, dict):
        observation.observe_unexpected_line(text)
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
    observation.final_stop_reason = stop_reason if isinstance(stop_reason, str) else None
    if count_usage:
        usage = message.get("usage")
        if isinstance(usage, dict):
            observation.usage.add_message_usage(usage)


def classify_run_status(
    *,
    process_exit_code: int | None,
    timed_out: bool,
    cancelled: bool,
    observation: StreamObservation,
) -> TerminalRunStatus:
    # Sticky timeout: once the task deadline is decided, a late SIGTERM during
    # teardown must not reclassify the run as cancelled.
    if timed_out:
        return "timed_out"
    if cancelled:
        return "cancelled"
    if process_exit_code is None:
        return "failed"
    if process_exit_code != 0:
        return "failed"
    if not observation.saw_assistant or observation.final_stop_reason is None:
        return "failed"
    if observation.final_stop_reason == "stop":
        return "succeeded"
    return "failed"
