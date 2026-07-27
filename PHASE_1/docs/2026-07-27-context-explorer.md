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
但没有同时保持 Runner 可靠性，因此不满足创建 PR 的验收条件。该实现仅作为后续优化的
可复现检查点提交到个人 Fork 分支，尚未创建 PR。

## Bad-case 定向增强

Evidence-first v2 的确定性 Inventory 能消除强制子 Agent 的固定成本，但真实实验中主
Agent 从未主动调用可选的 `explore`，说明仅靠 Prompt 建议无法稳定触发需要补充证据的
任务。本轮增强不按任务 ID 编写规则，而是从 Inventory 中可观察的结构事实生成客观歧义：
大 JSON 的部分结构、宽表与其他结构化来源并存，以及多个来源之间的同名字段、兼容类型
或有限样本值重叠。

Inventory schema 升级为 v2，并增加以下只读派生信息：

- `relation_candidates` 最多 12 个，记录路径、表、字段和候选关系信号；所有连接关系都
  明确标记为 candidate，不能作为已验证事实。
- `exploration` 包含 `recommended`、合并后的 `focus`、最多 8 个 `candidate_paths` 和
  `ambiguity_codes`。这些字段由通用确定性规则产生。
- 大 JSON 使用 `ijson` 在现有单文件字节预算内流式提取顶层类型、最多 48 个嵌套字段
  路径、类型和 3 个有界对象样本；预算耗尽保留已有证据并标记截断，损坏输入仍然
  fail-open。

当 `exploration.recommended=true` 时，主 Agent 的首次模型请求只暴露 `explore`，并要求
使用 Inventory 给出的精确 `focus` 和 `candidate_paths`。Explorer 完成或返回可恢复失败
后，运行时移除 `explore` 并恢复原有工具，避免重复探索。没有客观歧义、Explorer 被禁用
或 Inventory 扫描失败时，链路保持 v2 行为。

Explorer 报告增加 `recommended_checks`，仅允许 `source_relevance`、`field_semantics`、
`join_coverage` 和 `filter_domain` 四类检查。每项检查必须引用已存在的 evidence ID、候选
路径和真实字段，不允许携带可执行 SQL 或 Python。`context_inventory_created` 事件只增加
是否推荐探索、歧义数量和候选关系数量等聚合字段，不写入 Inventory、原始样本或模型内容。

固定快速评测清单位于 `configs/explorer_bad_cases.example.txt`。其中 9 个活动任务用于
验证 Explorer 可改善的核心组和观察组，6 个注释任务作为非 Explorer 对照。任务 ID 只属于
评测配置，不参与任何运行时分支。9 题进入全量实验的门槛为至少 2 题从零变为非零、Runner
成功不少于 6/9、缺失不超过 3，并满足每个被标记任务仅调用一次 Explorer、子 Agent 请求
和 preview 预算。只有通过该门槛才运行一次 50 题实验；单轮结果只记录为当前配置下的
观测，不用于宣称稳定因果提升。
