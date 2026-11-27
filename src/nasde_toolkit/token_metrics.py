"""Token usage and cost economics extracted from an agent trajectory.

Single source of truth shared by the run path (evaluator.py → assessment_summary.json)
and the export path (results_exporter.py → metrics.json), so the two never diverge.

Token volumes come from the trajectory's top-level ``final_metrics`` (written by
Harbor for both Claude and Codex agents):
  input       = total_prompt_tokens            (full, cache included)
  output      = total_completion_tokens + extra.reasoning_output_tokens
  cached      = total_cached_tokens            (cache reads)
  cache_write = extra.total_cache_creation_input_tokens  (Claude)
              or extra.usage_totals.cacheWriteTokens   (Cursor)
  total       = input + output

Cost is cache-aware (ADR-014): fresh input at the full rate, cache writes and
reads at their catalog rates. The cache-free ceiling is derivable from the
recorded token volumes and is not stored.

Cursor models (``cursor/*``) missing from the catalog fall back to Harbor's own
trajectory cost (``final_metrics.total_cost_usd``); ``cost_source`` records where
every figure came from (ADR-015).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from rich.console import Console
from rich.markup import escape

from nasde_toolkit.pricing import ModelPrice, compute_cost_usd, pricing_as_of

console = Console()

CURSOR_MODEL_PREFIX = "cursor/"


@dataclass
class TokenUsage:
    """Token volumes for one trial (one agent run)."""

    input_tokens: int
    output_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    cached_tokens: int
    cache_write_tokens: int
    total_tokens: int


def extract_token_usage(trajectory: dict) -> TokenUsage | None:
    """Build TokenUsage from a trajectory's final_metrics, or None if unavailable."""
    final_metrics = trajectory.get("final_metrics") or {}
    prompt_tokens = final_metrics.get("total_prompt_tokens")
    if prompt_tokens is None:
        return None
    extra = final_metrics.get("extra") or {}
    completion_tokens = final_metrics.get("total_completion_tokens", 0) or 0
    reasoning_tokens = extra.get("reasoning_output_tokens", 0) or 0
    cached_tokens = final_metrics.get("total_cached_tokens", 0) or 0
    cache_write_tokens = _cache_write_tokens(extra)
    output_tokens = completion_tokens + reasoning_tokens
    return TokenUsage(
        input_tokens=prompt_tokens,
        output_tokens=output_tokens,
        completion_tokens=completion_tokens,
        reasoning_tokens=reasoning_tokens,
        cached_tokens=cached_tokens,
        cache_write_tokens=cache_write_tokens,
        total_tokens=prompt_tokens + output_tokens,
    )


def dominant_normalized_score(groups: list[dict]) -> float | None:
    """Pick the dominant evaluator cluster's mean score from a summary's groups dict."""
    if not groups:
        return None
    dominant = next((g for g in groups if g.get("dominant")), groups[0])
    return dominant.get("normalized_score_mean")


def read_trajectory(trial_dir: Path) -> dict | None:
    """Read a trajectory from a run trial dir (agent/) or an export dir (flat)."""
    for candidate in (trial_dir / "agent" / "trajectory.json", trial_dir / "trajectory.json"):
        if candidate.exists():
            with open(candidate, encoding="utf-8") as f:
                data: dict = json.load(f)
            return data
    return None


def build_trial_economics(
    trial_dir: Path,
    model: str,
    pricing: dict[str, ModelPrice],
) -> dict:
    """Assemble the economics block for one trial (used by run and export paths).

    Only raw, baseline-invariant signals are recorded — token volumes, USD cost,
    the price catalog date and the cost's source. Quality-vs-cost comparison is a
    Pareto front over these raw axes (in the nasde-benchmark-runner skill), not a
    scalar score/cost ratio whose zero point is arbitrary. See ADR-011. A Cursor
    model absent from the catalog uses Harbor's trajectory cost (ADR-015).
    """
    trajectory = read_trajectory(trial_dir)
    usage = extract_token_usage(trajectory) if trajectory is not None else None
    if trajectory is None or usage is None:
        return _empty_economics(model)
    if model not in pricing and model.startswith(CURSOR_MODEL_PREFIX):
        return _harbor_estimated_economics(model, usage, trajectory)
    cost_usd = compute_cost_usd(
        usage.input_tokens,
        usage.output_tokens,
        model,
        pricing,
        cache_read_tokens=usage.cached_tokens,
        cache_write_tokens=usage.cache_write_tokens,
    )
    return {
        "model_name": model,
        "token_usage": asdict(usage),
        "cost_usd": cost_usd,
        "pricing_as_of": pricing_as_of(model, pricing) if cost_usd is not None else None,
        "cost_source": "pricing.toml" if cost_usd is not None else None,
    }


def _cache_write_tokens(extra: dict) -> int:
    claude_writes = extra.get("total_cache_creation_input_tokens")
    if claude_writes:
        return int(claude_writes)
    usage_totals = extra.get("usage_totals") or {}
    return int(usage_totals.get("cacheWriteTokens", 0) or 0)


def _harbor_estimated_economics(model: str, usage: TokenUsage, trajectory: dict) -> dict:
    final_metrics = trajectory.get("final_metrics") or {}
    cost_usd = final_metrics.get("total_cost_usd")
    harbor_source = (final_metrics.get("extra") or {}).get("cost_source", "unknown")
    cost_source = f"harbor:{harbor_source}" if cost_usd is not None else None
    outcome = f"using Harbor estimate ({escape(cost_source)})" if cost_source else "cost left unset"
    console.print(
        f"  [yellow]No pricing for model {escape(repr(model))} in pricing.toml — {outcome}. "
        f"Add it to pricing.toml for catalog-priced cost.[/yellow]"
    )
    return {
        "model_name": model,
        "token_usage": asdict(usage),
        "cost_usd": cost_usd,
        "pricing_as_of": None,
        "cost_source": cost_source,
    }


def _empty_economics(model: str) -> dict:
    return {
        "model_name": model,
        "token_usage": None,
        "cost_usd": None,
        "pricing_as_of": None,
        "cost_source": None,
    }
