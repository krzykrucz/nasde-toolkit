"""Tests for ConfigurableCursor agent."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nasde_toolkit.agents.configurable_cursor import ConfigurableCursor

MODEL = "cursor/composer-2.5"

EVENTS = [
    {
        "type": "system",
        "subtype": "init",
        "apiKeySource": "env",
        "cwd": "/app",
        "session_id": "s1",
        "model": "composer-2.5",
        "permissionMode": "default",
    },
    {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "do it"}]}, "session_id": "s1"},
    {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        "session_id": "s1",
        "model_call_id": "m1",
        "timestamp_ms": 1_700_000_000_000,
    },
    {
        "type": "usage",
        "subtype": "model_call",
        "model_call_id": "m1",
        "session_id": "s1",
        "usage": {"inputTokens": 100, "outputTokens": 50, "cacheReadTokens": 1_000, "cacheWriteTokens": 200},
    },
    {
        "type": "tool_call",
        "subtype": "completed",
        "call_id": "c1",
        "tool_call": {"readToolCall": {"args": {}}},
        "session_id": "s1",
        "model_call_id": "m2",
        "timestamp_ms": 1_700_000_001_000,
    },
    {
        "type": "usage",
        "subtype": "model_call",
        "model_call_id": "m2",
        "session_id": "s1",
        "usage": {"inputTokens": 10, "outputTokens": 5, "cacheReadTokens": 2_000, "cacheWriteTokens": 0},
    },
    {
        "type": "result",
        "subtype": "success",
        "duration_ms": 10,
        "duration_api_ms": 10,
        "is_error": False,
        "result": "done",
        "session_id": "s1",
        "usage": {"inputTokens": 110, "outputTokens": 55, "cacheReadTokens": 3_000, "cacheWriteTokens": 200},
    },
]


def _agent(**kwargs: object) -> ConfigurableCursor:
    return ConfigurableCursor(logs_dir=Path("/tmp/logs"), model_name=MODEL, **kwargs)


def test_name_returns_configurable_cursor_cli() -> None:
    assert ConfigurableCursor.name() == "configurable-cursor-cli"


def test_constructor_stores_sandbox_files() -> None:
    files = {"/app/AGENTS.md": "/tmp/AGENTS.md"}
    assert _agent(sandbox_files=files)._sandbox_files == files


def test_constructor_defaults_to_empty_sandbox_files() -> None:
    assert _agent()._sandbox_files == {}


def test_setup_calls_dns_fix_then_upload_then_parent() -> None:
    agent = _agent(sandbox_files={"/app/AGENTS.md": __file__})
    env = AsyncMock()
    env.exec.return_value = MagicMock(return_code=0, stderr="")

    with patch.object(ConfigurableCursor.__bases__[0], "setup", new_callable=AsyncMock) as mock_parent_setup:
        asyncio.run(agent.setup(env))

    dns_call = env.exec.call_args_list[0]
    cmd = dns_call.kwargs.get("command", dns_call.args[0] if dns_call.args else "")
    assert "api2.cursor.sh" in cmd
    mock_parent_setup.assert_awaited_once_with(env)
    assert env.upload_file.await_count == 1


def test_upload_raises_on_missing_source() -> None:
    agent = _agent(sandbox_files={"/app/AGENTS.md": "/nonexistent/path.md"})
    env = AsyncMock()
    env.exec.return_value = MagicMock(return_code=0, stderr="")
    with (
        patch.object(ConfigurableCursor.__bases__[0], "setup", new_callable=AsyncMock),
        pytest.raises(FileNotFoundError, match="does not exist"),
    ):
        asyncio.run(agent.setup(env))


def test_trajectory_steps_get_per_call_metrics_and_cost() -> None:
    agent = _agent(pricing={"input": 0.5, "output": 2.5, "cache_read": 0.2})
    trajectory = agent._convert_events_to_trajectory(EVENTS)
    agent_steps = [s for s in trajectory.steps if s.source == "agent"]
    assert len(agent_steps) == 2

    first = agent_steps[0].metrics
    assert first is not None
    assert first.prompt_tokens == 100 + 1_000 + 200
    assert first.completion_tokens == 50
    assert first.cached_tokens == 1_000
    assert first.cost_usd == pytest.approx((100 * 0.5 + 200 * 0.5 + 1_000 * 0.2 + 50 * 2.5) / 1e6)

    second = agent_steps[1].metrics
    assert second is not None
    assert second.prompt_tokens == 10 + 2_000
    assert second.cost_usd == pytest.approx((10 * 0.5 + 2_000 * 0.2 + 5 * 2.5) / 1e6)


def test_trajectory_steps_without_rates_have_tokens_but_no_cost() -> None:
    agent = _agent()
    with patch.object(ConfigurableCursor, "_resolve_pricing_rates", return_value=None):
        trajectory = agent._convert_events_to_trajectory(EVENTS)
    first = next(s for s in trajectory.steps if s.source == "agent").metrics
    assert first is not None
    assert first.prompt_tokens == 1_300
    assert first.cost_usd is None


def test_user_steps_get_no_metrics() -> None:
    trajectory = _agent()._convert_events_to_trajectory(EVENTS)
    assert all(s.metrics is None for s in trajectory.steps if s.source == "user")


def test_usage_lands_on_last_assistant_step_of_a_shared_model_call() -> None:
    events = [
        EVENTS[0],
        EVENTS[1],
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "first"}]},
            "session_id": "s1",
            "model_call_id": "m1",
            "timestamp_ms": 1_700_000_000_000,
        },
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "second"}]},
            "session_id": "s1",
            "model_call_id": "m1",
            "timestamp_ms": 1_700_000_000_500,
        },
        EVENTS[3],
        EVENTS[-1],
    ]
    trajectory = _agent()._convert_events_to_trajectory(events)
    agent_steps = [s for s in trajectory.steps if s.source == "agent"]
    assert len(agent_steps) == 2
    assert agent_steps[0].metrics is None
    assert agent_steps[1].metrics is not None
    assert agent_steps[1].metrics.completion_tokens == 50
