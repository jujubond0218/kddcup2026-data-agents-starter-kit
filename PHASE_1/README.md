<div align="center">

# DataAgent-Bench Starter Kit

English | [中文](README.zh.md)

[![Official Website](https://img.shields.io/badge/Official%20Website-Visit%20dataagent.top-0ea5e9?style=for-the-badge&logo=googlechrome&logoColor=white&labelColor=0f172a)](https://dataagent.top)
[![Demo Dataset](https://img.shields.io/badge/Demo%20Dataset-Download%20Phase%201-f59e0b?style=for-the-badge&logo=googledrive&logoColor=white&labelColor=0f172a)](https://drive.google.com/file/d/1c6u5WlFw4KV7CBRyXh5BvFYbKqxhBSbL/view)
[![Discord](https://img.shields.io/badge/Discord-Join%20Community-5865F2?style=for-the-badge&logo=discord&logoColor=white&labelColor=0f172a)](https://discord.com/invite/7eFwJQN3Fx)

</div>

> Official starter kit for the KDD Cup 2026 DataAgent-Bench challenge. The repository reads tasks from `data/public/input/` and writes predictions for downstream evaluation.

## Overview

| Item | Value |
| --- | --- |
| Dataset input | `data/public/input/` |
| Public demo ground truth | `data/public/output/task_<id>/gold.csv` |
| Hidden test data | `input/` only, no `output/` |
| Entry command | `uv run dabench <command> --config PATH` |
| Default run output | `artifacts/runs/` |

## Quick Start

1. Install `uv` by following the official guide:
   - https://docs.astral.sh/uv/getting-started/installation/
2. On macOS and Linux, the standalone installer is:

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

3. Install project dependencies:

   ```bash
   uv sync
   ```

4. Confirm the dataset root is visible:

   ```bash
   uv run dabench status --config configs/react_baseline.example.yaml
   ```

5. Run the baseline:

   ```bash
   uv run dabench run-benchmark --config configs/react_baseline.example.yaml
   ```

## Dataset

The public demo dataset lives under `data/public/input/`. Each task directory follows this structure:

```text
data/public/input/task_<id>/
├── task.json
└── context/
```

The corresponding public demo answers live separately under `data/public/output/task_<id>/gold.csv`.
Hidden test sets only include `input/`, so there is no `output/` directory there.

`task.json` contains:

- `task_id`
- `difficulty`
- `question`

The `context/` directory may contain one or more of:

- CSV files
- JSON files
- SQLite / DB files
- Text documents

## Configuration

An example config file lives at `configs/react_baseline.example.yaml`.

```yaml
dataset:
  root_path: data/public/input

agent:
  model: YOUR_MODEL_NAME
  api_base: YOUR_API_BASE_URL
  api_key: YOUR_API_KEY
  max_steps: 20
  temperature: 0.0
  model_request_timeout_seconds: 20
  model_max_retries: 1
  model_retry_backoff_seconds: 1

run:
  output_dir: artifacts/runs
  run_id:
  max_workers: 2
  task_timeout_seconds: 120

explorer:
  enabled: true
  max_steps: 2
  max_duration_seconds: 60
  max_files: 64
  max_preview_calls: 2
  max_preview_chars: 2000
  max_inventory_chars: 12000
  max_report_chars: 4000

evidence_plan:
  enabled: false
  max_commit_attempts: 2
  strict_keys: true
  verification_gate: true
```

Config fields:

| Field | Meaning |
| --- | --- |
| `dataset.root_path` | Root directory of the public demo `input/` dataset. Relative paths are resolved from the project root. |
| `agent.model` | Model name. |
| `agent.api_base` | OpenAI-compatible API base URL. |
| `agent.api_key` | API key, read directly from the config file. |
| `agent.max_steps` | Maximum ReAct steps per task. |
| `agent.temperature` | Sampling temperature. |
| `agent.model_request_timeout_seconds` | Wall-clock timeout for one model request attempt. |
| `agent.model_max_retries` | Application-level retries for transient model API failures. |
| `agent.model_retry_backoff_seconds` | Base retry delay; exponential backoff and jitter are capped at five seconds. |
| `run.output_dir` | Output directory for run artifacts. |
| `run.run_id` | Optional run directory name. Defaults to a UTC timestamp if omitted. Required by `--resume`. |
| `run.max_workers` | Parallel worker count for `run-benchmark`. |
| `run.task_timeout_seconds` | Maximum wall-clock time per task. Set to `0` or a negative value to disable the task-level timeout. |
| `explorer.enabled` | Exposes one argument-free `explore({})` call on the main Agent's first turn. Deterministic Inventory and knowledge evidence are built inside the tool. |
| `explorer.max_steps` | Explorer model-request ceiling. The default and effective hard limit are two: report immediately or request one targeted follow-up, then report only. Larger legacy values still parse but do not reopen an exploration loop. |
| `explorer.max_duration_seconds` | Explorer soft wall-clock limit. On expiry, successful evidence is converted into a bounded fallback data map before the task-level hard timeout. |
| `evidence_plan.enabled` | When true, the main Agent must first commit one deterministic evidence plan after `explore` reports an unresolved requirement or uncertainty. Default false; the feature never reads gold or judges answer semantics. |
| `evidence_plan.max_commit_attempts` | Invalid-plan or protocol-error budget while the plan is pending (1 or 2). A normal-tool call made during the pending phase is corrected without consuming this budget. |
| `evidence_plan.strict_keys` | Default true. Each pending requirement ID becomes a required key in the dynamic schema; missing or extra keys fail argument validation. False is retained only for the Option A ablation. |
| `evidence_plan.verification_gate` | Default true. After a plan commits, the first answer with unobserved successful `tool + path` checks is rejected once when the step budget permits. False is retained only for the A+B ablation. |

## CLI

```bash
uv run dabench <command> --config PATH [options]
```

| Command | Purpose | Example |
| --- | --- | --- |
| `status` | Show project paths, config path, dataset root, and public task counts. | `uv run dabench status --config configs/react_baseline.example.yaml` |
| `inspect-task` | Show task metadata and list accessible files under `context/`. | `uv run dabench inspect-task task_1 --config configs/react_baseline.local.yaml` |
| `run-task` | Run the baseline on one task and write outputs. | `uv run dabench run-task task_1 --config configs/react_baseline.local.yaml` |
| `run-benchmark` | Run the baseline across the public dataset. | `uv run dabench run-benchmark --config configs/react_baseline.local.yaml` |

`run-benchmark` supports:

- `--limit N` to cap the number of tasks;
- `--task-file PATH` to run one task ID per non-comment line;
- `--resume` to reuse the directory named by `run.run_id`;
- `--retry-failed` with `--resume` to archive and rerun completed failures.

Run the fixed 11-task regression selection:

```bash
uv run dabench run-benchmark \
  --config configs/react_baseline.local.yaml \
  --task-file configs/regression_tasks.example.txt
```

Run the fixed nine-task Explorer bad-case selection:

```bash
uv run dabench run-benchmark \
  --config configs/react_baseline.local.yaml \
  --task-file configs/explorer_bad_cases.example.txt
```

Run the seven-task stop-guard selection after setting the local task timeout to 300 seconds:

```bash
uv run dabench run-benchmark \
  --config configs/react_baseline.local.yaml \
  --task-file configs/stop_guard_bad_cases.example.txt
```

Set `run.run_id` to the existing run directory name, then resume an interrupted run or
retry only its completed failures:

```bash
uv run dabench run-benchmark \
  --config configs/react_baseline.local.yaml \
  --task-file configs/regression_tasks.example.txt \
  --resume

uv run dabench run-benchmark \
  --config configs/react_baseline.local.yaml \
  --task-file configs/regression_tasks.example.txt \
  --resume --retry-failed
```

## Tools

Tools are advertised through the OpenAI-compatible native `tools` field. The main Agent returns
one `tool_call` per turn, the registry validates its JSON arguments with Pydantic before
execution, and the result is returned as a `tool` message with the matching `tool_call_id`.
The Explorer is the only scoped exception: the runtime first performs a deterministic bounded
scan of every supported file and extracts source-anchored `knowledge.md` evidence. The child
Agent then decomposes the task and submits its guide in one model request. Only when Inventory
cannot resolve a critical source or field may it make one targeted follow-up; the second request
exposes `report` only, and the follow-up receives its observation under the original call ID.
An `answer` call must also pass deterministic CSV-safety verification before it can terminate
the task; rejected candidates receive a recoverable tool observation for correction.
The main Agent receives one step-budget reminder after 70% of its normal steps and one critical
reminder after 90%. If its final normal step calls a non-terminal tool after exploration has
finished, that tool is not executed: the runtime returns `FINAL_STEP_REQUIRES_ANSWER` under the
same call ID and allows one extra model request with only `answer` available. This finalization
request cannot create another retry and still uses the existing argument validation and answer
verification path.
The same Chat Completions flow works with Alibaba Cloud Model Studio's OpenAI-compatible
endpoint through the existing `agent.api_base` setting.

The baseline exposes these tools to the model:

| Tool | Purpose | Inputs |
| --- | --- | --- |
| `list_context` | List files and directories under `context/`. | `max_depth` |
| `explore` | Launch one Phase 1 reference-guide sub-agent. The runtime scans all supported files and extracts anchored knowledge evidence; the model decomposes the task and returns recommended sources/fields, candidate joins, and uncertainties. It may follow up once and is removed even on fail-open. | none (`{}`) |
| `read_csv` | Read a CSV preview. | `path`, `max_rows` |
| `read_json` | Read a JSON preview. | `path`, `max_chars` |
| `read_doc` | Read a text document preview. | `path`, `max_chars` |
| `inspect_sqlite_schema` | Inspect tables in a SQLite / DB file. | `path` |
| `execute_context_sql` | Execute read-only SQL against a SQLite / DB file in `context/`. | `path`, `sql`, `limit` |
| `execute_python` | Execute Python code inside the task `context/` directory in a clean spawned child process with a fixed 30-second execution limit and bounded cleanup. This lifecycle isolation is not an OS security sandbox. | `code` |
| `answer` | Submit the final answer table and terminate the task. | `columns`, `rows` |

All file paths passed to tools must be relative to the task `context/` directory.
Recoverable file errors from the main tools include structured correction guidance:
`PATH_NOT_FOUND` directs the model to `list_context` or an exact path from the Explorer report,
while `NOT_SQLITE` recommends the reader matching a non-SQLite file. Both mark
`do_not_retry_same_call` so the next turn can correct directly; tool availability and input
schemas remain unchanged.
`explore` no longer advertises `inspect_files` to the model. Deterministic Inventory is completed
before the first Explorer request and covers CSV/TSV, JSON, SQLite, Markdown, text, and text-based
PDF inputs. `knowledge.md` uses a separate budget to select the question-relevant section, which
is preserved under `knowledge.source_evidence`. The first request produces
`task_requirements`, `answer_projection`, `recommended_sources`, `knowledge.applicable_rules`,
candidate `join_paths`, and `uncertainties`. The answer projection separates direct fields,
derived outputs with real inputs and an operation, document-semantic outputs, and helper fields
used only for filtering, joining, sorting, or grouping. The runtime drops invented sources and
downgrades unsupported confirmed outputs to `candidate`. Only an entirely confirmed projection
that covers every resolved output requirement and links every column to one sets `enforceable=true`
and enables a final answer column-count correction. Column labels need not match physical fields;
incomplete or candidate projections never block submission. If one follow-up is needed, tool schemas enumerate only real
Inventory paths; SQL is absent without SQLite, and the second request exposes only `report`.
The runtime merges the complete file list and schemas, validates path/field/evidence references,
and ignores malformed semantic items individually. If follow-up arguments fail validation, the
tool fails, or the result contains no material evidence, the runtime injects an unresolved
requirement and uncertainty and downgrades a known target source to `candidate`. On model failure
or timeout, fallback keeps Inventory, exact knowledge evidence, and explicit unresolved
requirements instead of reopening an exploration loop.

## Outputs

Each successful task run may produce:

- `events.jsonl`
- `trace.json`
- `prediction.csv`

Per-task outputs are written to:

```text
artifacts/runs/<run_id>/<task_id>/
├── events.jsonl
├── trace.json
└── prediction.csv
```

Benchmark runs also write:

```text
artifacts/runs/<run_id>/manifest.json
artifacts/runs/<run_id>/summary.json
```

`events.jsonl` is flushed after every model request, tool call, and completed step, so a
hard timeout still leaves diagnostic progress. Retried task artifacts are archived under
`<task_id>/attempts/attempt_NNN/`.

## Local Evaluation

Score a run against the public demo answers:

```bash
uv run dabench score-run artifacts/runs/<run_id> \
  --gold-dir data/public/output \
  --input-dir data/public/input \
  --verbose
```

The command writes `scores.json` into the run directory. See
[`docs/evaluation.md`](docs/evaluation.md) for the scoring formula,
normalization rules, and interpretation of partial runs.

The motivation, implementation changes, and sanitized runtime comparison for the
reliability work are recorded in
[`docs/2026-07-24-runner-reliability.md`](docs/2026-07-24-runner-reliability.md).
The native tool protocol, validation behavior, trace compatibility, and verification
results are recorded in
[`docs/2026-07-24-native-tool-calling.md`](docs/2026-07-24-native-tool-calling.md).
The deterministic pre-submit checks and correction flow are documented in
[`docs/2026-07-24-answer-verification.md`](docs/2026-07-24-answer-verification.md).
The bounded Explorer design, limits, fallback behavior, and evaluation boundaries are recorded in
[`docs/2026-07-27-context-explorer.md`](docs/2026-07-27-context-explorer.md).
The step-budget reminders, final-step guard, Trace behavior, and targeted bad-case protocol are
documented in
[`docs/2026-07-31-step-budget-stop-guard.md`](docs/2026-07-31-step-budget-stop-guard.md).

## Contact

- Open issues: https://github.com/HKUSTDial/kddcup2026-data-agents-starter-kit/issues
- Official website: https://dataagent.top
- Discord: https://discord.com/invite/7eFwJQN3Fx
- WeChat official account: `数据智能与分析实验室 DIAL`

<div align="center">
  <table>
    <tr>
      <td align="center">
        <a href="https://dataagent.top">
          <img
            src="https://api.qrserver.com/v1/create-qr-code/?size=144x144&data=https://dataagent.top&bgcolor=ffffff&color=111827&margin=8"
            alt="Official website QR code"
            width="144"
          />
        </a>
        <br />
        Official Website
      </td>
      <td align="center">
        <a href="https://discord.com/invite/7eFwJQN3Fx">
          <img
            src="https://api.qrserver.com/v1/create-qr-code/?size=144x144&data=https://discord.com/invite/7eFwJQN3Fx&bgcolor=ffffff&color=111827&margin=8"
            alt="Discord QR code"
            width="144"
          />
        </a>
        <br />
        Discord
      </td>
      <td align="center">
        <img
          src="https://dataagent.top/HKUSTGZ_DIAL.jpg"
          alt="WeChat official account QR code"
          width="144"
        />
        <br />
        WeChat Official Account
      </td>
    </tr>
  </table>
</div>

## Main Modules

| Module | Responsibility |
| --- | --- |
| `src/data_agent_baseline/benchmark/dataset.py` | Public dataset loader |
| `src/data_agent_baseline/tools/filesystem.py` | `list_context`, `read_csv`, `read_json`, `read_doc` |
| `src/data_agent_baseline/tools/python_exec.py` | `execute_python` |
| `src/data_agent_baseline/tools/sqlite.py` | `inspect_sqlite_schema`, `execute_context_sql` |
| `src/data_agent_baseline/tools/contracts.py` | Pydantic input contracts for native tools |
| `src/data_agent_baseline/tools/registry.py` | JSON Schema generation, validation, dispatch, and terminal `answer` |
| `src/data_agent_baseline/exploration/` | Bounded Explorer sub-agent and deterministic context inventory |
| `src/data_agent_baseline/agents/model.py` | OpenAI-compatible messages and native `tool_calls` adapter |
| `src/data_agent_baseline/agents/prompt.py` | System and task prompts |
| `src/data_agent_baseline/agents/react.py` | Native tool-calling ReAct runtime |
| `src/data_agent_baseline/run/runner.py` | Single-task and benchmark execution |
