# Cursor CLI Support

**Date:** 2026-11-19
**Status:** Implemented — [#2], [#3], [#4], [#7], [#8] (updated 2026-11-23 to
match what shipped and to record live verification; see
[Changes after the design review](#changes-after-the-design-review) and
[Known gaps](#known-gaps-as-of-2026-11-23))

## Problem

nasde benchmarks three agent families (Claude Code, Codex, Gemini CLI) and judges
with two evaluator backends (`claude`, `codex`). Cursor's `cursor-agent` CLI is
neither a benchmarkable agent nor a judge option, so Cursor-hosted models
(Composer) and Cursor's agent harness cannot be compared against the
other families.

Harbor 0.24 (already a nasde dependency) ships a working `CursorCli` agent
(`harbor/agents/installed/cursor_cli.py`): it installs `cursor-agent`, runs it with
`--yolo --print --output-format=stream-json`, parses the stream into an ATIF
trajectory, supports `reasoning_effort` (`low|medium|high|xhigh|max`, rendered as
`model[effort=X]`), native skills (`config.agent.skills` → `~/.cursor/skills`), and
MCP servers (`~/.cursor/mcp.json`). What is missing is the nasde layer around it.

## Scope

Both halves, in one design:

1. **Cursor as a benchmarked agent** — variant `agent = "cursor"`.
2. **Cursor as an evaluator backend** — `[evaluation] backend = "cursor"`.

Plus the token/cost economics needed so Cursor trials get a non-null `cost_usd`.

Out of scope: injecting the host `cursor-agent login` token into the sandbox
(API key only), `.cursor/rules/` support, long-context price premiums, an Opik
`provider` attribution fix. (Opik *cost and token* reporting for Cursor traces
was not in the original scope either; after the agent shipped, Cursor traces
turned out to carry no cost or tokens, so it was added in [#8] — see §4.)

## Decisions

| Question | Decision |
|---|---|
| Sandbox auth | `CURSOR_API_KEY` only (env or `.env`); clear error if missing. Cursor user API keys bill against the Cursor plan, so there is no subscription-vs-API split to work around. |
| Instruction file | `AGENTS.md` → `/app/AGENTS.md` (same as Codex; `cursor-agent` reads it from repo root). |
| Skill snapshots | Reuse `agents_skills/` (a Codex variant becomes a Cursor variant by changing one line). |
| Cost source of truth | nasde's catalog; Harbor's reported cost recorded alongside for audit. |
| Model name | Provider-qualified `cursor/<slug>` end-to-end (the runner adds the prefix, so a variant may write a bare slug). |
| Price lookup | `cursor/<slug>` → `cursor/<base>` → `<slug>` → `<base>`; bundled `cursor/` entries only where Cursor's rate differs from the vendor's (Composer, and every Claude model with a cache-write rate). |
| Approach | Thin wrappers on Harbor's `CursorCli` + a layered slug lookup in pricing + a third subprocess judge backend. |

Fallback: if the live smoke test shows Harbor's install-time skill registration
is not discovered by `cursor-agent` (the ADR-012 timing risk), replace the wrapper
with a nasde-owned agent on `BaseInstalledAgent` that registers skills inside `run()`.
**Resolved 2026-11-23: no fallback needed.** In a live run of
`cursor-ntcoding-tactical-ddd`, the agent read
`/root/.cursor/skills/tactical-ddd/SKILL.md` in its first turn, without
searching for it, on all four trials. `AGENTS.md` names the skill but gives no
path, so Cursor lists the install-time-registered skill natively.

## Design

### 1. Benchmarked agent

**New `src/nasde_toolkit/agents/configurable_cursor.py`**, modelled on
`configurable_gemini.py`:

- `ConfigurableCursorOptions(CursorCliOptions)` declares `sandbox_files`
  (Harbor's preflight validates kwargs against `options_model` with
  `extra="forbid"`).
- `ConfigurableCursor(CursorCli)`:
  - `setup()` uploads `sandbox_files` then calls `super().setup()`.
  - `name()` → `"configurable-cursor-cli"`.
  - Overrides `build_cli_flags()` to append `--approve-mcps` when
    `self.mcp_servers` is non-empty — Harbor writes `~/.cursor/mcp.json` but
    never approves the servers, and a `--print` run cannot answer the prompt.

**Runner wiring (`runner.py`):**

- `_VALID_AGENT_TYPES` gains `"cursor"`.
- `_agent_import_path("cursor")` →
  `nasde_toolkit.agents.configurable_cursor:ConfigurableCursor`.
- New `_is_cursor_agent()`; `_agent_type_from_import_path` returns `"cursor"`.
- **One classifier.** Auth (`_ensure_auth`), skill routing and model
  qualification all branch on `_agent_type_from_import_path`, not on separate
  `_is_*_agent` substring checks. An import path that matches two families
  (e.g. `gemini_cursor_bridge.agent:BridgeAgent`) is therefore classified once,
  the same way everywhere, instead of being checked for Gemini auth but
  qualified as Cursor.
- `_build_merged_config` qualifies a Cursor agent's `model_name` with
  `_qualify_model_name`: a bare slug (`composer-2.5`, from `variant.toml` or
  `--model`) becomes `cursor/composer-2.5`, which is the `provider/model` form
  Harbor's `CursorCli._model_arg()` requires. Any other provider prefix is
  **replaced**, not kept: `anthropic/claude-opus-5-5-high` becomes
  `cursor/claude-opus-5-5-high`, so a Cursor trial always reaches the `cursor/`
  price entries. A name that already starts with `cursor/` is unchanged. The
  qualified name is what lands in `config.json`, the trajectory,
  `assessment_summary.json` and `metrics.json`; the run-summary table strips
  `cursor/` for display, as it already strips `google/`.
- The `nasde run` header labels the agent "Cursor" (it showed "Claude Code"
  until [`9d3254d`](https://github.com/krzykrucz/nasde-toolkit/commit/9d3254d)).
- `_AGENT_SKILLS_SUBDIR["cursor"] = "agents_skills"` — snapshot skills,
  `[[skill]]` references and `[nasde.plugin]` skills all flow through the
  existing native `config.agent.skills` path (ADR-012), unchanged.
- `AGENTS.md` → `/app/AGENTS.md` already works via `_collect_sandbox_files`.
- `_ensure_auth`: a Cursor agent requires `CURSOR_API_KEY`; otherwise print
  `ERROR: Set CURSOR_API_KEY (create a User API key in the Cursor dashboard)`
  and `SystemExit(1)`.
- Run-summary agent-name prefix stripping gains `"cursor-"`.

**Effort.** Variants use a slug exactly as `cursor-agent --list-models` prints it
(e.g. `claude-opus-5-5-high`, which embeds effort). The optional
`reasoning_effort` is still threaded through unchanged and Harbor renders it as
`[effort=X]`; docs say to use one or the other. No local validation (consistent
with `_resolve_effort`'s policy). When effort lives in the slug, the
`reasoning_effort` stamp is `""` — grouping still separates configurations
because the `model_name` differs.

**Variant example:**

```toml
agent = "cursor"
model = "composer-2.5"
```

**Known limitation.** Harbor's `~/.cursor/mcp.json` writer drops `env` for stdio
servers. `[nasde.plugin]` servers are unaffected (nasde env-wraps them into a
script); a hand-declared stdio server relying on `env` loses it. Documented, not
worked around.

### 2. Token and cost economics

All changes sit in the single extractor / single price lookup, so the run path
(`assessment_summary.json`) and export path (`metrics.json`) stay in agreement
(ADR-011).

**Cache-write extraction (`token_metrics.extract_token_usage`).** Read
`final_metrics.extra.total_cache_creation_input_tokens`; if absent, fall back to
`final_metrics.extra.usage_totals.cacheWriteTokens` (Cursor's location). The other
fields already match nasde's definitions: Harbor's Cursor `total_prompt_tokens` =
fresh + cache reads + cache writes (= nasde `input`), `total_cached_tokens` =
cache reads, output = completion tokens (no separate reasoning count).

**Why a Cursor namespace.** Cursor bills third-party models at "API list price",
and on 2026-11-19 its OpenAI rates match nasde's vendor entries exactly. Its
Claude rates do not: Cursor bills cache writes at the 5-minute rate (1.25x input,
e.g. Opus 5.5 = $5/1M) while nasde's Claude entries use the 1-hour rate Claude
Code uses (2x, $8/1M). Pricing Claude-through-Cursor against the vendor entries
would overstate the cache-write component by ~60%. The same model can therefore
cost differently per harness, so Cursor-specific rates live under a `cursor/`
key prefix.

**Layered key lookup (`pricing.resolve_catalog_key(model, catalog)`).** Used by
`compute_cost_usd`, `pricing_as_of`, the run-summary "Pricing used" table, and
`pricing_used.json`. Candidates, first hit wins:

1. the model name as recorded (`cursor/claude-opus-5-5-high`)
2. its effort-normalized form (`cursor/claude-opus-5-5`)
3. the name without its provider prefix (`claude-opus-5-5-high`)
4. that, effort-normalized (`claude-opus-5-5`)

Effort normalization (`pricing.normalize_model_slug`) strips bracket params
(`[effort=high]`, `[context=1m,...]`), then one trailing effort suffix from a
closed set (`-none|-minimal|-low|-medium|-high|-xhigh|-max`), optionally preceded
by `-thinking`, or a bare trailing `-thinking`:
`claude-fable-5-thinking-high` → `claude-fable-5`, `gpt-5.3-codex-xhigh` →
`gpt-5.3-codex`. `-fast` is deliberately not stripped (fast modes cost more).
For the same reason, a bracket group that contains `fast=true`
(`claude-opus-4-8[fast=true]`, `[effort=high,fast=true]`) leaves the slug
**untouched**, so it resolves as unpriced instead of silently taking the
standard rate; `fast=false` is stripped like any other bracket option.

No match → unpriced: `cost_usd = null` + the existing warning. It is not a
validation gate: a new Cursor suffix degrades to "unpriced", never blocks a run.
It applies to every agent; a bare `claude-opus-5-5` from Claude Code resolves on
the first candidate and never reaches a `cursor/` entry. As a side effect,
`google/gemini-*` names now fall through to an unprefixed entry if one is added.

Wherever the effective catalog is shown, a non-exact match is labelled, e.g.
`cursor/claude-opus-5-5-high (priced as cursor/claude-opus-5-5)`;
`pricing_used.json` gains a `catalog_key` field per model.

**Bundled `cursor/` entries** (`as_of = "2026-11-19"`,
`source = "https://cursor.com/docs/models-and-pricing"`), per 1M tokens.
Composer, plus a `cursor/` twin of **every** Claude entry in the catalog that has
a cache-write rate. The twin has the same input, cache-read and output rates as
the vendor entry, with cache writes at 1.25x input. The design review listed only
the four current Claude models; the rest were added because a Cursor trial on an
older Claude model would otherwise fall through to the vendor's 2x cache-write
rate. A test derives the expected set from the catalog, so a new Claude entry
without a `cursor/` twin fails CI.

| Key | Input | Cache write | Cache read | Output |
|---|---|---|---|---|
| `cursor/composer-2.5` | 0.5 | (omitted → input rate) | 0.2 | 2.5 |
| `cursor/claude-opus-5-5` | 4.0 | 5.0 | 0.2 | 20.0 |
| `cursor/claude-sonnet-5-5` | 2.0 | 2.5 | 0.1 | 10.0 |
| `cursor/claude-fable-5` | 10.0 | 12.5 | 1.0 | 50.0 |
| `cursor/claude-fable-5-1` | 10.0 | 12.5 | 0.25 | 50.0 |
| `cursor/claude-opus-5` | 5.0 | 6.25 | 0.5 | 25.0 |
| `cursor/claude-opus-4-8`, `-4-7`, `-4-6`, `-4-5` | 5.0 | 6.25 | 0.5 | 25.0 |
| `cursor/claude-sonnet-5` | 2.0 | 2.5 | 0.2 | 10.0 |
| `cursor/claude-sonnet-4-6`, `-4-5` | 3.0 | 3.75 | 0.3 | 15.0 |
| `cursor/claude-haiku-4-5` | 1.0 | 1.25 | 0.1 | 5.0 |

OpenAI models through Cursor fall through to the existing vendor entries.
`auto` stays unpriced: Cursor bills it as whatever model serves the request, so
there is no per-token rate. Grok, Composer Fast, Gemini 4 and Muse are not added
(not offered to the reference account / no vendor entry); users add them via a
`pricing.toml` override.

**Audit fields.** The per-trial economics block gains
`agent_reported_cost_usd` (from `final_metrics.total_cost_usd`) and
`agent_reported_cost_source` (from `final_metrics.extra.cost_source`, e.g.
`cursor_cli`, `cursor_pricing`, `litellm`; `null` when absent). Generic across
agents. Recorded only — `cost_usd` (catalog) remains the source of truth and the
run-summary table is unchanged.

**Known limitations.** Team and Enterprise plans add a $0.25/1M "Cursor Token
Rate" on eligible third-party models; individual plans do not pay it and it is not
modelled. Models with long-context surcharges (GPT-5.x above 272K input) are
understated on long requests, the same limitation as the Codex entries.

### 3. Evaluator backend (`evaluator_backends/cursor_subprocess.py`)

`CursorSubprocessBackend` implements the existing `EvaluatorBackend` protocol and
is selected by `[evaluation] backend = "cursor"`.

**Command:**

```
cursor-agent -p --output-format json --mode ask --trust \
  --model <model> --workspace <tmp> --add-dir <trial workspace> \
  [--add-dir <trial dir>] [--approve-mcps]
```

- **Read-only via `--mode ask`.** Cursor has no `--allowedTools` equivalent; if
  `allowed_tools` is set, print a yellow warning that it is ignored on this
  backend — **once per process**, not once per evaluation (a job runs
  `trials × eval_repetitions` evaluations). This departs from the Claude
  backend, which enforces the list (gap G2).
- **Always a temporary `--workspace`.** Unlike the Claude backend (temp dir only
  when skills are configured), Cursor may write `.cursor/` state into its
  workspace; anything written into the trial workspace would leak into
  `changes.patch` and calibration PRs. The temp dir holds `.cursor/skills/`
  (copied from `skills_dir`) and `.cursor/mcp.json` (copied from `mcp_config`;
  same `mcpServers` shape as Claude's; the server loads, but its tools can't be
  called yet — gap G1). Removed in `finally`, and also when
  populating it fails (e.g. a missing `mcp_config` file), so a bad config never
  leaks `nasde_eval_cursor_*` dirs into the temp folder.
- **Absolute paths in the prompt.** The judge's cwd is that empty temp dir, so a
  prompt saying "analyze the current working directory" points at nothing.
  Whenever the judge runs outside the trial workspace (`backend = "cursor"`, or
  `skills_dir` set on any backend, which also uses a temp cwd), the evaluator
  hands it the **absolute** workspace path. The trajectory path is now always
  absolute (`<trial>/agent/trajectory.json`), replacing the
  `../../agent/trajectory.json` relative path, which only resolved from the
  workspace itself.
- **Trial dir** via `--add-dir` under the same rule as Claude: when
  `include_trajectory` is on or `agent_changes.diff` exists.
- **`append_system_prompt`** has no flag → prepended to the prompt as a preamble.
- **`max_turns`** has no equivalent → documented as unsupported on this backend.
- **Result** from the JSON output's `result` field; `is_error: true` or a
  non-zero exit code raises `RuntimeError` with the stderr/stdout tails (same
  shape as the other backends).

**Auth (host).** `CURSOR_API_KEY` if set; otherwise the host `cursor-agent` login,
checked by `cursor-agent status`. Neither → error pointing at `cursor-agent login`.
The `status` check runs once per process and its result is cached, instead of
spawning a subprocess before every evaluation. An `OSError` or a 30 s timeout
counts as "not logged in".
`validate_cli_installed` checks `shutil.which("cursor-agent")` with an install hint
and the "set a different backend or pass `--without-eval`" escape hatch.

**Default model.** When `backend = "cursor"` and `[evaluation] model` is unset, the
default is `claude-opus-5-5-high` (the Claude backend's `claude-opus-5-5` is not a
valid Cursor slug). "Unset" means the `model` key is absent from the
`[evaluation]` table: the backend-aware default is chosen in `config.py` while
parsing, so an explicit `model = "claude-opus-5-5"` is passed through untouched
(and rejected by `cursor-agent` itself if invalid, consistent with the
no-local-validation policy).

*Observed after release:* `cursor-agent` **accepted** a bare `claude-opus-5-5`.
The 2026-11-20 example run with `backend = "cursor"` and
`model = "claude-opus-5-5"` completed and scored every trial. So the premise
above ("not a valid Cursor slug") is too strong. The default stays
`claude-opus-5-5-high` because it is the slug `cursor-agent --list-models`
prints and pins the effort explicitly. On 2026-11-23, `--list-models` showed
only effort-suffixed Claude 5.5 slugs (`claude-opus-5-5-low|medium|high`,
labelled "Claude Opus 5.5 1M"; `claude-sonnet-5-5-low|medium|high`), yet a bare
`claude-sonnet-5-5` was also accepted. Which effort Cursor applies to a bare
slug was not checked (gap G10).

Because `nasde init` writes an explicit `model = "claude-opus-5-5"` into
`[evaluation]`, the backend-aware default never applies to a scaffolded
project. The scaffold's commented `backend` line therefore tells Cursor users to
set the model themselves (`for "cursor", also set model = "claude-opus-5-5-high"`)
rather than claiming the model is defaulted.

**Evaluator-model stamp.** Cursor-judged evaluations are stamped
`evaluator_model = "cursor/<model>"` (the bare slug is still what is passed to
`--model`; a model that already contains `/` is left as is). Without the prefix,
a Cursor judge and a Claude judge configured with the same model name
(`claude-opus-5-5`) would be averaged into one cluster even though they are
different harnesses. With it, they cluster separately under the existing
clustering contract.

**Wiring.** `create_backend()` gains the `cursor` branch; the "Supported:" error
lists `'claude', 'codex', 'cursor'`.

**Prompt** is passed as the final positional argument after `--` (as Harbor's
`CursorCli` does); judge prompts are tens of KB, well under macOS `ARG_MAX`.

**Host auth check:** `cursor-agent status` exits 0 whether or not a user is
logged in, so the check parses its output (`Logged in as ...` vs `Not logged in`).

**Live checks:**

- **2026-11-20** (`examples/ddd-architectural-challenges`, `skills_dir` set, no
  `mcp_config`): `--mode ask` reads files across the `--add-dir` roots. Every
  trial was scored from the workspace plus `agent_changes.diff` in the trial
  dir, and `git status` in the trial workspace shows no `.cursor` leak.
- **2026-11-23, canary probe** through `CursorSubprocessBackend.run_evaluation`:
  an evaluator skill holding a secret phrase, and a stdio MCP server whose one
  tool, `canary_token`, returns a secret token.
  - **Evaluator skills work.** The skill appeared in the judge's native skill
    list and the judge returned the phrase.
  - **MCP servers load but their tools can't be called** (gap G1).
    `cursor-agent` loads the server from the temp workspace's
    `.cursor/mcp.json`, approves it via `--approve-mcps`, and lists its tool as
    ready. It then rejects the call with "User rejected MCP:
    nasde-canary-canary_token". Dropping `--mode ask` changes nothing. The call
    succeeds with `--force` (unsafe for a judge), or with a project
    `.cursor/cli.json` of `{"permissions": {"allow": ["Mcp(nasde-canary:*)"]}}`,
    which keeps `--mode ask`.
  - **The judge sees the host** (gap G3): about 90 of the user's global skills,
    every Cursor plugin and its MCP servers, including a ready GitHub MCP that
    exposes write tools.
- **2026-11-23, real eval** of the `cursor-ntcoding-tactical-ddd` job with
  `--eval-backend cursor` (`claude-opus-5-5-high`, 3 repetitions): every trial
  scored, stamped `cursor/claude-opus-5-5-high`, no `.cursor` leak, no leaked
  temp workspaces.

### 4. Opik reporting for Cursor traces ([#8])

nasde's ADR-006 patch defers each Harbor `Step` span until Harbor assigns the
step's `metrics`. Cursor's `CursorCli` never assigns per-step metrics; usage and
cost exist only on the trajectory's `final_metrics`. Under `--with-opik`, Cursor
agent steps therefore waited forever and the Opik trace had no tokens and no
cost.

`_patch_opik_deferred_metrics` now also patches `Trajectory.__init__`:

- **Flush pending steps.** Agent steps still waiting for metrics are emitted
  when the trajectory is built, so they appear in the trace.
- **One `agent_session` span.** When no step carried token usage and
  `final_metrics` has usage or `total_cost_usd`, one `llm` span named
  `agent_session` is emitted. It carries `prompt_tokens` / `completion_tokens` /
  `total_tokens`, `total_cost` (Cursor's own figure), the model, the session id
  and `cost_source` in metadata. It is emitted at most once per trace.
- Harnesses that do report per-step usage (Claude, Codex) are unaffected: the
  session span is skipped when any step has usage, so tokens are never counted
  twice.

This is Opik-only. nasde's own `assessment_summary.json` / `metrics.json`
economics come from `final_metrics` through `token_metrics.py` and were already
correct.

## Testing

Unit tests (fakes only, no live calls):

- `tests/test_configurable_cursor.py` (new): options model accepts
  `sandbox_files`; `--approve-mcps` only with MCP servers; `setup()` uploads
  `sandbox_files` before Harbor's setup.
- `tests/test_configurable_agents_options.py`: Cursor passes Harbor's preflight.
- `tests/test_runner.py`, `tests/test_runner_skills.py`: `agent = "cursor"`
  accepted; import path; `AGENTS.md` mapped; `agents_skills/`, `[[skill]]`, and
  plugin skills land in `config.agent.skills`; `_ensure_auth` with/without
  `CURSOR_API_KEY`; a bare Cursor model becomes `cursor/<slug>` in the merged
  config while other agents' models are untouched.
- `tests/test_token_metrics.py`: Cursor-shaped trajectory yields
  `cache_write_tokens` from `usage_totals`; `agent_reported_cost_*` populated.
- `tests/test_pricing.py`: the four-candidate lookup order; exact match wins
  (incl. full-slug project override); table-driven suffix/bracket stripping;
  unknown suffix → `None`; `cursor/claude-opus-5-5` bills cache writes at 1.25x
  while bare `claude-opus-5-5` keeps 2x.
- `tests/test_evaluator_backends.py`: command contains `--mode ask` and a temp
  `--workspace`; `--add-dir` rules; `--approve-mcps` with `mcp_config`; temp dir
  cleaned up on success and failure; `allowed_tools` warning; preamble
  prepending; `result` extraction; `is_error` raises; factory branch.
- `tests/test_config.py`: cursor backend default model.

Added during review, beyond the list above:

- `tests/test_pricing.py`: `[fast=true]` brackets stay unpriced (`fast=false`
  is stripped); the `cursor/` Claude twins are checked against every Claude
  catalog entry with a cache-write rate, derived from the catalog.
- `tests/test_runner.py`: `_qualify_model_name` replaces any provider prefix;
  an import path matching two families is classified once for auth, skills and
  model qualification.
- `tests/test_evaluator.py`: the Cursor (and `skills_dir`) prompt carries
  absolute workspace and trajectory paths; the `cursor/<model>` stamp, and
  Cursor and Claude judges on the same model never share a cluster.
- `tests/test_evaluator_backends.py`: `cursor-agent status` runs once per
  process, `OSError`/timeout means "not logged in", the `allowed_tools`
  warning prints once, and the temp workspace is removed when preparation fails
  and on `is_error` / non-zero exit.
- `tests/test_scaffold.py`: the commented `backend` line tells Cursor users to
  set the model.
- `tests/test_opik_step_patch.py` ([#8]): Cursor agent steps are flushed and one
  `agent_session` span carries session usage and cost; never duplicated.

Live smoke (each LLM run individually authorized by the owner):

1. A throwaway `cursor-skill-canary` variant (not committed; the shipped example
   is a plain `cursor-vanilla`) on `composer-2.5` with a canary skill in
   `agents_skills/`; one `nasde run` at `--eval-repetitions 1`. Pass: reward
   written, `trajectory.json` present, `cost_usd` non-null,
   `cache_write_tokens > 0`, canary skill natively invoked. A skill failure
   triggers the approach-B fallback.
2. `nasde eval` with `backend = "cursor"` against that job.

What actually ran (2026-11-20): instead of the canary, `cursor-vanilla` on
`composer-2.5` across the four `ddd-architectural-challenges` tasks, judged by
`backend = "cursor"` with `model = "claude-opus-5-5"`, 3 repetitions.

- Passed: reward written (1 on all four tasks); `trajectory.json` present;
  `model_name = "cursor/composer-2.5"`; `cost_usd` non-null and equal to the
  harness's own `agent_reported_cost_usd` (`cost_source = "cursor_pricing"`),
  e.g. $0.1226 on `ddd-threshold-discount`; evaluations stamped
  `cursor/claude-opus-5-5`; no `.cursor` leak in the workspace.
- Criterion did not hold, and does not apply: `cache_write_tokens > 0`.
  Cursor reported `usage_totals.cacheWriteTokens = 0` for Composer (only cache
  reads). A direct `cursor-agent -p --output-format json` call on
  `claude-sonnet-5-5` (2026-11-23) reported `usage.cacheWriteTokens = 12870` and
  `cacheReadTokens = 9356`. That is the field the extractor reads, so the
  cache-write path is correct for Claude models.

What ran on 2026-11-23: `cursor-ntcoding-tactical-ddd` on `composer-2.5`, all
four tasks (job `2026-11-23__12-50-46__cursor-ntcoding-tactical-ddd__skillcheck`).

- **Native skill registration verified** (see the Decisions fallback note).
  The ADR-012 timing risk does not apply: Harbor's install-time copy into
  `~/.cursor/skills` is discovered.
- Reward 1 on all four. `cost_usd` equals Cursor's `cursor_pricing` figure on
  every trial ($0.15, $0.12, $0.18, $0.40).
- The injected `/app/AGENTS.md` shows up in `agent_changes.diff` (gap G6).

Quality gates per `nasde-dev`: `ruff check`, `ruff format --check`, `mypy`, `pytest`.

## Documentation

- `CHANGELOG.md` `[Unreleased]` — Added: Cursor agent, Cursor judge backend,
  `cursor/` pricing entries, `agent_reported_cost_*` audit fields. Changed:
  price lookup falls back through effort suffixes and provider prefixes. Fixed:
  Cursor cache writes counted in cost.
- `docs/adr/015-cursor-cli-agent-and-judge.md` — thin wrapper, `cursor/`-qualified
  model names, harness-scoped price keys + layered lookup, `--mode ask` + temp
  workspace judge, key-only sandbox auth.
- `README.md`, `CLAUDE.md`, `ARCHITECTURE.md` — agent lists, auth, backend lists,
  package structure.
- Skills: `nasde-benchmark-runner` (`CURSOR_API_KEY`, `--list-models`),
  `nasde-benchmark-creator` (Cursor variant example), `nasde-dev` (Cursor auth in
  the e2e step).
- Scaffold: `NASDE_TOML_TEMPLATE` gains a commented
  `# backend = "claude"  # "claude" | "codex" | "cursor" (for "cursor", also set model = "claude-opus-5-5-high")`
  line in `[evaluation]`.
- Examples: `cursor-vanilla`, plus `cursor-guided` and
  `cursor-ntcoding-tactical-ddd` ([#7]) mirroring the Codex variants (the latter
  ships the `tactical-ddd` skill under `agents_skills/`), all on `composer-2.5`.

## Rollout

Planned as three PRs; shipped as five on `krzykrucz/nasde-toolkit`:

1. **Economics** — [#2]: `resolve_catalog_key` layered lookup, `cursor/`
   entries, cache-write fallback, audit fields, ADR-015.
2. **Cursor agent** — [#3]: `ConfigurableCursor` + runner wiring +
   `cursor-vanilla`. Built on the #2 branch and squash-merged into `main` after it.
3. **Cursor judge** — [#4]: `CursorSubprocessBackend`, backend-aware default
   model, absolute prompt paths, `cursor/<model>` stamp.
4. **Examples** — [#7]: `cursor-guided`, `cursor-ntcoding-tactical-ddd`.
5. **Opik** — [#8]: Cursor agent steps and the `agent_session` cost span (§4).

#2, #3, #4 and #7 each carry their own CHANGELOG entry and doc updates.

## Changes after the design review

Everything below is already folded into the sections above; this list is the
index.

| Area | Design review said | Shipped | PR |
|---|---|---|---|
| Slug normalization | Strip any trailing bracket group | A bracket with `fast=true` keeps the slug intact → unpriced | [#2] |
| `cursor/` price entries | Composer + four current Claude models | Composer + a twin for every Claude entry with a cache-write rate (13), guarded by a catalog-derived test | [#2] |
| Model qualification | Bare slug → `cursor/<slug>`; qualified passes through | Any provider prefix replaced by `cursor/` | [#3] |
| Agent classification | Separate `_is_*_agent` checks | One classifier (`_agent_type_from_import_path`) for auth, skills and model | [#3] |
| Run header | — | Labels Cursor variants "Cursor" | `9d3254d` |
| Judge prompt | Relative paths | Absolute workspace path when the judge runs outside the workspace; trajectory path always absolute | [#4] |
| Judge stamp | `evaluator_model = <model>` | `cursor/<model>` for the Cursor backend | [#4] |
| Judge auth / warnings | Per evaluation | `cursor-agent status` cached and `allowed_tools` warning printed once per process | [#4] |
| Judge temp workspace | Removed in `finally` | Also removed when preparing it fails | [#4] |
| Scaffold comment | Mentions the cursor default model | Tells Cursor users to set the model (the scaffold's explicit model bypasses the default) | [#4] |
| Bare `claude-opus-5-5` on Cursor | Rejected by `cursor-agent` | Accepted in the live run; default kept as `-high` | observed |
| Opik | Out of scope | Session span with Cursor usage and cost | [#8] |
| Examples | `cursor-vanilla` | + `cursor-guided`, `cursor-ntcoding-tactical-ddd` | [#7] |
| Skill canary | Gate for approach B | Verified live 2026-11-23 with `cursor-ntcoding-tactical-ddd`; approach B not needed | observed |
| Judge MCP | `.cursor/mcp.json` + `--approve-mcps` is enough | Server loads but every tool call is rejected (G1) | observed |

## Known gaps (as of 2026-11-23)

Measured against the Claude judge and the Claude/Codex agent paths. "Confirmed"
means reproduced live on 2026-11-23 with `cursor-agent` 2026.11.22-e9042b2;
"inferred" means read from code or docs and not reproduced.

| # | Gap | Status | Evidence | Direction |
|---|---|---|---|---|
| G1 | **Judge MCP tools are unusable.** `[evaluation] mcp_config` loads the server, but `cursor-agent -p` rejects every tool call. `--approve-mcps` approves servers, not tool calls. | Confirmed | Canary probe: "User rejected MCP: nasde-canary-canary_token", with and without `--mode ask` | Write `.cursor/cli.json` into the temp workspace with an `Mcp(<server>:<tool>)` allow rule per permitted tool. Tested: works under `--mode ask`. Never `--force` (it lifts every restriction). |
| G2 | **`allowed_tools` is ignored.** On Claude it is the judge's whitelist (default Read/Glob/Grep; MCP tools only when listed as `mcp__<server>__<tool>`, per the website's running-benchmarks guide). On Cursor it is dropped with a warning. | Confirmed (code) | `cursor_subprocess._warn_unsupported_options` | Same mechanism as G1: Read/Glob/Grep are what `--mode ask` already allows, `mcp__<server>__<tool>` maps to `Mcp(<server>:<tool>)`, and write/shell entries stay ignored with a warning. One `nasde.toml` then gives both judges the same tool set. |
| G3 | **The judge isn't isolated from the host.** It loads about 90 of the user's global skills plus every Cursor plugin and its MCP servers, including a ready GitHub MCP that exposes write tools (`merge_pull_request`, `push_files`). Judge context therefore depends on the machine, which hurts reproducibility. | Confirmed | Canary probe listings | Calls to plugin MCP tools should hit the same rejection as G1 unless allowlisted (inferred, not tested). The Claude judge has the same property by design: it skips `--bare` to keep keychain OAuth and auto-discover user skills, and passes `--mcp-config` without `--strict-mcp-config`. Parity with Claude therefore means documenting this, not fixing it; isolating both judges is a separate design. |
| G4 | **No turn limit.** `max_turns` has no Cursor equivalent. | Confirmed (CLI help) | `cursor-agent --help` | Document. Optionally enforce a wall-clock timeout on the subprocess. |
| G5 | **Session state accumulates.** Each judge call creates a `~/.cursor/projects/<temp-path>` entry (110 after a few days). Claude uses `--no-session-persistence`; `cursor-agent` has no equivalent flag. | Confirmed | `ls ~/.cursor/projects` | Document; possibly delete the entry for the temp workspace after the call. |
| G6 | **Injected instruction files leak into the agent's diff.** `/app/AGENTS.md` (`sandbox_files`) is untracked in the trial workspace, so it appears in `agent_changes.diff` (judge input), `changes.patch` (export) and calibration PRs. | Confirmed for Cursor (all 4 trials) | `git status` / `agent_changes.diff` | Not Cursor-specific: the same `sandbox_files` path injects Codex `AGENTS.md`, Claude `CLAUDE.md` and Gemini `GEMINI.md` (inferred; no local job to confirm). Exclude injected sandbox paths from `capture_patch`. |
| G7 | **`append_system_prompt` is not a system prompt.** It is prepended to the user prompt; Claude uses `--append-system-prompt`. | Confirmed (code) | `_compose_prompt` | Document; no Cursor flag exists. |
| G8 | **`cursor-agent` ignored the host's `~/.cursor/mcp.json`** ("No MCP servers configured", even from `$HOME`), while the IDE uses it. | Confirmed on one machine | `cursor-agent mcp list` | Host-specific, probably a file the CLI can't parse. No nasde change; worth a troubleshooting note. |
| G9 | **Long-context pricing.** Cursor offers Opus 5.5 as a 1M-context model; any long-context premium is not modelled. | Inferred (model list label) | `cursor-agent --list-models` | Already out of scope; check Cursor's pricing page when the catalog is next refreshed. |
| G10 | **Effort of bare slugs is unknown.** `claude-opus-5-5` and `claude-sonnet-5-5` are accepted though not listed. Which effort they run at is unknown, and the `reasoning_effort` stamp stays `""`. | Confirmed (accepted) / unknown (effort) | 2026-11-20 eval, 2026-11-23 direct call | Prefer listed slugs in docs and examples (`-high`, …). |
| G11 | **Opik change has no CHANGELOG entry.** [#8] added only the `[#8]` link-ref, and put a stray indent inside the backticks of the #88 Opik entry. | Confirmed | `CHANGELOG.md` | Add an `[Unreleased]` → `Fixed` entry; fix the indent. |
| G12 | **Harbor drops `env` for stdio MCP servers** in the sandbox's `~/.cursor/mcp.json`. | Inferred (Harbor code, unchanged) | Known limitation above | Unchanged; `[nasde.plugin]` servers are env-wrapped. |
| G13 | **Not exercised live:** agent-side task MCP servers (should work: Harbor runs the agent with `--yolo`, and `ConfigurableCursor` adds `--approve-mcps`), the judge with `include_trajectory = true`, and an Opik trace for a Cursor trial. | Open | — | Cover in the next Cursor run that touches these paths. |

[#2]: https://github.com/krzykrucz/nasde-toolkit/pull/2
[#3]: https://github.com/krzykrucz/nasde-toolkit/pull/3
[#4]: https://github.com/krzykrucz/nasde-toolkit/pull/4
[#7]: https://github.com/krzykrucz/nasde-toolkit/pull/7
[#8]: https://github.com/krzykrucz/nasde-toolkit/pull/8
