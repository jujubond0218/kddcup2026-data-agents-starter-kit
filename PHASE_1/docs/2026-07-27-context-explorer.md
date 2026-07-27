# Phase 1 受限 Context Explorer

本次改造在主 Agent 首次请求模型前生成确定性 Context Inventory，并将其紧凑视图直接
注入任务上下文，不消耗 ReAct 步骤。`explore` 保留为可选的定向工具：只有 Inventory
无法解决具体的数据源、字段语义、连接或文档映射歧义时，主 Agent 才传入 `focus` 和
Inventory 中的 `candidate_paths` 启动受限子 Agent。

## 边界与预算

Inventory 扫描覆盖 CSV/TSV、JSON、SQLite、Markdown、文本和
文本型 PDF；默认最多扫描 64 个文件、读取 4 MiB、普通单文件读取 256 KiB、PDF 读取
2 MiB 的前三页。完整 Inventory 最多 12,000 字符，首次 Prompt 视图最多 6,000 字符。
Explorer 最多三次模型请求、两次定向 `preview_file`，报告最多 4,000 字符；它不执行
Python、不执行 SQL、不进行 OCR、不写 ETL 产物，也不处理视频或 Phase 2。

所有扫描路径均限制在任务 `context/` 内。损坏、超限、无文本 PDF、外逃路径和不支持类型
会产生结构化 warning。Inventory 整体失败时，Runner 注入失败 warning，主 Agent 继续使用
基础工具。Explorer 无法提交报告或模型请求失败时，以已缓存的 Inventory evidence 返回
fail-open 报告。

Explorer 正常报告使用 evidence-first 契约：`selected_sources`、`evidence_refs`、
`key_fields`、`join_candidates`、`etl_candidates`、`warnings` 和 `uncertainties`。
preview observation 由运行时分配不可伪造的 evidence ID；报告只能引用实际存在的
evidence。Inventory 与 observation 是事实，字段和连接 candidate 必须由主 Agent 在计算前
验证，报告不能覆盖 Inventory。

## 兼容性与评测

`explore` 仍是普通原生工具调用，主 Agent 按原始 `tool_call_id` 接收 observation；主
`trace.json` 将其记录为普通步骤，`events.jsonl` 增加 Explorer 生命周期事件。现有 Runner
的 120 秒任务硬超时、重试、恢复与失败重跑不改变。

主 Agent 默认步数从 16 增至 20。`context_inventory_created` 和
`context_inventory_failed` 事件只记录文件数、warning 数、截断、字符和读字节等聚合信息，
不记录 Inventory 内容。最终 50 题实验只能衡量此目标方案的整体观测结果；真实服务存在
波动，单轮实验不构成稳定因果结论。

## 50 题实验结果

2026-07-27 使用本地目标配置完成一次 50 题实验，并与当前 `master` 已记录的最佳一次
50 题结果比较。两次 Runner 均成功 40/50，缺失预测均为 10；目标方案总分为 0.5303，
Mean Recall 为 0.5333，低于对照的 0.5703 与 0.5733。目标方案得到 25 个满分、3 个
部分匹配和 12 个零分任务；对照为 27 个满分、3 个部分匹配和 10 个零分任务。逐题分数
匿名比较显示 48/50 不变，2 个对照满分任务分别退化为零分和缺失，没有任务提升。

| 指标 | 当前 `master` 最佳一次 | Explorer + 20 主步骤 |
| --- | ---: | ---: |
| 总分 / Mean Recall | 0.5703 / 0.5733 | 0.5303 / 0.5333 |
| Runner 成功 / 缺失预测 | 40 / 10 | 40 / 10 |
| 满分 / 部分匹配 / 零分 | 27 / 3 / 10 | 25 / 3 / 12 |
| 墙钟时间 | 863.205 秒 | 1,263.989 秒 |
| 主 Agent 工具调用 | 513 | 496 |
| 主 Agent 平均完成步骤 | 10.22 | 9.90 |
| 主步骤耗尽任务 | 7 | 6 |
| 成功模型请求 | 513 | 754 |

49/50 个任务调用了 Explorer，其中 47 次正常完成、2 次 fail-open；正常完成平均使用
5.23 步，25 次用满 6 步。Explorer 消耗 460,289 Token，占目标方案总计 4,028,412
Token 的 11.4%；旧对照事件未记录可直接汇总的 Token，因此不报告其 Token 数。目标方案
墙钟增加约 46%，成功模型请求增加约 47%。

这次结果对应已经淘汰的 v1 强制多轮设计，没有证明收益，反而观测到分数和延迟退化。当前
evidence-first 版本改为自动 Inventory 加可选定向 Explorer；它的 50 题结果将在自动化检查
通过后单独记录，不能预先宣称提升。

## Evidence-first v2 50 题实验

2026-07-27 完成一次 v2 目标方案实验。总分为 0.5713，Mean Recall 为 0.5733；Runner
成功 38/50，缺失预测 12，得到 28 个满分、2 个部分匹配和 8 个非缺失零分任务。与当前
`master` 最佳一次相比，总分仅增加 0.0010、Mean Recall 相同，但 Runner 成功减少 2、
缺失增加 2，因此没有通过预先设定的成功率与缺失门槛。

| 指标 | 当前 `master` 最佳一次 | 强制 Explorer v1 | Evidence-first v2 |
| --- | ---: | ---: | ---: |
| 总分 / Mean Recall | 0.5703 / 0.5733 | 0.5303 / 0.5333 | 0.5713 / 0.5733 |
| Runner 成功 / 缺失预测 | 40 / 10 | 40 / 10 | 38 / 12 |
| 满分 / 部分匹配 / 零分 | 27 / 3 / 10 | 25 / 3 / 12 | 28 / 2 / 8 |
| 墙钟时间 | 863.205 秒 | 1,263.989 秒 | 953.882 秒 |
| 主 Agent 工具调用 | 513 | 496 | 434 |
| 主 Agent 平均完成步骤 | 10.22 | 9.90 | 8.66 |
| 主步骤耗尽任务 | 7 | 6 | 6 |
| 成功模型请求 | 513 | 754 | 434 |
| 总 Token | 未记录 | 4,028,412 | 2,909,542 |

50 个任务均成功生成 Inventory，平均 Prompt Inventory 为 2,225 字符，最大 5,591 字符，
没有 Inventory 失败。主 Agent 没有在任何任务中调用可选 `explore`，因此本次没有产生
Explorer 子 Agent 请求；真实服务实验实际验证的是“自动确定性 Inventory + 现有工具”，
定向子 Agent 只经过了 scripted 自动化测试。

相对 `master` 最佳一次，逐题分数有 46 题不变、2 题提升、2 题退化，净总分仅增加
0.0010，远小于单轮模型服务波动所能排除的范围。v2 明确消除了 v1 的固定子 Agent 成本，
但没有同时保持 Runner 可靠性，因此不满足本 PR 的验收条件，不提交或创建 PR。
