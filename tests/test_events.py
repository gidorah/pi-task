from __future__ import annotations

import json

import pytest

from pi_task.events import (
    StreamObservation,
    classify_run_status,
    consume_event_line,
    parse_result_report,
)


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
        classify_run_status(
            process_exit_code=0,
            timed_out=False,
            cancelled=False,
            observation=observation,
        )
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
        classify_run_status(
            process_exit_code=0,
            timed_out=False,
            cancelled=False,
            observation=observation,
        )
        == "succeeded"
    )


def test_final_assistant_result_block_is_parsed() -> None:
    observation = StreamObservation()
    content = (
        "Work is partly done.\n\n<pi-task-result>\n"
        '{"outcome":"partial","summary":"Updated config; restart remains."}\n'
        "</pi-task-result>"
    )
    consume_event_line(
        observation,
        json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": content}],
                    "stopReason": "stop",
                },
            }
        ),
    )
    consume_event_line(
        observation,
        json.dumps(
            {
                "type": "agent_end",
                "messages": [
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": content}],
                        "stopReason": "stop",
                    }
                ],
            }
        ),
    )

    assert observation.final_assistant_text == content
    assert parse_result_report(observation.final_assistant_text) == (
        "partial",
        "Updated config; restart remains.",
    )


@pytest.mark.parametrize(
    "text",
    [
        "No report",
        '<pi-task-result>{"outcome":"nope","summary":"bad"}</pi-task-result>',
        '<pi-task-result>{"outcome":"succeeded"}</pi-task-result>',
        "<pi-task-result>{not json}</pi-task-result>",
        (
            '<pi-task-result>{"outcome":"succeeded","summary":"one"}</pi-task-result>\n'
            '<pi-task-result>{"outcome":"failed","summary":"two"}</pi-task-result>'
        ),
        '<pi-task-result>{"outcome":"succeeded","summary":"done"}</pi-task-result> trailing',
    ],
)
def test_result_report_rejects_missing_or_malformed_final_block(text: str) -> None:
    assert parse_result_report(text) is None


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
    assert (
        classify_run_status(
            process_exit_code=0,
            timed_out=False,
            cancelled=False,
            observation=length,
        )
        == "failed"
    )

    missing = StreamObservation()
    assert (
        classify_run_status(
            process_exit_code=0,
            timed_out=False,
            cancelled=False,
            observation=missing,
        )
        == "failed"
    )


def test_final_assistant_without_stop_reason_fails() -> None:
    observation = StreamObservation()
    consume_event_line(
        observation,
        json.dumps(
            {
                "type": "message_end",
                "message": {"role": "assistant", "stopReason": "stop"},
            }
        ),
    )
    consume_event_line(
        observation,
        json.dumps(
            {
                "type": "message_end",
                "message": {"role": "assistant"},
            }
        ),
    )

    assert (
        classify_run_status(
            process_exit_code=0,
            timed_out=False,
            cancelled=False,
            observation=observation,
        )
        == "failed"
    )


def test_unresolved_terminal_tool_use_fails() -> None:
    observation = StreamObservation()
    consume_event_line(
        observation,
        json.dumps(
            {
                "type": "message_end",
                "message": {"role": "assistant", "stopReason": "toolUse"},
            }
        ),
    )
    assert (
        classify_run_status(
            process_exit_code=0,
            timed_out=False,
            cancelled=False,
            observation=observation,
        )
        == "failed"
    )


def test_unexpected_output_diagnostics_are_bounded() -> None:
    observation = StreamObservation()
    for line in ["first", "second", "x" * 500, "fourth", "fifth"]:
        consume_event_line(observation, line)

    assert observation.unexpected_line_count == 5
    assert observation.unexpected_line_samples == ["first", "second", "x" * 200]


def test_successful_completion_ignores_incidental_non_event_output() -> None:
    observation = StreamObservation()
    consume_event_line(observation, "incidental diagnostic")
    consume_event_line(
        observation,
        json.dumps(
            {
                "type": "message_end",
                "message": {"role": "assistant", "stopReason": "stop"},
            }
        ),
    )

    assert (
        classify_run_status(
            process_exit_code=0,
            timed_out=False,
            cancelled=False,
            observation=observation,
        )
        == "succeeded"
    )


def test_malformed_stream_timeout_cancel_and_process_error() -> None:
    malformed = StreamObservation()
    consume_event_line(malformed, "{not-json")
    assert (
        classify_run_status(
            process_exit_code=0,
            timed_out=False,
            cancelled=False,
            observation=malformed,
        )
        == "failed"
    )

    ok = StreamObservation()
    consume_event_line(
        ok,
        json.dumps({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}}),
    )
    assert (
        classify_run_status(
            process_exit_code=0,
            timed_out=True,
            cancelled=False,
            observation=ok,
        )
        == "timed_out"
    )
    assert (
        classify_run_status(
            process_exit_code=2,
            timed_out=False,
            cancelled=False,
            observation=ok,
        )
        == "failed"
    )
    assert (
        classify_run_status(
            process_exit_code=0,
            timed_out=False,
            cancelled=True,
            observation=ok,
        )
        == "cancelled"
    )
    assert (
        classify_run_status(
            process_exit_code=143,
            timed_out=False,
            cancelled=True,
            observation=ok,
        )
        == "cancelled"
    )


def test_sticky_timeout_beats_late_cancel_flag() -> None:
    ok = StreamObservation()
    consume_event_line(
        ok,
        json.dumps({"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}}),
    )
    assert (
        classify_run_status(
            process_exit_code=0,
            timed_out=True,
            cancelled=True,
            observation=ok,
        )
        == "timed_out"
    )


def test_find_session_path_can_use_session_id_alone(tmp_path, monkeypatch) -> None:
    from pi_task import runner

    agent = tmp_path / "agent"
    session_dir = agent / "sessions" / "--tmp-project--"
    session_dir.mkdir(parents=True)
    session_id = "019f0000-aaaa-bbbb-cccc-ddddeeeeffff"
    path = session_dir / f"2030-01-01T00-00-00-000Z_{session_id}.jsonl"
    path.write_text("{}\n")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent))

    found = runner.find_session_path(session_id)
    assert found == path
