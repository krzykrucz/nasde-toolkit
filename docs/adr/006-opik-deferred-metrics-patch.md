# ADR-006: Runtime monkey-patch for Opik Harbor token usage

**Status:** Accepted
**Date:** 2026-03-26

## Context

Opik's Harbor integration (`opik.integrations.harbor.opik_tracker._patch_step_class`) patches `Step.__init__` to read `self.metrics` and create Opik spans with `usage` data (token counts). However, Harbor's `ClaudeCode._convert_event_to_step()` creates `Step(...)` without `metrics`, then assigns `step.metrics = metrics` after construction. This means the Opik patch always sees `self.metrics = None`, all spans have `usage=None`, and the Opik backend has nothing to aggregate into the trace's "Total tokens" column.

This bug was originally fixed in the SDLC repo (commit `66c2c11`, 2026-03-09) via a vendor `.patch` file applied to the installed opik package (`evals/eval-platforms/patches/opik_harbor_deferred_metrics.patch`). When the evaluation infrastructure was extracted into nasde-toolkit (SDLC commit `74bfb06`, 2026-03-20), the patch was not ported.

As of opik 1.10.50 (latest), this bug remains unfixed upstream.

## Decision

Apply the fix as a **runtime monkey-patch** in `runner.py` instead of a vendor `.patch` file. The patch is applied immediately after `track_harbor()` and re-patches `Step.__init__` + adds `Step.__setattr__`:

- `__init__`: stashes Opik trace context on the Step instance; emits spans immediately for non-agent steps (which never receive metrics).
- `__setattr__`: when `metrics` is assigned, creates the Opik span with token usage data at that point.
- The re-patch wraps Harbor's **original** `Step.__init__` (`Step.__init__.__wrapped__`, exposed by opik's `functools.wraps`), so it *replaces* opik's eager span instead of stacking on it. Wrapping opik's patched `__init__` emitted every step twice — and for agents that pass `metrics` to the constructor (Codex), both copies carried usage, doubling Opik's token totals. Guarded by `tests/test_opik_step_patch.py` (2026-10, PR #88).

## Rationale

- **Survives `uv sync`**: vendor `.patch` files must be re-applied after every dependency install. Runtime patches are applied each run automatically.
- **No external scripts**: the SDLC approach required `apply_opik_patches.sh` — easy to forget after `uv sync`.
- **Same fix, different delivery**: the logic is identical to the SDLC `.patch` file; only the application mechanism differs.

## Consequences

- The patch must be maintained alongside opik upgrades. If opik changes `_patch_step_class` internals, our re-patch may need adjustment.
- Remove when: opik upstream fixes the timing issue (check changelog for "harbor" + "metrics" or "deferred" mentions).

## 2026-04-22 update: still required under opik 2.x

Opik 2.0.9 was inspected and still contains the same race condition in
`opik.integrations.harbor.opik_tracker._patch_step_class.patched_init` —
`self.metrics` is read synchronously during `Step.__init__`, before Harbor
assigns it. The only 2.x-relevant change is the internal API rename:
`opik_client.get_client_cached()` → `opik.get_global_client()`, and
`client.span(...)` → `client.__internal_api__span__(...)`. The runtime
monkey-patch in `runner.py` was adapted accordingly; semantics unchanged.

The `__internal_api__span__` double-underscore naming is a deliberate
"internal, may change" signal from Opik maintainers. Re-verify this patch
at every opik minor bump.

## 2026-11-20 update: session-level metrics (Cursor CLI)

Harbor's `CursorCli` never assigns `step.metrics`: `cursor-agent` reports
usage and cost only once per session, so the totals live on
`Trajectory.final_metrics`. Under the deferred patch its agent steps waited
for a `metrics` assignment that never came, so no agent step reached Opik
and the trace's cost and tokens were empty. The patch now also wraps
`Trajectory.__init__`, which runs inside the trial's trace context. It flushes
agent steps that are still pending, and when no step carried usage it emits a
single `agent_session` LLM span with the session's usage and
`total_cost = final_metrics.total_cost_usd`. Agents with per-step usage
(Claude, Codex) are unaffected, and the span is emitted at most once per
trace.
