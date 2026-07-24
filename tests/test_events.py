from __future__ import annotations

import json

import pytest

from pi_task.events import StreamObservation, classify_run_status, consume_event_line


def test_consume_session_and_successful_assistant_completion() -> None:
    observation = StreamObservation()
    consume_event_line(
        observation,
        json.dumps(
            {
                "type": "session",
                "id": "abc",
                "timestamp": "2030-01-01T00:00:00.000Z",
                "cwd": "/tmp/project",
            }
        ),
    )
    consume_event_line(observation, json.dumps({"type": "agent_start"}))
    consume_event_line(
        observation,
        json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "stopReason": "toolUse",
                    "usage": {
                        "input": 2,
                        "output": 1,
                        "cacheRead": 0,
                        "cacheWrite": 0,
                        "cost": {"total": 0.1},
                    },
                },
            }
        ),
    )
    consume_event_line(
        observation,
        json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "stopReason": "stop",
                    "usage": {
                        "input": 5,
                        "output": 4,
                        "cacheRead": 1,
                        "cacheWrite": 2,
                        "cost": {"total": 0.2},
                    },
                },
            }
        ),
    )

    assert observation.session_id == "abc"
    assert observation.session_cwd == "/tmp/project"
    assert observation.final_stop_reason == "stop"
    assert observation.usage.input_tokens == 7
    assert observation.usage.output_tokens == 5
    assert observation.usage.cache_read_tokens == 1
    assert observation.usage.cache_write_tokens == 2
    assert observation.usage.cost_total == pytest.approx(0.3)
    assert (
        classify_run_status(process_exit_code=0, timed_out=False, observation=observation)
        == "succeeded"
    )


def test_recovered_tool_error_does_not_override_final_success() -> None:
    observation = StreamObservation()
    consume_event_line(
        observation,
        json.dumps(
            {
                "type": "tool_execution_end",
                "toolName": "bash",
                "isError": True,
                "result": "boom",
            }
        ),
    )
    consume_event_line(
        observation,
        json.dumps(
            {
                "type": "message_end",
                "message": {"role": "assistant", "stopReason": "stop", "usage": {"input": 1}},
            }
        ),
    )
    assert (
        classify_run_status(process_exit_code=0, timed_out=False, observation=observation)
        == "succeeded"
    )


def test_length_exhaustion_and_missing_response_fail() -> None:
    length = StreamObservation()
    consume_event_line(
        length,
        json.dumps(
            {
                "type": "message_end",
                "message": {"role": "assistant", "stopReason": "length"},
            }
        ),
    )
    assert classify_run_status(process_exit_code=0, timed_out=False, observation=length) == "failed"

    missing = StreamObservation()
    assert (
        classify_run_status(process_exit_code=0, timed_out=False, observation=missing) == "failed"
    )


def test_malformed_stream_timeout_and_process_error() -> None:
    malformed = StreamObservation()
    consume_event_line(malformed, "{not-json")
    assert (
        classify_run_status(process_exit_code=0, timed_out=False, observation=malformed) == "failed"
    )

    ok = StreamObservation()
    consume_event_line(
        ok,
        json.dumps({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}}),
    )
    assert classify_run_status(process_exit_code=0, timed_out=True, observation=ok) == "timed_out"
    assert classify_run_status(process_exit_code=2, timed_out=False, observation=ok) == "failed"
