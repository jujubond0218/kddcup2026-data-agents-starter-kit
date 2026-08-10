# Phase 1 Engineering and Experiment Index

[English](#english) | [中文](#中文)

## English

This directory records the Phase 1 engineering decisions and experiments in this fork. Read the
documents by problem rather than by date. Runtime reliability, protocol correctness, process
auditability, and semantic answer quality are separate outcomes throughout these reports.

### Start here

| Topic | Document | What it establishes |
| --- | --- | --- |
| Local evaluation | [Evaluation pipeline](2026-07-23-phase1-evaluation-pipeline.md) and [scoring reference](evaluation.md) | Separates Runner completion from column-level semantic score and defines the public local benchmark. |
| Reliable execution | [Runner reliability](2026-07-24-runner-reliability.md) and [Python lifecycle](2026-07-30-python-exec-lifecycle.md) | Covers bounded requests, task subprocesses, live events, resume/retry, clean Python process recovery, and bounded stdout/stderr observations. |
| Native tool protocol | [Native tool calling](2026-07-24-native-tool-calling.md), [tool error guidance](2026-07-30-tool-error-guidance.md), and [repeated-call guard](2026-08-10-repeated-tool-call-guard.md) | Defines strict schemas, call-ID alignment, recoverable observations, targeted error correction, and a bounded identical-call loop guard. |
| Agent context | [Bounded Context Explorer](2026-07-27-context-explorer.md) | Builds a deterministic file inventory and a bounded semantic map with explicit unresolved items. |
| Context observability | [Request input metrics](2026-08-10-request-input-metrics.md) | Measures each model request without persisting prompts, observations, schemas, or other raw content. |
| Terminal correctness | [Answer verification](2026-07-24-answer-verification.md) and [step-budget stop guard](2026-07-31-step-budget-stop-guard.md) | Validates CSV safety and provides one bounded finalization opportunity without judging semantics. |
| Auditable planning | [Evidence Plan](2026-08-07-evidence-plan.md) | Records Options A/B/C, protocol metrics, full-run ablations, and the boundary between observed checks and semantic proof. |

### How to interpret the results

- The public 50-task dataset is a local development benchmark, not the official hidden
  leaderboard.
- A single run is an observation. Stable accuracy claims require repeated runs under the same
  model, configuration, task set, and scorer.
- Higher Runner success, plan submission, or verification observation does not by itself mean
  higher answer accuracy.
- Negative and no-go experiments are retained to document causal boundaries and stop repeated
  work, not to imply that the runtime feature is broken.

## 中文

本目录记录个人 Fork 在 Phase 1 中的工程决策与实验。建议按问题阅读，而不是按日期顺序
通读。所有报告都区分运行可靠性、协议正确性、过程可审计性和答案语义正确性。

### 推荐入口

| 主题 | 文档 | 可以证明什么 |
| --- | --- | --- |
| 本地评测 | [评测链路](2026-07-23-phase1-evaluation-pipeline.md)与[评分说明](evaluation.md) | 将 Runner 完成与列级语义得分分离，建立公开题本地基线。 |
| 可靠执行 | [Runner 可靠性](2026-07-24-runner-reliability.md)与[Python 生命周期](2026-07-30-python-exec-lifecycle.md) | 覆盖有界请求、任务子进程、实时事件、恢复重跑、Python 进程回收和 stdout/stderr observation 上限。 |
| 原生工具协议 | [原生工具调用](2026-07-24-native-tool-calling.md)、[工具错误引导](2026-07-30-tool-error-guidance.md)与[重复调用守卫](2026-08-10-repeated-tool-call-guard.md) | 定义严格 schema、call ID 对齐、可恢复 observation、目标错误纠正和有界的相同调用循环保护。 |
| Agent 上下文 | [有界 Context Explorer](2026-07-27-context-explorer.md) | 生成确定性文件清单、有界语义地图和显式未解决问题。 |
| 上下文可观测性 | [请求输入指标](2026-08-10-request-input-metrics.md) | 测量每次模型请求，但不持久化 Prompt、observation、schema 或其他原始内容。 |
| 终局正确性 | [Answer Verifier](2026-07-24-answer-verification.md)与[步数停止守卫](2026-07-31-step-budget-stop-guard.md) | 校验 CSV 安全性并提供一次有界收尾，但不判断答案语义。 |
| 可审计规划 | [Evidence Plan](2026-08-07-evidence-plan.md) | 记录 A/B/C 三层协议、过程指标、50 题消融和“调用已发生不等于语义已证明”的边界。 |

### 实验结果应如何解释

- 公开 50 题是本地开发 benchmark，不是官方隐藏榜成绩。
- 单轮结果只是一项观测；稳定准确率结论必须固定模型、配置、任务集和 scorer 并重复运行。
- Runner 成功率、计划提交率或核验观测率提高，不能直接推出答案正确率提高。
- 保留负实验和 no-go 结论，是为了说明因果边界并避免重复投入，不代表对应运行时功能失效。
