<div align="center">

# KDD Cup 2026 DataAgent-Bench — Phase 1 Engineering Fork

English | [中文](README.zh.md)

[![Official Website](https://img.shields.io/badge/Official%20Website-Visit%20dataagent.top-0ea5e9?style=for-the-badge&logo=googlechrome&logoColor=white&labelColor=0f172a)](https://dataagent.top)
[![Discord](https://img.shields.io/badge/Discord-Join%20Community-5865F2?style=for-the-badge&logo=discord&logoColor=white&labelColor=0f172a)](https://discord.com/invite/7eFwJQN3Fx)

</div>

> [!NOTE]
> This is a personal research fork of the
> [official KDD Cup 2026 starter kit](https://github.com/HKUSTDial/kddcup2026-data-agents-starter-kit).
> The work in this fork is limited to Phase 1; Phase 2 remains the upstream baseline. See
> [NOTICE.md](NOTICE.md) for project provenance and scope.

This project turns the instructional Phase 1 ReAct baseline into a reproducible data-agent
research harness for heterogeneous CSV, JSON, SQLite, Markdown, text, and text-based PDF inputs.
The main contribution is an end-to-end engineering loop that separates runtime success,
protocol correctness, auditability, and semantic answer quality.

## Phase 1 Highlights

| Area | Contribution | Evidence |
| --- | --- | --- |
| Native tool protocol | Replaced text JSON actions with OpenAI-compatible `tools/tool_calls`, strict Pydantic validation, matched call IDs, and recoverable tool observations. | In a 12-task regression, extra answer columns fell from 11 to 1; 12/12 targeted path/type errors were corrected on the next turn. |
| Reliable execution | Added bounded request retries, task subprocess timeouts, live events, resume/retry, and a clean `spawn + Pipe` Python lifecycle. | Historical 600-second stalls completed in about 18–29 seconds with diagnostics; 136/136 Python calls returned in a 50-task run. |
| Bounded planning and verification | Added deterministic context inventory, a bounded Explorer, answer verification, step-budget guards, and an opt-in Evidence Plan protocol. | Evidence-plan submission rose from 14.8% to 72.4%; declared-verification observation reached 92%–93.5%, without claiming stable semantic gain. |
| Reproducible evaluation | Added a local scorer, fixed regression selections, scripted tests, experiment records, and explicit go/no-go criteria. | Public 50-task local score: 0.4840 initial baseline, 0.7237 best single run; the repeated mainline reference averaged 0.6869 across three runs. |

All scores above come from the public 50-task local benchmark, not the official hidden
leaderboard. A single best run is an observed result rather than evidence that one change caused
the full improvement.

```text
Task + heterogeneous context
→ bounded Explorer / evidence map
→ native tool-calling ReAct agent
→ data tools and isolated Python execution
→ deterministic answer verification
→ prediction + live events + trace
→ local scorer and regression analysis
```

For implementation details and negative-result boundaries, start with the
[Phase 1 documentation index](PHASE_1/docs/README.md).

## Upstream Starter Kit

The upstream repository provides baseline starter kits for KDD Cup 2026 DataAgent-Bench. It is
organized by competition phase so participants can start from the package matching the phase they
are working on.

## Repository Layout

| Directory | Purpose |
| --- | --- |
| `PHASE_1/` | Starter kit for Phase 1 tasks. |
| `PHASE_2/` | Starter kit for Phase 2 tasks and demo data format. |

Each phase directory is self-contained and includes its own README, configuration files, source code, dependency lock file, and baseline command-line entry points.

## Which Directory Should I Use?

Use `PHASE_1/` if you are working with the Phase 1 task format.

Use `PHASE_2/` if you are working with the Phase 2 demo release or Phase 2 task format. Phase 2 keeps the same general agent workflow while allowing richer task context files.

## Quick Start

Choose the phase directory first, then follow that directory's README.

```bash
cd PHASE_1
# or
cd PHASE_2
```

Then install dependencies and run the baseline from inside the selected directory:

```bash
uv sync
uv run dabench status --config configs/react_baseline.example.yaml
uv run dabench run-benchmark --config configs/react_baseline.example.yaml
```

The exact dataset layout, configuration options, tools, and output paths are documented in each phase-specific README.

## Baseline Overview

The starter kit contains a minimal ReAct-style data agent. The baseline is intentionally simple: it reads task metadata, inspects files under each task's `context/` directory through a small set of tools, calls an OpenAI-compatible model endpoint, and writes `prediction.csv` outputs.

Participants are expected to adapt or replace the baseline logic for their own methods. The code reads model endpoint settings from configuration or environment-compatible values rather than hardcoding service credentials.

## Common Project Structure

Inside each phase directory, the main files are organized as follows:

```text
configs/                         # Example baseline configuration
src/data_agent_baseline/          # Baseline source code
artifacts/                        # Local run outputs, ignored except .gitkeep
README.md                         # Phase-specific usage guide
README.zh.md                      # Chinese usage guide
pyproject.toml                    # Python project metadata
uv.lock                           # Locked dependency versions
```

## Notes

- Run commands from inside `PHASE_1/` or `PHASE_2/`, not from the repository root.
- Keep local run outputs under `artifacts/`; they are not intended to be committed.
- Review the phase-specific README before packaging or submitting a solution.

## Documentation and Support

- Personal fork issues: https://github.com/jujubond0218/kddcup2026-data-agents-starter-kit/issues
- Phase 1 usage: [PHASE_1/README.md](PHASE_1/README.md)
- Engineering and experiment index: [PHASE_1/docs/README.md](PHASE_1/docs/README.md)
- Project provenance: [NOTICE.md](NOTICE.md)
- Official upstream and competition support:
  [HKUSTDial starter kit](https://github.com/HKUSTDial/kddcup2026-data-agents-starter-kit),
  [website](https://dataagent.top), and
  [Discord](https://discord.com/invite/7eFwJQN3Fx)

## Upstream Baseline Modules

The upstream baseline layout below is shared by both phase directories. The current Phase 1
module and tool map is documented in [PHASE_1/README.md](PHASE_1/README.md).

| Module | Responsibility |
| --- | --- |
| `src/data_agent_baseline/benchmark/dataset.py` | Dataset loader |
| `src/data_agent_baseline/tools/filesystem.py` | `list_context`, `read_csv`, `read_json`, `read_doc` |
| `src/data_agent_baseline/tools/python_exec.py` | `execute_python` |
| `src/data_agent_baseline/tools/sqlite.py` | `inspect_sqlite_schema`, `execute_context_sql` |
| `src/data_agent_baseline/tools/registry.py` | Tool registration and terminal `answer` |
| `src/data_agent_baseline/agents/prompt.py` | System and task prompts |
| `src/data_agent_baseline/agents/react.py` | ReAct runtime and model/tool protocol |
| `src/data_agent_baseline/run/runner.py` | Single-task and benchmark execution |
