"""Cursor CLI (`cursor-agent -p`) subprocess evaluator backend."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

from rich.console import Console

from nasde_toolkit.config import EvaluationConfig
from nasde_toolkit.evaluator_backends.claude_subprocess import _extract_result_text, _resolve_path
from nasde_toolkit.evaluator_backends.protocol import AGENT_DIFF_FILENAME

console = Console()

_CURSOR_MODEL_PREFIX = "cursor/"

_JUDGE_RULE_TEMPLATE = """---
description: nasde judge instructions
alwaysApply: true
---

{prompt}
"""


class CursorSubprocessBackend:
    """Evaluator backend that spawns `cursor-agent -p` as a subprocess.

    Mirrors the Claude backend as closely as the public `cursor-agent` CLI
    allows (ADR-015): `--mode ask` is the read-only mode standing in for
    Claude's `--allowedTools Read,Glob,Grep`; `skills_dir`, `mcp_config` and
    `append_system_prompt` are delivered through a temp working dir
    (`.cursor/skills`, `.cursor/mcp.json`, an always-apply
    `.cursor/rules/nasde-judge.mdc`) with the trial workspace added via
    `--add-dir`, so the workspace is never written to. `max_turns` has no
    equivalent and is ignored; a custom `allowed_tools` is warned about and
    ignored. Authenticates via CURSOR_API_KEY or the `cursor-agent login`
    session.
    """

    async def run_evaluation(
        self,
        prompt: str,
        workspace_path: Path,
        eval_config: EvaluationConfig,
        project_root: Path,
        trial_dir: Path | None = None,
    ) -> str:
        self.validate_cli_installed()
        self.validate_auth()
        self._warn_unsupported_options(eval_config)
        cmd, temp_dir = self._build_command_with_context(workspace_path, eval_config, project_root, trial_dir)
        cwd = temp_dir if temp_dir else workspace_path
        try:
            return await self._run_subprocess([*cmd, "--", prompt], cwd, dict(os.environ))
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

    def validate_auth(self) -> None:
        if os.environ.get("CURSOR_API_KEY"):
            return
        console.print("[dim]No CURSOR_API_KEY in the environment — relying on the cursor-agent login session.[/dim]")

    def validate_cli_installed(self) -> None:
        if shutil.which("cursor-agent") is not None:
            return
        console.print(
            "[red]ERROR: `cursor-agent` CLI not found on PATH.[/red]\n"
            "[yellow]Assessment evaluation with the Cursor backend requires the Cursor CLI.[/yellow]\n"
            "Install it with `curl https://cursor.com/install -fsS | bash`, then re-run. "
            'To use a different backend, set \\[evaluation] backend = "claude" in nasde.toml, '
            "or pass --without-eval to skip assessment evaluation."
        )
        raise SystemExit(1)

    def _warn_unsupported_options(self, eval_config: EvaluationConfig) -> None:
        if not eval_config.allowed_tools:
            return
        console.print(
            f"[yellow]WARNING: \\[evaluation] allowed_tools = {eval_config.allowed_tools} is ignored by the "
            "cursor backend — the judge always runs read-only (--mode ask).[/yellow]"
        )

    def _build_command_with_context(
        self,
        workspace_path: Path,
        eval_config: EvaluationConfig,
        project_root: Path,
        trial_dir: Path | None,
    ) -> tuple[list[str], Path | None]:
        cmd = self._build_command(eval_config, trial_dir)
        if not (eval_config.skills_dir or eval_config.mcp_config or eval_config.append_system_prompt):
            return cmd, None
        temp_dir = Path(tempfile.mkdtemp(prefix="nasde_eval_"))
        cursor_dir = temp_dir / ".cursor"
        cursor_dir.mkdir()
        _stage_skills(eval_config, project_root, cursor_dir)
        if _stage_mcp_config(eval_config, project_root, cursor_dir):
            cmd.append("--approve-mcps")
        _stage_system_prompt_rule(eval_config, cursor_dir)
        cmd.extend(["--add-dir", str(workspace_path)])
        return cmd, temp_dir

    def _build_command(self, eval_config: EvaluationConfig, trial_dir: Path | None) -> list[str]:
        cmd = [
            "cursor-agent",
            "-p",
            "--output-format",
            "json",
            "--trust",
            "--mode",
            "ask",
            "--model",
            eval_config.model.removeprefix(_CURSOR_MODEL_PREFIX),
        ]
        needs_trial_dir = trial_dir is not None and (
            eval_config.include_trajectory or (trial_dir / AGENT_DIFF_FILENAME).exists()
        )
        if needs_trial_dir:
            cmd.extend(["--add-dir", str(trial_dir)])
        return cmd

    async def _run_subprocess(self, cmd: list[str], cwd: Path, env: dict[str, str]) -> str:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=env,
        )
        stdout_bytes, stderr_bytes = await process.communicate()

        if process.returncode != 0:
            stderr_text = stderr_bytes.decode(errors="replace").strip()
            last_lines = "\n".join(stderr_text.splitlines()[-10:]) if stderr_text else ""
            model = cmd[cmd.index("--model") + 1]
            raise RuntimeError(
                f"cursor-agent process failed (exit code {process.returncode}). "
                f"Model: {model}, cwd: {cwd}. "
                f"stderr (last 10 lines):\n{last_lines}"
            )

        return _extract_result_text(stdout_bytes.decode())


def _stage_skills(eval_config: EvaluationConfig, project_root: Path, cursor_dir: Path) -> None:
    if not eval_config.skills_dir:
        return
    skills_source = _resolve_path(eval_config.skills_dir, project_root)
    if skills_source.is_dir():
        shutil.copytree(skills_source, cursor_dir / "skills")


def _stage_mcp_config(eval_config: EvaluationConfig, project_root: Path, cursor_dir: Path) -> bool:
    if not eval_config.mcp_config:
        return False
    shutil.copyfile(_resolve_path(eval_config.mcp_config, project_root), cursor_dir / "mcp.json")
    return True


def _stage_system_prompt_rule(eval_config: EvaluationConfig, cursor_dir: Path) -> None:
    if not eval_config.append_system_prompt:
        return
    rules_dir = cursor_dir / "rules"
    rules_dir.mkdir()
    (rules_dir / "nasde-judge.mdc").write_text(_JUDGE_RULE_TEMPLATE.format(prompt=eval_config.append_system_prompt))
