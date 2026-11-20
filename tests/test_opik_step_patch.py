"""ADR-006 Opik Step patch: exactly one span per Harbor Step, carrying token usage.

``track_harbor()`` wraps ``Step.__init__`` to emit a span eagerly (before Harbor
assigns ``metrics``). nasde's deferred patch runs after it and must replace that
behaviour, not stack on top of it — otherwise every step is emitted twice and
Opik's trace token totals double-count when metrics arrive in the constructor.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import opik
import pytest
from harbor.models.trajectories.agent import Agent
from harbor.models.trajectories.final_metrics import FinalMetrics
from harbor.models.trajectories.metrics import Metrics
from harbor.models.trajectories.step import Step
from harbor.models.trajectories.tool_call import ToolCall
from harbor.models.trajectories.trajectory import Trajectory
from opik import opik_context
from opik.integrations.harbor import opik_tracker

from nasde_toolkit import runner
from nasde_toolkit.agents.configurable_cursor import ConfigurableCursor

TIMESTAMP = "2026-10-15T00:00:00Z"


class _RecordingClient:
    def __init__(self) -> None:
        self.spans: list[dict[str, Any]] = []

    def __internal_api__span__(self, **kwargs: Any) -> None:
        self.spans.append(kwargs)


@pytest.fixture
def recording_client() -> Iterator[_RecordingClient]:
    original_init = Step.__init__
    original_setattr = Step.__setattr__
    original_trajectory_init = Trajectory.__init__
    client = _RecordingClient()
    fake_trace = type("FakeTrace", (), {"id": "trace-1", "project_name": "proj"})()
    with (
        patch.object(opik, "get_global_client", return_value=client),
        patch.object(opik_context, "get_current_trace_data", return_value=fake_trace),
        patch.object(opik_context, "get_current_span_data", return_value=None),
    ):
        opik_tracker._patch_step_class()
        runner._patch_opik_deferred_metrics()
        yield client
    Step.__init__ = original_init  # type: ignore[method-assign]
    Step.__setattr__ = original_setattr  # type: ignore[method-assign]
    Trajectory.__init__ = original_trajectory_init  # type: ignore[method-assign]
    for patch_fn, flag in (
        (opik_tracker._patch_step_class, "_patched"),
        (runner._patch_opik_deferred_metrics, "_applied"),
    ):
        if hasattr(patch_fn, flag):
            delattr(patch_fn, flag)


def _metrics() -> Metrics:
    return Metrics(prompt_tokens=100, completion_tokens=10)


def test_step_with_metrics_in_constructor_emits_one_span(recording_client: _RecordingClient) -> None:
    Step(step_id=1, source="agent", message="m", timestamp=TIMESTAMP, model_name="gpt-6-luna", metrics=_metrics())

    assert [span["name"] for span in recording_client.spans] == ["step_1"]
    assert recording_client.spans[0]["usage"]["total_tokens"] == 110


def test_step_with_metrics_assigned_later_emits_one_span_with_usage(recording_client: _RecordingClient) -> None:
    step = Step(step_id=2, source="agent", message="m", timestamp=TIMESTAMP, model_name="claude-sonnet-5-5")
    step.metrics = _metrics()

    assert [span["name"] for span in recording_client.spans] == ["step_2"]
    assert recording_client.spans[0]["usage"]["total_tokens"] == 110


def test_non_agent_step_emits_one_span(recording_client: _RecordingClient) -> None:
    Step(step_id=3, source="user", message="task", timestamp=TIMESTAMP)

    assert [span["name"] for span in recording_client.spans] == ["step_3"]


def _trajectory(steps: list[Step], final_metrics: FinalMetrics | None) -> Trajectory:
    return Trajectory(
        schema_version="ATIF-v1.7",
        session_id="session-1",
        agent=Agent(name="cursor-cli", version="1.0", model_name="cursor/composer-2.5"),
        steps=steps,
        final_metrics=final_metrics,
    )


def _session_spans(client: _RecordingClient) -> list[dict[str, Any]]:
    return [span for span in client.spans if span["name"] == "agent_session"]


def test_trajectory_flushes_agent_steps_that_never_got_metrics(recording_client: _RecordingClient) -> None:
    step = Step(step_id=1, source="agent", message="m", timestamp=TIMESTAMP, model_name="cursor/composer-2.5")
    step.tool_calls = [ToolCall(tool_call_id="c1", function_name="readToolCall", arguments={"path": "/app/x"})]
    assert recording_client.spans == []

    _trajectory([step], final_metrics=None)

    assert [span["name"] for span in recording_client.spans] == ["step_1"]
    assert recording_client.spans[0]["input"]["tool_calls"][0]["function_name"] == "readToolCall"


def test_trajectory_with_session_only_metrics_emits_session_span_with_cost(
    recording_client: _RecordingClient,
) -> None:
    step = Step(step_id=1, source="agent", message="m", timestamp=TIMESTAMP, model_name="cursor/composer-2.5")
    final_metrics = FinalMetrics(
        total_prompt_tokens=475459,
        total_completion_tokens=4866,
        total_cached_tokens=424256,
        total_cost_usd=0.1226177,
        extra={"cost_source": "cursor_pricing"},
    )

    _trajectory([step], final_metrics)

    sessions = _session_spans(recording_client)
    assert len(sessions) == 1
    assert sessions[0]["total_cost"] == pytest.approx(0.1226177)
    assert sessions[0]["usage"] == {"prompt_tokens": 475459, "completion_tokens": 4866, "total_tokens": 480325}
    assert sessions[0]["model"] == "cursor/composer-2.5"
    assert sessions[0]["metadata"]["cost_source"] == "cursor_pricing"


def test_trajectory_with_per_step_usage_emits_no_session_span(recording_client: _RecordingClient) -> None:
    step = Step(step_id=1, source="agent", message="m", timestamp=TIMESTAMP, model_name="claude-sonnet-5-5")
    step.metrics = _metrics()

    _trajectory([step], FinalMetrics(total_prompt_tokens=100, total_completion_tokens=10, total_cost_usd=0.01))

    assert _session_spans(recording_client) == []
    assert [span["name"] for span in recording_client.spans] == ["step_1"]


def test_session_span_is_emitted_once_per_trace(recording_client: _RecordingClient) -> None:
    final_metrics = FinalMetrics(total_prompt_tokens=10, total_completion_tokens=1, total_cost_usd=0.001)

    _trajectory([Step(step_id=1, source="user", message="task", timestamp=TIMESTAMP)], final_metrics)
    _trajectory([Step(step_id=1, source="user", message="task", timestamp=TIMESTAMP)], final_metrics)

    assert len(_session_spans(recording_client)) == 1


def test_cursor_trajectory_reports_agent_steps_and_session_cost(
    recording_client: _RecordingClient, tmp_path: Path
) -> None:
    agent = ConfigurableCursor(logs_dir=tmp_path, model_name="cursor/composer-2.5")
    events: list[dict[str, Any]] = [
        {
            "type": "system",
            "subtype": "init",
            "apiKeySource": "env",
            "cwd": "/app",
            "session_id": "s1",
            "model": "Composer 2.5",
            "permissionMode": "default",
        },
        {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": "task"}]},
            "session_id": "s1",
        },
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "reading"}]},
            "session_id": "s1",
            "model_call_id": "mc1",
            "timestamp_ms": 1795179453755,
        },
        {
            "type": "result",
            "subtype": "success",
            "duration_ms": 1,
            "duration_api_ms": 1,
            "is_error": False,
            "result": "done",
            "session_id": "s1",
            "request_id": "r1",
            "usage": {"inputTokens": 51203, "outputTokens": 4866, "cacheReadTokens": 424256, "cacheWriteTokens": 0},
        },
    ]

    trajectory = agent._convert_events_to_trajectory(events)

    span_names = [span["name"] for span in recording_client.spans]
    assert "step_2" in span_names
    sessions = _session_spans(recording_client)
    assert len(sessions) == 1
    assert trajectory.final_metrics is not None
    assert sessions[0]["total_cost"] == trajectory.final_metrics.total_cost_usd
    assert sessions[0]["total_cost"] is not None
