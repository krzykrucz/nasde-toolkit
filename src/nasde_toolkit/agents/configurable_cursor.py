"""Configurable Cursor CLI agent for Harbor evaluations.

Harbor's built-in CursorCli handles installing ``cursor-agent``, command
construction (``--yolo`` also auto-approves MCP servers), stream-json →
trajectory conversion, MCP registration (``~/.cursor/mcp.json``), native skill
injection (``config.agent.skills`` → ``~/.cursor/skills``), reasoning effort
(``model[effort=X]``) and cost estimation (its ``pricing`` kwarg, then a built-in
table, then LiteLLM). What CursorCli lacks:

1. A mechanism to inject arbitrary files (e.g. AGENTS.md) into the sandbox
   before the agent starts — ``sandbox_files``, as for ConfigurableClaude.
2. Per-step token/cost ``metrics``. CursorCli fills only ``final_metrics``, so
   nasde's deferred Opik span patch (ADR-006) never emitted Cursor agent spans.
   ``cursor-agent`` reports usage per model call (``type: usage``,
   ``subtype: model_call``); this class attaches it to the matching step so
   Opik gets per-step tokens and cost, as it does for Claude (ADR-015).

Usage in harbor_config.json::

    {
      "agents": [{
        "import_path": "nasde_toolkit.agents.configurable_cursor:ConfigurableCursor",
        "model_name": "cursor/composer-2.5",
        "kwargs": {
          "sandbox_files": {
            "/app/AGENTS.md": "path/to/AGENTS.md"
          }
        }
      }]
    }
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from harbor.agents.installed.cursor_cli import (
    CursorAssistantMessage,
    CursorCli,
    CursorCliOptions,
    CursorThinkingBlock,
    CursorToolCall,
)
from harbor.environments.base import BaseEnvironment
from harbor.models.trajectories import Metrics, Step, Trajectory
from pydantic import Field

logger = logging.getLogger(__name__)

_DNS_FIX_COMMAND = (
    "timeout 5 getent hosts api2.cursor.sh >/dev/null 2>&1"
    " || printf 'nameserver 8.8.8.8\\nnameserver 1.1.1.1\\n' > /etc/resolv.conf"
)

_USAGE_KEYS = ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens")


class ConfigurableCursorOptions(CursorCliOptions):
    """CursorCliOptions plus nasde's ``sandbox_files`` kwarg.

    Harbor's preflight validates raw kwargs against ``options_model`` with
    ``extra="forbid"`` before the agent is constructed, so every nasde kwarg
    must be declared here. The field has no ``Cli``/``Env`` annotation and is
    therefore never rendered into the agent command or environment.
    """

    sandbox_files: dict[str, str] | None = Field(
        default=None,
        description="Container path -> host file path, uploaded into the sandbox during setup().",
    )


class ConfigurableCursor(CursorCli):
    """CursorCli with declarative file injection and per-step metrics.

    Args:
        sandbox_files: Mapping of container paths to host-relative file paths.
            Each entry is uploaded into the sandbox during setup() before
            the agent starts. Paths are resolved relative to CWD.
    """

    options_model = ConfigurableCursorOptions

    def __init__(
        self,
        sandbox_files: dict[str, str] | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._sandbox_files = sandbox_files or {}
        self._model_call_step_ids: dict[str, int] = {}

    @staticmethod
    def name() -> str:
        return "configurable-cursor-cli"

    async def setup(self, environment: BaseEnvironment) -> None:
        """Fix cloud DNS, upload files, then run base setup (installs cursor-agent)."""
        await self._ensure_dns_resolution(environment)
        await self._upload_sandbox_files(environment)
        await super().setup(environment)

    def _build_agent_step(
        self,
        event: CursorAssistantMessage,
        step_id: int,
        thinking: list[CursorThinkingBlock],
    ) -> Step:
        if event.model_call_id is not None:
            self._model_call_step_ids.setdefault(event.model_call_id, step_id)
        return super()._build_agent_step(event, step_id, thinking)

    def _apply_tool_call_event(self, event: CursorToolCall, step: Step) -> None:  # type: ignore[override]
        self._model_call_step_ids.setdefault(event.model_call_id, step.step_id)
        CursorCli._apply_tool_call_event(event, step)

    def _convert_events_to_trajectory(self, events: list[dict[str, Any]]) -> Trajectory:
        self._model_call_step_ids = {}
        trajectory = super()._convert_events_to_trajectory(events)
        self._attach_step_metrics(trajectory, events)
        return trajectory

    def _attach_step_metrics(self, trajectory: Trajectory, events: list[dict[str, Any]]) -> None:
        usage_by_step = _sum_model_call_usage_by_step(events, self._model_call_step_ids)
        resolved = self._resolve_pricing_rates()
        for step in trajectory.steps:
            usage = usage_by_step.get(step.step_id)
            if usage is None:
                continue
            step.metrics = Metrics(
                prompt_tokens=usage["inputTokens"] + usage["cacheReadTokens"] + usage["cacheWriteTokens"],
                completion_tokens=usage["outputTokens"],
                cached_tokens=usage["cacheReadTokens"],
                cost_usd=self._cost_from_token_rates(usage, resolved[0]) if resolved else None,
            )

    async def _ensure_dns_resolution(self, environment: BaseEnvironment) -> None:
        """Prepend public DNS resolvers if the cloud sandbox cannot reach Cursor's API."""
        result = await environment.exec(command=_DNS_FIX_COMMAND)
        if result.return_code == 0:
            logger.debug("DNS resolution fix applied")
        else:
            logger.warning("DNS resolution fix failed: %s", result.stderr)

    async def _upload_sandbox_files(self, environment: BaseEnvironment) -> None:
        for target_path, source_path in self._sandbox_files.items():
            resolved = Path(source_path).resolve()
            if not resolved.is_file():
                raise FileNotFoundError(
                    f"sandbox_files: source '{source_path}' (resolved to '{resolved}') does not exist"
                )
            parent_dir = str(Path(target_path).parent)
            await environment.exec(command=f"mkdir -p {parent_dir}")
            await environment.upload_file(source_path=resolved, target_path=target_path)


def _sum_model_call_usage_by_step(
    events: list[dict[str, Any]],
    model_call_step_ids: dict[str, int],
) -> dict[int, dict[str, int]]:
    totals: dict[int, dict[str, int]] = {}
    for event in events:
        if event.get("type") != "usage" or event.get("subtype") != "model_call":
            continue
        step_id = model_call_step_ids.get(str(event.get("model_call_id", "")))
        usage = event.get("usage")
        if step_id is None or not isinstance(usage, dict):
            continue
        bucket = totals.setdefault(step_id, dict.fromkeys(_USAGE_KEYS, 0))
        for key in _USAGE_KEYS:
            bucket[key] += int(usage.get(key) or 0)
    return totals
