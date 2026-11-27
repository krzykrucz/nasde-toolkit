"""Tests for token usage + cost economics extraction."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nasde_toolkit.pricing import ModelPrice, load_pricing
from nasde_toolkit.token_metrics import (
    build_trial_economics,
    dominant_normalized_score,
    extract_token_usage,
    read_trajectory,
)

CLAUDE_TRAJ = {
    "final_metrics": {
        "total_prompt_tokens": 1_109_410,
        "total_completion_tokens": 55_518,
        "total_cached_tokens": 1_031_868,
        "extra": {"total_cache_creation_input_tokens": 74_666},
    }
}

CODEX_TRAJ = {
    "final_metrics": {
        "total_prompt_tokens": 2_817_494,
        "total_completion_tokens": 23_401,
        "total_cached_tokens": 2_646_272,
        "extra": {"reasoning_output_tokens": 13_921, "total_tokens": 2_840_895},
    }
}


def test_extract_claude_shape() -> None:
    usage = extract_token_usage(CLAUDE_TRAJ)
    assert usage is not None
    assert usage.input_tokens == 1_109_410
    assert usage.reasoning_tokens == 0
    assert usage.output_tokens == 55_518  # no reasoning to fold in
    assert usage.cached_tokens == 1_031_868
    assert usage.cache_write_tokens == 74_666
    assert usage.total_tokens == 1_109_410 + 55_518


def test_extract_codex_folds_reasoning_into_output() -> None:
    usage = extract_token_usage(CODEX_TRAJ)
    assert usage is not None
    assert usage.completion_tokens == 23_401
    assert usage.reasoning_tokens == 13_921
    assert usage.output_tokens == 23_401 + 13_921  # reasoning folded in
    assert usage.cache_write_tokens == 0  # Codex trajectories carry no cache-creation counter
    assert usage.total_tokens == 2_817_494 + 23_401 + 13_921


def test_extract_legacy_without_final_metrics_is_none() -> None:
    assert extract_token_usage({"steps": []}) is None
    assert extract_token_usage({"final_metrics": {"total_steps": 5}}) is None


def test_dominant_normalized_score_picks_dominant() -> None:
    groups = [
        {"dominant": False, "normalized_score_mean": 0.3},
        {"dominant": True, "normalized_score_mean": 0.8},
    ]
    assert dominant_normalized_score(groups) == 0.8


def test_dominant_normalized_score_falls_back_to_first() -> None:
    groups = [{"normalized_score_mean": 0.5}, {"normalized_score_mean": 0.9}]
    assert dominant_normalized_score(groups) == 0.5  # no dominant flag → first


def test_dominant_normalized_score_empty_is_none() -> None:
    assert dominant_normalized_score([]) is None


def test_read_trajectory_run_layout(tmp_path: Path) -> None:
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "trajectory.json").write_text(json.dumps(CODEX_TRAJ))
    assert read_trajectory(tmp_path) == CODEX_TRAJ


def test_read_trajectory_export_layout(tmp_path: Path) -> None:
    (tmp_path / "trajectory.json").write_text(json.dumps(CLAUDE_TRAJ))
    assert read_trajectory(tmp_path) == CLAUDE_TRAJ


def test_read_trajectory_missing_is_none(tmp_path: Path) -> None:
    assert read_trajectory(tmp_path) is None


def test_build_trial_economics_priced_model(tmp_path: Path) -> None:
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "trajectory.json").write_text(json.dumps(CODEX_TRAJ))
    econ = build_trial_economics(tmp_path, "gpt-6.1-sol", load_pricing())

    assert econ["model_name"] == "gpt-6.1-sol"
    assert econ["token_usage"]["total_tokens"] == 2_854_816
    # gpt-6.1-sol = $2 in / $10 out / $0.10 cached (ADR-014): fresh input at full
    # rate, the 2_646_272 cached reads at the cached rate, no write premium.
    fresh = 2_817_494 - 2_646_272
    assert econ["cost_usd"] == pytest.approx(fresh / 1e6 * 2.0 + 2_646_272 / 1e6 * 0.10 + 37_322 / 1e6 * 10)
    assert econ["pricing_as_of"] == "2026-10-14"


def test_build_trial_economics_no_trajectory_is_empty(tmp_path: Path) -> None:
    econ = build_trial_economics(tmp_path, "gpt-6.1-sol", load_pricing())
    assert econ["token_usage"] is None
    assert econ["cost_usd"] is None
    assert econ["pricing_as_of"] is None


def test_build_trial_economics_unpriced_model_keeps_tokens(tmp_path: Path) -> None:
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "trajectory.json").write_text(json.dumps(CODEX_TRAJ))
    econ = build_trial_economics(tmp_path, "unpriced", load_pricing())

    assert econ["token_usage"]["total_tokens"] == 2_854_816  # tokens still computed
    assert econ["cost_usd"] is None


def test_build_trial_economics_has_no_scalar_efficiency_keys(tmp_path: Path) -> None:
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "trajectory.json").write_text(json.dumps(CODEX_TRAJ))
    econ = build_trial_economics(tmp_path, "gpt-6.1-sol", load_pricing())
    assert "token_efficiency" not in econ  # removed: arbitrary zero → use Pareto front
    assert "cost_efficiency" not in econ


CURSOR_TRAJ = {
    "final_metrics": {
        "total_prompt_tokens": 1_000_000,
        "total_completion_tokens": 20_000,
        "total_cached_tokens": 900_000,
        "total_cost_usd": 0.42,
        "extra": {
            "usage_totals": {
                "inputTokens": 60_000,
                "outputTokens": 20_000,
                "cacheReadTokens": 900_000,
                "cacheWriteTokens": 40_000,
            },
            "cost_source": "litellm",
        },
    }
}

COMPOSER_PRICE = ModelPrice(input_per_1m=0.5, output_per_1m=2.5, cached_input_per_1m=0.2, as_of="2026-11-23")


def _write_traj(trial_dir: Path, traj: dict) -> None:
    (trial_dir / "agent").mkdir()
    (trial_dir / "agent" / "trajectory.json").write_text(json.dumps(traj))


def test_extract_cursor_reads_cache_writes_from_usage_totals() -> None:
    usage = extract_token_usage(CURSOR_TRAJ)
    assert usage is not None
    assert usage.cache_write_tokens == 40_000
    assert usage.cached_tokens == 900_000


def test_build_trial_economics_catalog_priced_has_pricing_toml_source(tmp_path: Path) -> None:
    _write_traj(tmp_path, CODEX_TRAJ)
    econ = build_trial_economics(tmp_path, "gpt-6.1-sol", load_pricing())
    assert econ["cost_source"] == "pricing.toml"


def test_build_trial_economics_cursor_catalog_price_wins_over_harbor(tmp_path: Path) -> None:
    _write_traj(tmp_path, CURSOR_TRAJ)
    econ = build_trial_economics(tmp_path, "cursor/composer-2.5", {"cursor/composer-2.5": COMPOSER_PRICE})
    fresh = 1_000_000 - 900_000 - 40_000
    expected = fresh / 1e6 * 0.5 + 40_000 / 1e6 * 0.5 + 900_000 / 1e6 * 0.2 + 20_000 / 1e6 * 2.5
    assert econ["cost_usd"] == pytest.approx(expected)
    assert econ["cost_source"] == "pricing.toml"
    assert econ["pricing_as_of"] == "2026-11-23"


def test_build_trial_economics_unpriced_cursor_falls_back_to_harbor(tmp_path: Path) -> None:
    _write_traj(tmp_path, CURSOR_TRAJ)
    econ = build_trial_economics(tmp_path, "cursor/some-new-model", {})
    assert econ["cost_usd"] == pytest.approx(0.42)
    assert econ["cost_source"] == "harbor:litellm"
    assert econ["pricing_as_of"] is None
    assert econ["token_usage"]["cache_write_tokens"] == 40_000


def test_build_trial_economics_unpriced_cursor_without_harbor_cost_is_none(tmp_path: Path) -> None:
    traj = json.loads(json.dumps(CURSOR_TRAJ))
    del traj["final_metrics"]["total_cost_usd"]
    _write_traj(tmp_path, traj)
    econ = build_trial_economics(tmp_path, "cursor/some-new-model", {})
    assert econ["cost_usd"] is None
    assert econ["cost_source"] is None


def test_build_trial_economics_unpriced_non_cursor_ignores_harbor_cost(tmp_path: Path) -> None:
    _write_traj(tmp_path, CURSOR_TRAJ)
    econ = build_trial_economics(tmp_path, "claude-haiku-5-5", {})
    assert econ["cost_usd"] is None
    assert econ["cost_source"] is None
