<div align="center">

# KDD Cup 2026 DataAgent-Bench — Phase 1 工程化 Fork

[English](README.md) | 中文

[![官方网站](https://img.shields.io/badge/Official%20Website-Visit%20dataagent.top-0ea5e9?style=for-the-badge&logo=googlechrome&logoColor=white&labelColor=0f172a)](https://dataagent.top)
[![Discord](https://img.shields.io/badge/Discord-Join%20Community-5865F2?style=for-the-badge&logo=discord&logoColor=white&labelColor=0f172a)](https://discord.com/invite/7eFwJQN3Fx)

</div>

> [!NOTE]
> 本仓库是
> [KDD Cup 2026 官方 Starter Kit](https://github.com/HKUSTDial/kddcup2026-data-agents-starter-kit)
> 的个人研究 Fork。个人改造仅覆盖 Phase 1，Phase 2 保持官方基线；项目来源和范围见
> [NOTICE.md](NOTICE.md)。

本项目将教学型 Phase 1 ReAct baseline 改造成可复现的数据分析 Agent 实验系统，处理
CSV、JSON、SQLite、Markdown、文本和文本型 PDF 等异构输入。核心价值是建立完整的工程
闭环，并明确区分运行成功、协议正确、过程可审计和答案语义正确四类指标。

## Phase 1 核心改造

| 方向 | 个人改造 | 验证结果 |
| --- | --- | --- |
| 原生工具协议 | 将文本 JSON action 迁移为 OpenAI-compatible `tools/tool_calls`，增加 Pydantic 严格校验、call ID 对齐和可恢复工具 observation。 | 12 题回归中额外答案列由 11 个降至 1 个；12/12 次目标路径/类型错误在下一轮完成纠正。 |
| 可靠执行 | 增加请求重试、任务子进程硬超时、实时事件、断点续跑，以及 `spawn + Pipe` 的 Python 生命周期。 | 历史 600 秒卡死任务缩短至约 18–29 秒并保留诊断；一次 50 题实验中 136/136 次 Python 调用完整返回。 |
| 有界规划与核验 | 增加确定性文件 Inventory、有界 Explorer、Answer Verifier、步数守卫和可选 Evidence Plan 协议。 | Evidence Plan 提交率由 14.8% 提升至 72.4%，声明核验观测率达到 92%–93.5%；不将过程指标描述为稳定语义提分。 |
| 可复现实验 | 增加本地 scorer、固定回归集、Scripted 测试、实验记录和明确的 go/no-go 条件。 | 公开 50 题本地分数从初始 0.4840 到单轮最高 0.7237；主线三轮参考均值为 0.6869。 |

以上分数均来自公开 50 题的本地 benchmark，不是官方隐藏榜成绩。单轮最高值只是一项
观测结果，不能据此把全部提升归因于某一个改造。

```text
任务与异构上下文
→ 有界 Explorer / 证据地图
→ 原生工具调用 ReAct Agent
→ 数据工具与隔离的 Python 执行
→ 确定性答案校验
→ prediction + 实时 events + trace
→ 本地评分与回归分析
```

实现细节、实验口径与负结果边界统一从
[Phase 1 文档索引](PHASE_1/docs/README.md)进入。

## 官方 Starter Kit

官方仓库提供 KDD Cup 2026 DataAgent-Bench 的 baseline starter kit，并按比赛阶段组织，
参赛者可以根据正在使用的数据格式进入对应目录。

## 仓库结构

| 目录 | 用途 |
| --- | --- |
| `PHASE_1/` | 第一阶段任务格式使用的 starter kit。 |
| `PHASE_2/` | 第二阶段任务格式和 demo release 使用的 starter kit。 |

每个阶段目录都是独立的，包含自己的 README、配置文件、源码、依赖锁文件和 baseline 命令行入口。

## 应该使用哪个目录？

如果你正在处理第一阶段任务格式，请使用 `PHASE_1/`。

如果你正在处理第二阶段 demo release 或第二阶段任务格式，请使用 `PHASE_2/`。第二阶段保留相同的基础 agent 工作流，同时支持更丰富的任务上下文文件。

## 快速开始

请先进入对应阶段目录，再按照该目录下的 README 操作。

```bash
cd PHASE_1
# 或
cd PHASE_2
```

然后在所选目录内安装依赖并运行 baseline：

```bash
uv sync
uv run dabench status --config configs/react_baseline.example.yaml
uv run dabench run-benchmark --config configs/react_baseline.example.yaml
```

具体的数据目录结构、配置字段、工具和输出路径，请查看各阶段目录下的 README。

## Baseline 概览

starter kit 中包含一个最小 ReAct-style data agent。baseline 保持简单：读取任务元信息，通过一组基础工具查看每个任务 `context/` 目录下的文件，调用 OpenAI-compatible 模型接口，并写出 `prediction.csv`。

参赛者可以在此基础上自行改造或替换 agent 逻辑。代码不会硬编码服务凭据，模型接口相关信息应通过配置或环境变量兼容的方式传入。

## 常见项目结构

每个阶段目录大致包含以下内容：

```text
configs/                         # baseline 示例配置
src/data_agent_baseline/          # baseline 源码
artifacts/                        # 本地运行产物，除 .gitkeep 外不应提交
README.md                         # 英文使用说明
README.zh.md                      # 中文使用说明
pyproject.toml                    # Python 项目元信息
uv.lock                           # 锁定的依赖版本
```

## 注意事项

- 请在 `PHASE_1/` 或 `PHASE_2/` 目录内运行命令，不要直接在仓库根目录运行。
- 本地运行产物请放在 `artifacts/` 下；这些文件不应提交到仓库。
- 在打包或提交方案前，请先阅读对应阶段目录下的 README。

## 文档与支持

- 个人 Fork 问题反馈：https://github.com/jujubond0218/kddcup2026-data-agents-starter-kit/issues
- Phase 1 使用说明：[PHASE_1/README.zh.md](PHASE_1/README.zh.md)
- 工程与实验索引：[PHASE_1/docs/README.md](PHASE_1/docs/README.md)
- 项目来源说明：[NOTICE.md](NOTICE.md)
- 官方仓库与比赛支持：
  [HKUSTDial Starter Kit](https://github.com/HKUSTDial/kddcup2026-data-agents-starter-kit)、
  [官方网站](https://dataagent.top)和
  [Discord](https://discord.com/invite/7eFwJQN3Fx)

## 官方基线模块

以下结构来自两个阶段共用的官方基线；当前 Phase 1 的完整模块与工具说明见
[PHASE_1/README.zh.md](PHASE_1/README.zh.md)。

| 模块 | 责任 |
| --- | --- |
| `src/data_agent_baseline/benchmark/dataset.py` | 数据集加载器 |
| `src/data_agent_baseline/tools/filesystem.py` | `list_context`、`read_csv`、`read_json`、`read_doc` |
| `src/data_agent_baseline/tools/python_exec.py` | `execute_python` |
| `src/data_agent_baseline/tools/sqlite.py` | `inspect_sqlite_schema`、`execute_context_sql` |
| `src/data_agent_baseline/tools/registry.py` | 工具注册与终止型 `answer` |
| `src/data_agent_baseline/agents/prompt.py` | system prompt 与 task prompt |
| `src/data_agent_baseline/agents/react.py` | ReAct runtime 与模型/工具协议 |
| `src/data_agent_baseline/run/runner.py` | 单任务和批量运行逻辑 |
